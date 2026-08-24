"""Design-Schicht fuer generierte Dashboards.

Arbeitsteilung mit dem Sprachmodell: das LLM entscheidet, WAS auf das Dashboard
gehoert (welche Entities, wie gruppiert, welcher Titel) - diese Schicht
entscheidet, WIE es aussieht. Das ist bewusst so getrennt, weil kleine lokale
Modelle bei komplexem Lovelace-YAML unzuverlaessig sind (falsche Kartentypen,
erfundene Schluessel), die semantische Gruppierung aber gut hinbekommen.

Gestaltungsgrundsaetze:

* Keine wertenden Farbskalen. Eine Ampel (rot/gelb/gruen) auf einem Messwert
  behauptet, es gaebe "gut" und "schlecht" - das ist ohne fachliche Vorgabe
  falsch (0 W Solarleistung sind nachts voellig normal, nicht "kritisch") und
  eine willkuerlich gewaehlte Gauge-Skala waere dieselbe Behauptung nochmal.
  Messwerte bekommen daher eine neutrale Verlaufsdarstellung: aktueller Wert
  gross, dazu die Kurve der letzten 24 h. Das zeigt Einordnung ohne Wertung.
* Karten bekommen nur so viel Groesse, wie ihr Inhalt braucht: Bedienkacheln,
  Sensoren und Kameras bleiben halbbreit, damit breite Displays dichter genutzt
  werden. Nur echte Sammeldiagramme bekommen volle Breite.
* Luecken werden nicht durch kuenstliches Aufblasen einzelner Karten kaschiert.
  Das Dashboard bleibt lieber gleichmaessig kompakt, statt grosse halb leere
  Karten zu erzeugen.
* Ruhiges Licht, ein einziger Akzent: jede Kachel nutzt dieselbe Theme-Farbe
  ("accent", siehe _ACCENT_COLOR) statt einer Farbe pro Geraeteart. Das ist
  die fuer diese Integration angepasste Version der "Spatial Glass"-Regel
  "genau EIN Akzent, keine zweite Akzentfarbe" - Unterscheidung zwischen
  Geraetearten kommt allein ueber Icon und Text, nicht ueber Farbe. Ausfuehrlich
  begruendet und gegen das restliche Regelwerk abgeglichen in DESIGN_POLICY.md.

Zwei Einstiegspunkte:

* :func:`check_layout_quality` - prueft die Gliederung, *bevor* gestaltet wird.
* :func:`render_designed_view` - baut aus der Gliederung die fertige View.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

# Genau EIN Akzent fuer alle Kacheln (DESIGN_POLICY.md, Abschnitt "Farbe"):
# "accent" ist ein von Home Assistant reserviertes Schluesselwort und loest zu
# var(--accent-color) auf - der Akzentfarbe des GERADE AKTIVEN Themes. Dashboards
# bleiben so automatisch synchron mit Hell/Dunkel und mit dem installierten
# visionOS/Liquid-Glass-Theme, ohne dass hier ein eigener Hex-Wert festgelegt
# werden muss. Bewusst NICHT wie frueher eine Farbe pro Geraeteart (amber fuer
# Licht, rot fuer Schloss, ...): das widerspraeche "ein einziger Akzent, Kontrast
# durch Helligkeit statt durch Buntheit". Unterscheidung kommt allein ueber
# Icon und Text.
_ACCENT_COLOR = "accent"

# Icon-Sprache pro Domain (siehe auch _TITLE_ICONS fuer Gruppen-Icons).
_DOMAIN_ICON: dict[str, str] = {
    "light": "mdi:lightbulb-group",
    "switch": "mdi:toggle-switch",
    "cover": "mdi:window-shutter",
    "lock": "mdi:lock",
    "camera": "mdi:cctv",
    "climate": "mdi:thermostat",
    "media_player": "mdi:play-circle",
    "binary_sensor": "mdi:motion-sensor",
    "sensor": "mdi:chart-line",
    "script": "mdi:script-text-play",
    "scene": "mdi:palette",
    "person": "mdi:account",
}
_FALLBACK_ICON = "mdi:view-dashboard"

# Schluesselwoerter im Gruppentitel -> passenderes Icon als das Domain-Default.
_TITLE_ICONS: list[tuple[tuple[str, ...], str]] = [
    (("licht", "lampe", "beleucht"), "mdi:lightbulb-group"),
    (("jalousie", "rollo", "rolllad", "beschatt"), "mdi:window-shutter"),
    (("kamera", "video", "ueberwach", "überwach"), "mdi:cctv"),
    (("schloss", "tuer", "tür", "sicherheit", "alarm"), "mdi:shield-home"),
    (
        ("photovoltaik", "solar", "pv", "energie", "strom", "ertrag", "wechselrichter"),
        "mdi:solar-power-variant",
    ),
    (("wohnzimmer",), "mdi:sofa"),
    (("schlafzimmer",), "mdi:bed"),
    (("kueche", "küche"), "mdi:countertop"),
    (("flur", "eingang", "diele"), "mdi:door-open"),
    (("bad", "dusche"), "mdi:shower"),
    (("garten", "außen", "außen"), "mdi:tree"),
    (("szene", "routine", "skript", "script"), "mdi:script-text-play"),
    (("medien", "musik", "lautsprecher", "player"), "mdi:play-circle"),
    (("person", "anwesenheit", "familie"), "mdi:account-group"),
    (("klima", "heizung", "temperatur"), "mdi:thermostat"),
]

# Kartentypen, die volle Breite brauchen - Sektionen mit solchem Inhalt bekommen
# zwei Rasterspalten, damit Diagramme und Kamerabilder nicht gequetscht werden.
_WIDE_CARD_TYPES = {"history-graph", "statistics-graph"}

# Home Assistants Sektionsraster hat 12 Spalten.
_HALF_WIDTH = {"columns": 6}
_FULL_WIDTH = {"columns": 12}


# --- Kleine Helfer rund um Entity-Zustaende ---------------------------------


def _state_of(hass: HomeAssistant, entity_id: str):
    """Aktueller Zustand oder ``None``, wenn die Entity (noch) nicht existiert."""

    return hass.states.get(entity_id)


def _attribute(hass: HomeAssistant, entity_id: str, name: str):
    """Ein Attribut der Entity, oder ``None``."""

    state = _state_of(hass, entity_id)
    return state.attributes.get(name) if state else None


def _short_name(hass: HomeAssistant, entity_id: str) -> str | None:
    """Kurzer Name ohne vorangestellten Geraetenamen oder Hersteller.

    Also "Leistung" statt "SolarEdge Wechselrichter Leistung", und "Lampe
    Wohnzimmer" statt "Aqara Lampe Wohnzimmer". Wird sowohl fuer Diagramme
    (dort ist nur Platz fuers Wesentliche) als auch fuer Kachelnamen verwendet:
    die Tile-Karte bricht lange Namen NICHT um, sondern schneidet sie fest mit
    "..." ab (white-space: nowrap in Home Assistants eigenem Frontend-Code) -
    ein kuerzerer Name ist die einzige verlaessliche Gegenmassnahme.
    """

    entry = er.async_get(hass).async_get(entity_id)
    name = (entry.original_name if entry is not None else None) or _attribute(
        hass, entity_id, "friendly_name"
    )
    if not name:
        return name

    # Der Herstellername steht oft redundant vor dem eigentlichen Geraetenamen
    # ("Aqara Lampe Wohnzimmer") - fuer die Kachel ist nur "Lampe Wohnzimmer"
    # relevant, der Hersteller steht ohnehin auf der Geraeteseite.
    manufacturer = None
    if entry is not None and entry.device_id:
        device = dr.async_get(hass).async_get(entry.device_id)
        manufacturer = device.manufacturer if device else None
    if manufacturer and name.lower().startswith(f"{manufacturer.lower()} "):
        name = name[len(manufacturer) + 1 :]

    return name


def _is_measurement(hass: HomeAssistant, entity_id: str) -> bool:
    """Momentanwert (Leistung, Temperatur ...) statt kumuliertem Zaehlerstand."""

    return _attribute(hass, entity_id, "state_class") == "measurement"


def _is_numeric(hass: HomeAssistant, entity_id: str) -> bool:
    """Laesst sich der aktuelle Zustand als Zahl lesen (Voraussetzung fuer Kurven)?"""

    state = _state_of(hass, entity_id)
    if state is None:
        return False
    try:
        float(state.state)
    except (TypeError, ValueError):
        return False
    return True


def _group_icon(title: str, entity_ids: list[str], view_title: str = "") -> str:
    """Waehlt das Icon einer Gruppe: Gruppentitel schlaegt Dashboard-Titel schlaegt
    Domain-Default.

    Der Dashboard-Titel als zweite Quelle faengt Faelle wie die Gruppe "Leistung und
    Ertraege" im Dashboard "PV-Anlage" ab, die sonst nur ein generisches Icon bekaeme.
    """

    for candidate in (title, view_title):
        lowered = (candidate or "").lower()
        for keywords, icon in _TITLE_ICONS:
            if any(keyword in lowered for keyword in keywords):
                return icon

    # Kein Stichwort getroffen: Icon der haeufigsten Domain in der Gruppe.
    domains = [eid.split(".", 1)[0] for eid in entity_ids]
    if domains:
        dominant = max(set(domains), key=domains.count)
        return _DOMAIN_ICON.get(dominant, _FALLBACK_ICON)
    return _FALLBACK_ICON


def _card_for_entity(hass: HomeAssistant, entity_id: str, with_graph: bool = False) -> dict:
    """Baut die zur Domain passende, bedienbare Karte statt einer reinen Listenzeile.

    with_graph: auch einen Zaehlerstand als Wert-plus-Kurve darstellen. Das lohnt sich,
    wenn er allein in seiner Gruppe steht - eine Kachel und daneben ein eigenes Diagramm
    fuer denselben Wert waeren zwei halbleere Karten statt einer aussagekraeftigen.
    """

    domain = entity_id.split(".", 1)[0]

    # Kamera: Livebild, ueber die volle Breite.
    if domain == "camera":
        return {
            "type": "picture-entity",
            "entity": entity_id,
            "camera_view": "live",
            "show_state": False,
            "show_name": True,
            "grid_options": dict(_HALF_WIDTH),
        }

    if (
        domain == "sensor"
        and (_is_measurement(hass, entity_id) or with_graph)
        and _is_numeric(hass, entity_id)
    ):
        # Aktueller Wert gross + Verlaufskurve, bewusst ohne Skala und ohne
        # Farbbewertung (siehe Modul-Docstring).
        return {
            "type": "sensor",
            "entity": entity_id,
            "name": _short_name(hass, entity_id) or entity_id,
            "graph": "line",
            "hours_to_show": 24,
            "detail": 1,
            "grid_options": dict(_HALF_WIDTH),
        }

    # Alles Uebrige wird eine halbbreite Kachel; je nach Domain mit dem passenden
    # Bedienelement direkt auf der Karte, damit man nicht erst den Dialog braucht.
    #
    if domain == "cover":
        name = _short_name(hass, entity_id) or entity_id
        return {
            "type": "vertical-stack",
            "grid_options": dict(_HALF_WIDTH),
            "cards": [
                {
                    "type": "tile",
                    "entity": entity_id,
                    "name": name,
                    "color": _ACCENT_COLOR,
                    "state_content": "none",
                    "tap_action": {"action": "more-info"},
                },
                {
                    "type": "horizontal-stack",
                    "cards": [
                        {
                            "type": "button",
                            "name": "Rauf",
                            "icon": "mdi:arrow-up-bold",
                            "tap_action": {
                                "action": "perform-action",
                                "perform_action": "cover.open_cover",
                                "target": {"entity_id": entity_id},
                            },
                        },
                        {
                            "type": "button",
                            "name": "Stopp",
                            "icon": "mdi:stop",
                            "tap_action": {
                                "action": "perform-action",
                                "perform_action": "cover.stop_cover",
                                "target": {"entity_id": entity_id},
                            },
                        },
                        {
                            "type": "button",
                            "name": "Runter",
                            "icon": "mdi:arrow-down-bold",
                            "tap_action": {
                                "action": "perform-action",
                                "perform_action": "cover.close_cover",
                                "target": {"entity_id": entity_id},
                            },
                        },
                    ],
                },
            ],
        }

    # Lichter werden per Tap ein-/ausgeschaltet; langer Druck oeffnet den
    # Detaildialog zum Dimmen. Zustands-/Prozentzeilen bleiben ausgeblendet.
    card: dict = {
        "type": "tile",
        "entity": entity_id,
        "color": _ACCENT_COLOR,
        "state_content": "none",
        "grid_options": dict(_HALF_WIDTH),
    }

    # Home Assistants Standardname ist oft "Geraetename + Entity-Name"
    # ("Wohnzimmer Aqara Lampe Wohnzimmer") und wird auf einer halbbreiten
    # Kachel mit "..." abgeschnitten. Der kurze Registry-Name ("Lampe
    # Wohnzimmer") passt dagegen normalerweise in eine Zeile.
    short_name = _short_name(hass, entity_id)
    if short_name:
        card["name"] = short_name

    if domain == "light":
        card["tap_action"] = {"action": "toggle"}
        card["hold_action"] = {"action": "more-info"}
        card["double_tap_action"] = {"action": "more-info"}
    elif domain == "lock":
        card["features_position"] = "bottom"
        card["features"] = [{"type": "lock-commands"}]
    elif domain == "climate":
        card["features_position"] = "bottom"
        card["features"] = [{"type": "target-temperature"}]
    elif domain == "media_player":
        card["features_position"] = "bottom"
        card["features"] = [{"type": "media-player-playback"}]
    elif domain in ("script", "scene"):
        # Ein Klick loest aus, statt nur den Detaildialog zu oeffnen.
        card["tap_action"] = {"action": "perform-action", "perform_action": f"{domain}.turn_on"}

    return card


def _trend_graph(hass: HomeAssistant, entity_ids: list[str]) -> dict | None:
    """Gemeinsamer Verlauf fuer die Zaehlerstaende einer Gruppe.

    Gibt ``None`` zurueck, wenn keine der Entities einen Zahlenwert liefert -
    dann waere die Kurve leer.
    """

    numeric = [eid for eid in entity_ids if _is_numeric(hass, eid)]
    if not numeric:
        return None
    return {
        "type": "history-graph",
        "title": "Verlauf (24 h)",
        "hours_to_show": 24,
        "entities": [{"entity": eid, "name": _short_name(hass, eid) or eid} for eid in numeric],
        "grid_options": dict(_FULL_WIDTH),
    }


def _extract_groups(cards: list) -> list[dict]:
    """Holt die semantischen Gruppen (Titel + Entities) aus der LLM-Antwort.

    Gedacht ist jede Karte als eine Gruppe. Die Entities koennen als Liste
    ("entities") oder als Einzelwert ("entity") kommen; Karten ohne jede Entity
    werden uebersprungen. Basis fuer :func:`check_layout_quality` und
    :func:`render_designed_view` - beide sollen dieselben Gruppen sehen.
    """

    groups: list[dict] = []
    for card in cards:
        if not isinstance(card, dict):
            continue

        entity_ids: list[str] = []
        if isinstance(card.get("entities"), list):
            for item in card["entities"]:
                if isinstance(item, str):
                    entity_ids.append(item)
                elif isinstance(item, dict) and isinstance(item.get("entity"), str):
                    entity_ids.append(item["entity"])
        if isinstance(card.get("entity"), str):
            entity_ids.append(card["entity"])

        if not entity_ids:
            continue

        groups.append(
            {
                "title": card.get("title") or card.get("name") or "",
                "icon": card.get("icon"),
                "entity_ids": entity_ids,
            }
        )
    return groups


def _merge_untitled(groups: list[dict]) -> list[dict]:
    """Fasst alle Gruppen ohne Titel zu einer Sammelsektion "Geräte" zusammen.

    Mehrere Einzelkarten ohne Titel wuerden je eine eigene Sektion ergeben - das
    sieht zerrissen aus.
    """

    titled = [g for g in groups if g["title"]]
    untitled = [g for g in groups if not g["title"]]
    if not untitled:
        return titled

    merged_ids: list[str] = []
    for group in untitled:
        merged_ids.extend(group["entity_ids"])
    titled.append({"title": "Geräte", "icon": None, "entity_ids": merged_ids})
    return titled


# Nichtssagende Gruppentitel. Sie ergeben im Dashboard Ueberschriften, die dem
# Nutzer nichts sagen, und sind ein zuverlaessiges Zeichen dafuer, dass das Modell
# nicht wirklich gruppiert, sondern nur Entities abgeladen hat.
_GENERIC_TITLES = {
    "gruppe",
    "gruppe 1",
    "gruppe 2",
    "sonstiges",
    "sonstige",
    "geraete",
    "geräte",
    "entities",
    "entitaeten",
    "entitäten",
    "diverse",
    "andere",
    "weitere",
    "verschiedenes",
    "misc",
    "allgemein",
    "dashboard",
    "uebersicht",
    "übersicht",
    # Englische Entsprechungen - die Antwortsprache (siehe SYSTEM_PROMPT_TEMPLATE in
    # ollama_client.py) ist nicht mehr fest Deutsch, REGEL 3 gilt also auch dort.
    "group",
    "group 1",
    "group 2",
    "misc.",
    "miscellaneous",
    "devices",
    "various",
    "other",
    "others",
    "general",
    "overview",
}

# Ab dieser Gesamtzahl an Entities ist eine einzige Gruppe keine Gliederung mehr.
_MIN_ENTITIES_FOR_GROUPING = 4
# Bewusst klein gehalten (Single-Page-Ziel, siehe DESIGN_POLICY.md Abschnitt 5):
# bei "max_columns" Spalten passen hoechstens so viele Sections nebeneinander in
# EINE Zeile. Mehr Gruppen wuerden auf eine zweite Zeile umbrechen und damit
# Scrollen erzwingen - weniger, dafuer volle Gruppen bleiben in einer Zeile.
_MAX_GROUPS = 3

# Wie viele Badges hoechstens in der Kopfzeile stehen (siehe render_designed_view).
# Klein gehalten fuers Single-Page-Ziel: jedes Badge kostet Zeilenhoehe, die auf
# kleineren Bildschirmen zum Scrollen zwingen kann.
_MAX_BADGES = 3


def check_layout_quality(view: dict) -> list[str]:
    """Prueft die Gliederung auf Layout-Maengel, bevor daraus ein Dashboard wird.

    Die Design-Schicht kann aus einer schlechten Gliederung kein gutes Dashboard
    machen: eine einzige Riesengruppe bleibt eine endlose Spalte, lauter
    Ein-Element-Gruppen bleiben ein zerrissenes Raster. Solche Faelle sind hier
    maschinell erkennbar - dann wird lieber neu generiert.

    Die Regeln sind identisch zu den LAYOUT-REGELN im System-Prompt formuliert,
    damit das Modell gegen dieselben Kriterien arbeitet, an denen es gemessen wird.
    Rueckgabe: Liste der Verstoesse (leer = in Ordnung).
    """

    groups = _extract_groups(view.get("cards") or [])
    problems: list[str] = []

    if not groups:
        return ["Es wurde keine einzige Gruppe mit Entities geliefert."]

    total_entities = sum(len(g["entity_ids"]) for g in groups)

    # Regel 1: sinnvolle Anzahl Gruppen (2 bis 5).
    if total_entities >= _MIN_ENTITIES_FOR_GROUPING and len(groups) < 2:
        problems.append(
            f"Regel 1 verletzt: {total_entities} Entities stecken in nur einer Gruppe. "
            "Teile sie auf 2 bis 5 thematische Gruppen auf."
        )
    if len(groups) > _MAX_GROUPS:
        problems.append(
            f"Regel 1 verletzt: {len(groups)} Gruppen sind zu viele (maximal {_MAX_GROUPS})."
        )

    # Regel 2: hoechstens eine Gruppe mit nur einer Entity.
    single_entity_groups = [g for g in groups if len(g["entity_ids"]) == 1]
    if len(groups) >= 2 and len(single_entity_groups) > 1:
        titles = ", ".join(f"'{g['title'] or '(ohne Titel)'}'" for g in single_entity_groups)
        problems.append(
            f"Regel 2 verletzt: mehrere Gruppen enthalten nur eine einzige Entity ({titles}). "
            "Fasse zusammengehörige Entities in gemeinsame Gruppen."
        )

    # Regel 3: aussagekraeftige Titel.
    for group in groups:
        title = (group["title"] or "").strip()
        if not title:
            problems.append("Regel 3 verletzt: eine Gruppe hat gar keinen Titel.")
        elif title.lower() in _GENERIC_TITLES:
            problems.append(
                f"Regel 3 verletzt: '{title}' ist ein nichtssagender Titel. "
                "Nutze konkrete Bezeichnungen wie 'Beleuchtung' oder 'Beschattung Erdgeschoss'."
            )

    # Regel 4: keine doppelten Titel.
    seen_titles: set[str] = set()
    for group in groups:
        key = (group["title"] or "").strip().lower()
        if key and key in seen_titles:
            problems.append(
                f"Regel 4 verletzt: der Gruppentitel '{group['title']}' kommt mehrfach vor."
            )
        seen_titles.add(key)

    # Regel 5: jede Entity nur in einer Gruppe.
    seen_entities: set[str] = set()
    duplicates: set[str] = set()
    for group in groups:
        for entity_id in group["entity_ids"]:
            if entity_id in seen_entities:
                duplicates.add(entity_id)
            seen_entities.add(entity_id)
    if duplicates:
        problems.append(
            f"Regel 5 verletzt: diese Entities stehen in mehreren Gruppen: {', '.join(sorted(duplicates))}."
        )

    return problems


# Prioritaet der Badge-Kandidaten je Gruppe: Anwesenheit und Sicherheit sagen auf
# einen Blick mehr als ein Momentanwert, der wiederum mehr sagt als ein
# kumulierter Zaehlerstand. Orientiert an der Badge-Zeile des offiziellen
# visionOS-Beispiel-Dashboards (device_tracker, media_player, alarm, Sensoren).
_BADGE_DOMAIN_PRIORITY = (
    ("person.", "device_tracker."),
    ("lock.", "alarm_control_panel.", "binary_sensor."),
    ("media_player.",),
    ("climate.",),
)


def _pick_badge(hass: HomeAssistant, entity_ids: list[str]) -> str | None:
    """Waehlt je Gruppe den aussagekraeftigsten Wert fuer die Kopfzeile."""

    for prefixes in _BADGE_DOMAIN_PRIORITY:
        for entity_id in entity_ids:
            if entity_id.startswith(prefixes):
                return entity_id
    for entity_id in entity_ids:
        if entity_id.startswith("sensor.") and _is_measurement(hass, entity_id):
            return entity_id
    for entity_id in entity_ids:
        if entity_id.startswith("sensor."):
            return entity_id
    return None


def render_designed_view(hass: HomeAssistant, view: dict) -> dict:
    """Baut aus der (bereits validierten) LLM-Gruppierung die gestaltete View.

    Aus jeder Gruppe wird eine Sektion mit Ueberschrift, den passenden Karten und
    - bei mehreren Zaehlerstaenden - einem gemeinsamen Verlaufsdiagramm. Dazu
    kommen bis zu _MAX_BADGES Badges als Kurzuebersicht.
    """

    groups = _merge_untitled(_extract_groups(view.get("cards") or []))
    if not groups:
        return view  # nichts Verwertbares - lieber unveraendert lassen als kaputt machen

    sections: list[dict] = []
    badge_entities: list[str] = []
    view_title = view.get("title") or "Dashboard"

    for group in groups:
        entity_ids = group["entity_ids"]

        cards: list[dict] = [
            {
                "type": "heading",
                "heading": group["title"],
                "heading_style": "title",
                "icon": group["icon"] or _group_icon(group["title"], entity_ids, view_title),
            }
        ]

        totals = [
            eid
            for eid in entity_ids
            if eid.startswith("sensor.") and not _is_measurement(hass, eid)
        ]
        # Steht ein Zaehlerstand allein, bekommt er Wert und Kurve in einer Karte;
        # bei mehreren ist ein gemeinsames Diagramm unter den Kacheln uebersichtlicher.
        single_total = totals[0] if len(totals) == 1 else None

        for entity_id in entity_ids:
            cards.append(_card_for_entity(hass, entity_id, with_graph=entity_id == single_total))

        if len(totals) > 1:
            graph = _trend_graph(hass, totals)
            if graph is not None:
                cards.append(graph)

        section: dict = {"type": "grid", "cards": cards}
        # Sektionen mit Diagramm oder Kamerabild duerfen zwei Spalten belegen.
        if any(card.get("type") in _WIDE_CARD_TYPES for card in cards):
            section["column_span"] = 2
        sections.append(section)

        # Je Gruppe hoechstens ein Badge, insgesamt hoechstens _MAX_BADGES - sonst
        # wird aus der Kurzuebersicht eine zweite Kartenreihe.
        badge = _pick_badge(hass, entity_ids)
        if badge is not None and len(badge_entities) < _MAX_BADGES:
            badge_entities.append(badge)

    designed: dict = {
        "title": view_title,
        "type": "sections",
        # Mindestens zwei Spalten (sonst wirkt es wie eine Liste), hoechstens drei
        # (sonst werden die Karten auf breiten Bildschirmen zu schmal).
        "max_columns": min(3, max(2, len(sections))),
        # Luecken im Raster auffuellen statt Sektionen strikt spaltenweise stapeln.
        "dense_section_placement": True,
        # Bewusst OHNE Begruessungs-Karte im Header - kein aggregierter
        # Status-Text wie "3 von 5 Lichtern an" mehr (DESIGN_POLICY.md
        # Abschnitt 4) und weniger Kopfbereich fuers Single-Page-Ziel
        # (Abschnitt 5). Titel und Badges
        # (falls vorhanden) zeigt Home Assistant automatisch als kompakte
        # Kopfzeile ohne eigene Karte - siehe hui-view-header.ts, das ohne
        # "card" nur Titel+Badges rendert statt eine leere Flaeche zu lassen.
        "header": {"badges_position": "bottom", "layout": "responsive"},
        "sections": sections,
    }
    if view.get("icon"):
        designed["icon"] = view["icon"]
    if badge_entities:
        # show_name/show_state/show_icon: wie in der Badge-Zeile des offiziellen
        # visionOS-Beispiel-Dashboards, damit ein Badge auf einen Blick lesbar ist
        # statt nur ein Icon zu zeigen.
        designed["badges"] = [
            {
                "type": "entity",
                "entity": eid,
                "show_name": True,
                "show_state": True,
                "show_icon": True,
            }
            for eid in badge_entities
        ]

    return designed
