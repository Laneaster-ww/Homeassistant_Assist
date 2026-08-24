"""Frontend registration for the Smart Homeassistant floating window."""

from __future__ import annotations

from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.core import HomeAssistant

from .const import DOMAIN

FRONTEND_URL_BASE = f"/{DOMAIN}"
FRONTEND_VERSION = "20260819b"


try:
    from homeassistant.components.http import StaticPathConfig

    async def _async_register_static_path(
        hass: HomeAssistant,
        url_path: str,
        path: str,
        cache_headers: bool = True,
    ) -> None:
        """Register a static file or directory with the HTTP component."""

        await hass.http.async_register_static_paths(
            [StaticPathConfig(url_path, path, cache_headers)]
        )

except ImportError:

    async def _async_register_static_path(
        hass: HomeAssistant,
        url_path: str,
        path: str,
        cache_headers: bool = True,
    ) -> None:
        """Register a static file or directory with older Home Assistant versions."""

        hass.http.register_static_path(url_path, path, cache_headers)


async def async_register_frontend(hass: HomeAssistant) -> None:
    """Register the global floating assistant frontend module."""

    frontend_dir = Path(__file__).parent / "frontend"
    await _async_register_static_path(
        hass,
        f"{FRONTEND_URL_BASE}/frontend",
        str(frontend_dir),
        cache_headers=False,
    )
    add_extra_js_url(
        hass,
        f"{FRONTEND_URL_BASE}/frontend/floating-window.js?v={FRONTEND_VERSION}",
    )
