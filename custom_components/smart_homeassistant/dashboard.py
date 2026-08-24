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
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import slugify
from homeassistant.util.yaml import loader as ha_yaml_loader

from .broker import BrokerError
from .const import DOMAIN
from .files import read_text, write_text
from .locks import get_file_lock
from .text_format import canonical

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


async def list_areas(hass: HomeAssistant) -> list[dict]:
    """Alle Bereiche mit der Anzahl darin zugeordneter Entities.

    Diese Liste geht in den System-Prompt: das Modell waehlt Bereiche aus, nicht mehr
    einzelne Entities. Die Anzahl steht dabei, damit leere Bereiche erkennbar sind und
    nicht als Dashboard vorgeschlagen werden.

    Gezaehlt wird so, wie Home Assistant selbst zuordnet - erst die Entity, ersatzweise
    ihr Geraet. Konfigurations- und Diagnose-Entities sowie vom Nutzer ausgeblendete
    zaehlen nicht mit, weil die Bereichsstrategie sie ohnehin nicht anzeigt.
    """

    areas = ar.async_get(hass)
    entities = er.async_get(hass)
    devices = dr.async_get(hass)

    anzahl: dict[str, int] = {}
    for entry in entities.entities.values():
        if entry.entity_category is not None or entry.hidden_by is not None:
            continue
        area_id = entry.area_id
        if area_id is None and entry.device_id:
            device = devices.async_get(entry.device_id)
            area_id = device.area_id if device else None
        if area_id:
            anzahl[area_id] = anzahl.get(area_id, 0) + 1

    return [
        {"area_id": area.id, "name": area.name, "entities": anzahl.get(area.id, 0)}
        for area in sorted(areas.async_list_areas(), key=lambda a: a.name)
    ]


def resolve_dashboard_areas(hass: HomeAssistant, areas) -> tuple[list[dict], list[str]]:
    """Prueft die vom Modell genannten Bereiche und loest sie kanonisch auf.

    Akzeptiert die area_id ebenso wie den Anzeigenamen, letzteren umlauttolerant (siehe
    ``text_format.canonical``) - das Modell schreibt mal "kuche", mal "Kueche", mal
    "Küche". Anders als frueher bei den Entities ist die Menge klein und geschlossen,
    eine Aufloesung ist damit eindeutig und braucht keine Heuristik.

    Rueckgabe: (erkannte Bereiche in der genannten Reihenfolge, nicht erkannte Angaben).
    Wirft ``BrokerError``, wenn gar nichts uebrig bleibt.
    """

    alle = list(ar.async_get(hass).async_list_areas())
    nach_id = {area.id: area for area in alle}
    nach_name = {canonical(area.name): area for area in alle}

    erkannt: list[dict] = []
    unbekannt: list[str] = []
    for roh in areas or []:
        if not isinstance(roh, str):
            continue
        area = nach_id.get(roh.strip()) or nach_name.get(canonical(roh))
        if area is None:
            unbekannt.append(roh)
        elif all(a["area_id"] != area.id for a in erkannt):
            erkannt.append({"area_id": area.id, "name": area.name})

    if not erkannt:
        verfuegbar = ", ".join(area.name for area in alle) or "(keine)"
        genannt = ", ".join(str(a) for a in (areas or [])) or "(nichts genannt)"
        raise BrokerError(
            f"Keiner der genannten Bereiche existiert: {genannt}. "
            f"Vorhanden sind: {verfuegbar}.",
            "unknown_area",
        )
    return erkannt, unbekannt


def build_area_views(areas: list[dict]) -> list[dict]:
    """Baut je Bereich eine View und ueberlaesst deren Inhalt Home Assistant.

    Die Strategie ``area`` gruppiert die Entities des Bereichs selbst (Licht, Klima,
    Beschattung, Medien, Sicherheit, Aktionen, Sonstiges), setzt Ueberschriften mit
    passenden Icons, waehlt die Kartentypen und haengt die richtigen Bedienelemente an -
    Helligkeitsregler, Jalousie-Tasten, Schloss-Befehle, Zieltemperatur. Temperatur- und
    Feuchtigkeitswerte des Bereichs erscheinen automatisch als Badges.

    Genau das hat frueher ``dashboard_design.py`` von Hand nachgebaut. Ueber die
    Strategie folgen generierte Dashboards jetzt automatisch dem, was Home Assistant in
    kuenftigen Versionen als gute Bereichsdarstellung ansieht - ohne dass hier Code
    nachgezogen werden muss.
    """

    return [
        {
            "title": area["name"],
            "path": slugify(area["name"]) or area["area_id"],
            "strategy": {"type": "area", "area": area["area_id"]},
        }
        for area in areas
    ]


async def activate_dashboard_draft(hass: HomeAssistant, payload: dict) -> dict:
    """Wird erst nach expliziter Nutzerbestaetigung aufgerufen.

    Legt ein neues, eigenstaendiges YAML-Dashboard an: eine View je gewaehltem Bereich,
    deren Inhalt Home Assistants Bereichsstrategie erzeugt. Der Eintrag in
    configuration.yaml wird nach dem Schreiben mit Home Assistants eigenem YAML-Loader
    re-validiert; schlaegt das fehl, wird alles zurueckgerollt.
    """

    # Erneute Pruefung: zwischen Vorschlag und Bestaetigung koennen Bereiche umbenannt
    # oder geloescht worden sein.
    areas, _unbekannt = resolve_dashboard_areas(hass, payload.get("areas"))
    title = payload.get("title") or (areas[0]["name"] if len(areas) == 1 else "KI-Dashboard")
    views = build_area_views(areas)

    async with get_file_lock(hass):
        existing = await hass.async_add_executor_job(_existing_dashboard_url_paths, hass)
        url_path = _unique_url_path(title, existing)
        filename = f"{DASHBOARD_DIRECTORY}/smart_homeassistant-{url_path}.yaml"
        dashboard_path = hass.config.path(filename)

        # Zuerst die Dashboard-Datei: sie ist fuer sich genommen wirkungslos, solange
        # configuration.yaml sie nicht referenziert.
        await hass.async_add_executor_job(
            _write_yaml_dict, dashboard_path, {"title": title, "views": views}
        )

        config_path = _configuration_yaml_path(hass)
        original_text = await hass.async_add_executor_job(read_text, config_path)
        entry = {
            "mode": "yaml",
            "title": title,
            "icon": payload.get("icon") or "mdi:view-dashboard",
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
            "Anlegen des Dashboards fehlgeschlagen, configuration.yaml wurde nicht veraendert",
            on_rollback=_remove_half_written_file,
        )

    return {
        "title": title,
        "url_path": url_path,
        "filename": filename,
        "areas": [a["name"] for a in areas],
        "restart_required": True,
    }
