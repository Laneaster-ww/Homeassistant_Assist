"""Conversation agent platform - der Chat-Einstiegspunkt fuer SMART-HOMEASSISTANT.

Jede Nutzereingabe landet in :meth:`SmartHomeAssistantConversationEntity.async_process`
und nimmt genau einen von zwei Wegen:

* Es steht eine Rueckfrage offen -> :meth:`_handle_pending` wertet ja/nein aus und
  fuehrt die zurueckgestellte Aktion aus oder verwirft sie.
* Sonst -> :meth:`_handle_new_message` fragt das Sprachmodell, prueft dessen
  Vorschlag und fuehrt ihn entweder direkt aus oder stellt ihn als Rueckfrage
  zurueck.

Grundregel fuer alles hier: nichts wird ausgefuehrt, was nicht vorher die Policy
passiert hat, und alles Eingreifende bekommt eine Rueckfrage. Fehler werden
bewusst als normale Chat-Antwort zurueckgegeben statt als Exception - der Nutzer
sitzt in einem Gespraech, nicht vor einem Stacktrace.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

import yaml

from homeassistant.components import conversation
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .automations import (
    activate_automation_draft,
    async_validate_automation_yaml,
    read_automations,
)
from .broker import BrokerError, check_plan_step, execute_step, is_expired, validate_plan
from .const import (
    ACTION_LOG_MAX_ENTRIES,
    CONF_CONFIRMATION_TTL,
    action_log_signal,
    DATA_ACTION_LOG,
    DATA_POLICY,
    DATA_OFFLINE_DOCS,
    DATA_PROVIDER_STORE,
    DATA_SESSIONS,
    DEFAULT_CONFIRMATION_TTL_MINUTES,
    DOMAIN,
    get_option,
)
from .dashboard import (
    activate_dashboard_draft,
    delete_dashboard,
    list_deletable_dashboards,
    list_known_entities,
    validate_dashboard_view_yaml,
)
from .dashboard_design import check_layout_quality
from .entity_filter import (
    ACTION_LIST_THRESHOLD,
    DASHBOARD_LIST_THRESHOLD,
    policy_data_for_prompt,
    relevance_query,
    select_relevant,
)
from .docs_knowledge import (
    ACTION_WORDS,
    DOCS_ONLY_SYSTEM_PROMPT,
    contains_term,
    docs_policy_from_data,
    is_documentation_question,
    retrieve_docs_context,
)
from .i18n import t
from .text_format import name_map, normalize_umlauts
from .llm_clients import LLMClient, create_client
from .ollama_client import InsecureEndpointError
from .ollama_client import build_system_prompt

_LOGGER = logging.getLogger(__name__)

# Zustimmung und Ablehnung auf eine Rueckfrage. Beide Listen enthalten auch die
# umlautlosen Schreibweisen, weil Spracheingaben unterschiedlich transkribiert
# werden ("bestaetige"/"bestätige").
YES_WORDS = {
    "ja",
    "jep",
    "jo",
    "jup",
    "jap",
    "yes",
    "mach das",
    "mach es",
    "bestaetige",
    "bestätige",
    "ok",
    "okay",
    "los",
    "klar",
    "gerne",
    "genau",
    "positiv",
    # Englische Entsprechungen.
    "yeah",
    "yep",
    "sure",
    "confirm",
    "correct",
    "do it",
    "go ahead",
}
NO_WORDS = {
    "nein",
    "no",
    "noe",
    "nö",
    "abbrechen",
    "stopp",
    "stop",
    "nicht",
    "lass es",
    "lass es sein",
    "auf keinen fall",
    # Englische Entsprechungen.
    "nope",
    "cancel",
    "don't",
    "do not",
    "negativ",
}

# Versuche fuer die Dashboard-Gliederung inklusive Neugenerierung bei Layout-Maengeln.
# Mehr als drei lohnen sich nicht: was das Modell dreimal nicht trifft, trifft es
# meist auch beim vierten Mal nicht, und jeder Versuch kostet den Nutzer Wartezeit.
DASHBOARD_MAX_ATTEMPTS = 3

# Dieselbe Ueberlegung wie bei DASHBOARD_MAX_ATTEMPTS, fuer Automation-Entwuerfe:
# kleine lokale Modelle treffen Entity-IDs oder gueltiges HA-Schema nicht immer
# beim ersten Versuch (siehe _generate_automation).
AUTOMATION_MAX_ATTEMPTS = 3

# Wie viele Nachrichten (user+assistant, also die Haelfte davon Runden) ein Gespraech
# maximal mit ins Modell nimmt. Ohne Begrenzung waechst der Verlauf unbegrenzt weiter:
# der ohnehin grosse System-Prompt kommt bei jeder Anfrage neu dazu, das Fenster kleiner
# lokaler Modelle laeuft irgendwann ueber, und jede Anfrage wird langsamer und teurer.
MAX_HISTORY_MESSAGES = 20

# Ab wann ein unbenutztes Gespraech aus dem Speicher faellt. Ohne Aufraeumen bleibt jede
# je vergebene Konversations-ID bis zum HA-Neustart liegen - das Frontend erzeugt bei
# jedem "Verlauf leeren" eine neue.
SESSION_MAX_IDLE_HOURS = 24

# Gesamtbudget fuer alle Versuche EINES Entwurfs (Dashboard bzw. Automation). Ein
# einzelner Modellaufruf darf bis zu 120s dauern (llm_clients._REQUEST_TIMEOUT); ohne
# Gesamtbudget haetten drei Versuche den Chat bis zu sechs Minuten blockiert, ohne dass
# der Nutzer erfaehrt, worauf er wartet. Ist das Budget aufgebraucht, wird der bis dahin
# beste Entwurf genommen statt weiterzuprobieren.
GENERATION_BUDGET_SECONDS = 240


# Anfang-/Kontext-Woerter, an denen eine Frage nach BESTEHENDEN Automationen erkennbar ist
# ("welche Automationen ...", "zeig mir meine Automationen", "was fuer Automationen gibt es").
_AUTOMATION_STATUS_STARTS = (
    "welche",
    "was für",
    "was fuer",
    "zeig mir",
    "zeige mir",
    "liste",
    "gibt es",
    "hast du",
    "wie viele",
    # Englische Entsprechungen - die Chat-Sprache ist nicht mehr fest Deutsch.
    "which",
    "what",
    "show me",
    "list",
    "are there",
    "is there",
    "do you have",
    "do i have",
    "how many",
)


def _is_automation_status_question(text: str) -> bool:
    """Erkennt Fragen nach bestehenden/aktiven Automationen, um sie deterministisch aus
    ``read_automations`` zu beantworten - ohne Umweg ueber die LLM-Klassifizierung.

    Genau diese Frageform ist wiederholt faelschlich als kind=automation_draft
    eingestuft worden (das Modell hat sogar teils unveraendert das Beispiel aus dem
    System-Prompt als "neue" Automation vorgeschlagen und zur Aktivierung angeboten -
    siehe Vorfaelle im Aktionsverlauf). Da sich diese Frage ohnehin ausschliesslich und
    zuverlaessig aus ``automations.yaml`` beantworten laesst, umgeht dieser Weg das LLM
    fuer diesen Fall komplett.
    """

    normalized = text.strip().lower()
    if "automat" not in normalized:  # deckt automation(en)/automatisierung(en) ab
        return False
    if contains_term(normalized, ACTION_WORDS):
        return False
    return any(
        normalized.startswith(start) or f" {start}" in normalized
        for start in _AUTOMATION_STATUS_STARTS
    )


def _describe_automations(automations: list[dict], language: str) -> str:
    """Textuelle Kurzuebersicht bestehender Automationen fuer den Chat."""

    if not automations:
        return t("no_automations", language)
    no_title = t("no_title", language)
    zeilen = "\n".join(f"- {a.get('alias') or no_title}" for a in automations)
    return f"{t('automations_list_header', language)}\n{zeilen}"


# Ordnet jeden BrokerError-Code auf den passenden i18n-Uebersetzungsschluessel ab - die
# technische BrokerError-Meldung (Entity-IDs, "example_copied" o.ae.) ist fuer eine
# Ablehnung im Chat nicht hilfreich, sie beschreibt Implementierungsdetails statt dem
# Nutzer zu sagen, was er tun kann. Der volle technische Fehler bleibt im Aktionsverlauf
# (siehe log_error in den Aufrufen von _reject).
_AUTOMATION_REJECTION_KEYS = {
    "invalid_yaml": "rejection_invalid_yaml",
    "entity_not_allowed": "rejection_entity_not_allowed",
    "service_not_allowed": "rejection_service_not_allowed",
    "invalid_homeassistant_automation": "rejection_invalid_homeassistant_automation",
    "example_copied": "rejection_example_copied",
}


def _automation_rejection_message(error_code: str | None, language: str) -> str:
    """Verstaendliche Nutzer-Formulierung fuer einen endgueltig verworfenen Automation-Entwurf."""

    key = _AUTOMATION_REJECTION_KEYS.get(error_code, "rejection_generic")
    grund = t(key, language)
    grund = grund[0].upper() + grund[1:]
    return f"{grund}. {t('retry_hint', language)}"


# Satzzeichen, die eine kurze Antwort umrahmen koennen ("ja!", "nein.", "ja, mach das").
_ANSWER_PUNCTUATION = ".,!?;:"


def _match_yes_no(text: str) -> bool | None:
    """Deutet die Antwort auf eine Rueckfrage: True=ja, False=nein, None=unklar.

    Einzelne Zustimmungs-/Ablehnungswoerter werden bewusst nur als vollstaendiges
    ERSTES WORT erkannt, nicht als Praefix der gesamten Eingabe: ein Praefix-Vergleich
    hat jede Eingabe als Bestaetigung gewertet, die zufaellig so anfaengt - "Jalousien
    schliessen" beginnt mit "ja" und haette damit eine offene Rueckfrage ausgeloest, im
    schlimmsten Fall das bestaetigungspflichtige Aufsperren des Tuerschlosses. Mehrwortige
    Antworten ("mach das", "go ahead") bleiben als Praefix erlaubt, weil sie fuer sich
    schon eindeutig sind.

    Bei ``None`` bleibt die Rueckfrage offen und wird erneut gestellt - im Zweifel wird
    also nichts ausgefuehrt.
    """

    normalized = text.strip().lower().strip(_ANSWER_PUNCTUATION)
    words = normalized.split()
    if not words:
        return None

    for phrase in (word for word in YES_WORDS if " " in word):
        if normalized.startswith(phrase):
            return True
    for phrase in (word for word in NO_WORDS if " " in word):
        if normalized.startswith(phrase):
            return False

    first_word = words[0].strip(_ANSWER_PUNCTUATION)
    if first_word in YES_WORDS:
        return True
    if first_word in NO_WORDS:
        return False
    return None


def _describe_steps(steps: list[dict]) -> str:
    """Kompakte Auflistung der tatsaechlich geprueften Schritte fuer die Rueckfrage.

    Zeigt Service und aufgeloeste Ziel-Entity statt nur den Freitext des Modells. Der
    Broker biegt geratene Entity-IDs still zurecht (siehe ``broker._resolve_entity_id``,
    inklusive Aufloesung nur ueber das Raum-Praefix) - ohne diese Vorschau bestaetigt der
    Nutzer eine Behauptung des Modells, nicht die Aktion, die tatsaechlich laeuft.
    """

    zeilen = []
    for step in steps:
        teile = [str(step.get("target", "?"))]
        service_data = step.get("service_data") or {}
        entity_id = service_data.get("entity_id")
        if entity_id:
            teile.append(
                ", ".join(str(e) for e in entity_id)
                if isinstance(entity_id, list)
                else str(entity_id)
            )
        weitere = {key: value for key, value in service_data.items() if key != "entity_id"}
        if weitere:
            teile.append(", ".join(f"{key}={value}" for key, value in weitere.items()))
        zeilen.append(f"- {' -> '.join(teile)}")
    return "\n".join(zeilen)


async def _restart_ha_delayed(hass: HomeAssistant) -> None:
    """Startet Home Assistant mit kurzer Verzoegerung neu.

    Die Verzoegerung sorgt dafuer, dass die Chat-Antwort den Nutzer noch
    erreicht, bevor Home Assistant fuer den Neustart die Verbindung trennt.
    """

    await asyncio.sleep(3)
    await hass.services.async_call("homeassistant", "restart", blocking=False)


@dataclass
class PendingAction:
    """Eine zurueckgestellte Aktion, die auf ein ja/nein des Nutzers wartet.

    ``payload`` enthaelt genau das, was die zugehoerige Ausfuehrung braucht
    (Schritte, YAML, url_path ...); ``reason`` ist der Text, mit dem die Aktion
    angekuendigt wurde. Ueber ``created_at`` verfaellt die Rueckfrage nach der
    eingestellten TTL.
    """

    # "action_plan" | "automation_draft" | "dashboard_draft" | "dashboard_delete" | "restart_ha"
    kind: str
    payload: dict
    reason: str
    created_at: datetime


@dataclass
class ConversationSession:
    """Gespraechszustand einer Konversations-ID: Verlauf, offene Rueckfrage und Modellwahl."""

    conversation_id: str
    history: list[dict] = field(default_factory=list)
    pending: PendingAction | None = None
    # None = Standard-Provider aus dem Provider-Store; sonst per Service
    # "smart_homeassistant.set_conversation_model" fuer dieses Gespraech gesetzt
    # (siehe frontend/floating-window.js, "Modell wechseln"-Button).
    provider_id: str | None = None
    # Grundlage fuers Aufraeumen alter Gespraeche (siehe _prune_sessions).
    last_used: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # Serialisiert die Bearbeitung innerhalb EINES Gespraechs. Ohne den Lock koennen zwei
    # schnell hintereinander abgeschickte Nachrichten (Doppelklick im Chat-Fenster, Text-
    # und Spracheingabe parallel) dieselbe offene Rueckfrage sehen und die zurueckgestellte
    # Aktion zweimal ausfuehren; ausserdem wuerden beide gleichzeitig an "history" haengen.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _prune_sessions(
    sessions: dict[str, ConversationSession], keep_id: str, ttl_minutes: int
) -> None:
    """Verwirft lange unbenutzte Gespraeche aus dem Speicher.

    Das aktuelle Gespraech bleibt immer erhalten. Ein Gespraech mit offener Rueckfrage
    bleibt so lange verschont, wie die Rueckfrage ueberhaupt noch beantwortbar ist (siehe
    ``is_expired``); danach faellt es unter dieselbe Idle-Regel wie jedes andere. Vorher
    genuegte eine einzige nie beantwortete Rueckfrage, damit ein Gespraech bis zum
    HA-Neustart im Speicher liegen blieb.
    """

    cutoff = datetime.now(timezone.utc) - timedelta(hours=SESSION_MAX_IDLE_HOURS)
    for conversation_id, session in list(sessions.items()):
        if conversation_id == keep_id:
            continue
        if session.pending is not None and not is_expired(session.pending.created_at, ttl_minutes):
            continue
        if session.last_used < cutoff:
            del sessions[conversation_id]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Richtet die Conversation-Entity fuer diesen Config Entry ein."""

    async_add_entities([SmartHomeAssistantConversationEntity(hass, entry)])


