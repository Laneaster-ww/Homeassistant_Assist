"""Verwaltung mehrerer KI-Modell-Provider (Ollama, OpenAI, Anthropic, Gemini, ...).

Anders als die urspruengliche Ollama-Konfiguration im Config Entry (fest, nur ueber den
Optionen-Dialog aenderbar) leben Provider hier in einem eigenen Store: sie sollen per
Chat-Fenster-Dialog hinzufuegbar sein, ohne dafuer die ganze Integration neu zu laden - ein
Config-Entry-Reload wuerde laufende Conversation-Sessions verwerfen (siehe
``conversation.ConversationSession``).

Sicherheit: API-Keys liegen hier wie bei anderen Home-Assistant-Integrationen ueblich
(z.B. ``core.config_entries``) als Klartext in ``.storage/`` - Home Assistant verschluesselt
Zugangsdaten nicht zusaetzlich, sondern verlaesst sich auf Dateisystem-Berechtigungen.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}_providers"

# Unterstuetzte Provider-Typen; bestimmt, welcher Client in llm_clients.create_client
# instanziiert wird und welche Felder im Frontend-Dialog noetig sind (Ollama braucht z.B.
# keinen API-Key, die anderen drei schon).
PROVIDER_TYPES = ("ollama", "openai", "anthropic", "gemini")


@dataclass
class ModelProvider:
    """Eine vom Nutzer konfigurierte KI-Anbindung."""

    id: str
    name: str
    type: str
    model: str
    url: str = ""
    api_key: str = ""
    is_default: bool = False

    def to_storage_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_storage_dict(cls, data: dict) -> ModelProvider:
        return cls(**data)

    def to_safe_dict(self) -> dict:
        """Repraesentation ohne API-Key - einzige Form, die das Frontend zu sehen bekommt."""

        data = asdict(self)
        data["has_api_key"] = bool(data.pop("api_key", ""))
        return data


class ProviderStore:
    """Laedt/speichert die Liste konfigurierter Provider in einem eigenen Store."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._providers: dict[str, ModelProvider] = {}

    async def async_load(self) -> None:
        """Liest die gespeicherten Provider ein (einmalig beim Setup)."""

        data = await self._store.async_load() or {}
        self._providers = {
            p["id"]: ModelProvider.from_storage_dict(p) for p in data.get("providers", [])
        }

    async def _async_save(self) -> None:
        await self._store.async_save(
            {"providers": [p.to_storage_dict() for p in self._providers.values()]}
        )

    def list(self) -> list[ModelProvider]:
        """Alle konfigurierten Provider, in Anlegereihenfolge."""

        return list(self._providers.values())

    def get(self, provider_id: str) -> ModelProvider | None:
        return self._providers.get(provider_id)

    def get_default(self) -> ModelProvider | None:
        """Der als Standard markierte Provider, sonst der zuerst angelegte."""

        for provider in self._providers.values():
            if provider.is_default:
                return provider
        providers = self.list()
        return providers[0] if providers else None

    async def async_add(
        self,
        name: str,
        type_: str,
        model: str,
        url: str = "",
        api_key: str = "",
        make_default: bool = False,
    ) -> ModelProvider:
        """Legt einen neuen Provider an und speichert ihn dauerhaft.

        Der allererste angelegte Provider wird automatisch zum Standard, damit nach der
        Ersteinrichtung nicht extra manuell einer ausgewaehlt werden muss.
        """

        if type_ not in PROVIDER_TYPES:
            raise ValueError(f"Unbekannter Provider-Typ: {type_}")

        provider = ModelProvider(
            id=str(uuid.uuid4()),
            name=name,
            type=type_,
            model=model,
            url=url,
            api_key=api_key,
            is_default=make_default or not self._providers,
        )
        if provider.is_default:
            for existing in self._providers.values():
                existing.is_default = False
        self._providers[provider.id] = provider
        await self._async_save()
        return provider

    async def async_update(
        self,
        provider_id: str,
        name: str | None = None,
        type_: str | None = None,
        model: str | None = None,
        url: str | None = None,
        api_key: str | None = None,
        make_default: bool | None = None,
    ) -> ModelProvider:
        """Aendert einen bestehenden Provider; nicht uebergebene Felder bleiben unveraendert.

        Ein leerer oder fehlender ``api_key`` behaelt den gespeicherten Schluessel bewusst
        bei: das Frontend bekommt Keys nie zu sehen (siehe :meth:`ModelProvider.to_safe_dict`)
        und kann sie beim Bearbeiten daher gar nicht zuruecksenden - ein leer gelassenes
        Feld darf den vorhandenen Key also nicht loeschen. Zum Ersetzen wird schlicht ein
        neuer Key uebergeben.

        Bewusst als eigener Weg statt "entfernen und neu anlegen": die Provider-ID bleibt
        stabil, sodass laufende Gespraeche (``ConversationSession.provider_id``) und die
        Modellwahl im Frontend eine Korrektur ueberleben.
        """

        provider = self._providers.get(provider_id)
        if provider is None:
            raise ValueError(f"Unbekannter Provider: {provider_id}")

        if type_ is not None:
            if type_ not in PROVIDER_TYPES:
                raise ValueError(f"Unbekannter Provider-Typ: {type_}")
            provider.type = type_
        if name is not None:
            provider.name = name
        if model is not None:
            provider.model = model
        if url is not None:
            provider.url = url
        if api_key:
            provider.api_key = api_key
        if make_default:
            for existing in self._providers.values():
                existing.is_default = existing.id == provider_id

        await self._async_save()
        return provider

    async def async_remove(self, provider_id: str) -> None:
        """Entfernt einen Provider; war er Standard, ruecklt ein anderer nach (falls vorhanden)."""

        removed = self._providers.pop(provider_id, None)
        if removed is None:
            return
        if removed.is_default and self._providers:
            next(iter(self._providers.values())).is_default = True
        await self._async_save()

    async def async_seed_default_ollama(self, url: str, model: str) -> None:
        """Legt beim allerersten Start einen "Lokales Ollama"-Provider aus der bisherigen
        Config-Entry-Konfiguration an, damit bestehende Installationen ohne manuelles
        Nacharbeiten weiterlaufen. Greift nur, wenn noch kein Provider existiert.
        """

        if self._providers:
            return
        await self.async_add(
            name="Lokales Ollama", type_="ollama", model=model, url=url, make_default=True
        )
