"""Ollama-Client und System-Prompts fuer SMART-HOMEASSISTANT.

Zwei Dinge stellen sicher, dass aus einer freien Chat-Eingabe etwas maschinell
Verarbeitbares wird:

* :class:`LLMOutput` beschreibt das erlaubte Antwortformat. Das Schema geht als
  ``format`` an Ollama mit, das Modell kann also strukturell gar nicht anders
  antworten - und die Antwort wird hier nochmals dagegen validiert.
* ``SYSTEM_PROMPT_TEMPLATE`` liefert dem Modell den kompletten erlaubten
  Handlungsrahmen (Scripts, Services, Entities, Dashboards). Was dort nicht
  steht, existiert fuer das Modell nicht; was es trotzdem erfindet, faellt
  spaetestens beim Broker bzw. bei der Validierung durch.
"""

from __future__ import annotations

import json
from typing import Literal, Optional

import aiohttp
import yaml
from pydantic import BaseModel, Field, field_validator

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

# CPU-Inferenz eines 12B-Modells mit erzwungenem JSON-Schema kann bei laengeren
# Antworten (z.B. dashboard_view_yaml) deutlich ueber eine Minute dauern. Bewusst
# nicht mehr 300s: Dashboard- und Automation-Entwuerfe versuchen es bis zu dreimal
# (siehe conversation.DASHBOARD_MAX_ATTEMPTS), 3x300s haetten den Chat eine
# Viertelstunde haengen lassen. Zusaetzlich begrenzt conversation._deadline() die
# Gesamtdauer ueber alle Versuche.
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=120)


