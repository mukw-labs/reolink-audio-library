"""Use real HA when installed, minimal import stubs for pure protocol tests."""

from __future__ import annotations

import sys
from types import ModuleType


def _install_homeassistant_stubs() -> None:
    try:
        import homeassistant  # noqa: F401

        return  # real HA available -> use it
    except ImportError:
        pass

    ha = ModuleType("homeassistant")

    core = ModuleType("homeassistant.core")

    class HomeAssistant:
        pass

    core.HomeAssistant = HomeAssistant

    config_entries = ModuleType("homeassistant.config_entries")

    class ConfigEntry:
        pass

    config_entries.ConfigEntry = ConfigEntry

    helpers = ModuleType("homeassistant.helpers")
    config_validation = ModuleType("homeassistant.helpers.config_validation")
    config_validation.config_entry_only_config_schema = lambda domain: lambda config: config
    helpers.config_validation = config_validation
    sys.modules["homeassistant.helpers.config_validation"] = config_validation
    aiohttp_client = ModuleType("homeassistant.helpers.aiohttp_client")

    def async_get_clientsession(hass):
        raise NotImplementedError("homeassistant stub: not usable in unit tests")

    aiohttp_client.async_get_clientsession = async_get_clientsession

    network = ModuleType("homeassistant.helpers.network")

    def get_url(hass, **kwargs):
        raise NotImplementedError("homeassistant stub: not usable in unit tests")

    network.get_url = get_url

    ha.core = core
    ha.config_entries = config_entries
    ha.helpers = helpers
    helpers.aiohttp_client = aiohttp_client
    helpers.network = network

    sys.modules["homeassistant"] = ha
    sys.modules["homeassistant.core"] = core
    sys.modules["homeassistant.config_entries"] = config_entries
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.aiohttp_client"] = aiohttp_client
    sys.modules["homeassistant.helpers.network"] = network


_install_homeassistant_stubs()
