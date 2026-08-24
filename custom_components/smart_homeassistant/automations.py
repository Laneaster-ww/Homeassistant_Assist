"""Validierung und Aktivierung per KI erzeugter Automationen.

Neue Automationen entstehen in drei Schritten: das LLM liefert YAML,
:func:`validate_automation_yaml` prueft es syntaktisch und gegen die Policy, und
erst nach Nutzerbestaetigung schreibt :func:`activate_automation_draft` es in
``automations.yaml`` und laedt die Automationen neu.
"""

from __future__ import annotations

import logging
import os
import re
import uuid

import yaml

from homeassistant.components.automation.config import async_validate_config_item
from homeassistant.core import HomeAssistant
from homeassistant.util.yaml import loader as ha_yaml_loader

from .broker import BrokerError, canonicalize_object_id, reject_uncheckable_targets
from .files import read_text, write_text
from .locks import get_file_lock
from .policy import Policy

_LOGGER = logging.getLogger(__name__)


def _automations_path(hass: HomeAssistant) -> str:
    """Pfad der von der HA-UI verwalteten ``automations.yaml``."""

    return hass.config.path("automations.yaml")


def _read_yaml_file(path: str) -> list[dict]:
    """Liest die Automationsliste fuer den SCHREIBENDEN Pfad (blockierend, Executor).

    Bewusst streng und bewusst mit ``yaml.safe_load`` statt Home Assistants Loader:
    dieser Rueckgabewert wird in :func:`activate_automation_draft` ergaenzt und
    komplett zurueckgeschrieben. Wuerde hier ein Fehler verschluckt und ``[]``
    geliefert, ueberschriebe der naechste Schreibvorgang saemtliche bestehenden
    Automationen. Und wuerde HA's Loader Tags wie ``!include`` aufloesen, landete beim
    Zurueckschreiben deren aufgeloester Inhalt in der Datei statt des Tags. Ein
    Parse-Fehler soll hier also laut scheitern; die Anzeige-Variante darunter ist die
    tolerante.
    """

    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or []


def _read_automations_tolerant(path: str) -> list[dict]:
    """Liest die Automationsliste zur ANZEIGE (blockierend, Executor) - fehlertolerant.

    Diese Liste geht ausschliesslich in den System-Prompt und in die Uebersicht
    "welche Automationen gibt es". Sie wird bei JEDER Chat-Nachricht gebraucht, und
    zwar ausserhalb der Fehlerbehandlung um den LLM-Aufruf - eine kaputte oder
    ungewoehnlich strukturierte ``automations.yaml`` legte damit bisher den kompletten
    Chat lahm (ein Mapping statt einer Liste liess ausserdem
    ``ollama_client.build_system_prompt`` mit ``AttributeError`` auf ``str.get``
    laufen). Hier wird deshalb Home Assistants eigener Loader benutzt (kennt
    ``!include``/``!secret``), das Ergebnis auf eine Liste von Dicts normalisiert und
    im Fehlerfall eine leere Liste geliefert.
    """

    if not os.path.exists(path):
        return []
    try:
        data = ha_yaml_loader.load_yaml(path)
    except Exception:  # noqa: BLE001 - die Anzeige darf am Dateizustand nicht scheitern
        _LOGGER.exception("automations.yaml konnte nicht gelesen werden")
        return []

    if isinstance(data, dict):  # einzelne Automation als Mapping statt als Liste
        data = [data]
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _write_yaml_file(path: str, automations: list[dict]) -> None:
    """Schreibt die Automationsliste zurueck (blockierend, gehoert in den Executor).

    ``sort_keys=False`` erhaelt die gewohnte Reihenfolge (alias, trigger, action ...),
    ``allow_unicode=True`` verhindert entstellte Umlaute in den Titeln. Geschrieben wird
    ueber :func:`files.write_text`, also atomar - ein Absturz mitten im Schreiben laesst
    sonst eine halbe ``automations.yaml`` zurueck, gegen die auch das Rollback in
    :func:`activate_automation_draft` nicht hilft (das greift nur bei einem
    fehlgeschlagenen Reload).
    """

    write_text(path, yaml.safe_dump(automations, allow_unicode=True, sort_keys=False))


