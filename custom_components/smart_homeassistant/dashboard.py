"""Dashboard-Generierung aus bestehenden Entities per Chat.

Analog zu automations.py: das LLM schlaegt eine neue Lovelace-View vor, die
ausschliesslich tatsaechlich existierende Entities referenziert. Nach
Nutzerbestaetigung wird daraus ein eigenstaendiges YAML-Dashboard (eigener
Eintrag in der Seitenleiste, genau wie "Smart Homeassistant" selbst). Dafuer
muss zusaetzlich zur neuen Dashboard-Datei ein Eintrag unter
"lovelace: dashboards:" in configuration.yaml ergaenzt werden - im Gegensatz
zum Hinzufuegen einer View zu einem bereits registrierten YAML-Dashboard
(das HA automatisch per Datei-mtime neu laedt) werden neue Dashboard-
*Registrierungen* nur beim Start verarbeitet, ein Neustart ist also
unvermeidbar, damit der neue Sidebar-Eintrag erscheint.

Aufbau des Moduls:

* Datei-Helfer und das Ein-/Austragen in configuration.yaml (``_insert_``/
  ``_remove_dashboard_config``).
* Auswahl und Pruefung der Entities (:func:`list_known_entities`,
  :func:`validate_dashboard_view_yaml`).
* Die beiden Aktionen, die erst nach Nutzerbestaetigung laufen:
  :func:`activate_dashboard_draft` und :func:`delete_dashboard`. Beide teilen
  sich ihre Schreib-/Verifikations-/Rollback-Logik in
  :func:`_write_config_and_verify`.

Beide schreibenden Aktionen sichern sich gleich ab: nach dem Schreiben wird
configuration.yaml mit Home Assistants eigenem Loader erneut eingelesen und das
Ergebnis geprueft; passt es nicht, wird der Originalstand zurueckgeschrieben.
Eine kaputte configuration.yaml wuerde sonst den naechsten Start verhindern.
"""

from __future__ import annotations

import os
from typing import Callable

import yaml

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import slugify
from homeassistant.util.yaml import loader as ha_yaml_loader

from .broker import BrokerError, canonicalize_object_id
from .const import DOMAIN
from .dashboard_design import render_designed_view
from .files import read_text, write_text
from .locks import get_file_lock
from .text_format import name_map, normalize_umlauts

CONFIGURATION_YAML_FILENAME = "configuration.yaml"
DASHBOARD_DIRECTORY = "smart_homeassistant_dashboards"

# Praefix, an dem per Chat erstellte Dashboards erkennbar sind (siehe _unique_url_path).
# Loeschen ist bewusst NUR fuer so markierte Dashboards erlaubt - alles andere (allen
# voran "smart-homeassistant" selbst, der Chat-Einstiegspunkt) darf die KI niemals
# entfernen koennen, egal was das Sprachmodell vorschlaegt.
MANAGED_URL_PATH_PREFIX = "ki-"

# Domains, die fuer generierte Dashboards keinen Mehrwert bieten bzw. keine
# sinnvollen Karten ergeben.
_EXCLUDED_DOMAINS = {"automation", "conversation", "tts", "event", "todo"}

# Plattformen (Integrationen), deren Entities fuer ein Smart-Home-Dashboard
# irrelevant sind. Ohne diesen Filter dominieren z.B. die ~150 WebUntis-
# Kalender/Sensoren (ein Stundenplan-Kalender pro Schulklasse) die Liste und
# begraben die paar tatsaechlich relevanten Entities darin - das LLM waehlt
# dann unvollstaendig/willkuerlich aus einer viel zu grossen, groesstenteils
# irrelevanten Liste statt zuverlaessig alle passenden Entities zu finden.
_EXCLUDED_PLATFORMS = {"webuntis", "sun", "backup", DOMAIN}


def _configuration_yaml_path(hass: HomeAssistant) -> str:
    """Pfad der zentralen configuration.yaml."""

    return hass.config.path(CONFIGURATION_YAML_FILENAME)


