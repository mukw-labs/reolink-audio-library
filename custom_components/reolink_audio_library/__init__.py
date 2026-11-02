"""Persistent Reolink recordings, independent of any Talk integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
PLATFORMS = ["sensor"]


async def async_setup(hass: HomeAssistant, _config: dict) -> bool:
    from .audio_services import async_setup_audio_services

    async_setup_audio_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from .audio_library import libraries_for_entry
    from .audio_preview import get_preview_store

    entry.runtime_data = libraries_for_entry(hass, entry)
    get_preview_store(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from .audio_preview import get_preview_store

    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        get_preview_store(hass).unload(entry.entry_id)
    return unloaded


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