def _strip_yaml_fence(yaml_text: str) -> str:
    """Entfernt Markdown-Codeblock-Zaeune, die kleine LLMs gerne um YAML legen."""

    text = yaml_text.strip()
    match = re.fullmatch(r"```(?:yaml|yml)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return text


async def read_automations(hass: HomeAssistant) -> list[dict]:
    """Alle bestehenden Automationen (fuer den System-Prompt und die Suche)."""

    return await hass.async_add_executor_job(
        _read_automations_tolerant, _automations_path(hass)
    )


def _collect_entity_ids(node) -> set[str]:
    """Sammelt rekursiv alle ``entity_id``-Werte aus einer YAML-Struktur.

    Die Verschachtelung ist beliebig tief (trigger, condition, action, choose ...),
    daher wird der komplette Baum durchlaufen statt nur die bekannten Ebenen.
    """

    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "entity_id":
                if isinstance(value, list):
                    found.update(value)
                elif isinstance(value, str):
                    found.add(value)
            else:
                found |= _collect_entity_ids(value)
    elif isinstance(node, list):
        for item in node:
            found |= _collect_entity_ids(item)
    return found


def _iter_action_steps(node):
    """Liefert alle Aktions-Dicts aus action/action-Liste/choose-Strukturen."""

    if isinstance(node, dict):
        yield node
        for key in ("choose", "sequence", "default", "then", "else"):
            value = node.get(key)
            if isinstance(value, list):
                for item in value:
                    yield from _iter_action_steps(item)
            elif isinstance(value, dict):
                yield from _iter_action_steps(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_action_steps(item)


def _normalize_automation(parsed) -> dict:
    """Bringt gaengige LLM-YAML-Varianten auf ein einheitliches Automation-Dict."""

    if isinstance(parsed, list):
        if len(parsed) != 1 or not isinstance(parsed[0], dict):
            raise BrokerError(
                "Bitte erzeuge genau eine Automation pro Anfrage.", "invalid_yaml"
            )
        parsed = parsed[0]

    if not isinstance(parsed, dict):
        raise BrokerError("Automation muss ein YAML-Objekt sein.", "invalid_yaml")

    # HA-Beispiele und UI-Exporte nutzen mal die Einzahl-, mal die Mehrzahlform.
    aliases = {
        "triggers": "trigger",
        "conditions": "condition",
        "actions": "action",
    }
    for source, target in aliases.items():
        if source in parsed and target not in parsed:
            parsed[target] = parsed.pop(source)

    return parsed


def _object_id_index(allowed: set[str]) -> dict[str, list[str]]:
    """Ordnet jedem Objekt-Namen (Teil nach dem Punkt) die passenden Entity-IDs zu.

    Grundlage fuer :func:`_normalize_entity_id_refs`: nur bei genau einem Treffer
    ist eine Korrektur eindeutig. Analog zu ``dashboard._object_id_index``, aber
    bewusst gegen die enge Automation-Whitelist statt gegen alle HA-Entities -
    eine automatisch generierte Automation soll niemals auf eine Entity
    korrigiert werden, die nicht ohnehin freigegeben ist.
    """

    index: dict[str, list[str]] = {}
    for eid in allowed:
        _, _, object_id = eid.partition(".")
        index.setdefault(object_id, []).append(eid)
        canonical = canonicalize_object_id(object_id)
        if canonical != object_id:
            index.setdefault(canonical, []).append(eid)
    return index


def _normalize_entity_id_refs(node, allowed: set[str], index: dict[str, list[str]]) -> None:
    """Korrigiert 'entity_id'-Referenzen in-place, wenn eindeutig moeglich.

    Kleine lokale Modelle treffen die exakte entity_id nicht immer (fehlende
    Domain, falsch geratene Domain, Standard-Umlaut-Transliteration wie "kueche"
    statt echt "kuche") - analog zu ``broker._normalize_entity_id`` und
    ``dashboard._normalize_entity_refs``. Bleibt der Objekt-Name mehrdeutig oder
    unbekannt, wird nichts veraendert - danach greift unveraendert die
    Policy-Pruefung in :func:`validate_automation_yaml`.
    """

    def fix(value: str) -> str:
        if value in allowed:
            return value
        object_id = value.split(".", 1)[1] if "." in value else value
        matches = index.get(object_id) or index.get(canonicalize_object_id(object_id))
        return matches[0] if matches and len(matches) == 1 else value

    if isinstance(node, dict):
        for key, value in node.items():
            if key == "entity_id":
                if isinstance(value, list):
                    node[key] = [fix(v) if isinstance(v, str) else v for v in value]
                elif isinstance(value, str):
                    node[key] = fix(value)
            else:
                _normalize_entity_id_refs(value, allowed, index)
    elif isinstance(node, list):
        for item in node:
            _normalize_entity_id_refs(item, allowed, index)


def _expand_collective_entity_id(value, domain: str, service_allowed_entities: list[str]):
    """Loest einen erfundenen Sammel-Platzhalter ("all_light", "light.all", "alle") auf.

    Trotz ausdruecklichem Verbot im Prompt greifen kleine lokale Modelle bei "alle X"/"die X"
    (ohne konkrete Aufzaehlung) gerne zu einer in Home Assistant nicht existierenden
    Sammel-Entity, statt jede einzelne aufzulisten. Da die Absicht ("alle Entities dieser
    Domain fuer diesen Service") eindeutig ist, wird hier automatisch durch die vollstaendige,
    fuer den Service freigegebene Liste ersetzt, statt die ganze Automation abzulehnen.
    """

    if not isinstance(value, str):
        return value
    object_id = (value.split(".", 1)[1] if "." in value else value).lower()
    if object_id in {"all", "alle", f"all_{domain}", f"all_{domain}s", f"alle_{domain}"}:
        return list(service_allowed_entities) or value
    return value


def _expand_collective_action_entities(policy: Policy, parsed: dict) -> None:
    """Loest erfundene Sammel-Platzhalter in allen Action-Schritten in-place auf.

    Muss VOR der Entity-Whitelist-Pruefung in :func:`validate_automation_yaml` laufen
    (``_collect_entity_ids``/``not_allowed``) - sonst wuerde "all_light" & Co. dort
    schon als nicht freigegebene Entity abgelehnt, bevor :func:`_expand_collective_entity_id`
    ueberhaupt zum Zug kommt.
    """

    for step in _iter_action_steps(parsed.get("action")):
        service = step.get("service") or step.get("action")
        if not isinstance(service, str) or "." not in service:
            continue

        domain = service.split(".", 1)[0]
        service_allowed_entities = (
            policy.data.get("services", {}).get(service, {}).get("allowed_entities", [])
        )
        target = step.get("target") or {}
        data = step.get("data") or step.get("service_data") or {}
        container = target if isinstance(target, dict) and "entity_id" in target else data
        if isinstance(container, dict) and "entity_id" in container:
            container["entity_id"] = _expand_collective_entity_id(
                container["entity_id"], domain, service_allowed_entities
            )


def _validate_action_services(policy: Policy, parsed: dict) -> None:
    """Lehnt Automation-Aktionen ab, deren Services ausserhalb der Action-Policy liegen."""

    for step in _iter_action_steps(parsed.get("action")):
        service = step.get("service") or step.get("action")
        if not isinstance(service, str) or "." not in service:
            continue

        domain, service_name = service.split(".", 1)
        target = step.get("target") or {}
        data = step.get("data") or step.get("service_data") or {}
        # Vor der Entity-Pruefung: device/area/label/floor als Ziel wuerde die
        # Whitelist unterlaufen, weil _collect_entity_ids nur "entity_id" sieht -
        # eine Aktion mit solchem Ziel traefe also ungeprueft ganze Raeume/Geraete.
        reject_uncheckable_targets(target)
        reject_uncheckable_targets(data)
        entity_id = data.get("entity_id")
        if isinstance(target, dict) and "entity_id" in target:
            entity_id = target["entity_id"]

        if policy.service_allowed(domain, service_name, entity_id) is None:
            raise BrokerError(
                f"Service-Aufruf nicht freigegeben: {service} für {entity_id or '(kein entity_id)'}",
                "service_not_allowed",
            )


# Die drei Beispiele aus dem System-Prompt (siehe ollama_client.SYSTEM_PROMPT_TEMPLATE)
# dienen nur der Formatveranschaulichung. Trotz ausdruecklichem Verbot im Prompt geben
# kleine lokale Modelle bei Unsicherheit (z.B. bei einer Anfrage, die eigentlich nur nach
# bestehenden Automationen fragt, oder bei einer komplexeren Anfrage, die dem Modell nicht
# gelingt) manchmal exakt eines der Beispiele zurueck statt einer echten Antwort -
# beobachtet ueber mehrere unabhaengige Vorfaelle im Aktionsverlauf. Da "trigger" und
# "action" bei einer echten Anfrage praktisch nie zufaellig exakt einem der Beispiele
# entsprechen, wird ein exakter Treffer hier als (fast sicher falsche) Kopie abgelehnt,
# statt sie unbemerkt aktivieren zu lassen.
_PROMPT_EXAMPLE_SIGNATURES = (
    (
        [{"platform": "time", "at": "18:00:00"}],
        [
            {
                "service": "light.turn_on",
                "target": {"entity_id": "light.wohnzimmer_aqara_lampe_wohnzimmer"},
            }
        ],
    ),
    (
        [
            {
                "platform": "numeric_state",
                "entity_id": "sensor.garten_solaredge_wechselrichter_leistung",
                "above": 3000,
            }
        ],
        [
            {
                "service": "light.turn_on",
                "target": {"entity_id": "light.wohnzimmer_aqara_lampe_wohnzimmer"},
            }
        ],
    ),
    (
        [{"platform": "time", "at": "20:00:00"}],
        [
            {
                "service": "cover.close_cover",
                "target": {
                    "entity_id": [
                        "cover.wohnzimmer_aqara_jalousie_wohnzimmer",
                        "cover.schlafzimmer_aqara_jalousie_schlafzimmer",
                        "cover.kuche_aqara_jalousie_kuche",
                    ]
                },
            }
        ],
    ),
)


def _is_prompt_example(parsed: dict) -> bool:
    """Prueft, ob 'trigger'+'action' exakt einem der Beispiele aus dem System-Prompt entsprechen."""

    signature = (parsed.get("trigger"), parsed.get("action"))
    return signature in _PROMPT_EXAMPLE_SIGNATURES


def validate_automation_yaml(policy: Policy, yaml_text: str) -> dict:
    """Syntaktische und Berechtigungs-Pruefung eines Automation-Entwurfs.

    Entspricht den im Konzept geforderten Schritten "syntaktisch validiert"
    und "auf erlaubte Geraete geprueft", bevor irgendetwas aktiviert wird.

    Gibt die geparste Automation zurueck; wirft sonst ``BrokerError``.
    """

    try:
        parsed = yaml.safe_load(_strip_yaml_fence(yaml_text))
    except yaml.YAMLError as exc:
        raise BrokerError(f"Ungültiges YAML: {exc}", "invalid_yaml") from exc

    parsed = _normalize_automation(parsed)

    # Kleine lokale Modelle treffen Entity-IDs nicht immer exakt (fehlende/falsche
    # Domain, Tippfehler) - eindeutig korrigierbare Faelle werden hier stillschweigend
    # gefixt, bevor die strikte Policy-Pruefung unten greift. Nur gegen die enge
    # Automation-Whitelist, nie gegen alle HA-Entities (siehe _object_id_index).
    allowed = set(policy.automation_policy().get("allowed_entities", []))
    _normalize_entity_id_refs(parsed, allowed, _object_id_index(allowed))

    # Ebenfalls vor der Whitelist-Pruefung: erfundene Sammel-Platzhalter wie "all_light"
    # durch die vollstaendige, tatsaechlich freigegebene Entity-Liste ersetzen.
    _expand_collective_action_entities(policy, parsed)

    for required in ("trigger", "action"):
        if required not in parsed:
            raise BrokerError(f"Automation muss '{required}' enthalten.", "invalid_yaml")
        if parsed[required] in (None, "", []):
            raise BrokerError(f"Automation darf '{required}' nicht leer lassen.", "invalid_yaml")

    if _is_prompt_example(parsed):
        raise BrokerError(
            "Das ist unverändert das Beispiel aus der Anleitung, keine echte Antwort auf "
            "die Anfrage.",
            "example_copied",
        )

    # Anders als bei Dashboards wird hier nichts stillschweigend herausgefiltert:
    # eine Automation mit fehlenden Schritten wuerde real etwas anderes tun als
    # angekuendigt, also lieber komplett ablehnen.
    used = _collect_entity_ids(parsed)
    not_allowed = used - allowed
    if not_allowed:
        raise BrokerError(
            f"Automation verwendet nicht freigegebene Entities: {', '.join(sorted(not_allowed))}",
            "entity_not_allowed",
        )

    _validate_action_services(policy, parsed)

    return parsed


# Domain-neutrale Uebersetzung von Service-Namen und Domains fuer :func:`_fallback_alias`.
# Bewusst klein gehalten (nur die Domains, die die Policy ueberhaupt fuer Automationen
# freigibt) - fehlt ein Eintrag, greift der lesbare, aber technischere Rohname als Fallback.
_SERVICE_VERB_PHRASES = {
    "turn_on": "einschalten",
    "turn_off": "ausschalten",
    "toggle": "umschalten",
    "open_cover": "öffnen",
    "close_cover": "schließen",
    "stop_cover": "stoppen",
    "set_cover_position": "positionieren",
    "lock": "verriegeln",
    "unlock": "entriegeln",
}
_DOMAIN_NOUNS = {
    "light": "Licht",
    "cover": "Rollos",
    "lock": "Schloss",
    "switch": "Schalter",
    "climate": "Klima",
    "media_player": "Medien",
    "script": "Script",
    "scene": "Szene",
}


def _fallback_alias(parsed: dict) -> str | None:
    """Baut aus Trigger+Action einen lesbaren Titel, wenn weder 'alias' noch 'description'
    vorhanden sind.

    Bei Lichtern liefert das Modell fast immer wenigstens eine "description" (siehe
    :func:`async_validate_automation_yaml`), bei anderen Domains (beobachtet: Jalousien)
    aber haeufig weder das eine noch das andere. Domain-neutral aus der tatsaechlichen
    Struktur abgeleitet statt auf das Modell zu vertrauen, funktioniert dieser Fallback
    fuer jede Geraeteart gleichermassen.
    """

    action_steps = parsed.get("action")
    if not isinstance(action_steps, list) or not action_steps or not isinstance(action_steps[0], dict):
        return None
    service = action_steps[0].get("service") or action_steps[0].get("action")
    if not isinstance(service, str) or "." not in service:
        return None

    domain, service_name = service.split(".", 1)
    verb = _SERVICE_VERB_PHRASES.get(service_name, service_name.replace("_", " "))
    noun = _DOMAIN_NOUNS.get(domain, domain.capitalize())

    zeit = ""
    trigger_steps = parsed.get("trigger")
    if isinstance(trigger_steps, list) and trigger_steps and isinstance(trigger_steps[0], dict):
        if trigger_steps[0].get("platform") == "time":
            at = str(trigger_steps[0].get("at", ""))[:5]
            if at:
                zeit = f" um {at} Uhr"

    return f"{noun} {verb}{zeit}".strip()


async def async_validate_automation_yaml(
    hass: HomeAssistant, policy: Policy, yaml_text: str
) -> dict:
    """Prueft einen Entwurf gegen die Policy und Home Assistants Automation-Schema."""

    parsed = validate_automation_yaml(policy, yaml_text)
    parsed.setdefault("id", str(uuid.uuid4()))
    # Drei Stufen: das Modell liefert "alias" -> sonst "description" -> sonst ein aus
    # Trigger/Action abgeleiteter Titel (siehe _fallback_alias) -> sonst der generische
    # Platzhalter als letzte Instanz.
    if not parsed.get("alias"):
        parsed["alias"] = (
            parsed.get("description") or _fallback_alias(parsed) or "KI-generierte Automation"
        )

    try:
        await async_validate_config_item(hass, parsed["id"], parsed)
    except Exception as exc:
        raise BrokerError(
            f"Home Assistant lehnt die Automation ab: {exc}",
            "invalid_homeassistant_automation",
        ) from exc

    return parsed


async def activate_automation_draft(hass: HomeAssistant, policy: Policy, payload: dict) -> dict:
    """Wird erst nach expliziter Nutzerbestaetigung aufgerufen.

    Prueft den Entwurf erneut (die Policy kann sich seit der Rueckfrage geaendert
    haben), haengt ihn an ``automations.yaml`` an und laedt die Automationen neu,
    damit sie ohne HA-Neustart aktiv sind.
    """

    parsed = await async_validate_automation_yaml(hass, policy, payload["yaml"])
    # payload["alias"] (result.automation_alias) ist, falls vorhanden, der vom Modell explizit
    # gewaehlte Kurztitel und geht daher vor; parsed["alias"] hat als Fallback bereits die
    # description (siehe async_validate_automation_yaml), "KI-generierte Automation" greift
    # also nur noch, wenn wirklich weder Alias noch Beschreibung vorhanden waren.
    parsed["alias"] = payload.get("alias") or parsed.get("alias") or "KI-generierte Automation"

    path = _automations_path(hass)
    async with get_file_lock(hass):
        original_text = await hass.async_add_executor_job(read_text, path)
        automations = await hass.async_add_executor_job(_read_yaml_file, path)
        automations.append(parsed)
        await hass.async_add_executor_job(_write_yaml_file, path, automations)

        try:
            await hass.services.async_call("automation", "reload", blocking=True)
        except Exception as exc:
            await hass.async_add_executor_job(write_text, path, original_text)
            try:
                await hass.services.async_call("automation", "reload", blocking=True)
            except Exception as rollback_exc:
                raise BrokerError(
                    "Aktivieren der Automation ist fehlgeschlagen; die Datei wurde "
                    f"zurückgerollt, aber automation.reload schlug danach ebenfalls fehl: {rollback_exc}",
                    "automation_reload_failed",
                ) from exc
            raise BrokerError(
                f"Aktivieren der Automation ist fehlgeschlagen; automations.yaml wurde zurückgerollt: {exc}",
                "automation_reload_failed",
            ) from exc
    return {"automation_id": parsed["id"], "alias": parsed["alias"]}
