"""Nachbearbeitung des vom Sprachmodell erzeugten Freitexts: Umlaute ausschreiben.

Der weitaus groesste Teil einer Chat-Antwort kommt vom Modell, nicht aus i18n.py. Der
System-Prompt verlangt zwar ausdruecklich echte Umlaute (siehe
``ollama_client.SYSTEM_PROMPT_TEMPLATE``, Abschnitt "WICHTIG - Rechtschreibung"), aber
das ist eine Bitte, keine Garantie: kleine lokale Modelle wie llama3.1:8b halten sich
nicht zuverlaessig daran und schreiben weiter "Kueche" oder "geloescht". Dieses Modul
korrigiert das deterministisch, nachdem die Antwort da ist.

Zwei Korrekturquellen, weil beide allein Luecken haetten:

* **Namen aus Home Assistant** (:func:`name_map`). Bereichs- und Anzeigenamen stehen im
  System korrekt mit Umlauten ("Küche", "Gästemodus"). Schreibt das Modell "Kueche",
  laesst sich das ueber die Kanonisierung eindeutig zurueckbilden. Das deckt genau die
  Eigennamen ab, um die es im Chat meistens geht, und braucht keine Wortliste.
* **Kuratierte Wortliste** (:data:`WORDS`) fuer das allgemeine Vokabular ("geloescht",
  "moechte", "Rueckfrage").

Bewusst KEIN regelbasiertes "ue -> ü": im Deutschen ist "ue" genauso oft kein Umlaut
("aktuell", "neue", "Steuer", "Quelle", "eventuell") wie einer, und "ss" ist ueberwiegend
korrekt ("muss", "dass", "Erdgeschoss"). Eine Regel ohne Woerterbuch produziert hier mehr
Fehler, als sie behebt - deshalb nur exakte, gepruefte Treffer.

Geschuetzt bleiben in jedem Fall:

* alles in Code-Zaeunen (```...```) und Inline-Code (`...`) - dort stehen YAML-Vorschauen
  und entity_ids,
* jedes Wort, das an einen Punkt oder Unterstrich grenzt - "light.kueche_lampe" ist eine
  entity_id und muss Zeichen fuer Zeichen so bleiben, sonst zeigt die Vorschau etwas
  anderes an als das, was tatsaechlich ausgefuehrt wird.
"""

from __future__ import annotations

import re

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar

# Kanonisierung wie in entity_filter/broker: "Kueche", "Küche" und "kuche" fallen
# zusammen. Nur fuer den Vergleich, nie fuer die Ausgabe.
_UMLAUT_CHARS = str.maketrans({"ä": "a", "ö": "o", "ü": "u", "Ä": "a", "Ö": "o", "Ü": "u"})


def canonical(text: str) -> str:
    """Vergleichsform eines Wortes: klein, ohne Umlaute und ohne deren Umschreibungen."""

    result = text.lower().translate(_UMLAUT_CHARS).replace("ß", "ss")
    for pair in ("ue", "oe", "ae"):
        result = result.replace(pair, pair[0])
    return result