class SmartHomeAssistantConversationEntity(conversation.ConversationEntity):
    """Der eigentliche KI-Assistent, registriert als HA Conversation Agent."""

    _attr_has_entity_name = True
    _attr_name = "Smart Homeassistant"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = entry.entry_id

    @property
    def supported_languages(self) -> list[str] | Literal["*"]:
        """Alle Sprachen: die Antwortsprache folgt der Anfrage bzw. der HA-Systemsprache
        (siehe ``async_process``), nicht mehr fest Deutsch."""

        return MATCH_ALL

    async def async_added_to_hass(self) -> None:
        """Meldet die Entity als aktiven Conversation Agent an."""

        await super().async_added_to_hass()
        conversation.async_set_agent(self.hass, self._entry, self)

    async def async_will_remove_from_hass(self) -> None:
        """Meldet den Conversation Agent wieder ab."""

        conversation.async_unset_agent(self.hass, self._entry)
        await super().async_will_remove_from_hass()

    # --- Zugriff auf den gemeinsamen Zustand aus hass.data (siehe __init__.py) ---

    def _sessions(self) -> dict[str, ConversationSession]:
        """Alle laufenden Gespraeche, nach Konversations-ID."""

        return self.hass.data[DOMAIN][self._entry.entry_id][DATA_SESSIONS]

    def _policy(self):
        """Die aktuell geladene Policy-Whitelist."""

        return self.hass.data[DOMAIN][self._entry.entry_id][DATA_POLICY]

    def _provider_store(self):
        """Die konfigurierten KI-Modell-Provider (siehe providers.ProviderStore)."""

        return self.hass.data[DOMAIN][self._entry.entry_id][DATA_PROVIDER_STORE]

    def _client_for_session(self, session: ConversationSession, language: str) -> LLMClient:
        """Der Client fuer das in diesem Gespraech gewaehlte Modell (sonst der Standard).

        ``session.provider_id`` wird per Service ``smart_homeassistant.set_conversation_model``
        gesetzt (siehe frontend/floating-window.js, "Modell wechseln"-Button) - jedes
        Gespraech kann so ein anderes Modell nutzen, ohne dass ein Wechsel andere
        laufende Gespraeche beeinflusst.
        """

        store = self._provider_store()
        provider = store.get(session.provider_id) if session.provider_id else None
        if provider is None:
            provider = store.get_default()
        if provider is None:
            raise BrokerError(t("no_provider_configured", language), "no_provider")
        try:
            return create_client(self.hass, provider)
        except InsecureEndpointError as exc:
            # Als BrokerError weitergereicht, weil _handle_new_message genau den faengt
            # und dessen Text unveraendert in den Chat stellt - der Nutzer erfaehrt so den
            # tatsaechlichen Grund statt eines generischen "Anfrage fehlgeschlagen".
            raise BrokerError(
                t("insecure_endpoint", language, host=exc.host), "insecure_endpoint"
            ) from exc

    def _display_names(self) -> dict[str, str]:
        """Korrekte Schreibweisen aus Home Assistant fuer die Umlaut-Nachbearbeitung."""

        return name_map(self.hass)

    def _normalize_output(self, result):
        """Schreibt Umlaute im Freitext einer Modellantwort aus (siehe text_format).

        Betrifft nur die Felder, die der Nutzer als Text zu sehen bekommt bzw. die als
        Titel gespeichert werden. ``automation_yaml`` und ``dashboard_view_yaml`` bleiben
        bewusst unberuehrt: dort stehen entity_ids, die Zeichen fuer Zeichen stimmen
        muessen - deren Kartentitel normalisiert dashboard.validate_dashboard_view_yaml
        gezielt.
        """

        namen = self._display_names()
        result.message = normalize_umlauts(result.message, namen)
        if result.automation_alias:
            result.automation_alias = normalize_umlauts(result.automation_alias, namen)
        if result.dashboard_title:
            result.dashboard_title = normalize_umlauts(result.dashboard_title, namen)
        return result

    def _offline_docs(self) -> str:
        """Gespeicherte Home-Assistant-Dokumentation fuer Docs-only Antworten."""

        return self.hass.data[DOMAIN][self._entry.entry_id].get(DATA_OFFLINE_DOCS, "")

    def _ttl_minutes(self) -> int:
        """Wie lange eine offene Rueckfrage gueltig bleibt."""

        return get_option(self._entry, CONF_CONFIRMATION_TTL, DEFAULT_CONFIRMATION_TTL_MINUTES)

    def _log_action(self, kind: str, reason: str, status: str) -> None:
        """Haelt eine Aktion im Verlauf fest (status: executed/rejected/failed).

        Der Verlauf speist den Sensor "Letzte Aktion" und ist auf die letzten
        ACTION_LOG_MAX_ENTRIES Eintraege begrenzt.
        """

        log = self.hass.data[DOMAIN][self._entry.entry_id][DATA_ACTION_LOG]
        log.append(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "kind": kind,
                "reason": reason,
                "status": status,
            }
        )
        del log[:-ACTION_LOG_MAX_ENTRIES]
        # Der Sensor haelt keine eigenen Daten und wuesste sonst nichts von der Aenderung
        # (siehe sensor.py) - ohne dieses Signal zeigt er den neuen Eintrag erst beim
        # naechsten Poll.
        async_dispatcher_send(self.hass, action_log_signal(self._entry.entry_id))

    def _queue_restart(self, session: ConversationSession, reason: str) -> None:
        """Stellt die Rueckfrage "jetzt neu starten?" als naechste offene Aktion zurueck."""

        session.pending = PendingAction(
            kind="restart_ha", payload={}, reason=reason, created_at=datetime.now(timezone.utc)
        )

    def _reject(
        self,
        session: ConversationSession,
        language: str,
        kind: str,
        message: str,
        reply_error: str,
        log_error: str | None = None,
    ) -> conversation.ConversationResult:
        """Lehnt einen KI-Vorschlag ab: loggt ihn und antwortet mit Begruendung.

        Gemeinsames Ende der "kann nicht ausgefuehrt werden"-Zweige in
        :meth:`_handle_new_message` (Policy-Ablehnung, unbrauchbares Dashboard,
        unbekanntes Loesch-Ziel ...). ``log_error`` weicht beim Loeschen eines
        unbekannten Dashboards bewusst vom angezeigten Fehler ab (kuerzere
        Zusammenfassung fuer den Aktionsverlauf).
        """

        reply = f"{message}\n\n{t('abgelehnt_prefix', language, reason=reply_error)}"
        self._log_action(kind, f"{message} (Abgelehnt: {log_error or reply_error})".strip(), "rejected")
        return self._respond(reply, session.conversation_id, False, language)

    def _ask_confirmation(
        self,
        session: ConversationSession,
        language: str,
        kind: str,
        payload: dict,
        reason: str,
        question: str,
    ) -> conversation.ConversationResult:
        """Stellt einen KI-Vorschlag als Rueckfrage zurueck, bis der Nutzer zustimmt.

        Gemeinsames Ende der "wirkt dauerhaft, erst nach ja/nein"-Zweige in
        :meth:`_handle_new_message` (Aktionsplan, neue Automation, neues bzw.
        geloeschtes Dashboard).
        """

        session.pending = PendingAction(
            kind=kind, payload=payload, reason=reason, created_at=datetime.now(timezone.utc)
        )
        return self._respond(question, session.conversation_id, True, language)

    # --- Gespraechsablauf ---

    async def async_process(
        self, user_input: conversation.ConversationInput
    ) -> conversation.ConversationResult:
        """Nimmt eine Nutzereingabe entgegen und beantwortet sie.

        Verteilt auf die beiden Wege "offene Rueckfrage beantworten" und "neue
        Anfrage bearbeiten"; eine abgelaufene Rueckfrage wird vorher verworfen.
        """

        sessions = self._sessions()
        conv_id = user_input.conversation_id or str(uuid.uuid4())
        session = sessions.setdefault(conv_id, ConversationSession(conversation_id=conv_id))
        # Die vom Pipeline/Frontend uebergebene Sprache hat Vorrang; ohne Angabe gilt die in
        # Home Assistant selbst eingestellte Systemsprache (Settings > System > General) als
        # Standard - nicht mehr fest Deutsch.
        language = user_input.language or self.hass.config.language or "de"

        # Alles ab hier arbeitet auf history/pending dieses Gespraechs - siehe
        # ConversationSession.lock.
        async with session.lock:
            session.last_used = datetime.now(timezone.utc)
            _prune_sessions(sessions, conv_id, self._ttl_minutes())

            if session.pending is not None and is_expired(
                session.pending.created_at, self._ttl_minutes()
            ):
                expired = session.pending
                session.pending = None
                self._log_action(expired.kind, expired.reason, "rejected")
                # Eine abgelaufene Rueckfrage einfach still zu verwerfen hat das darauf
                # folgende "ja" als ganz normale neue Nachricht ins Modell laufen lassen -
                # mit vollem System-Prompt, aus dem das Modell einen beliebigen neuen
                # Vorschlag baute. Deshalb wird nur eine Eingabe abgefangen, die wie die
                # Antwort auf genau diese Rueckfrage aussieht; eine inhaltlich neue Anfrage
                # laeuft unveraendert weiter.
                if _match_yes_no(user_input.text) is not None:
                    return self._respond(
                        t("confirmation_expired", language),
                        session.conversation_id,
                        False,
                        language,
                    )

            if session.pending is not None:
                result = await self._handle_pending(session, user_input.text, language)
            else:
                result = await self._handle_new_message(session, user_input.text, language)

            # Zentral statt an jeder einzelnen append-Stelle: egal welchen Weg die Anfrage
            # genommen hat, der Verlauf bleibt danach beschraenkt (MAX_HISTORY_MESSAGES).
            del session.history[:-MAX_HISTORY_MESSAGES]
            return result

    async def _handle_pending(
        self, session: ConversationSession, text: str, language: str
    ) -> conversation.ConversationResult:
        """Wertet die Antwort auf eine offene Rueckfrage aus.

        Bei "ja" wird die zurueckgestellte Aktion ausgefuehrt, bei "nein"
        verworfen; ist die Antwort unklar, bleibt die Rueckfrage bestehen.
        """

        decision = _match_yes_no(text)
        if decision is None:
            return self._respond(
                t("yes_no_prompt", language), session.conversation_id, True, language
            )

        pending = session.pending
        session.pending = None

        if not decision:
            reply = t("cancelled", language)
            self._log_action(pending.kind, pending.reason, "rejected")
            session.history.append({"role": "user", "content": text})
            session.history.append({"role": "assistant", "content": reply})
            return self._respond(reply, session.conversation_id, False, language)

        policy = self._policy()
        # Damit die Zweitpruefung unten wirklich etwas pruefen kann, muss die Policy den
        # aktuellen Dateistand haben - sonst prueft sie gegen exakt dieselben Daten wie
        # beim Erstellen des Vorschlags (siehe Policy.async_reload_if_changed).
        await policy.async_reload_if_changed()

        # Loest die Aktion selbst eine neue Rueckfrage aus (z.B. "neu starten?"),
        # bleibt das Gespraech offen.
        continue_conversation = False
        # Zaehler fuer einen teilweise ausgefuehrten Aktionsplan (siehe except-Zweig).
        executed_steps = 0
        total_steps = 0
        try:
            if pending.kind == "action_plan":
                steps = pending.payload["steps"]
                total_steps = len(steps)
                # Erst alle Schritte pruefen, dann ausfuehren: so scheitert ein
                # unerlaubter Schritt, bevor der erste Effekt eingetreten ist.
                for step in steps:
                    check_plan_step(
                        policy, step
                    )  # defense in depth: Policy kann sich geaendert haben
                for step in steps:
                    await execute_step(self.hass, step)
                    executed_steps += 1
                reply = f"{pending.reason} {t('done', language)}"
            elif pending.kind == "automation_draft":
                result = await activate_automation_draft(self.hass, policy, pending.payload)
                reply = t("automation_activated", language, alias=result["alias"])
            elif pending.kind == "dashboard_draft":
                result = await activate_dashboard_draft(self.hass, pending.payload)
                # Ein neuer Sidebar-Eintrag erscheint erst nach einem Neustart
                # (siehe Modul-Docstring von dashboard.py) - also gleich anbieten.
                if result.get("restart_required"):
                    self._queue_restart(session, f"Neustart für Dashboard '{result['title']}'")
                    reply = t(
                        "restart_after_dashboard_create_question", language, title=result["title"]
                    )
                    continue_conversation = True
                else:
                    reply = t("dashboard_created", language, title=result["title"])
            elif pending.kind == "dashboard_delete":
                result = await delete_dashboard(self.hass, pending.payload["url_path"])
                if result.get("restart_required"):
                    self._queue_restart(
                        session, f"Neustart nach Löschen von Dashboard '{result['title']}'"
                    )
                    reply = t(
                        "restart_after_dashboard_delete_question", language, title=result["title"]
                    )
                    continue_conversation = True
                else:
                    reply = t("dashboard_deleted", language, title=result["title"])
            elif pending.kind == "restart_ha":
                reply = t("restarting", language)
                self.hass.async_create_task(_restart_ha_delayed(self.hass))
            else:
                reply = t("unknown_action", language)
            self._log_action(pending.kind, pending.reason, "executed")
        # BLE001 bewusst: jeder Fehler soll als Sprachantwort landen, nicht als
        # HA-Fehler-Toast - der Nutzer sitzt in einem Gespraech. Trotzdem mit vollem
        # Traceback geloggt, sonst ist ein Fehler wie ein falscher API-Key oder
        # Modellname von aussen nicht diagnostizierbar (nur "Fehler: ..." im Chat).
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("Ausführen der bestätigten Aktion '%s' fehlgeschlagen", pending.kind)
            # Bei einem mittendrin gescheiterten Plan haben die vorherigen Schritte bereits
            # gewirkt - das gehoert in die Antwort, sonst liest sich der Fehler so, als sei
            # nichts passiert.
            teilhinweis = (
                f"{t('plan_partially_executed', language, done=executed_steps, total=total_steps)} "
                if 0 < executed_steps < total_steps
                else ""
            )
            reply = f"{teilhinweis}{t('error_prefix', language, error=exc)}"
            self._log_action(pending.kind, pending.reason, "failed")

        session.history.append({"role": "user", "content": text})
        session.history.append({"role": "assistant", "content": reply})
        return self._respond(reply, session.conversation_id, continue_conversation, language)

    async def _handle_new_message(
        self, session: ConversationSession, text: str, language: str
    ) -> conversation.ConversationResult:
        """Fragt das Sprachmodell und verarbeitet dessen Vorschlag.

        Der System-Prompt wird bei jeder Anfrage neu gebaut, damit das Modell mit
        dem aktuellen Stand von Policy, Entities und Dashboards arbeitet. Was
        danach passiert, haengt an ``result.kind`` - siehe die Verzweigungen unten.
        """

        policy = self._policy()
        # Vor jeder Anfrage: hat jemand die Policy-Datei bearbeitet? (Nur ein stat(), siehe
        # Policy.async_reload_if_changed.)
        await policy.async_reload_if_changed()

        try:
            client = self._client_for_session(session, language)
        except BrokerError as exc:
            return self._respond(str(exc), session.conversation_id, False, language)

        if is_documentation_question(text):
            reply = await self._answer_documentation_question(policy, client, text, language)
            # None heisst: kein passender Doku-Abschnitt gefunden (die Heuristik hat also
            # vermutlich danebengegriffen). Dann laeuft die Frage als normale Anfrage
            # weiter, statt in "dazu finde ich nichts" zu enden - Fragen wie "welche
            # Services gibt es fuer media_player?" beantwortet der normale System-Prompt
            # mit dem echten Systemzustand ohnehin besser als die gespeicherte Doku.
            if reply is not None:
                session.history.append({"role": "user", "content": text})
                session.history.append({"role": "assistant", "content": reply})
                return self._respond(reply, session.conversation_id, False, language)

        # Fragen nach bestehenden Automationen werden deterministisch aus automations.yaml
        # beantwortet statt vom LLM klassifiziert (siehe _is_automation_status_question).
        if _is_automation_status_question(text):
            reply = _describe_automations(await read_automations(self.hass), language)
            session.history.append({"role": "user", "content": text})
            session.history.append({"role": "assistant", "content": reply})
            return self._respond(reply, session.conversation_id, False, language)

        # Der Kontextaufbau liest automations.yaml und configuration.yaml und faellt damit
        # auf den Dateizustand herein - bisher ausserhalb jeder Fehlerbehandlung, sodass
        # eine kaputte Datei den kompletten Chat mit einem HA-Fehler statt einer Antwort
        # beendet hat. Der Modul-Docstring verspricht ausdruecklich das Gegenteil.
        try:
            states = await self._relevant_states(policy)
            automations = await read_automations(self.hass)
            dashboard_entities = await list_known_entities(self.hass)
            deletable_dashboards = await list_deletable_dashboards(self.hass)
            automation_entities = self._automation_entities(policy)
            # Nur was zur Anfrage passt in den Prompt (siehe entity_filter). Betrifft
            # ausschliesslich die Darstellung - policy.data bleibt vollstaendig, der
            # Broker prueft unveraendert dagegen.
            prompt_policy_data, states, automation_entities, dashboard_entities = self._shortlist(
                policy.data,
                states,
                automation_entities,
                dashboard_entities,
                relevance_query(session.history, text),
            )
            system_prompt = build_system_prompt(
                prompt_policy_data,
                states,
                automations,
                dashboard_entities,
                deletable_dashboards,
                automation_entities,
                language,
            )
        except Exception:  # noqa: BLE001 - als Chat-Antwort statt als HA-Fehler-Toast
            _LOGGER.exception("Kontext für den System-Prompt konnte nicht aufgebaut werden")
            reply = t("context_failed", language)
            session.history.append({"role": "user", "content": text})
            session.history.append({"role": "assistant", "content": reply})
            return self._respond(reply, session.conversation_id, False, language)
        messages = (
            [{"role": "system", "content": system_prompt}]
            + session.history
            + [{"role": "user", "content": text}]
        )

        try:
            result = await client.ask(messages)
        except Exception:  # noqa: BLE001 - LLM/Transport-Fehler sollen als Sprachantwort landen
            _LOGGER.exception(
                "Anfrage an das Sprachmodell (Provider-Client %s) fehlgeschlagen",
                type(client).__name__,
            )
            reply = t("request_failed", language)
            session.history.append({"role": "user", "content": text})
            session.history.append({"role": "assistant", "content": reply})
            return self._respond(reply, session.conversation_id, False, language)

        # Umlaute ausschreiben, bevor die Antwort in Verlauf, Aktionsverlauf und Chat
        # geht - der System-Prompt bittet darum, aber kleine Modelle halten sich nicht
        # daran (siehe text_format).
        result = self._normalize_output(result)

        session.history.append({"role": "user", "content": text})
        session.history.append({"role": "assistant", "content": result.message})

        # Aktionsplan: Schritte gegen die Policy pruefen, dann je nach Ergebnis
        # sofort ausfuehren oder zur Bestaetigung zurueckstellen.
        if result.kind == "action_plan" and result.steps:
            steps = [s.model_dump() for s in result.steps]
            try:
                needs_confirmation = validate_plan(policy, steps)
            except BrokerError as exc:
                return self._reject(session, language, "action_plan", result.message, str(exc))

            if needs_confirmation:
                return self._ask_confirmation(
                    session,
                    language,
                    "action_plan",
                    {"steps": steps},
                    result.message,
                    f"{result.message}\n\n{t('steps_preview_header', language)}\n"
                    f"{_describe_steps(steps)}\n\n{t('continue_question', language)}",
                )

            executed_steps = 0
            try:
                for step in steps:
                    await execute_step(self.hass, step)
                    executed_steps += 1
                reply = result.message
                self._log_action("action_plan", result.message, "executed")
            except Exception as exc:  # noqa: BLE001
                _LOGGER.exception("Ausführen des Aktionsplans fehlgeschlagen")
                teilhinweis = (
                    f"{t('plan_partially_executed', language, done=executed_steps, total=len(steps))} "
                    if 0 < executed_steps < len(steps)
                    else ""
                )
                reply = (
                    f"{result.message}\n\n{teilhinweis}"
                    f"{t('error_prefix', language, error=exc)}"
                )
                self._log_action("action_plan", result.message, "failed")
            return self._respond(reply, session.conversation_id, False, language)

        # Automations-Entwurf: die Policy-/Schema-Pruefung wird ggf. mehrfach neu
        # versucht (siehe _generate_automation), dann immer nachfragen - eine neue
        # Automation wirkt dauerhaft und ungefragt.
        if result.kind == "automation_draft" and result.automation_yaml:
            best, parsed, last_error, error_code = await self._generate_automation(
                client, policy, messages, result
            )

            if best is None:
                return self._reject(
                    session,
                    language,
                    "automation_draft",
                    result.message,
                    _automation_rejection_message(error_code, language),
                    log_error=last_error,
                )

            result = self._normalize_output(best)
            session.history[-1]["content"] = result.message

            # Vorschau der tatsaechlich geprueften Automation, unabhaengig vom Freitext in
            # "message" - der Nutzer soll vor dem Bestaetigen sehen, was wirklich aktiviert
            # wuerde (Schutz gegen z.B. ein vom Modell missverstandenes/kopiertes Beispiel).
            preview = yaml.safe_dump(
                {k: parsed[k] for k in ("alias", "trigger", "condition", "action") if k in parsed},
                allow_unicode=True,
                sort_keys=False,
            ).strip()

            return self._ask_confirmation(
                session,
                language,
                "automation_draft",
                {"yaml": result.automation_yaml, "alias": result.automation_alias},
                result.message,
                f"{result.message or t('automation_draft_created', language)}\n\n```\n{preview}\n```\n"
                f"{t('activate_automation_question', language)}",
            )

        # Dashboard-Entwurf: die Gliederung wird ggf. mehrfach neu erzeugt, bis sie
        # den Layout-Regeln genuegt (siehe _generate_dashboard).
        if result.kind == "dashboard_draft" and result.dashboard_view_yaml:
            best, dropped, problems, last_error = await self._generate_dashboard(
                client, messages, result
            )

            if best is None:
                return self._reject(
                    session, language, "dashboard_draft", result.message, last_error
                )

            # Der Verlauf soll den letzten, tatsaechlich verwendeten Entwurf zeigen.
            result = self._normalize_output(best)
            session.history[-1]["content"] = result.message

            # Einschraenkungen des Ergebnisses offenlegen, statt sie zu verschweigen.
            notes = []
            if dropped:
                notes.append(
                    t("dropped_entities_note", language, entities=", ".join(sorted(dropped)))
                )
            if problems:
                # Nach allen Versuchen bleibt die beste Variante - der Nutzer soll aber
                # wissen, dass das Layout nicht ideal ist, statt es stillschweigend zu bekommen.
                notes.append(t("layout_not_optimal_note", language, problem=problems[0]))
            note = f"\n\n{t('hint_prefix', language, notes='; '.join(notes))}" if notes else ""

            return self._ask_confirmation(
                session,
                language,
                "dashboard_draft",
                {"yaml": result.dashboard_view_yaml, "title": result.dashboard_title},
                result.message,
                f"{result.message}{note} {t('create_dashboard_question', language)}",
            )

        # Dashboard loeschen: nur was auch in der Liste der loeschbaren Dashboards
        # steht - das Modell koennte sonst einen beliebigen url_path nennen.
        if result.kind == "dashboard_delete" and result.dashboard_delete_target:
            target = result.dashboard_delete_target
            deletable_by_path = {d["url_path"]: d for d in deletable_dashboards}
            entry = deletable_by_path.get(target)
            if entry is None:
                return self._reject(
                    session,
                    language,
                    "dashboard_delete",
                    result.message,
                    t("not_a_deletable_dashboard", language, target=target),
                    log_error=t("not_found_short", language),
                )

            return self._ask_confirmation(
                session,
                language,
                "dashboard_delete",
                {"url_path": target},
                result.message,
                t("delete_dashboard_warning", language, title=entry["title"]),
            )

        # Rueckfrage des Modells: Gespraech offen halten, damit die Antwort des
        # Nutzers als Fortsetzung ankommt.
        if result.kind == "clarification":
            return self._respond(result.message, session.conversation_id, True, language)

        # kind == "chat" | "explanation": reine Textantwort, nichts auszufuehren.
        return self._respond(result.message, session.conversation_id, False, language)

    def _shortlist(
        self,
        policy_data: dict,
        states: list[dict],
        automation_entities: list[dict],
        dashboard_entities: list[dict],
        query: str,
    ) -> tuple[dict, list[dict], list[dict], list[dict]]:
        """Kuerzt die drei Entity-Listen des System-Prompts auf das zur Anfrage Passende.

        Die drei Listen werden getrennt betrachtet, weil ein Fehlgriff unterschiedlich
        teuer ist: auf dem handelnden Pfad (Service-Ziele, Automation-Entities) schaltet
        eine falsche Auswahl real etwas Falsches oder blockiert die Anfrage ganz, auf dem
        Dashboard-Pfad ist sie rein optisch und wird ohnehin nachtraeglich gefiltert
        (``dashboard._filter_known_entities``). Der handelnde Pfad bekommt deshalb die
        deutlich hoehere Schwelle - bei den heutigen Listengroessen bleibt er unangetastet
        und waechst erst mit der Installation hinein.

        Der Anzeigename geht als zusaetzlicher Suchtext mit ein: die entity_id heisst
        ``light.kuche_aqara_lampe_kuche``, der Nutzer sagt aber "Deckenlampe".
        """

        def suchtext(entity_id: str) -> str:
            state = self.hass.states.get(entity_id)
            return state.attributes.get("friendly_name", "") if state is not None else ""

        aktionsziele = {
            entity_id: suchtext(entity_id)
            for config in (policy_data.get("services") or {}).values()
            for entity_id in (config or {}).get("allowed_entities", [])
            if entity_id != "*"
        }
        behalten = select_relevant(aktionsziele, query, ACTION_LIST_THRESHOLD)
        if behalten is None:
            prompt_policy_data = policy_data
        else:
            prompt_policy_data = policy_data_for_prompt(policy_data, behalten)
            states = [s for s in states if s["entity_id"] in behalten]

        automation_kandidaten = {e["entity_id"]: suchtext(e["entity_id"]) for e in automation_entities}
        behalten = select_relevant(automation_kandidaten, query, ACTION_LIST_THRESHOLD)
        if behalten is not None:
            automation_entities = [e for e in automation_entities if e["entity_id"] in behalten]

        dashboard_kandidaten = {e["entity_id"]: e.get("name", "") for e in dashboard_entities}
        behalten = select_relevant(dashboard_kandidaten, query, DASHBOARD_LIST_THRESHOLD)
        if behalten is not None:
            dashboard_entities = [e for e in dashboard_entities if e["entity_id"] in behalten]

        return prompt_policy_data, states, automation_entities, dashboard_entities

    async def _answer_documentation_question(
        self, policy, client: LLMClient, text: str, language: str
    ) -> str | None:
        """Beantwortet Home-Assistant-Dokumentationsfragen nur aus Offline-Doku.

        Gibt ``None`` zurueck, wenn diese Frage hier gar nicht behandelt werden sollte -
        weil der Dokumentationsmodus abgeschaltet ist oder sich in der gespeicherten Doku
        kein einziger passender Abschnitt findet. Der Aufrufer laesst sie dann als normale
        Anfrage weiterlaufen (siehe ``_handle_new_message``), statt sie mit "dazu finde ich
        nichts" abzuwuergen: dass nichts passt, heisst meist, dass die Heuristik
        ``is_documentation_question`` danebengegriffen hat.

        Wurde dagegen passender Kontext gefunden und das Modell kann daraus trotzdem nicht
        antworten, bleibt es bei der ehrlichen Absage - genau dafuer ist
        ``strict_docs_only`` da.
        """

        docs_policy = docs_policy_from_data(self.hass, policy.data)
        if not docs_policy.enabled:
            return None

        docs = self._offline_docs()
        context = retrieve_docs_context(docs, text)
        if not context:
            return None

        sources = "\n".join(f"- {url}" for url in docs_policy.source_urls)
        system_prompt = DOCS_ONLY_SYSTEM_PROMPT.format(
            language=language, context=f"{context}\n\nOffizielle Quellen:\n{sources}"
        )
        try:
            answer = await client.ask_plain(system_prompt, text)
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "Dokumentationsfrage an das Sprachmodell (Provider-Client %s) fehlgeschlagen",
                type(client).__name__,
            )
            return t("docs_question_failed", language)

        if docs_policy.strict and not answer.strip():
            return t("docs_not_found", language)
        return normalize_umlauts(answer, self._display_names())

    async def _generate_automation(
        self, client: LLMClient, policy, messages: list[dict], first_result
    ):
        """Erzeugt den Automation-Entwurf und laesst bei Policy-/Schema-Fehlern neu generieren.

        Analog zu :meth:`_generate_dashboard`: ein kleines lokales Modell trifft nicht
        jedes Mal exakte Entity-IDs oder ein gueltiges HA-Automation-Schema. Der konkrete
        Fehler geht als Rueckmeldung in den naechsten Versuch ein, statt beim ersten
        Fehlversuch endgueltig aufzugeben.

        Rueckgabe: (LLMOutput mit gueltigem Entwurf oder None, geparste Automation oder None,
        letzter Fehler oder None, letzter Fehler-Code oder None).
        """

        candidate = first_result
        last_error: str | None = None
        error_code: str | None = None
        deadline = time.monotonic() + GENERATION_BUDGET_SECONDS

        for attempt in range(AUTOMATION_MAX_ATTEMPTS):
            try:
                parsed = await async_validate_automation_yaml(
                    self.hass, policy, candidate.automation_yaml or ""
                )
                return candidate, parsed, None, None
            except BrokerError as exc:
                last_error = str(exc)
                error_code = exc.code

            if attempt == AUTOMATION_MAX_ATTEMPTS - 1 or time.monotonic() >= deadline:
                break

            # Die Korrektur-Anleitung passt zur Fehlerart - "nutze exakte entity_ids" hilft
            # nichts, wenn das Modell gar kein YAML-Objekt geliefert hat (leeres/erklaerendes
            # "automation_yaml" statt Struktur), und umgekehrt.
            if error_code in ("entity_not_allowed", "service_not_allowed"):
                hinweis = (
                    "Nutze AUSSCHLIESSLICH exakte entity_ids Zeichen für Zeichen aus der Liste "
                    "'Zusätzlich für NEUE Automationen verfügbare Entities' oben - kopiere sie "
                    "wörtlich, erfinde keine neuen Namen oder Domains."
                )
            elif error_code == "invalid_homeassistant_automation":
                hinweis = (
                    "Achte auf gültige Home-Assistant-Trigger-/Condition-/Action-Syntax (z.B. "
                    "'platform: numeric_state' mit 'above'/'below' für Zahlen-Schwellenwerte, "
                    "NICHT 'platform: state' mit einem Vergleichsausdruck als Zustand)."
                )
            elif error_code == "example_copied":
                hinweis = (
                    "Das war unverändert eines der Beispiele aus der Anleitung, keine Antwort auf "
                    "die tatsächliche Anfrage. Falls die Anfrage gar keine neue Automation erstellen "
                    "wollte (z.B. eine Frage nach bestehenden Automationen war), antworte stattdessen "
                    "mit kind=chat oder kind=explanation. Andernfalls erzeuge eine Automation mit "
                    "einem zur Anfrage passenden Auslöser und einer passenden Aktion - nicht die "
                    "18-Uhr-Wohnzimmerlicht-Beispiele."
                )
            else:
                hinweis = (
                    "\"automation_yaml\" muss ein VOLLSTÄNDIGES YAML-Objekt mit den Schlüsseln "
                    "'alias', 'trigger', 'action' sein - kein Fließtext, keine Erklärung, kein "
                    "Markdown-Codeblock, keine leere Antwort. \"kind\" bleibt \"automation_draft\"."
                )

            feedback = (
                f"Deine Antwort wurde verworfen. Fehler: {last_error} {hinweis} "
                "Antworte erneut im gleichen JSON-Format für dieselbe Anfrage."
            )
            try:
                candidate = await client.ask(
                    messages
                    + [
                        {"role": "assistant", "content": candidate.model_dump_json()},
                        {"role": "user", "content": feedback},
                    ]
                )
            except Exception:  # noqa: BLE001 - LLM/Transport-Fehler beenden nur die Schleife
                _LOGGER.exception("Retry-Anfrage für Automation-Entwurf fehlgeschlagen")
                break

        return None, None, last_error or "Automation konnte nicht erzeugt werden.", error_code

    async def _generate_dashboard(self, client, messages: list[dict], first_result):
        """Erzeugt die Dashboard-Gliederung und laesst bei Maengeln neu generieren.

        Geprueft wird zweistufig: erst ob die Entities ueberhaupt existieren, dann ob
        die Gliederung den Layout-Regeln genuegt. Beides fliesst als konkrete
        Rueckmeldung in den naechsten Versuch ein - ein blosses "nochmal" wuerde beim
        selben Modell meist denselben Fehler nochmal produzieren.

        Rueckgabe: (bestes Ergebnis oder None, ignorierte Entities, verbliebene
        Layout-Maengel, letzter harter Fehler).
        """

        candidate = first_result
        # Beste bisherige Variante als (Maengel, Ergebnis, ignorierte Entities) -
        # falls kein Versuch fehlerfrei bleibt, wird sie am Ende genommen.
        best: tuple[list[str], object, set[str]] | None = None
        last_error: str | None = None
        deadline = time.monotonic() + GENERATION_BUDGET_SECONDS

        for attempt in range(DASHBOARD_MAX_ATTEMPTS):
            try:
                parsed, dropped = validate_dashboard_view_yaml(
                    self.hass, candidate.dashboard_view_yaml or ""
                )
            # Harter Fehler: die Gliederung ist unbrauchbar (kaputtes YAML oder
            # ausschliesslich erfundene Entities).
            except BrokerError as exc:
                last_error = str(exc)
                feedback = (
                    f"Deine Antwort wurde verworfen. Fehler: {exc} "
                    "Nutze AUSSCHLIESSLICH exakte entity_ids Zeichen für Zeichen aus der Liste "
                    "'Für Dashboards verfügbare Entities' oben - kopiere sie wörtlich, erfinde "
                    "keine neuen Namen oder Domains. Antworte erneut im gleichen JSON-Format für "
                    "dieselbe Anfrage."
                )
            # Die Gliederung ist verwertbar - jetzt zaehlt nur noch die Layout-Qualitaet.
            else:
                last_error = None
                problems = check_layout_quality(parsed)
                if not problems:
                    return candidate, dropped, [], None

                # Weniger Maengel als bisher -> neuer Rueckfall-Kandidat.
                if best is None or len(problems) < len(best[0]):
                    best = (problems, candidate, dropped)

                aufzaehlung = "\n".join(f"- {p}" for p in problems)
                feedback = (
                    "Deine Antwort wurde wegen Verstößen gegen die LAYOUT-REGELN verworfen:\n"
                    f"{aufzaehlung}\n"
                    "Erzeuge die Gliederung komplett neu und halte dabei ALLE 5 LAYOUT-REGELN ein. "
                    "Antworte erneut im gleichen JSON-Format für dieselbe Anfrage."
                )

            if attempt == DASHBOARD_MAX_ATTEMPTS - 1 or time.monotonic() >= deadline:
                break

            # Naechster Versuch: eigene Fehlantwort plus konkrete Kritik als
            # Gespraechsverlauf mitgeben, damit das Modell sieht, was falsch war.
            try:
                candidate = await client.ask(
                    messages
                    + [
                        {"role": "assistant", "content": candidate.model_dump_json()},
                        {"role": "user", "content": feedback},
                    ]
                )
            except Exception:  # noqa: BLE001 - LLM/Transport-Fehler beenden nur die Schleife
                _LOGGER.exception("Retry-Anfrage für Dashboard-Entwurf fehlgeschlagen")
                break

        # Kein fehlerfreier Versuch: die am wenigsten mangelhafte Variante nehmen ...
        if best is not None:
            problems, result, dropped = best
            return result, dropped, problems, None
        # ... oder aufgeben, wenn nicht einmal eine verwertbare Gliederung kam.
        return None, set(), [], last_error or "Dashboard konnte nicht erzeugt werden."

    async def _relevant_states(self, policy) -> list[dict]:
        """Aktueller Zustand aller per Policy freigegebenen Entities.

        Bewusst nur diese: das Modell soll ueber das urteilen koennen, was es auch
        steuern darf, und der Prompt bleibt klein.
        """

        entity_ids: set[str] = set()
        for cfg in policy.data.get("services", {}).values():
            entity_ids.update(cfg.get("allowed_entities", []))

        states = []
        for entity_id in entity_ids:
            state = self.hass.states.get(entity_id)
            if state is not None:
                states.append({"entity_id": state.entity_id, "state": state.state})
        return states

    def _automation_entities(self, policy) -> list[dict]:
        """Zustand aller Entities, die neue Automationen referenzieren duerfen.

        Eigene, meist groessere Menge als :meth:`_relevant_states`: Automationen
        duerfen z.B. auch Sensoren als Ausloeser nutzen, die kein Service-Ziel
        sind und daher sonst nirgends im Prompt auftauchen wuerden (siehe
        ``policy.automation_policy``).
        """

        entity_ids = policy.automation_policy().get("allowed_entities", [])
        entities = []
        for entity_id in entity_ids:
            state = self.hass.states.get(entity_id)
            entities.append(
                {"entity_id": entity_id, "state": state.state if state else "unbekannt"}
            )
        return entities

    def _respond(
        self, text: str, conversation_id: str, continue_conversation: bool, language: str
    ) -> conversation.ConversationResult:
        """Verpackt eine Textantwort als Conversation-Ergebnis.

        ``continue_conversation=True`` haelt das Gespraech offen (Sprachassistent
        hoert weiter zu) - genau dann sinnvoll, wenn eine Rueckfrage offen ist.
        """

        response = intent.IntentResponse(language=language)
        response.async_set_speech(text)
        return conversation.ConversationResult(
            response=response,
            conversation_id=conversation_id,
            continue_conversation=continue_conversation,
        )
