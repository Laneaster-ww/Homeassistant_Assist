"""Uebersetzungen der wenigen vom Code selbst formulierten Chat-Texte.

Der weitaus groesste Teil jeder Antwort kommt direkt vom Sprachmodell und folgt der
Sprache ueber die Prompt-Anweisung in ``ollama_client.SYSTEM_PROMPT_TEMPLATE`` bzw.
``docs_knowledge.DOCS_ONLY_SYSTEM_PROMPT`` (beide bekommen die Zielsprache als
Platzhalter) - ein aktuelles Sprachmodell schreibt praktisch jede Sprache, dafuer
braucht es keine Uebersetzungstabelle.

Dieses Modul deckt nur den kleinen Rest ab, den der Code selbst formuliert (feste
Bestaetigungsfragen wie "Soll ich fortfahren? (ja/nein)", Fehlermeldungen, ...) - diese
Texte muessen ebenfalls zur eingestellten Sprache passen, sonst wirkt die Antwort trotz
Punkt 1 gemischt.

Explizit uebersetzt sind nur Deutsch und Englisch. Jede andere in Home Assistant
konfigurierte Sprache faellt fuer GENAU DIESE Texte auf Englisch zurueck - ein
verstaendlicherer Standard-Fallback als stur bei Deutsch zu bleiben, auch wenn es nicht
die perfekte Uebersetzung fuer jede Sprache ist (siehe :func:`t`).
"""

from __future__ import annotations

