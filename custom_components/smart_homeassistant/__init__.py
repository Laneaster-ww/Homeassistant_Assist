"""Die SMART-HOMEASSISTANT Integration.

Einstiegspunkt der Integration: baut beim Laden eines Config Entry die
langlebigen Objekte auf (Policy, Provider-Store, Sitzungs-/Log-Speicher), legt
sie unter ``hass.data[DOMAIN][entry_id]`` ab und startet die Plattformen.

Die Aufteilung der Module:

* ``policy``     - Whitelist: was darf die KI ueberhaupt anfassen?
* ``broker``     - Sicherheitsschicht: prueft und fuehrt einzelne Aktionen aus.
* ``providers``  - Verwaltung mehrerer KI-Modell-Anbindungen (Ollama, OpenAI, ...).
* ``llm_clients`` - Provider-uebergreifende Clients inkl. ``ollama_client.OllamaClient``.
* ``conversation``  - Chat-Einstiegspunkt, der alles zusammenfuehrt.
* ``automations`` / ``dashboard`` / ``dashboard_design`` - die vom Chat aus
  erzeugbaren Artefakte.
"""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)

from .const import (
    CONF_OLLAMA_MODEL,
    CONF_OLLAMA_URL,
    DATA_ACTION_LOG,
    DATA_OFFLINE_DOCS,
    DATA_POLICY,
    DATA_PROVIDER_STORE,
    DATA_SESSIONS,
    DOMAIN,
    get_option,
)
from .conversation import ConversationSession
from .docs_knowledge import docs_policy_from_data, load_offline_docs
from .docs_knowledge import refresh_official_docs, save_offline_docs
from .frontend import async_register_frontend
from .policy import Policy
from .providers import PROVIDER_TYPES, ProviderStore

PLATFORMS = [Platform.CONVERSATION, Platform.SENSOR]

