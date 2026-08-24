"""Relevanz-Vorauswahl der Entity-Listen im System-Prompt.

Der System-Prompt listet bei jeder Anfrage den kompletten Handlungsrahmen auf: alle
freigegebenen Service-Ziele, alle fuer Automationen erlaubten Entities und alle fuer
Dashboards verfuegbaren Entities (siehe ``ollama_client.build_system_prompt``). Das ist
die Ursache einer ganzen Fehlerklasse: das Modell schreibt entity_ids aus einer langen
Liste ab, die tausende Token vor seiner Antwort steht, und trifft sie nicht exakt.
Genau dagegen existieren heute sechs nachgelagerte Reparaturschichten
(``broker._normalize_entity_id``, ``broker.canonicalize_object_id``,
``broker._resolve_entity_id``, ``automations._normalize_entity_id_refs``,
``automations._expand_collective_entity_id``, ``dashboard._filter_known_entities``).
Dieses Modul setzt eine Stufe frueher an und zeigt dem Modell von vornherein nur, was
zur Anfrage passt. Dieselbe Ueberlegung steht schon hinter ``dashboard._EXCLUDED_PLATFORMS``
("je weniger irrelevante Eintraege, desto zuverlaessiger findet das Modell die
passenden") - nur nicht mehr statisch, sondern anfragebezogen.

Zwei Eigenschaften sind dabei wichtig:

* **Die Vorauswahl betrifft ausschliesslich den Prompt.** ``policy.data`` bleibt
  unangetastet, der Action Broker prueft unveraendert gegen die vollstaendige
  Whitelist. Eine zu enge Auswahl kann das Modell also hoechstens daran hindern, etwas
  vorzuschlagen - sie kann ihm niemals erlauben, etwas Unerlaubtes auszufuehren.
* **Im Zweifel die volle Liste.** Kurze Listen werden gar nicht erst gekuerzt, und
  liefert die Anfrage zu wenig Signal, wird ebenfalls alles gezeigt. Eine falsche
  Vorauswahl ist teurer als eine zu lange Liste: fehlt die gesuchte Entity, kann das
  Modell die Anfrage ueberhaupt nicht erfuellen.
"""

from __future__ import annotations

import re

# Ab wie vielen Eintraegen eine Liste ueberhaupt gekuerzt wird. Bewusst unterschiedlich
# je nach Verwendungszweck: ein Fehler auf dem handelnden Pfad (Services, Automationen)
# schaltet real etwas Falsches oder gar nichts, ein Fehler auf dem Dashboard-Pfad ist
# rein optisch und wird ohnehin nachtraeglich gefiltert. Der handelnde Pfad wird deshalb
# erst bei deutlich groesseren Listen angefasst.
ACTION_LIST_THRESHOLD = 40
DASHBOARD_LIST_THRESHOLD = 20

# Weniger Treffer als das gelten als "zu wenig Signal" - dann lieber die volle Liste.
MINIMUM_SELECTED = 3

# So viele vorherige Nutzer-Nachrichten fliessen in den Suchtext ein. Ohne sie verliert
# eine Folgeanfrage ihren Bezug: "und in der Kueche" nennt weder Geraeteart noch die
# gemeinte Aktion, waehrend die Nachricht davor beides enthaelt.
HISTORY_TURNS_FOR_CONTEXT = 2

# Umlaute und ihre ASCII-Umschreibungen werden beidseitig auf denselben Grundbuchstaben
# gefaltet: die Nutzeranfrage schreibt "Kueche" oder "Küche", die Entity heisst
# tatsaechlich "kuche" (siehe broker.canonicalize_object_id, dieselbe Ueberlegung).
_UMLAUT_CHARS = str.maketrans({"ä": "a", "ö": "o", "ü": "u", "Ä": "a", "Ö": "o", "Ü": "u"})
_UMLAUT_PAIRS = (("ss", "ss"), ("ue", "u"), ("oe", "o"), ("ae", "a"))

_TOKEN_PATTERN = re.compile(r"[a-z0-9]{3,}")


def canonicalize(text: str) -> str:
    """Faltet Umlaute, Umlaut-Umschreibungen und Grossschreibung zusammen."""

    result = text.lower().translate(_UMLAUT_CHARS).replace("ß", "ss")
    for pair, replacement in _UMLAUT_PAIRS:
        result = result.replace(pair, replacement)
    return result


def tokenize(text: str) -> set[str]:
    """Zerlegt Freitext oder eine entity_id in vergleichbare Wortbausteine.

    Punkte und Unterstriche trennen wie Leerzeichen, damit
    ``light.wohnzimmer_aqara_lampe_wohnzimmer`` zu ``{light, wohnzimmer, aqara, lampe}``
    wird und die Nutzeranfrage "Lampe im Wohnzimmer" darauf trifft.
    """

    return set(_TOKEN_PATTERN.findall(canonicalize(text).replace(".", " ").replace("_", " ")))