# Allgemeines Vokabular, das im Chat vorkommt. Nur eindeutige Faelle - im Zweifel lieber
# nicht ersetzen (siehe Modul-Docstring).
WORDS: dict[str, str] = {
    "aendern": "ändern", "aendert": "ändert", "Aenderung": "Änderung",
    "aehnlich": "ähnlich",
    "ausfuehren": "ausführen", "Ausfuehren": "Ausführen", "ausgefuehrt": "ausgeführt",
    "ausfuehrlich": "ausführlich",
    "Ausloeser": "Auslöser", "ausloesen": "auslösen", "ausloest": "auslöst",
    "ausschliesslich": "ausschließlich",
    "aussagekraeftig": "aussagekräftig", "aussagekraeftige": "aussagekräftige",
    "benoetigt": "benötigt", "benoetigen": "benötigen",
    "bestaetigen": "bestätigen", "bestaetigt": "bestätigt", "Bestaetigung": "Bestätigung",
    "dafuer": "dafür", "Dafuer": "Dafür",
    "eingeschraenkt": "eingeschränkt",
    "enthaelt": "enthält", "Enthaelt": "Enthält",
    "erhaelt": "erhält",
    "erklaeren": "erklären", "erklaert": "erklärt", "Erklaerung": "Erklärung",
    "faellt": "fällt",
    "fuegt": "fügt", "hinzufuegen": "hinzufügen", "Fuege": "Füge", "fuege": "füge",
    "fuer": "für", "Fuer": "Für", "fuers": "fürs",
    "fuehrt": "führt", "ausgefuehrte": "ausgeführte",
    "gaengig": "gängig",
    "geaendert": "geändert",
    "gehoert": "gehört", "gehoeren": "gehören",
    "geloescht": "gelöscht", "Geloescht": "Gelöscht",
    "geoeffnet": "geöffnet", "Geoeffnet": "Geöffnet",
    "gepruefte": "geprüfte", "geprueft": "geprüft", "pruefen": "prüfen", "pruefe": "prüfe",
    "Geraet": "Gerät", "geraet": "Gerät", "Geraete": "Geräte", "geraete": "Geräte",
    "Geraeten": "Geräten", "Geraeteart": "Geräteart", "Geraetename": "Gerätename",
    "geschlossen": "geschlossen",
    "Gespraech": "Gespräch", "Gespraeche": "Gespräche",
    "gewaehlt": "gewählt", "gewaehlte": "gewählte",
    "groesser": "größer", "Groesse": "Größe", "groesste": "größte",
    "gueltig": "gültig", "gueltige": "gültige", "gueltiger": "gültiger",
    "haeufig": "häufig",
    "haette": "hätte", "haetten": "hätten",
    "Helligkeit": "Helligkeit",
    "hoechste": "höchste", "hoechstens": "höchstens", "Hoehe": "Höhe", "hoeher": "höher",
    "koennen": "können", "koennte": "könnte", "Koennen": "Können",
    "laeuft": "läuft", "laesst": "lässt",
    "loeschen": "löschen", "Loeschen": "Löschen", "loesche": "lösche", "loescht": "löscht",
    "moechte": "möchte", "Moechte": "Möchte",
    "moechtest": "möchtest", "Moechtest": "Möchtest",
    "moechten": "möchten", "Moechten": "Möchten",
    "moeglich": "möglich", "Moeglichkeit": "Möglichkeit", "moeglichen": "möglichen",
    "muessen": "müssen", "muesste": "müsste",
    "naechste": "nächste", "naechsten": "nächsten", "naechster": "nächster",
    "noetig": "nötig", "Noetig": "Nötig", "noetige": "nötige",
    "oeffnen": "öffnen", "Oeffnen": "Öffnen", "oeffne": "öffne", "oeffnet": "öffnet",
    "Rueckfrage": "Rückfrage", "rueckgaengig": "rückgängig",
    "schliessen": "schließen", "Schliessen": "Schließen",
    "schliesse": "schließe", "schliesst": "schließt",
    "Schluessel": "Schlüssel",
    "spaeter": "später",
    "staendig": "ständig",
    "taeglich": "täglich", "Taeglich": "Täglich",
    "tatsaechlich": "tatsächlich", "tatsaechliche": "tatsächliche",
    "ueber": "über", "Ueber": "Über", "ueberall": "überall", "Uebersicht": "Übersicht",
    "uebernommen": "übernommen", "ueberprueft": "überprüft", "ueberpruefen": "überprüfen",
    "uebrig": "übrig", "uebrigen": "übrigen",
    "unabhaengig": "unabhängig", "abhaengig": "abhängig",
    "ungueltig": "ungültig", "ungueltige": "ungültige",
    "unterstuetzt": "unterstützt", "unterstuetzen": "unterstützen",
    "unveraendert": "unverändert", "veraendert": "verändert", "veraendern": "verändern",
    "verfuegbar": "verfügbar", "Verfuegbar": "Verfügbar", "verfuegbare": "verfügbare",
    "verfuegbaren": "verfügbaren",
    "vollstaendig": "vollständig", "vollstaendige": "vollständige",
    "waehle": "wähle", "waehlen": "wählen", "waehlt": "wählt",
    "waehrend": "während",
    "waere": "wäre", "waeren": "wären",
    "woertlich": "wörtlich",
    "zunaechst": "zunächst",
    "zurueck": "zurück", "zurueckgesetzt": "zurückgesetzt",
    "zusaetzlich": "zusätzlich", "Zusaetzlich": "Zusätzlich",
    "zustaendig": "zuständig",
    # Raum- und Geraetevokabular, das auch ohne passenden HA-Namen auftauchen kann.
    "Kueche": "Küche", "Tuer": "Tür", "Tueren": "Türen", "Tuerschloss": "Türschloss",
    "Waermepumpe": "Wärmepumpe", "Anhaenger": "Anhänger", "Vorhaenge": "Vorhänge",
    "Lautstaerke": "Lautstärke", "Gaeste": "Gäste", "Gaestezimmer": "Gästezimmer",
    "Gaestemodus": "Gästemodus", "Buero": "Büro", "Flurlicht": "Flurlicht",
    "Ertraege": "Erträge", "Waermer": "Wärmer", "kuehler": "kühler", "Kuehlschrank": "Kühlschrank",
}

# Code-Zaeune und Inline-Code werden unveraendert durchgereicht (YAML-Vorschauen,
# entity_ids). Die Klammer haelt die Trenner in re.split erhalten.
_CODE_RE = re.compile(r"(```.*?```|`[^`\n]*`)", re.DOTALL)