# Die folgenden Datei-Helfer sind blockierend und gehoeren in den Executor
# (hass.async_add_executor_job), nicht in den Event Loop.


def _write_yaml_dict(path: str, data: dict) -> None:
    """Schreibt die Dashboard-Datei (atomar, siehe files.write_text)."""

    write_text(path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))


def _existing_dashboard_url_paths(hass: HomeAssistant) -> set[str]:
    """Alle bereits registrierten Dashboard-Pfade - Grundlage fuer eindeutige Namen."""

    config = ha_yaml_loader.load_yaml(_configuration_yaml_path(hass))
    dashboards = ((config or {}).get("lovelace") or {}).get("dashboards") or {}
    return set(dashboards)


def _unique_url_path(title: str, existing: set[str]) -> str:
    """Baut aus dem Titel einen freien url_path mit dem Praefix "ki-".

    Home Assistant verlangt mindestens einen Bindestrich im url_path; das Praefix
    erfuellt das nebenbei und markiert das Dashboard zugleich als per Chat erstellt.
    Ist der Pfad schon vergeben, wird durchnummeriert.
    """

    base = slugify(f"ki {title}", separator="-") or "ki-dashboard"
    if "-" not in base:
        base = f"ki-{base}"
    candidate = base
    counter = 2
    while candidate in existing:
        candidate = f"{base}-{counter}"
        counter += 1
    return candidate


def _find_dashboards_block(lines: list[str]) -> tuple[int, int]:
    """Findet den Abschnitt "lovelace: dashboards:" zeilenweise.

    Gemeinsame Grundlage fuer :func:`_insert_dashboard_config` und
    :func:`_remove_dashboard_config`: beide arbeiten bewusst zeilenweise statt
    per vollstaendiger YAML-Reserialisierung, weil das die von Home Assistant
    genutzten "!include"-Tags zerstoeren wuerde.

    Gibt (Zeilenindex von "dashboards:", dessen Einrueckung) zurueck.
    """

    lovelace_idx = next(
        (i for i, line in enumerate(lines) if line.rstrip("\n") == "lovelace:"), None
    )
    if lovelace_idx is None:
        raise BrokerError(
            "Abschnitt 'lovelace:' nicht in configuration.yaml gefunden.", "config_not_found"
        )

    # "dashboards:" nur bis zum naechsten Abschnitt auf oberster Ebene (Einrueckung
    # 0) suchen, sonst waere es ein fremdes "dashboards:".
    for i in range(lovelace_idx + 1, len(lines)):
        line = lines[i]
        if line.strip() == "":
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent == 0:
            break
        if line.strip() == "dashboards:":
            return i, indent

    raise BrokerError(
        "Abschnitt 'lovelace: dashboards:' nicht in configuration.yaml gefunden.",
        "config_not_found",
    )


def _insert_dashboard_config(text: str, url_path: str, entry: dict) -> str:
    """Fuegt einen neuen Eintrag unter 'lovelace: dashboards:' ein."""

    lines = text.splitlines(keepends=True)
    dash_idx, dash_indent = _find_dashboards_block(lines)

    # Einfuegestelle ist das Ende des dashboards-Blocks: die erste Zeile, die
    # nicht mehr tiefer eingerueckt ist (oder das Dateiende).
    insert_at = len(lines)
    for i in range(dash_idx + 1, len(lines)):
        line = lines[i]
        if line.strip() == "":
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= dash_indent:
            insert_at = i
            break

    # Den neuen Eintrag serialisieren und auf die Einrueckung der Geschwister bringen.
    child_indent = dash_indent + 2
    snippet = yaml.safe_dump({url_path: entry}, allow_unicode=True, sort_keys=False)
    prefix = " " * child_indent
    block_lines = [
        f"{prefix}{line}" if line.strip() else line for line in snippet.splitlines(keepends=True)
    ]

    return "".join(lines[:insert_at] + block_lines + lines[insert_at:])


