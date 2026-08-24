"""Leichtgewichtige Aktionsuebersicht - letzte Aktion + kurzer Verlauf.

Der Sensor haelt keine eigenen Daten: er zeigt nur den Aktionsverlauf an, den
die Conversation-Entity unter ``hass.data[DOMAIN][entry_id][DATA_ACTION_LOG]``
fuehrt (siehe ``conversation._log_action``).
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DATA_ACTION_LOG, DOMAIN, action_log_signal


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Richtet den Aktionsverlauf-Sensor fuer diesen Config Entry ein."""

    async_add_entities([SmartHomeAssistantActionLogSensor(hass, entry)])


class SmartHomeAssistantActionLogSensor(SensorEntity):
    """Zeigt die zuletzt vom Action Broker verarbeitete Aktion und einen kurzen Verlauf."""

    _attr_has_entity_name = True
    _attr_name = "Letzte Aktion"
    _attr_icon = "mdi:robot"
    # Der Verlauf wird an anderer Stelle im Speicher fortgeschrieben; die Conversation-
    # Entity kuendigt jede Aenderung per Dispatcher-Signal an (siehe
    # conversation._log_action), deshalb ist Pollen nicht noetig.
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_action_log"

    async def async_added_to_hass(self) -> None:
        """Abonniert die Aenderungen am Aktionsverlauf."""

        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                action_log_signal(self._entry.entry_id),
                self._handle_log_changed,
            )
        )

    @callback
    def _handle_log_changed(self) -> None:
        """Uebernimmt einen neuen Verlaufseintrag in den Entity-Zustand."""

        self.async_write_ha_state()

    def _log(self) -> list[dict]:
        """Der gemeinsame Aktionsverlauf dieses Config Entry."""

        return self.hass.data[DOMAIN][self._entry.entry_id][DATA_ACTION_LOG]

    @property
    def native_value(self) -> str:
        """Status und Anlass der juengsten Aktion.

        Auf 255 Zeichen gekuerzt, weil HA laengere Zustaende nicht speichert.
        """

        log = self._log()
        if not log:
            return "Keine Aktion bisher"
        last = log[-1]
        return f"{last['status']}: {last['reason']}"[:255]

    @property
    def extra_state_attributes(self) -> dict:
        """Kompletter Verlauf, juengster Eintrag zuerst."""

        return {"history": list(reversed(self._log()))}