# Woerter, mit denen eine Anfrage eine ganze Geraeteklasse meint, statt ein einzelnes
# Geraet zu nennen ("mach alle Lichter aus", "Jalousien runter"). Trifft eines davon,
# kommt die komplette Domain in die Auswahl - sonst haette ausgerechnet die haeufigste
# Sammelanfrage nur die zufaellig namentlich passenden Entities gesehen.
_DOMAIN_KEYWORD_SOURCE: dict[str, tuple[str, ...]] = {
    "light": ("licht", "lichter", "lampe", "lampen", "beleuchtung", "light", "lights", "lamp"),
    "cover": (
        "jalousie", "jalousien", "rollo", "rollos", "rolladen", "rollladen", "beschattung",
        "vorhang", "vorhaenge", "cover", "covers", "blind", "blinds", "shutter", "curtain",
    ),
    "lock": ("schloss", "tuerschloss", "tuer", "tueren", "lock", "locks", "door", "unlock"),
    "media_player": (
        "musik", "lautsprecher", "radio", "sonos", "wiedergabe", "sender", "playlist",
        "media", "speaker", "speakers", "music", "player", "playback", "volume", "lautstaerke",
    ),
    "camera": ("kamera", "kameras", "camera", "cameras", "ueberwachung"),
    "climate": ("heizung", "klima", "thermostat", "temperatur", "heating", "climate", "temperature"),
    "switch": ("steckdose", "steckdosen", "schalter", "switch", "switches", "socket", "plug"),
    "sensor": (
        "sensor", "sensoren", "messwert", "leistung", "ertrag", "verbrauch", "energie",
        "photovoltaik", "solar", "power", "consumption", "energy",
    ),
    "script": ("script", "scripts", "skript", "routine", "szenario"),
    "scene": ("szene", "szenen", "scene", "scenes"),
    "person": ("person", "anwesenheit", "presence"),
    "input_boolean": ("modus", "schalter", "mode"),
    "input_number": ("helligkeit", "wert", "brightness", "level"),
}

# Die Stichworte werden mit derselben Faltung verglichen wie die Anfrage selbst.
DOMAIN_KEYWORDS: dict[str, frozenset[str]] = {
    domain: frozenset(canonicalize(word) for word in words)
    for domain, words in _DOMAIN_KEYWORD_SOURCE.items()
}


def relevance_query(history: list[dict], text: str) -> str:
    """Baut den Suchtext aus der aktuellen Nachricht und den letzten Nutzer-Turns."""

    frueher = [
        message.get("content", "")
        for message in history
        if message.get("role") == "user" and isinstance(message.get("content"), str)
    ]
    return " ".join([*frueher[-HISTORY_TURNS_FOR_CONTEXT:], text])


def select_relevant(
    entities: dict[str, str], query: str, threshold: int
) -> set[str] | None:
    """Waehlt die zur Anfrage passenden Entities aus.

    ``entities`` bildet ``entity_id`` auf zusaetzlichen Suchtext ab (ueblicherweise den
    Anzeigenamen; ``""`` wenn keiner bekannt ist). ``threshold`` legt fest, ab welcher
    Listenlaenge ueberhaupt gekuerzt wird.

    Rueckgabe ist ``None``, wenn die volle Liste verwendet werden soll - entweder weil
    sie ohnehin kurz genug ist, weil die Anfrage kein verwertbares Stichwort enthaelt
    oder weil zu wenig uebrig bliebe, um der Auswahl zu trauen.
    """

    if len(entities) < threshold:
        return None

    query_tokens = tokenize(query)
    if not query_tokens:
        return None

    wanted_domains = {
        domain for domain, keywords in DOMAIN_KEYWORDS.items() if query_tokens & keywords
    }

    selected = {
        entity_id
        for entity_id, label in entities.items()
        if entity_id.split(".", 1)[0] in wanted_domains
        or tokenize(f"{entity_id} {label}") & query_tokens
    }

    if len(selected) < MINIMUM_SELECTED:
        return None
    return selected


def policy_data_for_prompt(policy_data: dict, keep: set[str]) -> dict:
    """Kopie der Policy-Daten, deren ``allowed_entities`` auf ``keep`` gekuerzt sind.

    Bewusst eine Kopie: ``policy.data`` selbst darf sich nicht aendern, sonst wuerde die
    Vorauswahl zur Berechtigungspruefung (siehe Modul-Docstring). Services, fuer die
    danach kein einziges Ziel uebrig bleibt, fallen aus der Auflistung heraus - einen
    Service ohne erlaubtes Ziel vorzuschlagen kann ohnehin nur in einer Ablehnung enden.
    Ein ``"*"`` als Freigabe bleibt unangetastet, weil es kein aufzaehlbares Ziel hat.
    """

    services = {}
    for key, config in (policy_data.get("services") or {}).items():
        config = dict(config or {})
        allowed = config.get("allowed_entities", [])
        if "*" not in allowed:
            gekuerzt = [entity_id for entity_id in allowed if entity_id in keep]
            if not gekuerzt:
                continue
            config["allowed_entities"] = gekuerzt
        services[key] = config
    return {**policy_data, "services": services}