async def list_deletable_dashboards(hass: HomeAssistant) -> list[dict]:
    """Per Chat erstellte Dashboards, die auch per Chat wieder geloescht werden duerfen.

    Diese Liste geht sowohl in den System-Prompt als auch in die Pruefung vor dem
    Loeschen ein - das Modell kann also gar nichts anderes vorschlagen, und selbst
    wenn, wuerde es hier scheitern.
    """

    def _load() -> list[dict]:
        config = ha_yaml_loader.load_yaml(_configuration_yaml_path(hass))
        dashboards = ((config or {}).get("lovelace") or {}).get("dashboards") or {}
        return [
            {
                "url_path": url_path,
                "title": conf.get("title", url_path),
                "filename": conf.get("filename"),
            }
            for url_path, conf in dashboards.items()
            if url_path.startswith(MANAGED_URL_PATH_PREFIX)
        ]

    return await hass.async_add_executor_job(_load)


def _remove_dashboard_config(text: str, url_path: str) -> str:
    """Entfernt den Eintrag 'url_path' unter 'lovelace: dashboards:' wieder.

    Kehrseite von :func:`_insert_dashboard_config`.
    """

    lines = text.splitlines(keepends=True)
    dash_idx, dash_indent = _find_dashboards_block(lines)

    # Die Zeile "<url_path>:" auf Kind-Ebene des dashboards-Blocks suchen.
    child_indent = dash_indent + 2
    entry_idx = None
    for i in range(dash_idx + 1, len(lines)):
        line = lines[i]
        if line.strip() == "":
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= dash_indent:
            break
        if indent == child_indent and line.strip() == f"{url_path}:":
            entry_idx = i
            break

    if entry_idx is None:
        raise BrokerError(
            f"Dashboard-Eintrag '{url_path}' nicht in configuration.yaml gefunden.", "not_found"
        )

    # Der Eintrag reicht bis zur naechsten Zeile auf gleicher oder hoeherer Ebene.
    entry_end = len(lines)
    for i in range(entry_idx + 1, len(lines)):
        line = lines[i]
        if line.strip() == "":
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= child_indent:
            entry_end = i
            break

    return "".join(lines[:entry_idx] + lines[entry_end:])


async def _write_config_and_verify(
    hass: HomeAssistant,
    config_path: str,
    original_text: str,
    new_text: str,
    verify: Callable[[dict], None],
    error_message: str,
    on_rollback: Callable[[], None] | None = None,
) -> None:
    """Schreibt configuration.yaml und prueft das Ergebnis sofort nach.

    Gemeinsame Grundlage fuer :func:`activate_dashboard_draft` und
    :func:`delete_dashboard`: beide schreiben, lesen mit Home Assistants
    eigenem YAML-Loader neu ein und rollen bei einer fehlgeschlagenen Pruefung
    zurueck - eine kaputte configuration.yaml wuerde sonst den naechsten Start
    verhindern.

    ``verify`` bekommt die neu eingelesene Konfiguration und wirft (mit
    aussagekraeftiger Meldung), wenn die Aenderung nicht wie erwartet
    angekommen ist. ``on_rollback`` raeumt zusaetzliche Nebeneffekte auf (z.B.
    eine halb angelegte Dashboard-Datei), bevor der Fehler als
    ``BrokerError`` mit ``error_message`` weitergereicht wird.
    """

    def _apply_and_verify() -> None:
        write_text(config_path, new_text)
        try:
            reloaded = ha_yaml_loader.load_yaml(config_path) or {}
            verify(reloaded)
        except Exception:
            # Rollback: configuration.yaml unveraendert lassen.
            write_text(config_path, original_text)
            if on_rollback is not None:
                on_rollback()
            raise

    try:
        await hass.async_add_executor_job(_apply_and_verify)
    except Exception as exc:
        raise BrokerError(f"{error_message}: {exc}", "config_write_failed") from exc