def _build_pattern(mapping: dict[str, str]) -> re.Pattern | None:
    """Regex ueber alle Schluessel, verankert gegen Wortteile UND entity_id-Bestandteile.

    Verankerung, in drei Teilen:

    * ``(?<![\\w.])`` - davor darf weder ein Wortzeichen noch ein Punkt stehen. Das
      schuetzt den hinteren Teil einer entity_id ("light.kueche").
    * ``(?!\\w)`` - danach darf kein Wortzeichen folgen. Das schuetzt den vorderen Teil
      ("kueche_lampe"), denn der Unterstrich ist selbst ein Wortzeichen.
    * ``(?!\\.\\w)`` - ein folgender Punkt blockiert nur dann, wenn direkt ein Wortzeichen
      dahinter steht ("kueche.lampe"). Ein Satzpunkt muss durchgehen: eine pauschale
      Sperre auf jeden folgenden Punkt hat ausgerechnet den haeufigsten Fall
      uebersehen - das Wort am Satzende ("Die Automation wurde geloescht.").
    """

    if not mapping:
        return None
    alternativen = "|".join(re.escape(k) for k in sorted(mapping, key=len, reverse=True))
    return re.compile(rf"(?<![\w.])({alternativen})(?!\w)(?!\.\w)")


_WORD_PATTERN = _build_pattern(WORDS)


_TRANSLITERATION = {"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"}


def transliterate(wort: str) -> str:
    """Die ASCII-Umschreibung eines Wortes - genau die Form, die das Modell liefert."""

    for umlaut, ersatz in _TRANSLITERATION.items():
        wort = wort.replace(umlaut, ersatz)
    return wort


def build_name_map(texte) -> dict[str, str]:
    """Baut aus vorhandenen Namen die Zuordnung ASCII-Umschreibung -> korrekte Schreibweise.

    Aufgenommen wird nur, was ueberhaupt einen Umlaut enthaelt und dessen Vergleichsform
    eindeutig ist: gaebe es zwei verschieden geschriebene Namen mit derselben
    Vergleichsform, waere die Rueckbildung geraten - dann lieber nichts aendern.

    Als reine Funktion gehalten (statt direkt auf ``hass`` zu arbeiten), damit sie ohne
    laufende Home-Assistant-Instanz pruefbar ist.
    """

    kandidaten: dict[str, set[str]] = {}
    for text in texte:
        for wort in re.findall(r"[A-Za-zÄÖÜäöüß]{3,}", text or ""):
            if any(c in wort for c in "äöüÄÖÜß"):
                kandidaten.setdefault(canonical(wort), set()).add(wort)

    ergebnis = {}
    for schreibweisen in kandidaten.values():
        if len(schreibweisen) != 1:
            continue
        korrekt = next(iter(schreibweisen))
        ergebnis[transliterate(korrekt)] = korrekt
    return ergebnis


def name_map(hass: HomeAssistant) -> dict[str, str]:
    """Korrekte Schreibweisen aus dieser Home-Assistant-Installation.

    Quelle sind die Bereichsnamen und die Anzeigenamen aller Entities - dort stehen
    Umlaute bereits richtig ("Küche", "Gästemodus"), das Modell schreibt sie aber gern
    als "Kueche"/"Gaestemodus". Damit deckt die Nachbearbeitung genau die Eigennamen ab,
    um die es im Chat meistens geht, ganz ohne Wortliste.
    """

    texte = [area.name for area in ar.async_get(hass).async_list_areas()]
    texte += [state.attributes.get("friendly_name") or "" for state in hass.states.async_all()]
    return build_name_map(texte)


def normalize_umlauts(text: str, extra: dict[str, str] | None = None) -> str:
    """Schreibt ASCII-Umschreibungen im Freitext des Modells als echte Umlaute aus.

    ``extra`` sind zusaetzliche, aus Home Assistant abgeleitete Namen (siehe
    :func:`name_map`). Sie gewinnen gegen die allgemeine Wortliste, weil sie die
    tatsaechliche Schreibweise dieser Installation abbilden.

    Code-Abschnitte bleiben unangetastet - dort stehen YAML-Vorschau und entity_ids.
    """

    if not text:
        return text

    muster = [(_WORD_PATTERN, WORDS)]
    if extra:
        muster.insert(0, (_build_pattern(extra), extra))

    teile = _CODE_RE.split(text)
    for index, teil in enumerate(teile):
        if _CODE_RE.fullmatch(teil):
            continue  # Code-Block unveraendert lassen
        for pattern, mapping in muster:
            if pattern is not None:
                teil = pattern.sub(lambda m: mapping[m.group(0)], teil)
        teile[index] = teil
    return "".join(teile)
