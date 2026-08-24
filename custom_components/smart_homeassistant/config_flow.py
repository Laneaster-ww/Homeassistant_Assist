"""Config flow fuer SMART-HOMEASSISTANT.

Einrichtung und Optionen nutzen dasselbe Formular (:func:`_schema`): was beim
Einrichten eingetragen wurde, laesst sich spaeter ueber "Konfigurieren" wieder
aendern. Die Optionen haben dabei Vorrang vor den urspruenglichen Eingaben
(siehe ``const.get_option``).
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback

from .const import (
    CONF_CONFIRMATION_TTL,
    CONF_OLLAMA_MODEL,
    CONF_OLLAMA_URL,
    DEFAULT_CONFIRMATION_TTL_MINUTES,
    DEFAULT_OLLAMA_MODEL,
    DEFAULT_OLLAMA_URL,
    DOMAIN,
)


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    """Formular fuer Einrichtung und Optionen, vorbelegt mit ``defaults``."""

    return vol.Schema(
        {
            vol.Required(
                CONF_OLLAMA_URL,
                default=defaults.get(CONF_OLLAMA_URL, DEFAULT_OLLAMA_URL),
            ): str,
            vol.Required(
                CONF_OLLAMA_MODEL,
                default=defaults.get(CONF_OLLAMA_MODEL, DEFAULT_OLLAMA_MODEL),
            ): str,
            vol.Required(
                CONF_CONFIRMATION_TTL,
                default=defaults.get(CONF_CONFIRMATION_TTL, DEFAULT_CONFIRMATION_TTL_MINUTES),
            ): int,
        }
    )


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Smart Homeassistant."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Verknuepft den Entry mit dem Optionen-Dialog unten."""

        return OptionsFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Einrichtung ueber die UI.

        Die Integration haelt genau einen Conversation Agent samt Policy, ein
        zweiter Eintrag haette keine sinnvolle Bedeutung - daher nur eine Instanz.
        """

        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        if user_input is not None:
            return self.async_create_entry(title="Smart Homeassistant", data=user_input)

        return self.async_show_form(step_id="user", data_schema=_schema({}))


class OptionsFlowHandler(config_entries.OptionsFlow):
    """Editable Optionen: Ollama-URL/Modell und Bestaetigungs-TTL."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """Zeigt das Formular mit den aktuellen Werten und speichert die Aenderung.

        Das Speichern loest ueber den Update-Listener in ``__init__.py`` einen
        Reload des Entry aus, damit z.B. ein neues Modell sofort greift.
        """

        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        # Optionen ueberlagern die urspruenglichen Einrichtungsdaten.
        current = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_schema(current))