async def delete_dashboard(hass: HomeAssistant, url_path: str) -> dict:
    """Wird erst nach expliziter Nutzerbestaetigung aufgerufen.

    Entfernt den Eintrag aus configuration.yaml (mit derselben Rollback-Absicherung
    wie beim Anlegen) und loescht danach die zugehoerige Dashboard-Datei.

    Die Reihenfolge ist Absicht: die Datei verschwindet erst, wenn die Aenderung
    an configuration.yaml nachweislich gelungen ist.
    """

    # Doppelte Absicherung gegen das Loeschen fremder Dashboards - unabhaengig
    # davon, was Modell und Aufrufer vorher geprueft haben.
    if not url_path.startswith(MANAGED_URL_PATH_PREFIX):
        raise BrokerError(
            f"Dashboard '{url_path}' wurde nicht per Chat erstellt und darf nicht per Chat gelöscht werden.",
            "not_allowed",
        )

    async with get_file_lock(hass):
        deletable = {d["url_path"]: d for d in await list_deletable_dashboards(hass)}
        entry = deletable.get(url_path)
        if entry is None:
            raise BrokerError(f"Dashboard '{url_path}' nicht gefunden.", "not_found")

        config_path = _configuration_yaml_path(hass)
        original_text = await hass.async_add_executor_job(read_text, config_path)
        new_text = _remove_dashboard_config(original_text, url_path)

        def _verify(reloaded: dict) -> None:
            dashboards = ((reloaded.get("lovelace") or {}).get("dashboards")) or {}
            if url_path in dashboards:
                raise ValueError(
                    "Dashboard-Eintrag nach dem Entfernen weiterhin in configuration.yaml vorhanden."
                )

        await _write_config_and_verify(
            hass,
            config_path,
            original_text,
            new_text,
            _verify,
            "Löschen des Dashboards fehlgeschlagen, configuration.yaml wurde nicht verändert",
        )

        if entry.get("filename"):
            dashboard_path = hass.config.path(entry["filename"])

            def _remove_file() -> None:
                if os.path.exists(dashboard_path):
                    os.remove(dashboard_path)

            await hass.async_add_executor_job(_remove_file)

    return {"title": entry["title"], "url_path": url_path, "restart_required": True}


async def list_known_entities(hass: HomeAssistant) -> list[dict]:
    """Alle Entities, aus denen das LLM fuer Dashboards waehlen darf.

    Die Liste wird bewusst kurz gehalten (siehe die Filter oben): je weniger
    irrelevante Eintraege darin stehen, desto zuverlaessiger findet das Modell
    die passenden.
    """

    registry = er.async_get(hass)
    entities = []
    for state in hass.states.async_all():
        if state.domain in _EXCLUDED_DOMAINS:
            continue
        entry = registry.async_get(state.entity_id)
        # Diagnose-/Konfigurations-Entities und vom Nutzer ausgeblendete gehoeren
        # nicht auf ein Uebersichts-Dashboard.
        if entry is not None and (entry.entity_category is not None or entry.hidden_by is not None):
            continue
        if entry is not None and entry.platform in _EXCLUDED_PLATFORMS:
            continue
        entities.append(
            {
                "entity_id": state.entity_id,
                "name": state.attributes.get("friendly_name", state.entity_id),
                "domain": state.domain,
            }
        )
    entities.sort(key=lambda e: e["entity_id"])
    return entities


def _object_id_index(existing_ids: set[str]) -> dict[str, list[str]]:
    """Ordnet jedem Objekt-Namen (Teil nach dem Punkt) die passenden Entity-IDs zu.

    Grundlage fuer :func:`_normalize_entity_refs`: nur bei genau einem Treffer
    ist eine Korrektur eindeutig.
    """

    index: dict[str, list[str]] = {}
    for eid in existing_ids:
        _, _, object_id = eid.partition(".")
        index.setdefault(object_id, []).append(eid)
        canonical = canonicalize_object_id(object_id)
        if canonical != object_id:
            index.setdefault(canonical, []).append(eid)
    return index


