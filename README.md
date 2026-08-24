# Smart Homeassistant

Ein lokaler KI-Assistent als Home-Assistant-Integration: freie Spracheingabe im Chat,
aber **keine Aktion ohne Freigabe**. Das Sprachmodell schlägt nur vor — was tatsächlich
ausgeführt wird, entscheidet allein eine Whitelist.

Unterstützte Modell-Anbindungen: **Ollama** (lokal), **OpenAI-kompatibel** (OpenAI,
OpenRouter, Mistral, LM Studio), **Anthropic**, **Google Gemini**.

## Was der Assistent kann

| Fähigkeit | Beispiel |
|---|---|
| Geräte schalten | „Schalte das Licht im Wohnzimmer ein" |
| Automationen erzeugen | „Schließe die Jalousien jeden Abend um 20 Uhr" |
| Dashboards erzeugen | „Erstelle ein Dashboard für die Küche" |
| Dashboards löschen | nur solche, die per Chat entstanden sind |
| Bestehendes erklären | „Welche Automationen gibt es?" |
| Doku beantworten | ausschließlich aus einem Offline-Schnappschuss der HA-Doku |

## Sicherheitsmodell

Alles läuft durch eine einzige Engstelle. Das Modell kann nichts ausführen — es kann nur
etwas vorschlagen.

```
Nutzereingabe
   ↓
Conversation-Agent            Vorfilter ohne LLM (ja/nein, Dokufrage, Statusfrage)
   ↓
System-Prompt                 nur freigegebene Scripts, Services, Entities
   ↓
Sprachmodell                  erzwungenes JSON-Schema (LLMOutput)
   ↓
Action Broker                 prüft JEDEN Schritt gegen smart_homeassistant_policy.yaml
   ↓
Rückfrage (ja/nein)           bei allem Eingreifenden, mit Vorschau der echten Ziele
   ↓
Ausführung
```

Konkret:

* **Whitelist statt Blacklist.** Nur Scripts, Services und Ziel-Entities, die in
  `smart_homeassistant_policy.yaml` stehen, sind überhaupt erreichbar.
* **Doppelte Prüfung.** Eine bestätigte Aktion wird unmittelbar vor der Ausführung erneut
  geprüft — die Policy-Datei kann sich in der Zwischenzeit geändert haben.
* **Nicht prüfbare Ziele werden abgelehnt.** `device_id`, `area_id`, `label_id` und
  `floor_id` treffen ganze Räume auf einmal und lassen sich gegen eine Entity-Whitelist
  nicht prüfen.
* **Geratene Entities erzwingen eine Rückfrage.** Musste eine erfundene Entity-ID
  zurechtgebogen werden, wird immer nachgefragt — auch bei `confirmation_required: false`.
  Die Rückfrage zeigt das tatsächlich aufgelöste Ziel, nicht den Text des Modells.
* **Rückfragen verfallen.** Nach der eingestellten TTL (Standard 15 Minuten) wird eine
  offene Rückfrage verworfen statt später überraschend ausgeführt.
* **Keine beliebigen Stream-URLs.** `media_player.play_media` akzeptiert nur kuratierte
  Inhalte, kein `media_content_type: url`.

## Installation

1. `custom_components/smart_homeassistant/` in das Home-Assistant-Konfigurationsverzeichnis
   kopieren.
2. Home Assistant neu starten.
3. *Einstellungen → Geräte & Dienste → Integration hinzufügen → Smart Homeassistant*.
4. Beim ersten Start wird `smart_homeassistant_policy.yaml` neben `configuration.yaml`
   angelegt. **Diese Datei an die eigenen Entities anpassen** — sie entscheidet, was der
   Assistent anfassen darf. Ein Beispiel liegt unter `examples/`.
5. Modell hinterlegen: im Chat-Fenster über „Modell wechseln", oder über den Dienst
   `smart_homeassistant.add_model_provider`.

Zum Ausprobieren mit Docker liegt eine `docker-compose.yml` bei.

## Dienste

| Dienst | Zweck |
|---|---|
| `smart_homeassistant.reload_policy` | Policy-Datei neu einlesen |
| `smart_homeassistant.refresh_documentation` | Offline-Doku von den offiziellen Quellen aktualisieren |
| `smart_homeassistant.add_model_provider` | Modell-Anbindung anlegen |
| `smart_homeassistant.update_model_provider` | Modell-Anbindung ändern |
| `smart_homeassistant.remove_model_provider` | Modell-Anbindung entfernen |
| `smart_homeassistant.list_model_providers` | Anbindungen auflisten (ohne API-Keys) |
| `smart_homeassistant.set_conversation_model` | Modell für ein laufendes Gespräch setzen |

## Aufbau

| Modul | Aufgabe |
|---|---|
| `conversation.py` | Chat-Einstiegspunkt, führt alles zusammen |
| `policy.py` | Whitelist: was darf die KI überhaupt anfassen |
| `broker.py` | Sicherheitsschicht: prüft und führt einzelne Aktionen aus |
| `providers.py` | Verwaltung mehrerer Modell-Anbindungen |
| `llm_clients.py` | Provider-übergreifende Clients (Ollama, OpenAI, Anthropic, Gemini) |
| `ollama_client.py` | Ollama-Client, Antwortschema `LLMOutput`, System-Prompt |
| `automations.py` | Automationen prüfen und aktivieren |
| `dashboard.py` | Dashboards anlegen und löschen (Inhalt: HA-Bereichsstrategie) |
| `docs_knowledge.py` | Offline-Doku und Abruf für Dokumentationsfragen |
| `entity_filter.py` | Relevanz-Vorauswahl der Entity-Listen im Prompt |
| `text_format.py` | Umlaut-Nachbearbeitung der Modellantworten |
| `i18n.py` | die wenigen vom Code selbst formulierten Texte (de/en) |

## Warum das Modell nicht frei entscheiden darf

Kleine lokale Modelle treffen Entity-IDs nicht zuverlässig, verwechseln „jetzt ausführen"
mit „Automation anlegen" und geben unter Unsicherheit gern das Beispiel aus dem
System-Prompt zurück. Die Integration begegnet dem an mehreren Stellen: exakte Auflösung
von Entity-IDs, Erkennung kopierter Prompt-Beispiele, deterministische Antworten für
Statusfragen ganz ohne LLM, und mehrere Generierungsversuche mit konkreter Rückmeldung,
was am vorherigen Versuch falsch war.

Die Kommentare im Quelltext begründen durchgehend das *Warum* — meist mit Bezug auf einen
konkreten beobachteten Fehlversuch.

## Hinweise

* API-Keys liegen wie bei anderen HA-Integrationen üblich als Klartext in `.storage/`.
  Home Assistant verschlüsselt Zugangsdaten nicht zusätzlich, sondern verlässt sich auf
  Dateisystem-Berechtigungen.
* Ein neues Dashboard erscheint erst nach einem Neustart in der Seitenleiste — HA
  verarbeitet Dashboard-*Registrierungen* nur beim Start. Der Assistent bietet den
  Neustart deshalb direkt an.
* Kostenlose Modell-Varianten bei OpenRouter (`:free`) teilen sich ein Kontingent beim
  Anbieter und antworten häufig mit HTTP 429. Für verlässlichen Betrieb einen eigenen
  Anbieter-Key hinterlegen oder ein bezahltes Modell wählen.
