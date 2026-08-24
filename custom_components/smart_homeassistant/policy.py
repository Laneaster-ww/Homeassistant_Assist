"""Policy-Whitelist fuer den Action Broker.

Alles, was hier nicht explizit erlaubt ist, kann der Action Broker nicht
ausfuehren - unabhaengig davon, was das Sprachmodell vorschlaegt.

Die Whitelist liegt als YAML-Datei im Konfigurationsverzeichnis und hat drei
Abschnitte (Beispiel siehe ``const.DEFAULT_POLICY_YAML``):

* ``scripts``    - erlaubte Scripts je Entity-ID.
* ``services``   - erlaubte ``domain.service``-Aufrufe samt Ziel-Entities.
* ``automations``- welche Entities eine per KI erzeugte Automation nutzen darf.
"""

from __future__ import annotations

import logging
import os

import yaml

from homeassistant.core import HomeAssistant

from .const import DEFAULT_POLICY_YAML, POLICY_FILENAME

_LOGGER = logging.getLogger(__name__)


def _mtime_of(path: str) -> float | None:
    """Aenderungszeitpunkt der Policy-Datei, oder ``None``, wenn es sie nicht gibt."""

    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def _read_yaml(path: str) -> tuple[dict, float | None]:
    """Liest die Policy-Datei samt Aenderungszeitpunkt (blockierend, gehoert in den Executor).

    Der Zeitstempel wird VOR dem Lesen genommen: aendert sich die Datei genau dazwischen,
    merkt sich die Policy lieber einen zu alten Stand und liest beim naechsten Mal erneut,
    als eine Aenderung dauerhaft zu verpassen.
    """

    mtime = _mtime_of(path)
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}, mtime


def _ensure_default(path: str) -> None:
    """Legt die Policy-Datei beim ersten Start an; vorhandene bleibt unberuehrt."""

    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(DEFAULT_POLICY_YAML)


class Policy:
    """Laedt und befragt die Whitelist aus der Policy-YAML-Datei."""

    def __init__(self, hass: HomeAssistant, path: str | None = None) -> None:
        self.hass = hass
        self.path = path or hass.config.path(POLICY_FILENAME)
        self.data: dict = {}
        # Stand der zuletzt eingelesenen Datei - Grundlage fuer async_reload_if_changed.
        self._mtime: float | None = None

    async def async_load(self) -> None:
        """Legt die Datei bei Bedarf an und liest sie ein."""

        await self.hass.async_add_executor_job(_ensure_default, self.path)
        await self.reload()

    async def reload(self) -> None:
        """Liest die Policy-Datei neu ein (Service ``reload_policy``)."""

        self.data, self._mtime = await self.hass.async_add_executor_job(_read_yaml, self.path)

    async def async_reload_if_changed(self) -> bool:
        """Liest die Policy neu ein, falls die Datei seit dem letzten Mal geaendert wurde.

        Wird vor jeder Chat-Anfrage aufgerufen (siehe ``conversation._handle_new_message``).
        Ohne diese Pruefung ist die Whitelist bis zum naechsten Aufruf von
        ``smart_homeassistant.reload_policy`` bzw. bis zum HA-Neustart eingefroren - die
        Zweitpruefung unmittelbar vor dem Ausfuehren einer bestaetigten Aktion (siehe
        ``conversation._handle_pending``) haette dann gegen exakt dieselben Daten geprueft
        wie die Erstpruefung und waere wirkungslos gewesen. Ein ``stat()`` pro Nachricht
        ist dafuer der billigste Preis.

        Eine kaputt gespeicherte Policy-Datei laesst bewusst den bisherigen Stand stehen,
        statt das Gespraech mit einem YAML-Fehler abzubrechen; der Zeitstempel wird
        trotzdem uebernommen, damit nicht bei jeder Nachricht erneut geparst und geloggt
        wird.
        """

        mtime = await self.hass.async_add_executor_job(_mtime_of, self.path)
        if mtime == self._mtime:
            return False
        try:
            await self.reload()
        except (OSError, yaml.YAMLError):
            _LOGGER.exception(
                "Geaenderte Policy-Datei %s konnte nicht gelesen werden - der zuletzt "
                "gueltige Stand bleibt aktiv",
                self.path,
            )
            self._mtime = mtime
            return False
        return True

    def script_allowed(self, entity_id: str) -> dict | None:
        """Konfiguration des Scripts, oder ``None``, wenn es nicht freigegeben ist."""

        return self.data.get("scripts", {}).get(entity_id)

    def service_allowed(self, domain: str, service: str, entity_id) -> dict | None:
        """Konfiguration des Service-Aufrufs, oder ``None``, wenn er nicht erlaubt ist.

        Erlaubt ist ein Aufruf nur, wenn der Service selbst eingetragen ist *und*
        jedes einzelne Ziel in ``allowed_entities`` steht. ``"*"`` gibt alle Ziele
        frei; ohne Ziel-Angabe greift die Freigabe nur bei ``"*"``.
        """

        key = f"{domain}.{service}"
        cfg = self.data.get("services", {}).get(key)
        if not cfg:
            return None

        allowed = cfg.get("allowed_entities", [])
        if "*" in allowed:
            return cfg

        targets = entity_id if isinstance(entity_id, list) else [entity_id]
        if targets and all(t in allowed for t in targets):
            return cfg
        return None

    def automation_policy(self) -> dict:
        """Regeln fuer per KI neu erzeugte Automationen (Abschnitt ``automations: create``)."""

        return self.data.get("automations", {}).get("create", {})