def _normalize_entity_refs(node, existing: set[str], index: dict[str, list[str]]) -> None:
    """Korrigiert Entity-Referenzen in-place, wenn eindeutig moeglich:

    - fehlende Domain, z.B. "licht_wohnzimmer" statt "input_boolean.licht_wohnzimmer"
      (analog broker._normalize_entity_id)
    - falsche Domain, z.B. "light.licht_wohnzimmer", obwohl die Entity nur als
      "input_boolean.licht_wohnzimmer" existiert - das LLM nimmt gerne die "logische"
      HA-Domain (light/switch/...) an statt die tatsaechlich verwendete.

    Bleibt der Objekt-Name (Teil nach dem Punkt) mehrdeutig oder unbekannt, wird nichts
    veraendert - danach greift unveraendert die Existenz-Pruefung/Filterung.
    """

    def fix(value: str) -> str:
        if value in existing:
            return value
        object_id = value.split(".", 1)[1] if "." in value else value
        matches = index.get(object_id) or index.get(canonicalize_object_id(object_id))
        return matches[0] if matches and len(matches) == 1 else value

    if isinstance(node, dict):
        for key, value in list(node.items()):
            if key == "entity" and isinstance(value, str):
                node[key] = fix(value)
            elif key == "entities" and isinstance(value, list):
                for i, item in enumerate(value):
                    if isinstance(item, str):
                        value[i] = fix(item)
                    elif isinstance(item, dict) and isinstance(item.get("entity"), str):
                        item["entity"] = fix(item["entity"])
                    elif isinstance(item, dict):
                        _normalize_entity_refs(item, existing, index)
            else:
                _normalize_entity_refs(value, existing, index)
    elif isinstance(node, list):
        for item in node:
            _normalize_entity_refs(item, existing, index)


def _filter_known_entities(cards: list, existing: set[str]) -> tuple[list, set[str]]:
    """Entfernt Referenzen auf nicht existierende Entities statt die ganze Ansicht abzulehnen.

    Anders als bei Automationen/Aktionen (die reale Effekte ausloesen) ist ein Dashboard rein
    visuell - kleine Modell-Halluzinationen (z.B. das ueberall in HA-Tutorials vorkommende
    "group.all_lights") sollen nicht die komplette Anfrage scheitern lassen, wenn der Rest
    der Ansicht sinnvoll ist. Karten, die dadurch keine gueltige Entity mehr haetten, fallen
    komplett weg.

    Rueckgabe: (behaltene Karten, entfernte Entity-IDs).
    """

    dropped: set[str] = set()
    kept_cards = []
    for card in cards:
        if not isinstance(card, dict):
            continue
        card = dict(card)  # Kopie: die Eingabe bleibt unveraendert.
        if "entity" in card:
            if card["entity"] not in existing:
                dropped.add(card["entity"])
                continue
        if "entities" in card and isinstance(card["entities"], list):
            filtered_entities = []
            for item in card["entities"]:
                # Eintraege sind entweder "light.xyz" oder {"entity": "light.xyz", ...};
                # alles andere (z.B. Trennzeilen) bleibt unangetastet.
                eid = (
                    item
                    if isinstance(item, str)
                    else (item.get("entity") if isinstance(item, dict) else None)
                )
                if eid is None or eid in existing:
                    filtered_entities.append(item)
                else:
                    dropped.add(eid)
            if not filtered_entities:
                continue
            card["entities"] = filtered_entities
        kept_cards.append(card)
    return kept_cards, dropped