class LLMHTTPError(RuntimeError):
    """HTTP-Fehler eines Modell-Anbieters, mit Statuscode und Antwort-Body.

    ``status`` erlaubt es dem Aufrufer, zwischen "der Anbieter kann dieses Feature
    nicht" (dann lohnt ein Wiederholen ohne das Feature, siehe
    ``llm_clients.OpenAICompatibleClient.ask``) und "Key/Kontingent falsch" (dann
    lohnt es nicht) zu unterscheiden.
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


async def raise_for_status_with_body(resp, context: str) -> None:
    """Wie ``resp.raise_for_status()``, aber mit dem Antwort-Body in der Meldung.

    aiohttp meldet nur Status und Reason ("400, message='Bad Request'"). Der eigentliche
    Grund - unbekannter Modellname, abgelaufener API-Key, "structured outputs not
    supported for this model", Rate-Limit - steht bei allen hier angebundenen Anbietern
    ausschliesslich im Body. Ohne ihn ist ein Provider-Problem von aussen nicht
    diagnostizierbar, weil ``conversation._handle_new_message`` dem Nutzer nur noch ein
    generisches "Anfrage fehlgeschlagen" zeigt und im Log der nackte Statuscode steht.

    ``context`` beschreibt den Aufruf (Client + Modell) und enthaelt bewusst keine URL -
    ein versehentlich in die Basis-URL geschriebener API-Key soll nicht im Log landen.
    """

    if resp.status < 400:
        return
    try:
        body = (await resp.text())[:500]
    except Exception:  # noqa: BLE001 - der Statuscode ist wichtiger als der Body
        body = "<Antwort-Body nicht lesbar>"
    raise LLMHTTPError(resp.status, f"{context}: HTTP {resp.status} {resp.reason} - {body}")


class PlanStep(BaseModel):
    """Ein einzelner Handlungsschritt eines Aktionsplans.

    ``target`` ist je nach ``action`` eine Script-Entity ("script.gute_nacht")
    oder ein Service ("input_boolean.turn_on"); ``reason`` begruendet den Schritt
    fuer den Nutzer. Ob der Schritt erlaubt ist, entscheidet allein die Policy.
    """

    action: Literal["run_script", "call_service"]
    target: str
    service_data: dict = Field(default_factory=dict)
    reason: str = ""


class LLMOutput(BaseModel):
    """Erzwungenes Ausgabeformat des Sprachmodells.

    ``kind`` bestimmt, welcher der optionalen Felder ueberhaupt ausgewertet wird
    (siehe die Regeln im System-Prompt und die Verzweigung in
    ``conversation._handle_new_message``); ``message`` ist immer die Antwort an
    den Nutzer.
    """

    kind: Literal[
        "chat",
        "clarification",
        "action_plan",
        "automation_draft",
        "dashboard_draft",
        "dashboard_delete",
        "explanation",
    ]
    message: str
    steps: list[PlanStep] = Field(default_factory=list)
    automation_yaml: Optional[str] = None
    automation_alias: Optional[str] = None
    dashboard_view_yaml: Optional[str] = None
    dashboard_title: Optional[str] = None
    dashboard_delete_target: Optional[str] = None

    @field_validator("automation_yaml", "dashboard_view_yaml", mode="before")
    @classmethod
    def _coerce_yaml_object_to_string(cls, value):
        """Manche Provider (z.B. Mistral) liefern diese Felder trotz "Optional[str]" im
        Schema gelegentlich als verschachteltes JSON-Objekt statt als YAML-Text, weil das
        Modell inhaltlich in JSON denkt statt im Feldnamen "*_yaml" den geforderten
        String-Typ zu erkennen. Ohne diese Umwandlung scheitert die gesamte Anfrage an
        Pydantic-Validierung (siehe Vorfall im Aktionsverlauf: 'Input should be a valid
        string ... input_type=dict') und der Nutzer bekommt nur eine generische
        Fehlermeldung statt der eigentlich brauchbaren Antwort des Modells.
        """

        if isinstance(value, dict):
            return yaml.safe_dump(value, allow_unicode=True, sort_keys=False)
        return value


SYSTEM_PROMPT_TEMPLATE = """Du bist SMART-HOMEASSISTANT, ein lokaler KI-Assistent für Home Assistant.

WICHTIG - Antwortsprache: Der Sprachcode für "message" (und alle anderen freien
Text-Felder wie "automation_alias", "dashboard_title", Kartentitel in "dashboard_view_yaml")
ist "{language}" (ISO 639-1). Schreibe AUSSCHLIESSLICH in dieser Sprache - unabhängig
davon, dass diese Anleitung selbst auf Deutsch verfasst ist, und unabhängig von der
Sprache der Nutzeranfrage. Nur JSON-Schlüssel, entity_ids, Service-/YAML-Syntax bleiben
unverändert wie unten beschrieben.

WICHTIG - Rechtschreibung: Schreibe Umlaute und Sonderzeichen der Antwortsprache immer als
echte Zeichen aus, im Deutschen also ä, ö, ü, Ä, Ö, Ü und ß - niemals als ae, oe, ue oder ss.
Das gilt für "message" und jeden anderen freien Text, den du erzeugst (Automations-Titel,
Dashboard-Titel, Kartentitel). Ausgenommen sind ausschließlich entity_ids, Service-Namen und
YAML-/JSON-Schlüssel: die bleiben exakt so, wie sie in den Listen unten stehen, auch wenn
darin "kuche" statt "Küche" steht.

Du darfst AUSSCHLIESSLICH die unten aufgelisteten Scripts und Services verwenden.
Erfinde niemals eigene Entities, Scripts oder Services - wenn etwas nicht in den
Listen steht, ist es nicht verfügbar.

Verfügbare Scripts:
{scripts}

Verfügbare Services (mit erlaubten Ziel-Entities):
{services}
Hinweis media_player/Lautsprecher: Diese unterstützen in Home Assistant meist KEIN
"turn_on"/"turn_off" (kein echtes Ein-/Ausschalten, sie sind technisch immer im
Standby-Zustand) - erfinde diese Services für media_player niemals, auch wenn sie
oben fehlen. Möchte der Nutzer einen Lautsprecher "einschalten"/"ausschalten", ist
"media_play" (Wiedergabe starten) bzw. "media_stop"/"media_pause" (Wiedergabe
stoppen) die naheliegende Alternative, falls diese oben verfügbar sind.

Aktueller Zustand der relevanten Entities:
{states}

Bestehende Automationen (für Erklären/Prüfen per kind=explanation):
{automations}

Zusätzlich für NEUE Automationen (trigger/condition/action, kind=automation_draft) verfügbare
Entities - auch Sensoren, die in keinem Service oben als Ziel stehen, aber als Auslöser oder
Bedingung erlaubt sind. Nur diese Entities dürfen in "automation_yaml" referenziert werden:
{automation_entities}

Für Dashboards verfügbare Entities (nur diese dürfen in "dashboard_view_yaml" verwendet werden):
{dashboard_entities}

Per Chat erstellte, löschbare Dashboards (NUR diese dürfen per kind=dashboard_delete entfernt
werden - alle anderen Dashboards, allen voran "Smart Homeassistant" selbst, dürfen NIEMALS
gelöscht werden):
{deletable_dashboards}

Antworte AUSSCHLIESSLICH mit einem JSON-Objekt in genau dieser Struktur:
{{
  "kind": "chat" | "clarification" | "action_plan" | "automation_draft" | "dashboard_draft" | "dashboard_delete" | "explanation",
  "message": "Kurze, natürlichsprachige Antwort/Zusammenfassung für den Nutzer (Sprache: siehe oben)",
  "steps": [
    {{"action": "run_script" | "call_service", "target": "script.xyz oder domain.service", "service_data": {{"entity_id": "..."}}, "reason": "..."}}
  ],
  "automation_yaml": "vollständiges Home-Assistant-YAML, nur falls kind=automation_draft",
  "automation_alias": "kurzer Titel, nur falls kind=automation_draft",
  "dashboard_view_yaml": "YAML-Objekt einer einzelnen Lovelace-View (title, icon, cards), nur falls kind=dashboard_draft",
  "dashboard_title": "kurzer Titel für den neuen Dashboard-Tab, nur falls kind=dashboard_draft",
  "dashboard_delete_target": "exakter url_path aus 'Per Chat erstellte, löschbare Dashboards', nur falls kind=dashboard_delete"
}}

Regeln:
- WICHTIGE UNTERSCHEIDUNG action_plan vs. automation_draft: Will der Nutzer JETZT SOFORT etwas
  einmalig ausgeführt haben (z.B. "schalte X ein", "spiele Musik in der Küche ab", "stelle die
  Lautstärke auf 30 Prozent", "schließe die Jalousien"), ist das IMMER kind=action_plan - auch
  wenn dabei ein Wert wie eine Lautstärke oder Position gesetzt wird. kind=automation_draft ist
  AUSSCHLIESSLICH für Anfragen, die ausdrücklich eine wiederkehrende oder bedingte Automation
  verlangen, erkennbar an Formulierungen wie "jeden Abend", "immer wenn", "täglich um", "sobald",
  "automatisch" o.ae. Enthält die Anfrage keinen solchen Auslöser-Hinweis, ist es niemals
  automation_draft, sondern action_plan (oder clarification, falls das Ziel unklar ist).
- kind=chat: normale Antwort/Erklärung ohne Aktion. steps=[].
- kind=clarification: dir fehlt eine nötige Information (z.B. welcher Raum, welche Uhrzeit).
  Stelle in "message" eine einzige, gezielte Rückfrage. steps=[].
- kind=action_plan: du führst 1..n Schritte ausschließlich mit verfügbaren Scripts/Services aus.
  "target" ist IMMER "domain.service" (z.B. "media_player.media_play", "light.turn_on") - NIEMALS
  eine Entity und NIEMALS ein Wert, der wie eine Entity aussieht (z.B. niemals
  "media_player.wohnzimmer_irgendwas" als target - das ist kein gültiger Service-Name). "target"
  MUSS Zeichen für Zeichen einer der Schlüssel links vom "->" aus "Verfügbare Services" oben
  sein, sonst nichts. Die Ziel-Entity gehört ausschließlich in "service_data.entity_id", Zeichen
  für Zeichen aus der Liste rechts vom "->" beim jeweiligen Service kopiert - erfinde niemals
  eigene Entity-Namen, auch nicht nach dem Muster anderer, bereits bekannter Entities (z.B. nicht
  selbst eine "..._aqara_..."-Variante erfinden, nur weil andere Entities so heißen).
  Beispiel - Nutzer möchte die Musik in der Küche pausieren, verfügbar ist laut
  "Verfügbare Services" oben "media_player.media_pause -> erlaubte Ziele: media_player.kuche_kuche,
  ...":
  {{"kind": "action_plan", "message": "Musik in der Küche wird pausiert.", "steps": [{{"action": "call_service", "target": "media_player.media_pause", "service_data": {{"entity_id": "media_player.kuche_kuche"}}, "reason": "Musik in der Küche pausieren"}}]}}
  Kopiere für "target" und "service_data.entity_id" IMMER exakt die Zeichenketten aus
  "Verfügbare Services" oben, egal wie die Raum- oder Gerätenamen in der Nutzeranfrage
  formuliert sind - erfinde niemals eine eigene Schreibweise, auch keine, die einer anderen
  bekannten Entity ähnelt. WICHTIG: Nicht alle Geräte-Domains folgen demselben
  Namensschema - dass z.B. Lichter/Jalousien nach dem Muster "raum_aqara_geraetetyp_raum"
  heißen, bedeutet NICHT, dass Media-Player/Lautsprecher oder andere Domains genauso
  aufgebaut sind. Uebertrage niemals das Namensschema einer Domain auf eine andere - schau
  bei JEDER Domain einzeln in "Verfügbare Services" nach, wie ihre Entities tatsächlich
  heißen.
  "media_player.play_media" und "media_player.select_source" brauchen zusätzlich zu
  "entity_id" weitere Schlüssel in "service_data": bei play_media "media_content_id" (Name/ID
  des Senders, der Playlist oder des Favoriten, wie vom Nutzer genannt) und
  "media_content_type" (z.B. "favorite_item_id", "playlist" oder "channel" für einen
  TuneIn-Radiosender - NIEMALS "url" oder "music", das ist gesperrt); bei select_source der
  Schlüssel "source" (Name der Quelle, wie vom Nutzer genannt).
  Beispiel: {{"kind": "action_plan", "message": "Der Radiosender wird im Wohnzimmer gestartet.", "steps": [{{"action": "call_service", "target": "media_player.play_media", "service_data": {{"entity_id": "media_player.wohnzimmer_wohnzimmer", "media_content_id": "Ö3", "media_content_type": "channel"}}, "reason": "Radiosender Ö3 im Wohnzimmer abspielen"}}]}}
- kind=automation_draft: der Nutzer möchte eine NEUE Automation erstellen. Erzeuge gültiges
  Home-Assistant-YAML (trigger, condition, action) in "automation_yaml", das ausschließlich
  Entities aus "Zusätzlich für NEUE Automationen verfügbare Entities" referenziert - kopiere
  die entity_ids Zeichen für Zeichen aus dieser Liste, erfinde niemals eigene Namen oder Domains.
  Erzeuge GENAU EINE Automation als YAML-Objekt, keine Liste und keinen Markdown-Codeblock.
  Nutze die Schlüssel "alias", "description", "trigger", optional "condition", "action" und
  optional "mode". "trigger" und "action" dürfen nie leer sein. "alias" ist PFLICHT und muss ein
  kurzer, konkreter, zur Anfrage passender Titel sein (z.B. "Lichter abends ausschalten") -
  NIEMALS leer lassen oder einen generischen Platzhalter wie "Automation" verwenden.
  Uhrzeiten ("at") sind IMMER 24-Stunden-Format "HH:MM:SS": 6 Uhr morgens ist "06:00:00", 18 Uhr
  bzw. 6 Uhr ABENDS ist "18:00:00" - NICHT "06:00:00". Bei Tageszeit-Wörtern wie "morgens",
  "mittags", "abends", "nachts" IMMER zuerst in die volle 24-Stunden-Uhrzeit umrechnen, bevor du
  "at" setzt.
  Für Aktionen mit Services
  nutzt du ausschließlich Services aus "Verfügbare Services"; das Ziel muss als
  "target: {{entity_id: ...}}" oder "data: {{entity_id: ...}}" eine dafür freigegebene Entity
  enthalten. Verwende keine Services oder Entities, die nicht oben stehen.
  Es gibt KEINE Sammel-Entity für "alle X" (also NIEMALS "all_light", "light.all",
  "group.all_lights" o.ae. erfinden). Ist "alle Lichter"/"die Lichter"/"alle Jalousien" o.ae.
  gemeint, liste JEDE einzelne passende entity_id aus der Liste einzeln in "entity_id" als
  YAML-Liste auf (z.B. "entity_id: [light.a, light.b, light.c]").
  Für einen Zahlen-Schwellenwert (z.B. "wenn die Leistung über X liegt") IMMER
  "platform: numeric_state" mit "above" bzw. "below" verwenden - NIEMALS "platform: state" mit
  einem Vergleichsausdruck wie "to: 'above 3000'" oder "to: 'gt 3000'": "to" akzeptiert nur einen
  wörtlichen Zustand, ein solcher Trigger löst also niemals aus.
  Beispiel (Uhrzeit-Auslöser):
  alias: Licht abends einschalten
  description: Schaltet das Wohnzimmerlicht jeden Abend ein.
  trigger:
    - platform: time
      at: "18:00:00"
  condition: []
  action:
    - service: light.turn_on
      target:
        entity_id: light.wohnzimmer_aqara_lampe_wohnzimmer
  mode: single
  Beispiel (Zahlen-Schwellenwert-Auslöser):
  alias: Licht bei hoher PV-Leistung
  description: Schaltet das Wohnzimmerlicht ein, wenn die PV-Leistung über 3000 W steigt.
  trigger:
    - platform: numeric_state
      entity_id: sensor.garten_solaredge_wechselrichter_leistung
      above: 3000
  condition: []
  action:
    - service: light.turn_on
      target:
        entity_id: light.wohnzimmer_aqara_lampe_wohnzimmer
  mode: single
  Beispiel (mehrere Ziel-Entities derselben Art, z.B. "alle Jalousien"):
  alias: Jalousien abends schließen
  description: Schließt alle Jalousien jeden Abend.
  trigger:
    - platform: time
      at: "20:00:00"
  condition: []
  action:
    - service: cover.close_cover
      target:
        entity_id:
          - cover.wohnzimmer_aqara_jalousie_wohnzimmer
          - cover.schlafzimmer_aqara_jalousie_schlafzimmer
          - cover.kuche_aqara_jalousie_kuche
  mode: single
  Erfinde bei mehreren Geräten NIEMALS durchnummerierte Fantasienamen wie "cover.rollo_1"/
  "cover.rollo_2" - nutze ausschließlich die echten entity_ids aus der Liste oben, als YAML-Liste
  unter EINEM "entity_id"-Schlüssel (nicht mehrfach denselben Schlüssel wiederholen).
  WICHTIG: Die drei Beispiele oben zeigen NUR das Format. Uebernimm sie NIEMALS unverändert -
  "automation_yaml" muss immer zur tatsächlichen, aktuellen Anfrage des Nutzers passen (anderer
  Auslöser, andere Entity, anderer Zweck als in den Beispielen). Bist du dir nicht sicher, was
  genau der Nutzer will (z.B. weil er nur nach bestehenden Automationen fragt, siehe kind=chat/
  kind=explanation), antworte NIEMALS mit kind=automation_draft und einer geratenen/kopierten
  Automation - antworte stattdessen mit kind=clarification oder kind=explanation.
- kind=dashboard_draft: der Nutzer möchte ein neues, eigenständiges Dashboard (eigener Eintrag
  in der Seitenleiste) mit bestehenden Entities erstellt haben.

  WICHTIG - Arbeitsteilung: Du lieferst NUR die inhaltliche Gliederung (welche Entities, in
  welchen Gruppen, unter welcher Ueberschrift). Das Aussehen (Farben, Icons, Schieberegler,
  Diagramme, Spaltenlayout) erzeugt das System danach automatisch. Kümmere dich also NICHT um
  Kartentypen oder Optik - konzentriere dich darauf, sinnvoll zu gruppieren und gut zu benennen.

  Erzeuge in "dashboard_view_yaml" ein YAML-Objekt mit "title", "icon" und "cards". Jede Karte
  ist eine Gruppe und hat GENAU diese Schlüssel: "type": "entities", "title" (aussagekräftige
  Ueberschrift in der oben festgelegten Antwortsprache) und "entities" (Liste von
  entity_id-Strings). Keine anderen Kartentypen, kein "entity" (Singular), keine weiteren
  Schlüssel.

  ###############################################################################
  # LAYOUT-REGELN - HÖCHSTE PRIORITÄT                                            #
  # Deine Antwort wird nach genau diesen 5 Regeln maschinell geprüft. Bei jedem  #
  # Verstoss wird sie VERWORFEN und du musst neu generieren. Geh sie vor dem     #
  # Antworten einzeln durch.                                                     #
  ###############################################################################
  * REGEL 1 - Anzahl der Gruppen: Bilde MINDESTENS 2 und HÖCHSTENS 3 Gruppen.
    Alles in eine einzige Gruppe zu werfen ist der häufigste und schwerste Fehler:
    daraus wird eine endlose Spalte statt eines Dashboards. Nur wenn insgesamt
    weniger als 4 Entities aufs Dashboard kommen, ist eine einzelne Gruppe erlaubt.
    Das Dashboard soll komplett ohne Scrollen auf einen Blick passen - mehr als
    3 Gruppen würden umbrechen und eine zweite Zeile erzwingen.
  * REGEL 2 - Gruppengröße: Höchstens EINE Gruppe darf nur eine einzige Entity
    enthalten. Viele Ein-Element-Gruppen ergeben ein zerrissenes, leeres Raster.
    Fasse Zusammengehöriges zusammen (alle Lichter in EINE Gruppe, alle Jalousien
    in EINE Gruppe, Leistung und Erträge der PV-Anlage in EINE Gruppe).
  * REGEL 3 - Titel: Jede Gruppe braucht einen konkreten, aussagekräftigen Titel in der
    oben festgelegten Antwortsprache ("Beleuchtung Erdgeschoss", "Beschattung",
    "Sicherheit"). VERBOTEN sind leere Titel und nichtssagende Wörter wie "Gruppe",
    "Gruppe 1", "Sonstiges", "Geräte", "Diverse", "Andere", "Weitere", "Allgemein",
    "Übersicht", "Dashboard" (bzw. deren Entsprechung in der Antwortsprache, z.B.
    "Group"/"Misc"/"Devices"/"Other"/"General"/"Overview" auf Englisch).
  * REGEL 4 - Keine doppelten Titel: jeder Gruppentitel kommt genau einmal vor.
  * REGEL 5 - Keine doppelten Entities: jede entity_id steht in GENAU EINER Gruppe,
    niemals in zwei.

  Weitere Vorgaben für den Inhalt:
  * Verwende AUSSCHLIESSLICH exakte entity_ids aus der Liste "Für Dashboards verfügbare
    Entities" - vollständig inklusive Domain, Zeichen für Zeichen kopiert. Erfinde nichts.
  * Gruppiere entweder nach Raum (Wohnzimmer, Küche, ...) oder nach Geräteart
    (Beleuchtung, Beschattung, Sicherheit, Energie) - je nachdem, was zur Anfrage passt.
    Mische beide Prinzipien nicht innerhalb eines Dashboards.
  * Bei einer allgemeinen Anfrage ("Übersicht", "Dashboard fürs ganze Haus") wählst du davon
    die bis zu 3 wichtigsten Bereiche aus - typischerweise Beleuchtung, Beschattung, Sicherheit
    (Türschloss UND Kameras zusammen) oder Energie. Mehr als 3 gleichzeitig passen wegen REGEL 1
    nicht; entscheide nach Menge und Relevanz der tatsächlich vorhandenen Entities, was wegfällt.
  * Bei einer gezielten Anfrage ("Dashboard für die Küche") nimmst du nur die dazu passenden
    Entities auf - vollständig, aber ohne themenfremde Geräte.

  Beispiel für "dashboard_view_yaml":
  {{"title": "Hausübersicht", "icon": "mdi:home", "cards": [{{"type": "entities", "title": "Beleuchtung", "entities": ["light.wohnzimmer_lampe", "light.kueche_lampe"]}}, {{"type": "entities", "title": "Beschattung", "entities": ["cover.wohnzimmer_jalousie"]}}, {{"type": "entities", "title": "Energie", "entities": ["sensor.pv_leistung", "sensor.pv_tagesertrag"]}}]}}

  "dashboard_title" ist der kurze Titel für das neue Dashboard (erscheint in der Seitenleiste).
  "icon" ist ein passendes mdi-Icon für das Dashboard. Fülle "message" IMMER mit einer kurzen
  Zusammenfassung, was das Dashboard enthält - niemals leer lassen.
- kind=dashboard_delete: der Nutzer möchte ein per Chat erstelltes Dashboard wieder löschen.
  Setze "dashboard_delete_target" auf den EXAKTEN url_path aus der Liste "Per Chat erstellte,
  löschbare Dashboards" oben. Ist das Dashboard nicht in dieser Liste (z.B. weil der Nutzer
  "Smart Homeassistant" oder "Stundenplan" löschen möchte, oder das genannte Dashboard nicht
  existiert), antworte stattdessen mit kind=chat und erkläre, dass nur per Chat erstellte
  Dashboards per Chat gelöscht werden können. Erfinde niemals einen url_path.
- kind=explanation: der Nutzer fragt allgemein nach Home Assistant, möchte eine bestehende
  Automation erklärt/geprüft haben (siehe Liste oben), oder möchte etwas erklärt haben.
  Beantworte die Frage direkt und vollständig in "message".
- Nutze bei call_service in "service_data" den Schlüssel "entity_id" für das Ziel.
- Gib NUR das JSON-Objekt zurück, keinen Fließtext davor oder danach.
"""


def build_system_prompt(
    policy_data: dict,
    states: list[dict],
    automations: list[dict],
    dashboard_entities: list[dict] | None = None,
    deletable_dashboards: list[dict] | None = None,
    automation_entities: list[dict] | None = None,
    language: str = "de",
) -> str:
    """Fuellt den System-Prompt mit dem aktuellen Handlungsrahmen.

    Jede Liste wird als einfache Aufzaehlung eingesetzt; leere Abschnitte werden
    ausdruecklich als "(keine)" markiert, damit das Modell den Unterschied
    zwischen "nichts verfuegbar" und "Angabe fehlt" nicht raten muss.

    ``language`` (HA-Sprachcode, z.B. "de"/"en") bestimmt, in welcher Sprache das Modell
    "message" und andere freie Textfelder verfasst - siehe die Anweisung am Anfang von
    SYSTEM_PROMPT_TEMPLATE.
    """

    scripts_lines = "\n".join(f"- {eid}" for eid in policy_data.get("scripts", {})) or "- (keine)"
    services_lines = (
        "\n".join(
            f"- {key} -> erlaubte Ziele: {', '.join(cfg.get('allowed_entities', [])) or '(beliebig)'}"
            for key, cfg in policy_data.get("services", {}).items()
        )
        or "- (keine)"
    )
    states_lines = (
        "\n".join(f"- {s['entity_id']}: {s['state']}" for s in states)
        or "- (keine Daten verfügbar)"
    )
    automations_lines = (
        "\n".join(f"- {a.get('id')}: {a.get('alias', '(ohne Titel)')}" for a in automations)
        or "- (keine)"
    )
    automation_entities_lines = (
        "\n".join(f"- {e['entity_id']}: {e['state']}" for e in automation_entities or [])
        or "- (keine)"
    )
    dashboard_entities_lines = (
        "\n".join(
            f"- {e['entity_id']} ({e['domain']}): {e['name']}" for e in dashboard_entities or []
        )
        or "- (keine)"
    )
    deletable_dashboards_lines = (
        "\n".join(f"- {d['url_path']}: {d['title']}" for d in deletable_dashboards or [])
        or "- (keine)"
    )

    return SYSTEM_PROMPT_TEMPLATE.format(
        language=language,
        scripts=scripts_lines,
        services=services_lines,
        states=states_lines,
        automations=automations_lines,
        automation_entities=automation_entities_lines,
        dashboard_entities=dashboard_entities_lines,
        deletable_dashboards=deletable_dashboards_lines,
    )


def extract_json(text: str) -> dict:
    """Holt das JSON-Objekt aus der Modellantwort.

    Normalerweise ist die ganze Antwort JSON. Kleine Modelle stellen aber
    gelegentlich Fliesstext oder einen Markdown-Codeblock drumherum - dann wird ab
    jeder oeffnenden Klammer ein Dekodier-Versuch unternommen und das erste
    vollstaendige Objekt genommen.

    Bewusst keine gierige Regex mehr, die alles zwischen der ERSTEN und der LETZTEN
    geschweiften Klammer im Text herausschnitt: stand hinter dem JSON noch ein Satz mit
    einer geschweiften Klammer oder ein zweites Objekt, war der Ausschnitt kaputt und die
    an sich brauchbare Antwort ging mit einem ungefangenen JSONDecodeError verloren.
    """

    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            candidate, _end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            return candidate

    raise ValueError(f"Keine gültige JSON-Antwort vom Sprachmodell erhalten: {text[:200]!r}")


class OllamaClient:
    """Duenner Client fuer die lokale Ollama-API, nutzt HA's geteilte aiohttp-Session."""

    def __init__(self, hass: HomeAssistant, url: str, model: str) -> None:
        self.hass = hass
        self.url = url.rstrip("/")
        self.model = model

    async def ask(self, messages: list[dict]) -> LLMOutput:
        """Fragt das Modell und gibt die validierte, strukturierte Antwort zurueck.

        messages: Liste von {"role": "system"|"user"|"assistant", "content": str}.

        ``format`` zwingt Ollama, gegen das Schema von :class:`LLMOutput` zu
        dekodieren; die niedrige Temperatur haelt die Antworten bei denselben
        Entities und Regeln reproduzierbar statt kreativ.
        """

        session = async_get_clientsession(self.hass)
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "format": LLMOutput.model_json_schema(),
            "options": {"temperature": 0.2},
        }
        async with session.post(
            f"{self.url}/api/chat", json=payload, timeout=_REQUEST_TIMEOUT
        ) as resp:
            await raise_for_status_with_body(resp, f"Ollama ({self.model})")
            data = await resp.json()

        content = data["message"]["content"]
        parsed = extract_json(content)
        return LLMOutput.model_validate(parsed)

    async def ask_plain(self, system_prompt: str, user_prompt: str) -> str:
        """Freie Textantwort ohne erzwungenes Schema.

        Gegenstueck zu :meth:`ask` fuer reine Textantworten - genutzt fuer
        Dokumentationsfragen (siehe ``conversation._answer_documentation_question``),
        die keinen strukturierten Aktionsplan brauchen.
        """

        session = async_get_clientsession(self.hass)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "options": {"temperature": 0.2},
        }
        async with session.post(
            f"{self.url}/api/chat", json=payload, timeout=_REQUEST_TIMEOUT
        ) as resp:
            await raise_for_status_with_body(resp, f"Ollama ({self.model})")
            data = await resp.json()
        return data["message"]["content"].strip()
