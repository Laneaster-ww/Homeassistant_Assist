"""Action Broker - zentrale Sicherheitsschicht zwischen KI und Home Assistant.

Jede Aktion, die das Sprachmodell vorschlaegt, laeuft zweimal hier durch:

1. beim Planen  (:func:`validate_plan` / :func:`check_plan_step`) - erlaubt die
   Policy den Schritt ueberhaupt, und braucht er eine Bestaetigung?
2. beim Ausfuehren (:func:`execute_step`) - erst danach ruft irgendetwas
   tatsaechlich einen Home-Assistant-Service auf.

Wird eine Aktion erst nach Rueckfrage ausgefuehrt, prueft der Aufrufer sie
unmittelbar davor erneut: die Policy-Datei kann sich in der Zwischenzeit
geaendert haben (siehe ``conversation._handle_pending``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from homeassistant.core import HomeAssistant

from .policy import Policy


class BrokerError(Exception):
    """Abgelehnte oder fehlgeschlagene Aktion, mit maschinenlesbarem ``code``."""

    def __init__(self, message: str, code: str = "broker_error") -> None:
        super().__init__(message)
        self.code = code


def canonicalize_object_id(object_id: str) -> str:
    """Normalisiert gaengige deutsche Umlaut-Transliterationen fuer den Vergleich.

    Kleine lokale Modelle schreiben Umlaute in erratenen Entity-Namen meist als
    Standard-ASCII-Transliteration aus ("ue"/"oe"/"ae", z.B. "kueche" fuer "Küche").
    In diesem System wurden die Umlaute beim Anlegen der Demo-Entities aber schlicht
    entfernt statt transliteriert (echt: "kuche", nicht "kueche") - ohne Kanonisierung
    faellt eine an sich eindeutige Entity durchs Raster der Objekt-Namen-Suche in
    :func:`automations._normalize_entity_id_refs` und :func:`dashboard._normalize_entity_refs`.
    Nur fuer den Vergleich gedacht, nicht fuer die tatsaechliche Entity-ID.
    """

    result = object_id
    for pair in ("ue", "oe", "ae"):
        result = result.replace(pair, pair[0])
    return result.replace("ß", "ss")


def _entity_id_index(allowed: list[str]) -> dict[str, list[str]]:
    """Ordnet Objekt-Namen und deren Raum-/Bereichs-Praefix (Teil vor dem ersten "_")
    den passenden Entity-IDs zu. Analog zu ``automations._object_id_index``, aber
    zusaetzlich mit Praefix-Index: kleine lokale Modelle treffen bei Ziel-Entities
    oft den Raumnamen richtig ("wohnzimmer"), erfinden aber den Rest nach dem Muster
    anderer, bereits bekannter Entities (z.B. "media_player.wohnzimmer_sonos" statt
    echt "media_player.wohnzimmer_wohnzimmer").
    """

    index: dict[str, list[str]] = {}
    for eid in allowed:
        _, _, object_id = eid.partition(".")
        for key in (object_id, canonicalize_object_id(object_id), object_id.split("_", 1)[0]):
            bucket = index.setdefault(key, [])
            if eid not in bucket:
                bucket.append(eid)
    return index


def _resolve_entity_id(value: str, allowed: list[str]) -> str:
    """Korrigiert eine einzelne erfundene Ziel-Entity, wenn eindeutig moeglich.

    Nur gegen die fuer DIESEN Service freigegebenen Entities geprueft (schmaler,
    sicherer Suchraum) - bleibt der Name mehrdeutig oder findet sich kein
    Kandidat, wird nichts veraendert und die anschliessende Policy-Pruefung lehnt
    wie gehabt ab.
    """

    if value in allowed:
        return value
    object_id = value.split(".", 1)[1] if "." in value else value
    index = _entity_id_index(allowed)
    for key in (object_id, canonicalize_object_id(object_id), object_id.split("_", 1)[0]):
        matches = index.get(key)
        if matches and len(matches) == 1:
            return matches[0]
    return value


def _resolve_entity_id_refs(entity_id, allowed: list[str]):
    """Wendet :func:`_resolve_entity_id` auf einen einzelnen Wert oder eine Liste an.

    Rueckgabe: ``(aufgeloester Wert, wurde etwas veraendert)``. Das zweite Element
    entscheidet in :func:`check_plan_step`, ob der Schritt zwingend bestaetigt werden
    muss - siehe die Begruendung dort.
    """

    if isinstance(entity_id, list):
        resolved = [_resolve_entity_id(e, allowed) if isinstance(e, str) else e for e in entity_id]
        return resolved, resolved != entity_id
    if isinstance(entity_id, str):
        resolved = _resolve_entity_id(entity_id, allowed)
        return resolved, resolved != entity_id
    return entity_id, False


def _normalize_entity_id(domain: str, entity_id):
    """Ergaenzt eine fehlende Domain in der Ziel-Entity.

    Das LLM laesst beim entity_id manchmal die Domain weg (z.B. 'licht_wohnzimmer'
    statt 'input_boolean.licht_wohnzimmer'). Bei call_service gehoert das Ziel immer
    zur Domain des aufgerufenen Service, daher ist das Ergaenzen hier eindeutig und
    sicher (die Policy-Pruefung greift unveraendert danach).

    Akzeptiert einen einzelnen Wert oder eine Liste und gibt dieselbe Form zurueck.
    """

    def fix(value: str) -> str:
        return value if "." in value else f"{domain}.{value}"

    if isinstance(entity_id, list):
        return [fix(e) for e in entity_id]
    if isinstance(entity_id, str):
        return fix(entity_id)
    return entity_id


# Home Assistant akzeptiert als Ziel eines Service-Aufrufs neben "entity_id" auch
# "device_id", "area_id", "label_id" und "floor_id". Diese treffen ganze Geraete, Raeume
# oder Etiketten auf einmal und lassen sich gegen eine Whitelist einzelner Entities gar
# nicht pruefen - ein Aufruf damit wuerde also ungeprueft weit mehr schalten als
# freigegeben ist. Sie werden deshalb komplett abgelehnt statt teilweise geprueft.
_UNCHECKABLE_TARGET_SELECTORS = ("device_id", "area_id", "label_id", "floor_id")


def reject_uncheckable_targets(container) -> None:
    """Lehnt Ziel-Selektoren ab, die sich nicht gegen die Entity-Whitelist pruefen lassen."""

    if not isinstance(container, dict):
        return
    for selector in _UNCHECKABLE_TARGET_SELECTORS:
        if container.get(selector):
            raise BrokerError(
                f"Ziel '{selector}' kann nicht gegen die Freigabeliste geprüft werden - "
                "bitte einzelne Geräte über 'entity_id' ansprechen.",
                "unsupported_target",
            )


_PLAY_MEDIA_FORBIDDEN_CONTENT_TYPES = {"url", "music"}


def _validate_play_media(service_data: dict) -> None:
    """Verbietet beliebige Internet-Stream-URLs bei ``media_player.play_media``.

    Nur kuratierte Inhalte (Favoriten, Playlists, TuneIn-Sender ueber deren
    Katalog-ID) sind erlaubt - "media_content_type: url"/"music" liesse das
    Modell auf Zuruf JEDE beliebige Stream-URL abspielen, was ausdruecklich
    nicht gewuenscht ist (siehe smart_homeassistant_policy.yaml, Kommentar bei
    media_player.play_media).
    """

    content_type = str(service_data.get("media_content_type", "")).strip().lower()
    if content_type in _PLAY_MEDIA_FORBIDDEN_CONTENT_TYPES:
        raise BrokerError(
            f"media_content_type '{content_type}' ist nicht erlaubt - nur kuratierte Inhalte "
            "(Favoriten, Playlists, Sender), keine beliebigen Stream-URLs.",
            "play_media_content_type_forbidden",
        )


def check_plan_step(policy: Policy, step: dict) -> bool:
    """Prueft einen einzelnen geplanten Schritt gegen die Policy.

    Wirft BrokerError, wenn die Aktion nicht erlaubt ist.
    Gibt zurueck, ob fuer diesen Schritt eine Bestaetigung noetig ist.

    Nebeneffekt: normalisiert die Ziel-Entity im ``step`` (siehe
    :func:`_normalize_entity_id`), damit Pruefung und spaetere Ausfuehrung
    garantiert dieselbe Entity meinen.
    """

    if step["action"] == "run_script":
        cfg = policy.script_allowed(step["target"])
        if cfg is None:
            raise BrokerError(f"Script '{step['target']}' ist nicht freigegeben.", "not_allowed")
        # Im Zweifel (kein Eintrag) lieber nachfragen als einfach ausfuehren.
        return bool(cfg.get("confirmation_required", True))

    if step["action"] == "call_service":
        if "." not in step["target"]:
            raise BrokerError(f"Ungültiges Ziel '{step['target']}'.", "invalid_target")
        domain, service = step["target"].split(".", 1)
        service_data = step.setdefault("service_data", {}) or {}
        reject_uncheckable_targets(service_data)
        reject_uncheckable_targets(service_data.get("target"))
        if step["target"] == "media_player.play_media":
            _validate_play_media(service_data)
        entity_guessed = False
        if "entity_id" in service_data:
            service_data["entity_id"] = _normalize_entity_id(domain, service_data["entity_id"])
            allowed_entities = (
                policy.data.get("services", {}).get(step["target"], {}).get("allowed_entities", [])
            )
            service_data["entity_id"], entity_guessed = _resolve_entity_id_refs(
                service_data["entity_id"], allowed_entities
            )
        entity_id = service_data.get("entity_id")
        cfg = policy.service_allowed(domain, service, entity_id)
        if cfg is None:
            if step["target"] not in policy.data.get("services", {}):
                # Zwei grundverschiedene Ablehnungsgruende bisher in derselben generischen
                # Meldung verschwunden: "Service existiert in der Policy gar nicht" (haeufig ein
                # vom Modell erfundener Service, siehe SYSTEM_PROMPT_TEMPLATE-Warnung zu
                # action_plan) vs. "Service existiert, aber diese Entity ist nicht freigegeben".
                # Nur der erste Fall bekommt hier eine Liste der tatsaechlich verfuegbaren
                # Services derselben Domain - das macht die Meldung fuer den Nutzer sofort
                # actionable, statt nur "nicht freigegeben" ohne Kontext zu zeigen.
                available = sorted(
                    key for key in policy.data.get("services", {}) if key.startswith(f"{domain}.")
                )
                hint = (
                    f" Verfügbar für '{domain}' sind: {', '.join(available)}."
                    if available
                    else f" Für '{domain}' ist kein Service freigegeben."
                )
                raise BrokerError(f"Service '{step['target']}' existiert nicht.{hint}", "unknown_service")
            raise BrokerError(
                f"Service '{step['target']}' für Ziel '{entity_id}' ist nicht freigegeben.",
                "not_allowed",
            )
        # Musste die Ziel-Entity erst zurechtgebogen werden, hat das Modell sie geraten -
        # dann ist NICHT sicher, dass das Ergebnis dem entspricht, was der Nutzer meinte.
        # Besonders die Praefix-Aufloesung in :func:`_resolve_entity_id` ("wohnzimmer" ->
        # die einzige Wohnzimmer-Entity dieses Service) trifft im Zweifel ein ganz anderes
        # Geraet als gemeint. Solche Schritte laufen deshalb nie ungefragt durch, auch wenn
        # die Policy fuer sie "confirmation_required: false" sagt; die Rueckfrage zeigt die
        # tatsaechlich aufgeloesten Ziele (siehe conversation._describe_steps).
        return bool(cfg.get("confirmation_required", True)) or entity_guessed

    raise BrokerError(f"Unbekannte Aktion '{step['action']}'.")


def validate_plan(policy: Policy, steps: list[dict]) -> bool:
    """Validiert alle Schritte; gibt True zurueck, wenn IRGENDEIN Schritt eine
    Bestaetigung durch den Nutzer benoetigt.

    Bewusst ohne vorzeitigen Abbruch: auch die spaeteren Schritte sollen geprueft
    werden, damit ein unerlaubter Schritt den ganzen Plan stoppt, statt erst
    mittendrin aufzufallen.
    """

    needs_confirmation = False
    for step in steps:
        if check_plan_step(policy, step):
            needs_confirmation = True
    return needs_confirmation


async def execute_step(hass: HomeAssistant, step: dict) -> dict:
    """Fuehrt einen bereits geprueften Schritt aus.

    Ruft ausschliesslich Home-Assistant-Services auf und wartet jeweils ihr Ende
    ab (``blocking=True``), damit aufeinander aufbauende Schritte eines Plans in
    der geplanten Reihenfolge wirken.
    """

    if step["action"] == "run_script":
        _, object_id = step["target"].split(".", 1)
        await hass.services.async_call("script", object_id, blocking=True)
        return {"target": step["target"], "ok": True}

    if step["action"] == "call_service":
        domain, service = step["target"].split(".", 1)
        # entity_id ist bereits normalisiert: check_plan_step hat sie zuvor im
        # selben step["service_data"]-Dict in-place ergaenzt (siehe dessen Docstring).
        data = dict(step.get("service_data") or {})
        await hass.services.async_call(domain, service, data, blocking=True)
        return {"target": step["target"], "ok": True}

    raise BrokerError("Unbekannte Aktion.")


def is_expired(created_at: datetime, ttl_minutes: int) -> bool:
    """Ist eine offene Rueckfrage zu alt, um noch mit ja/nein beantwortet zu werden?"""

    return datetime.now(timezone.utc) - created_at > timedelta(minutes=ttl_minutes)