def validate_dashboard_view_yaml(hass: HomeAssistant, yaml_text: str) -> tuple[dict, set[str]]:
    """Prueft und bereinigt die vom Modell gelieferte View.

    Drei Schritte: syntaktische Pruefung, Normalisierung fehlender oder falscher
    Domains und Herausfiltern nicht existierender Entities. Gibt die (ggf.
    bereinigte) View sowie die Menge der dabei entfernten, nicht existierenden
    Entity-IDs zurueck.
    """

    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise BrokerError(f"Ungültiges YAML: {exc}", "invalid_yaml") from exc

    if not isinstance(parsed, dict):
        raise BrokerError("Dashboard-Ansicht muss ein YAML-Objekt sein.", "invalid_yaml")

    cards = parsed.get("cards")
    if not isinstance(cards, list) or not cards:
        raise BrokerError("Dashboard-Ansicht muss mindestens eine Karte enthalten.", "invalid_yaml")

    existing = {state.entity_id for state in hass.states.async_all()}
    _normalize_entity_refs(cards, existing, _object_id_index(existing))

    kept_cards, dropped = _filter_known_entities(cards, existing)
    if not kept_cards:
        raise BrokerError(
            f"Dashboard verwendet ausschließlich nicht existierende Entities: {', '.join(sorted(dropped))}",
            "entity_not_allowed",
        )

    # Nur die sichtbaren Titel: die Entity-Listen bleiben unangetastet, dort muss jede
    # ID Zeichen für Zeichen stimmen (siehe text_format).
    namen = name_map(hass)
    if isinstance(parsed.get("title"), str):
        parsed["title"] = normalize_umlauts(parsed["title"], namen)
    for card in kept_cards:
        if isinstance(card.get("title"), str):
            card["title"] = normalize_umlauts(card["title"], namen)

    parsed["cards"] = kept_cards
    return parsed, dropped


async def activate_dashboard_draft(hass: HomeAssistant, payload: dict) -> dict:
    """Wird erst nach expliziter Nutzerbestaetigung aufgerufen.

    Legt ein neues, eigenstaendiges YAML-Dashboard an (eigene Datei + eigener
    Sidebar-Eintrag), analog zu den bereits bestehenden Dashboards. Der Eintrag
    in configuration.yaml wird nach dem Schreiben mit Home Assistants eigenem
    YAML-Loader re-validiert; schlaegt das fehl, wird alles zurueckgerollt.
    """

    # Erneute Pruefung: zwischen Vorschlag und Bestaetigung koennen Entities
    # verschwunden sein.
    parsed, _dropped = validate_dashboard_view_yaml(hass, payload["yaml"])
    title = payload.get("title") or parsed.get("title") or "KI-Dashboard"
    icon = parsed.pop("icon", None) or "mdi:view-dashboard"
    parsed["title"] = title

    # Das LLM liefert nur die semantische Gruppierung; das eigentliche Layout
    # (Sektionen, Tile-Karten mit Bedienelementen, Gauges, Farben) baut die
    # Design-Schicht, damit jedes generierte Dashboard gleich gut aussieht.
    view = render_designed_view(hass, parsed)

    async with get_file_lock(hass):
        existing = await hass.async_add_executor_job(_existing_dashboard_url_paths, hass)
        url_path = _unique_url_path(title, existing)
        filename = f"{DASHBOARD_DIRECTORY}/smart_homeassistant-{url_path}.yaml"
        dashboard_path = hass.config.path(filename)

        # Zuerst die Dashboard-Datei: sie ist fuer sich genommen wirkungslos, solange
        # configuration.yaml sie nicht referenziert.
        await hass.async_add_executor_job(
            _write_yaml_dict, dashboard_path, {"title": title, "views": [view]}
        )

        config_path = _configuration_yaml_path(hass)
        original_text = await hass.async_add_executor_job(read_text, config_path)
        entry = {
            "mode": "yaml",
            "title": title,
            "icon": icon,
            "show_in_sidebar": True,
            "filename": filename,
        }
        new_text = _insert_dashboard_config(original_text, url_path, entry)

        def _verify(reloaded: dict) -> None:
            dashboards = ((reloaded.get("lovelace") or {}).get("dashboards")) or {}
            if dashboards.get(url_path, {}).get("filename") != filename:
                raise ValueError("Neuer Dashboard-Eintrag nach dem Schreiben nicht auffindbar.")

        def _remove_half_written_file() -> None:
            if os.path.exists(dashboard_path):
                os.remove(dashboard_path)

        await _write_config_and_verify(
            hass,
            config_path,
            original_text,
            new_text,
            _verify,
            "Anlegen des Dashboards fehlgeschlagen, configuration.yaml wurde nicht verändert",
            on_rollback=_remove_half_written_file,
        )

    return {"title": title, "url_path": url_path, "filename": filename, "restart_required": True}
