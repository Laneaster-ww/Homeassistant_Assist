"""Konstanten der SMART-HOMEASSISTANT Integration."""

DOMAIN = "smart_homeassistant"

# --- Schluessel der Config-Entry-Daten bzw. -Optionen (siehe config_flow.py) ---
CONF_OLLAMA_URL = "ollama_url"
CONF_OLLAMA_MODEL = "ollama_model"
CONF_CONFIRMATION_TTL = "confirmation_ttl_minutes"

# --- Vorbelegung des Einrichtungsformulars ---
# host.docker.internal: HA laeuft im Container, Ollama daneben auf dem Host.
DEFAULT_OLLAMA_URL = "http://host.docker.internal:11434"
DEFAULT_OLLAMA_MODEL = "llama3.1:8b"
# Solange darf eine offene Rueckfrage ("... fortfahren? (ja/nein)") beantwortet
# werden; danach verfaellt sie und muss neu angestossen werden.
DEFAULT_CONFIRMATION_TTL_MINUTES = 15

# Liegt im HA-Konfigurationsverzeichnis neben configuration.yaml.
POLICY_FILENAME = "smart_homeassistant_policy.yaml"

# --- Schluessel innerhalb von hass.data[DOMAIN][entry_id] (siehe __init__.py) ---
DATA_SESSIONS = "sessions"
DATA_POLICY = "policy"
DATA_PROVIDER_STORE = "provider_store"
DATA_ACTION_LOG = "action_log"
DATA_FILE_LOCK = "file_lock"
DATA_OFFLINE_DOCS = "offline_docs"

# Der Aktionsverlauf ist eine Anzeigehilfe, kein Archiv: aeltere Eintraege
# fallen hinten raus, damit die Sensor-Attribute klein bleiben.
ACTION_LOG_MAX_ENTRIES = 20


def action_log_signal(entry_id: str) -> str:
    """Dispatcher-Signal, mit dem ein neuer Verlaufseintrag angekuendigt wird.

    Der Aktionsverlauf liegt in ``hass.data`` und wird von der Conversation-Entity
    fortgeschrieben; der Sensor in sensor.py haengt sich daran, statt eine Liste im
    Speicher im Sekundentakt abzufragen.
    """

    return f"{DOMAIN}_action_log_{entry_id}"


def get_option(entry, key: str, default):
    """Optionen ueberschreiben die urspruenglichen Config-Flow-Daten."""

    return entry.options.get(key, entry.data.get(key, default))


# Offizielle Quellen fuer den Dokumentationsmodus (siehe docs_knowledge.py). Einzige
# Quelle dieser Liste: sowohl der "source_urls"-Abschnitt unten als auch
# docs_knowledge.docs_policy_from_data() (Fallback, falls die Policy-Datei den
# Abschnitt nicht enthaelt) greifen auf dieselbe Liste zurueck.
DEFAULT_DOCS_URLS = [
    "https://www.home-assistant.io/docs/",
    "https://www.home-assistant.io/integrations/conversation/",
    "https://developers.home-assistant.io/docs/intent_conversation_api/",
    "https://developers.home-assistant.io/docs/voice/overview/",
    "https://developers.home-assistant.io/docs/creating_integration_manifest/",
]
_DOCS_SOURCE_URLS_YAML = "\n".join(f"    - {url}" for url in DEFAULT_DOCS_URLS)

# Wird beim ersten Start als Policy-Datei angelegt, falls noch keine existiert
# (siehe policy.Policy.async_load). Danach ist die Datei Sache des Nutzers und
# wird nie wieder ueberschrieben.
DEFAULT_POLICY_YAML = f"""\
# Sicherheits-Policy fuer den Action Broker.
#
# Nur hier eingetragene Scripts/Services darf die KI ausfuehren, und auch nur
# auf die hier freigegebenen Ziel-Entities. Alles andere lehnt der Broker ab,
# unabhaengig davon, was die KI vorschlaegt.
#
# Passe diese Datei an deine tatsaechlichen Home-Assistant-Entities an.

scripts:
  script.gute_nacht:
    confirmation_required: true
  script.filmabend:
    confirmation_required: true
  script.haus_verlassen:
    confirmation_required: true
  script.gaestemodus:
    confirmation_required: false
  script.alle_lichter_aus:
    confirmation_required: false
  script.morgenroutine:
    confirmation_required: true

services:
  input_boolean.turn_on:
    confirmation_required: false
    allowed_entities:
      - input_boolean.licht_wohnzimmer
      - input_boolean.licht_schlafzimmer
      - input_boolean.licht_flur
      - input_boolean.licht_kueche
      - input_boolean.nachtmodus
      - input_boolean.gaestemodus
      - input_boolean.medienwiedergabe
  input_boolean.turn_off:
    confirmation_required: false
    allowed_entities:
      - input_boolean.licht_wohnzimmer
      - input_boolean.licht_schlafzimmer
      - input_boolean.licht_flur
      - input_boolean.licht_kueche
      - input_boolean.nachtmodus
      - input_boolean.gaestemodus
      - input_boolean.medienwiedergabe
  input_number.set_value:
    confirmation_required: false
    allowed_entities:
      - input_number.helligkeit_wohnzimmer
      - input_number.helligkeit_schlafzimmer

# Policy fuer per KI neu erstellte Automationen (Feature "Automatisierungen aus
# natuerlicher Sprache erstellen"). Nur Automationen, die ausschliesslich diese
# Entities referenzieren, werden nach Bestaetigung tatsaechlich aktiviert.
automations:
  create:
    confirmation_required: true
    allowed_entities:
      - input_boolean.licht_wohnzimmer
      - input_boolean.licht_schlafzimmer
      - input_boolean.licht_flur
      - input_boolean.licht_kueche
      - input_boolean.nachtmodus
      - input_boolean.medienwiedergabe
      - input_number.helligkeit_wohnzimmer
      - input_number.helligkeit_schlafzimmer

# Dokumentationsmodus fuer Fragen zu Home-Assistant-Funktionen. Wenn eine Frage
# als Dokumentationsfrage erkannt wird, darf die Antwort ausschliesslich aus der
# Offline-Dokumentation bzw. den hier gelisteten offiziellen Quellen abgeleitet
# werden. Reicht die gespeicherte Doku nicht aus, muss die KI das sagen statt zu
# raten.
documentation:
  enabled: true
  strict_docs_only: true
  offline_path: custom_components/smart_homeassistant/homeassistant_docs_offline.md
  source_urls:
{_DOCS_SOURCE_URLS_YAML}
"""
