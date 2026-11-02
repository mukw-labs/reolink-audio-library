"""Real HA config/options flows and package boundaries."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.data_entry_flow import FlowResultType, InvalidData  # noqa: E402
from homeassistant.setup import async_setup_component  # noqa: E402
from homeassistant.helpers import selector  # noqa: E402
from homeassistant.helpers.service import _SERVICES_SCHEMA  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402
from test_audio_services_ha import audio_setup as audio_setup  # noqa: E402

from custom_components.reolink_audio_library.const import DOMAIN, CONF_REOLINK_ENTRY_IDS  # noqa: E402


async def test_setup_requires_official_reolink_entries(hass, enable_custom_integrations):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reolink_required"


async def test_config_flow_selects_existing_cameras_and_rejects_foreign_ids(hass, enable_custom_integrations):
    camera = MockConfigEntry(domain="reolink", title="Doorbell", data={})
    camera.add_to_hass(hass)
    with (
        patch("custom_components.reolink_audio_library.async_setup_entry", AsyncMock(return_value=True)),
        patch("custom_components.reolink_audio_library.async_unload_entry", AsyncMock(return_value=True)),
        patch("homeassistant.components.reolink.async_setup_entry", AsyncMock(return_value=True)),
        patch("homeassistant.components.reolink.async_unload_entry", AsyncMock(return_value=True)),
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        assert result["type"] is FlowResultType.FORM
        for chosen in ([], [camera.entry_id, camera.entry_id]):
            result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_REOLINK_ENTRY_IDS: chosen})
            assert result["errors"] == {"base": "select_camera"}
        with pytest.raises(InvalidData):
            await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_REOLINK_ENTRY_IDS: ["missing"]})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_REOLINK_ENTRY_IDS: [camera.entry_id]}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        assert result["title"] == "Reolink Audio Library"
        assert result["data"] == {CONF_REOLINK_ENTRY_IDS: [camera.entry_id]}
        await hass.async_block_till_done()
        duplicate = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        assert duplicate["type"] is FlowResultType.ABORT and duplicate["reason"] == "single_instance_allowed"
        await hass.config_entries.async_unload(result["result"].entry_id)
        await hass.config_entries.async_unload(camera.entry_id)


async def test_options_change_reloads_only_audio_library(hass, audio_setup):
    with patch.object(hass.config_entries, "async_reload", AsyncMock(return_value=True)) as reload:
        result = await hass.config_entries.options.async_init(audio_setup.entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_REOLINK_ENTRY_IDS: [audio_setup.official_entries[0].entry_id]}
        )
        assert result["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
        reload.assert_awaited_once_with(audio_setup.entry.entry_id)


async def test_action_registration_survives_no_config_entries(hass, enable_custom_integrations):
    assert await async_setup_component(hass, DOMAIN, {})
    assert set(hass.services.async_services()[DOMAIN]) == {"list_audio", "upload_audio", "rename_audio", "delete_audio"}


def test_native_action_descriptions_and_selectors_validate():
    import yaml

    component = Path(__file__).parents[1] / "custom_components/reolink_audio_library"
    descriptions = yaml.safe_load((component / "services.yaml").read_text())
    _SERVICES_SCHEMA(descriptions)
    for action in descriptions.values():
        for field in action.get("fields", {}).values():
            selector.validate_selector(field["selector"])
    assert all("device_id" in action["fields"] for action in descriptions.values())
    assert not any(
        (component / name).exists() for name in ("talk.py", "media_player.py", "audio_browser.py", "util.py")
    )