ADD_MODEL_PROVIDER_SCHEMA = vol.Schema(
    {
        vol.Required("name"): str,
        vol.Required("type"): vol.In(PROVIDER_TYPES),
        vol.Required("model"): str,
        vol.Optional("url", default=""): str,
        vol.Optional("api_key", default=""): str,
        vol.Optional("make_default", default=False): bool,
    }
)
REMOVE_MODEL_PROVIDER_SCHEMA = vol.Schema({vol.Required("id"): str})
# Alle Felder ausser "id" sind optional: uebergeben wird nur, was sich tatsaechlich
# aendert (siehe ProviderStore.async_update) - ein weggelassener API-Key laesst den
# gespeicherten unveraendert, statt ihn zu loeschen.
UPDATE_MODEL_PROVIDER_SCHEMA = vol.Schema(
    {
        vol.Required("id"): str,
        vol.Optional("name"): str,
        vol.Optional("type"): vol.In(PROVIDER_TYPES),
        vol.Optional("model"): str,
        vol.Optional("url"): str,
        vol.Optional("api_key"): str,
        vol.Optional("make_default"): bool,
    }
)
SET_CONVERSATION_MODEL_SCHEMA = vol.Schema(
    {vol.Required("conversation_id"): str, vol.Required("provider_id"): str}
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Richtet die Integration fuer einen Config Entry ein."""

    hass.data.setdefault(DOMAIN, {})

    policy = Policy(hass)
    await policy.async_load()
    docs_policy = docs_policy_from_data(hass, policy.data)
    offline_docs = await hass.async_add_executor_job(
        load_offline_docs,
        docs_policy.offline_path,
    )

    provider_store = ProviderStore(hass)
    await provider_store.async_load()
    # Migration: bestehende Installationen hatten Ollama-URL/-Modell fest im Config Entry.
    # Legt beim allerersten Start (Provider-Store noch leer) daraus einen Standard-Provider
    # an, damit nichts manuell nachkonfiguriert werden muss.
    await provider_store.async_seed_default_ollama(
        get_option(entry, CONF_OLLAMA_URL, ""),
        get_option(entry, CONF_OLLAMA_MODEL, ""),
    )

    # Gemeinsamer Zustand aller Plattformen dieses Entry: Conversation-Entity und
    # Sensor greifen ueber diesen Eintrag auf dieselben Objekte zu.
    hass.data[DOMAIN][entry.entry_id] = {
        DATA_POLICY: policy,
        DATA_PROVIDER_STORE: provider_store,
        DATA_SESSIONS: {},
        DATA_ACTION_LOG: [],
        DATA_OFFLINE_DOCS: offline_docs,
    }

    await async_register_frontend(hass)

    async def _handle_reload_policy(call: ServiceCall) -> None:
        """Service ``smart_homeassistant.reload_policy``: Policy-YAML neu einlesen."""

        await policy.reload()

    hass.services.async_register(DOMAIN, "reload_policy", _handle_reload_policy)

    async def _handle_refresh_documentation(call: ServiceCall) -> None:
        """Service ``smart_homeassistant.refresh_documentation``: Offline-Doku aktualisieren."""

        await policy.reload()
        refreshed_docs_policy = docs_policy_from_data(hass, policy.data)
        # Der bisherige Stand geht mit: faellt eine einzelne Quelle aus, uebernimmt
        # refresh_official_docs deren alten Abschnitt, statt ihn ersatzlos zu verlieren.
        refreshed = await refresh_official_docs(
            hass,
            refreshed_docs_policy,
            hass.data[DOMAIN][entry.entry_id].get(DATA_OFFLINE_DOCS, ""),
        )
        await hass.async_add_executor_job(
            save_offline_docs, refreshed_docs_policy.offline_path, refreshed
        )
        hass.data[DOMAIN][entry.entry_id][DATA_OFFLINE_DOCS] = refreshed

    hass.services.async_register(
        DOMAIN, "refresh_documentation", _handle_refresh_documentation
    )

    # Die folgenden vier Services verwalten die KI-Modell-Anbindungen und werden vom
    # "Modell wechseln"-Button im Chat-Fenster aufgerufen (siehe frontend/floating-window.js).
    # Bewusst eigene Services statt Config-Entry-Optionen: Hinzufuegen/Entfernen eines
    # Providers oder Wechseln des Modells in einem laufenden Gespraech soll die Integration
    # nicht neu laden (das wuerde alle offenen Sessions verwerfen).

    async def _handle_add_model_provider(call: ServiceCall) -> ServiceResponse:
        """Service ``smart_homeassistant.add_model_provider``: neuen Provider anlegen."""

        provider = await provider_store.async_add(
            name=call.data["name"],
            type_=call.data["type"],
            model=call.data["model"],
            url=call.data["url"],
            api_key=call.data["api_key"],
            make_default=call.data["make_default"],
        )
        return {"provider": provider.to_safe_dict()}

    hass.services.async_register(
        DOMAIN,
        "add_model_provider",
        _handle_add_model_provider,
        schema=ADD_MODEL_PROVIDER_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def _handle_update_model_provider(call: ServiceCall) -> ServiceResponse:
        """Service ``smart_homeassistant.update_model_provider``: bestehenden Provider aendern."""

        provider = await provider_store.async_update(
            call.data["id"],
            name=call.data.get("name"),
            type_=call.data.get("type"),
            model=call.data.get("model"),
            url=call.data.get("url"),
            api_key=call.data.get("api_key"),
            make_default=call.data.get("make_default"),
        )
        return {"provider": provider.to_safe_dict()}

    hass.services.async_register(
        DOMAIN,
        "update_model_provider",
        _handle_update_model_provider,
        schema=UPDATE_MODEL_PROVIDER_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def _handle_remove_model_provider(call: ServiceCall) -> None:
        """Service ``smart_homeassistant.remove_model_provider``: Provider entfernen."""

        await provider_store.async_remove(call.data["id"])

    hass.services.async_register(
        DOMAIN,
        "remove_model_provider",
        _handle_remove_model_provider,
        schema=REMOVE_MODEL_PROVIDER_SCHEMA,
    )

    async def _handle_list_model_providers(call: ServiceCall) -> ServiceResponse:
        """Service ``smart_homeassistant.list_model_providers``: konfigurierte Provider auflisten
        (ohne API-Keys - siehe ``ModelProvider.to_safe_dict``)."""

        return {"providers": [p.to_safe_dict() for p in provider_store.list()]}

    hass.services.async_register(
        DOMAIN,
        "list_model_providers",
        _handle_list_model_providers,
        supports_response=SupportsResponse.ONLY,
    )

    async def _handle_set_conversation_model(call: ServiceCall) -> None:
        """Service ``smart_homeassistant.set_conversation_model``: Modell fuer ein
        (laufendes oder kuenftiges) Gespraech festlegen."""

        sessions = hass.data[DOMAIN][entry.entry_id][DATA_SESSIONS]
        conversation_id = call.data["conversation_id"]
        session = sessions.setdefault(
            conversation_id, ConversationSession(conversation_id=conversation_id)
        )
        session.provider_id = call.data["provider_id"]

    hass.services.async_register(
        DOMAIN,
        "set_conversation_model",
        _handle_set_conversation_model,
        schema=SET_CONVERSATION_MODEL_SCHEMA,
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Geaenderte Optionen (TTL) greifen erst nach einem Reload des Entry.
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Laedt den Entry neu, wenn der Nutzer die Optionen geaendert hat."""

    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Raeumt Plattformen, gemeinsamen Zustand und Services wieder ab."""

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        hass.services.async_remove(DOMAIN, "reload_policy")
        hass.services.async_remove(DOMAIN, "refresh_documentation")
        hass.services.async_remove(DOMAIN, "add_model_provider")
        hass.services.async_remove(DOMAIN, "update_model_provider")
        hass.services.async_remove(DOMAIN, "remove_model_provider")
        hass.services.async_remove(DOMAIN, "list_model_providers")
        hass.services.async_remove(DOMAIN, "set_conversation_model")
    return unload_ok
