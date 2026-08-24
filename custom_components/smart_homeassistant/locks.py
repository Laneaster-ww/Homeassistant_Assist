"""Gemeinsame Synchronisations-Helfer fuer datei-basierten SMART-HOMEASSISTANT-Zustand."""

from __future__ import annotations

import asyncio

from homeassistant.core import HomeAssistant

from .const import DATA_FILE_LOCK, DOMAIN


def get_file_lock(hass: HomeAssistant) -> asyncio.Lock:
    """Gibt den integrationsweiten Lock fuer Read-Modify-Write-Zyklen auf Config-Dateien zurueck."""

    domain_data = hass.data.setdefault(DOMAIN, {})
    lock = domain_data.get(DATA_FILE_LOCK)
    if lock is None:
        lock = asyncio.Lock()
        domain_data[DATA_FILE_LOCK] = lock
    return lock
