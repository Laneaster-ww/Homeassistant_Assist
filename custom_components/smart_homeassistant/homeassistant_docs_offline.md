---
title: Home Assistant offline documentation snapshot
source:
  - https://www.home-assistant.io/docs/
  - https://www.home-assistant.io/integrations/conversation/
  - https://developers.home-assistant.io/docs/intent_conversation_api/
  - https://developers.home-assistant.io/docs/voice/overview/
  - https://developers.home-assistant.io/docs/creating_integration_manifest/
last_reviewed: 2026-08-13
policy: Answers to Home Assistant documentation questions must use only this file or the listed official URLs.
---

# Home Assistant Documentation Snapshot

This file is an offline, curated snapshot for Smart Homeassistant. It is not a full mirror of the Home Assistant website. If a question cannot be answered from the sections below, the assistant must say that the answer is not present in the saved documentation.

## Official Documentation Links

The current online Home Assistant documentation is available at https://www.home-assistant.io/docs/.

Developer documentation for custom integrations is available at https://developers.home-assistant.io/docs/.

## Conversation

The Conversation integration lets a user converse with Home Assistant. A user can use the microphone in the frontend where supported, or call the `conversation.process` action with transcribed text.

The base YAML entry is:

```yaml
conversation:
```

The developer Conversation API accepts text through REST or WebSocket. A REST request can be sent to `/api/conversation/process`. A WebSocket request uses the message type `conversation/process`. The request contains `text`, and can contain `language`.

Home Assistant tracks a conversation across multiple inputs and responses by using a conversation id.

## Voice

Home Assistant voice assistants are built from several parts:

- Assist Pipeline turns speech into text, sends it for processing, and turns the response into speech.
- Conversation processes the user's text.
- Intent executes recognized intents and returns responses.
- Text-to-Speech turns text into spoken audio.
- Speech-to-Text turns spoken audio into text.

Integrations can provide custom conversation agents, custom text-to-speech agents, and custom speech-to-text agents.

## Custom Integration Manifest

Every integration has a `manifest.json` file in its integration directory. For custom integrations, a `version` key is required.

Common manifest fields include:

- `domain`: stable integration domain; it must match the directory name.
- `name`: integration name.
- `config_flow`: set to `true` when the integration has a config flow.
- `dependencies`: integrations that must be set up before this integration.
- `documentation`: website containing documentation for the integration.
- `integration_type`: describes the integration focus.
- `iot_class`: describes polling/push and local/cloud behavior.
- `requirements`: Python packages Home Assistant should install for the integration.
- `loggers`: external or custom loggers associated with the integration.

For an integration submitted to Home Assistant Core, `documentation` should normally point to `https://www.home-assistant.io/integrations/<domain>`. For a custom local integration, it can point to the relevant external documentation page.

## Custom Integration Localization

Custom integrations can include translations in a `translations` directory next to the integration code. Translation files are named by language code, for example `translations/de.json` and `translations/en.json`.

Custom integrations should include full translated strings directly in those translation files. They should not rely on Home Assistant Core build-time placeholder processing from `strings.json`.

## Basic Custom Integration Structure

A minimal integration needs a domain constant and setup code. Async integrations implement `async_setup` or `async_setup_entry` and return whether setup was successful.

Custom integrations live under `<config>/custom_components/<domain>`. The domain in `manifest.json` must match `<domain>`.

## Documentation Answer Policy

For questions about Home Assistant features, configuration, integrations, services, conversation, voice, Assist, dashboards, manifests, custom integrations, YAML, or automations, Smart Homeassistant must answer only from this offline snapshot or from explicitly configured official Home Assistant documentation URLs.

If the saved documentation does not contain enough information, the assistant must answer:

`Dazu finde ich in der gespeicherten Home-Assistant-Dokumentation keine ausreichende Information.`

It must not fill gaps from model memory.