_TRANSLATIONS: dict[str, dict[str, str]] = {
    "yes_no_prompt": {
        "de": "Bitte antworte mit ja oder nein.",
        "en": "Please answer with yes or no.",
    },
    "cancelled": {
        "de": "Abgebrochen.",
        "en": "Cancelled.",
    },
    "done": {
        "de": "Erledigt.",
        "en": "Done.",
    },
    "continue_question": {
        "de": "Soll ich fortfahren? (ja/nein)",
        "en": "Should I proceed? (yes/no)",
    },
    "activate_automation_question": {
        "de": "Soll ich diese Automation aktivieren? (ja/nein)",
        "en": "Should I activate this automation? (yes/no)",
    },
    "create_dashboard_question": {
        "de": "Soll ich das Dashboard anlegen? (ja/nein)",
        "en": "Should I create the dashboard? (yes/no)",
    },
    "delete_dashboard_warning": {
        "de": (
            "⚠️ Achtung: Das Dashboard '{title}' wird dauerhaft gelöscht (Konfiguration "
            "und Datei) - das kann NICHT rückgängig gemacht werden. Wirklich löschen? "
            "(ja/nein)"
        ),
        "en": (
            "⚠️ Warning: The dashboard '{title}' will be permanently deleted (configuration "
            "and file) - this CANNOT be undone. Really delete it? (yes/no)"
        ),
    },
    "restarting": {
        "de": "Home Assistant wird jetzt neu gestartet ...",
        "en": "Home Assistant is restarting now ...",
    },
    "restart_after_dashboard_create_question": {
        "de": (
            "Dashboard '{title}' wurde angelegt. Damit es in der Seitenleiste erscheint, "
            "ist ein Neustart nötig. Soll ich Home Assistant jetzt neu starten? (ja/nein)"
        ),
        "en": (
            "Dashboard '{title}' has been created. A restart is needed for it to appear in "
            "the sidebar. Should I restart Home Assistant now? (yes/no)"
        ),
    },
    "restart_after_dashboard_delete_question": {
        "de": (
            "Dashboard '{title}' wurde gelöscht. Damit es aus der Seitenleiste "
            "verschwindet, ist ein Neustart nötig. Soll ich Home Assistant jetzt neu "
            "starten? (ja/nein)"
        ),
        "en": (
            "Dashboard '{title}' has been deleted. A restart is needed for it to disappear "
            "from the sidebar. Should I restart Home Assistant now? (yes/no)"
        ),
    },
    "dashboard_created": {
        "de": "Dashboard '{title}' wurde angelegt.",
        "en": "Dashboard '{title}' has been created.",
    },
    "dashboard_deleted": {
        "de": "Dashboard '{title}' wurde gelöscht.",
        "en": "Dashboard '{title}' has been deleted.",
    },
    "automation_activated": {
        "de": "Automation '{alias}' wurde aktiviert.",
        "en": "Automation '{alias}' has been activated.",
    },
    "unknown_action": {
        "de": "Unbekannte Aktion.",
        "en": "Unknown action.",
    },
    "error_prefix": {
        "de": "Fehler: {error}",
        "en": "Error: {error}",
    },
    "request_failed": {
        "de": "Entschuldigung, ich konnte die Anfrage nicht verarbeiten.",
        "en": "Sorry, I couldn't process the request.",
    },
    "docs_question_failed": {
        "de": "Entschuldigung, ich konnte die Dokumentationsfrage nicht verarbeiten.",
        "en": "Sorry, I couldn't process the documentation question.",
    },
    "docs_not_found": {
        "de": (
            "Dazu finde ich in der gespeicherten Home-Assistant-Dokumentation keine "
            "ausreichende Information."
        ),
        "en": (
            "I couldn't find enough information about that in the stored Home Assistant "
            "documentation."
        ),
    },
    "no_automations": {
        "de": "Aktuell sind keine Automationen eingerichtet.",
        "en": "No automations are currently set up.",
    },
    "automations_list_header": {
        "de": "Aktuell eingerichtete Automationen:",
        "en": "Currently configured automations:",
    },
    "no_title": {
        "de": "(ohne Titel)",
        "en": "(untitled)",
    },
    "automation_draft_created": {
        "de": "Automation-Entwurf erstellt.",
        "en": "Automation draft created.",
    },
    "retry_hint": {
        "de": (
            "Magst du es nochmal versuchen, am besten mit konkreteren Angaben (z.B. genaue "
            "Geräte oder Uhrzeit)?"
        ),
        "en": "Would you like to try again, ideally with more specific details (e.g. exact devices or time)?",
    },
    "rejection_invalid_yaml": {
        "de": "ich konnte daraus keine gültige Automation erzeugen",
        "en": "I couldn't turn that into a valid automation",
    },
    "rejection_entity_not_allowed": {
        "de": "dabei wären Geräte verwendet worden, die dafür nicht freigegeben sind",
        "en": "that would have used devices that aren't approved for this",
    },
    "rejection_service_not_allowed": {
        "de": "dabei wäre eine Aktion verwendet worden, die dafür nicht freigegeben ist",
        "en": "that would have used an action that isn't approved for this",
    },
    "rejection_invalid_homeassistant_automation": {
        "de": "Home Assistant hat den Entwurf als ungültig abgelehnt",
        "en": "Home Assistant rejected the draft as invalid",
    },
    "rejection_example_copied": {
        "de": "mir ist dabei kein zu deiner Anfrage passender Entwurf gelungen",
        "en": "I wasn't able to come up with a draft that matched your request",
    },
    "rejection_generic": {
        "de": "das hat leider nicht funktioniert",
        "en": "unfortunately that didn't work",
    },
    "abgelehnt_prefix": {
        "de": "(Abgelehnt: {reason})",
        "en": "(Rejected: {reason})",
    },
    "not_a_deletable_dashboard": {
        "de": "'{target}' ist kein per Chat erstelltes, löschbares Dashboard.",
        "en": "'{target}' is not a dashboard created via chat that can be deleted.",
    },
    "not_found_short": {
        "de": "nicht gefunden",
        "en": "not found",
    },
    "steps_preview_header": {
        "de": "Geplante Schritte:",
        "en": "Planned steps:",
    },
    "confirmation_expired": {
        "de": (
            "Die offene Rückfrage ist abgelaufen und wurde verworfen - es wurde nichts "
            "ausgeführt. Bitte stelle die Anfrage noch einmal."
        ),
        "en": (
            "The pending confirmation has expired and was discarded - nothing was executed. "
            "Please ask again."
        ),
    },
    "plan_partially_executed": {
        "de": "Achtung: {done} von {total} Schritten wurden bereits ausgeführt.",
        "en": "Careful: {done} of {total} steps had already been executed.",
    },
    "context_failed": {
        "de": (
            "Ich konnte den aktuellen Stand von Home Assistant nicht auslesen. Bitte prüfe "
            "automations.yaml und configuration.yaml auf Fehler - Details stehen im Log."
        ),
        "en": (
            "I could not read the current Home Assistant state. Please check automations.yaml "
            "and configuration.yaml for errors - details are in the log."
        ),
    },
    "insecure_endpoint": {
        "de": (
            "Der API-Key wuerde unverschluesselt an '{host}' gesendet - das wurde "
            "verhindert. Bitte die Server-Adresse des Modells auf https:// umstellen "
            "(Chat-Fenster, 'Modell wechseln')."
        ),
        "en": (
            "The API key would be sent unencrypted to '{host}' - this was blocked. "
            "Please change the model's server address to https:// ('Switch model' in "
            "the chat window)."
        ),
    },
    "dashboard_areas_preview": {
        "de": "Bereiche im Dashboard: {areas}",
        "en": "Areas in the dashboard: {areas}",
    },
    "unknown_areas_note": {
        "de": "(Hinweis: diese Bereiche gibt es nicht und wurden weggelassen: {areas})",
        "en": "(Note: these areas do not exist and were left out: {areas})",
    },
    "no_provider_configured": {
        "de": (
            "Es ist noch kein KI-Modell eingerichtet. Füge im Chat-Fenster über 'Modell "
            "wechseln' zuerst eines hinzu."
        ),
        "en": "No AI model is set up yet. Add one first via 'Switch model' in the chat window.",
    },
}


def t(key: str, language: str | None, **kwargs) -> str:
    """Uebersetzt einen der wenigen vom Code selbst erzeugten Texte.

    ``language`` ist ein HA-Sprachcode ("de", "en", "en-GB", ...) - nur der Teil vor
    einem eventuellen Regions-Suffix wird verglichen. Faellt bei unbekannter Sprache auf
    Englisch zurueck, bei komplett fehlendem Schluessel auf den Schluessel selbst (faellt
    im Test auf statt eine Exception auszuloesen).
    """

    entry = _TRANSLATIONS.get(key, {})
    lang = (language or "de").split("-")[0].lower()
    template = entry.get(lang) or entry.get("en") or entry.get("de") or key
    return template.format(**kwargs) if kwargs else template
