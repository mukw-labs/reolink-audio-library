"""Select existing official Reolink entries; no new camera credentials."""

from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import CONF_REOLINK_ENTRY_IDS, DOMAIN, NAME


def _schema(entries, selected):
    return vol.Schema(
        {
            vol.Required(CONF_REOLINK_ENTRY_IDS, default=selected): selector.SelectSelector(
                {
                    "options": [{"value": entry.entry_id, "label": entry.title} for entry in entries],
                    "multiple": True,
                    "mode": "dropdown",
                }
            )
        }
    )


def _valid(entries, user_input):
    chosen = user_input.get(CONF_REOLINK_ENTRY_IDS, [])
    return bool(chosen) and len(chosen) == len(set(chosen)) and set(chosen) <= {entry.entry_id for entry in entries}


class ReolinkAudioLibraryConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        entries = self.hass.config_entries.async_entries("reolink")
        if not entries:
            return self.async_abort(reason="reolink_required")
        errors = {}
        if user_input is not None:
            if _valid(entries, user_input):
                return self.async_create_entry(title=NAME, data=user_input)
            errors["base"] = "select_camera"
        return self.async_show_form(step_id="user", data_schema=_schema(entries, []), errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(_config_entry):
        return ReolinkAudioLibraryOptionsFlow()


class ReolinkAudioLibraryOptionsFlow(config_entries.OptionsFlow):
    async def async_step_init(self, user_input=None):
        entries = self.hass.config_entries.async_entries("reolink")
        selected = self.config_entry.options.get(
            CONF_REOLINK_ENTRY_IDS, self.config_entry.data.get(CONF_REOLINK_ENTRY_IDS, [])
        )
        errors = {}
        if user_input is not None:
            if _valid(entries, user_input):
                return self.async_create_entry(title="", data=user_input)
            errors["base"] = "select_camera"
        selected = [entry_id for entry_id in selected if any(e.entry_id == entry_id for e in entries)]
        return self.async_show_form(step_id="init", data_schema=_schema(entries, selected), errors=errors)
