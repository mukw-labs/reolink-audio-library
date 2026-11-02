"""Native HA recording picker/actions; no physical camera or playback."""

from dataclasses import replace
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

import voluptuous as vol  # noqa: E402
from homeassistant.core import Context  # noqa: E402
from homeassistant.exceptions import ServiceValidationError, Unauthorized  # noqa: E402
from test_audio_services_ha import audio_setup as audio_setup, supported_library as supported_library, action  # noqa: E402

from custom_components.reolink_audio_library.audio_edit import RecordingChangeError, RecordingChangeReceipt  # noqa: E402
from custom_components.reolink_audio_library.audio_preview import get_preview_store  # noqa: E402
from custom_components.reolink_audio_library.recording_identity import recording_media_id, recording_fingerprint  # noqa: E402

USED = "sensor.reolink_audio_library_front_custom_recordings"
FREE = "sensor.reolink_audio_library_front_free_recording_slots"
STATUS = "sensor.reolink_audio_library_front_upload_status"


@pytest.fixture
def selection(audio_setup, supported_library):
    lib = supported_library.library
    catalog = supported_library.catalog
    record = catalog.native.files[-1]
    return recording_media_id(audio_setup.entry.entry_id, lib, catalog, record)


async def manage(hass, setup, selection, kind="rename", *, picker=True, **kwargs):
    data = (
        {"recording": {"media_content_id": selection, "media_content_type": "audio/wav"}}
        if picker
        else {"media_content_id": selection}
    )
    data.update({"new_name": "Changed"} if kind == "rename" else {"confirm_delete": True})
    return await action(hass, setup, kind + "_audio", {**data, **kwargs})


def mutate_fake(lib, original, kind, new_name="Changed"):
    def changed(items):
        return tuple(
            replace(item, name=new_name) if item.id == 4 else item for item in items if kind != "delete" or item.id != 4
        )

    lib.async_read.return_value = replace(
        original,
        custom=replace(original.custom, files=changed(original.custom.files)),
        native=replace(original.native, files=changed(original.native.files)),
    )


def successful(lib, catalog, kind):
    async def change(*args, **kwargs):
        assert kwargs["device_write_authorized"] is True
        kwargs["before_write"]()
        mutate_fake(lib, catalog, kind)
        return RecordingChangeReceipt(
            kind,
            4,
            "Uploaded reply",
            "Changed" if kind == "rename" else None,
            True,
            True,
            True,
            kind + "d_verified_after_reconnect",
        )

    return AsyncMock(side_effect=change)


@pytest.mark.parametrize("kind", ["rename", "delete"])
@pytest.mark.parametrize("picker", [True, False])
async def test_native_actions_pick_recording_refresh_capacity_and_revoke_cached_audio(
    hass, audio_setup, supported_library, selection, hass_client, kind, picker
):
    lib = supported_library.library
    catalog = supported_library.catalog
    store = get_preview_store(hass)
    old = store.put(
        audio_setup.entry.entry_id, lib, recording_fingerprint(catalog, catalog.native.files[-1]), b"RIFFfake"
    )
    client = await hass_client()
    assert (await client.get(old.url)).status == 200
    with patch(
        "custom_components.reolink_audio_library.audio_services.async_edit_recording", successful(lib, catalog, kind)
    ) as edit:
        result = await manage(hass, audio_setup, selection, kind, picker=picker)
    edit.assert_awaited_once()
    assert result["success"] and result["verified_after_reconnect"] and result["inventory_refreshed"]
    assert (await client.get(old.url)).status == 404
    assert hass.states.get(USED).state == ("1" if kind == "delete" else "2")
    assert hass.states.get(FREE).state == ("11" if kind == "delete" else "10")
    assert hass.states.get(USED).attributes["last_recording_change"]["action"] == kind
    slots = hass.states.get(USED).attributes["slots"]
    assert slots[4]["status"] == ("available" if kind == "delete" else "occupied")
    if kind == "rename":
        assert slots[4]["name"] == "Changed"
    assert hass.states.get("sensor.reolink_audio_library_front_upload_status").state == "idle"


@pytest.mark.parametrize("data", [{}, {"confirm_delete": False}, {"confirm_delete": None}])
async def test_delete_requires_explicit_confirmation(hass, audio_setup, supported_library, selection, data):
    with patch("custom_components.reolink_audio_library.audio_services.async_edit_recording", AsyncMock()) as edit:
        with pytest.raises(vol.Invalid):
            await action(hass, audio_setup, "delete_audio", {"media_content_id": selection, **data})
    edit.assert_not_awaited()


@pytest.mark.parametrize("kind", ["rename", "delete"])
async def test_administrators_only(hass, audio_setup, supported_library, selection, hass_read_only_user, kind):
    fields = {"new_name": "Changed"} if kind == "rename" else {"confirm_delete": True}
    with patch("custom_components.reolink_audio_library.audio_services.async_edit_recording", AsyncMock()) as edit:
        with pytest.raises(Unauthorized):
            await action(
                hass,
                audio_setup,
                kind + "_audio",
                {"media_content_id": selection, **fields},
                context=Context(user_id=hass_read_only_user.id),
            )
    edit.assert_not_awaited()


@pytest.mark.parametrize("kind", ["rename", "delete"])
@pytest.mark.parametrize("file_id", [0, 1, 2])
async def test_factory_selection_rejected_before_connection(hass, audio_setup, supported_library, kind, file_id):
    lib = supported_library.library
    catalog = supported_library.catalog
    uri = recording_media_id(audio_setup.entry.entry_id, lib, catalog, catalog.native.files[file_id])
    lib.connect.reset_mock()
    with pytest.raises(ServiceValidationError, match="Factory"):
        await manage(hass, audio_setup, uri, kind)
    lib.connect.assert_not_called()


async def test_wrong_camera_uri_is_rejected_without_connect(hass, audio_setup, supported_library, selection):
    lib = supported_library.library
    lib.connect.reset_mock()
    other = selection.replace(selection.split("/")[-3], "0" * 24)
    with pytest.raises(ServiceValidationError, match="selected camera"):
        await manage(hass, audio_setup, other)
    lib.connect.assert_not_called()


@pytest.mark.parametrize(
    "uri",
    [
        "media-source://media_source/local/file.wav",
        "media-source://tts/hello",
        "media-source://reolink_audio_library/../bad",
        "https://camera.invalid/4",
    ],
)
async def test_only_actual_audio_library_selections_are_accepted(hass, audio_setup, supported_library, uri):
    with pytest.raises(vol.Invalid):
        await manage(hass, audio_setup, uri, picker=False)
    supported_library.library.connect.assert_not_called()


async def test_confirmed_change_remains_successful_when_display_refresh_fails(
    hass, audio_setup, supported_library, selection
):
    lib = supported_library.library

    async def changed(*args, **kwargs):
        kwargs["before_write"]()
        lib.async_read.side_effect = RuntimeError("private details")
        return RecordingChangeReceipt(
            "rename", 4, "Uploaded reply", "Changed", True, True, True, "renamed_verified_after_reconnect"
        )

    with patch(
        "custom_components.reolink_audio_library.audio_services.async_edit_recording", AsyncMock(side_effect=changed)
    ):
        result = await manage(hass, audio_setup, selection)
    assert result["success"] and not result["inventory_refreshed"]
    assert hass.states.get(USED).state == "unavailable"
    assert hass.states.get(STATUS).attributes["last_recording_change"]["verified_after_reconnect"]


async def test_uncertain_result_invalidates_cache_and_marks_inventory_unavailable(
    hass, audio_setup, supported_library, selection, hass_client
):
    lib = supported_library.library
    catalog = supported_library.catalog
    store = get_preview_store(hass)
    old = store.put(
        audio_setup.entry.entry_id, lib, recording_fingerprint(catalog, catalog.native.files[-1]), b"RIFFfake"
    )
    with patch(
        "custom_components.reolink_audio_library.audio_services.async_edit_recording",
        AsyncMock(side_effect=RecordingChangeError("Inspect inventory", outcome_uncertain=True)),
    ):
        with pytest.raises(ServiceValidationError, match="Inspect inventory"):
            await manage(hass, audio_setup, selection)
    client = await hass_client()
    assert (await client.get(old.url)).status == 404
    assert hass.states.get(USED).state == "unavailable"
    assert hass.states.get(STATUS).attributes["last_recording_error"]["outcome_uncertain"]


async def test_preflight_failure_keeps_cache_and_capacity(hass, audio_setup, supported_library, selection, hass_client):
    lib = supported_library.library
    catalog = supported_library.catalog
    store = get_preview_store(hass)
    old = store.put(
        audio_setup.entry.entry_id, lib, recording_fingerprint(catalog, catalog.native.files[-1]), b"RIFFfake"
    )
    with patch(
        "custom_components.reolink_audio_library.audio_services.async_edit_recording",
        AsyncMock(side_effect=RecordingChangeError("Configured automatic reply")),
    ):
        with pytest.raises(ServiceValidationError, match="automatic reply"):
            await manage(hass, audio_setup, selection, "delete")
    client = await hass_client()
    assert (await client.get(old.url)).status == 200
    assert hass.states.get(USED).state == "2"


async def test_last_error_does_not_expose_arbitrary_exception_text(hass, audio_setup, supported_library, selection):
    with patch(
        "custom_components.reolink_audio_library.audio_services.async_edit_recording",
        AsyncMock(side_effect=RuntimeError("SECRET camera password")),
    ):
        with pytest.raises(Exception, match="RuntimeError") as error:
            await manage(hass, audio_setup, selection)
    assert "SECRET" not in str(error.value)
    assert "SECRET" not in str(hass.states.get(USED).attributes)


async def test_list_response_contains_fresh_picker_identifiers_and_factory_flags(hass, audio_setup):
    result = await action(hass, audio_setup, "list_audio")
    for row in result["files"]:
        assert row["media_content_id"].startswith("media-source://reolink_audio_library/")
        assert row["can_rename"] == row["can_delete"] == (row["id"] >= 3)


async def test_alias_preview_tokens_are_revoked_for_same_physical_camera(
    hass, audio_setup, supported_library, selection
):
    from types import SimpleNamespace

    lib = supported_library.library
    catalog = supported_library.catalog
    store = get_preview_store(hass)
    alias = SimpleNamespace(entry_id="alias-camera", channel=0, lock=lib.lock)
    item = store.put(
        audio_setup.entry.entry_id, alias, recording_fingerprint(catalog, catalog.native.files[-1]), b"PRIVATE_WAV"
    )
    assert "PRIVATE_WAV" not in repr(item)
    with patch(
        "custom_components.reolink_audio_library.audio_services.async_edit_recording",
        successful(lib, catalog, "rename"),
    ):
        await manage(hass, audio_setup, selection)
    assert item.token not in store.items


@pytest.mark.parametrize("name", ["", " ", "x" * 128, "\ud800", "bad\0name"])
async def test_invalid_new_names_fail_before_camera_access(hass, audio_setup, supported_library, selection, name):
    with pytest.raises((vol.Invalid, ServiceValidationError)):
        await manage(hass, audio_setup, selection, new_name=name)
    supported_library.library.connect.assert_not_called()


async def test_cancelled_change_releases_lock_and_revokes_old_preview(hass, audio_setup, supported_library, selection):
    import asyncio

    lib = supported_library.library
    catalog = supported_library.catalog
    store = get_preview_store(hass)
    item = store.put(
        audio_setup.entry.entry_id, lib, recording_fingerprint(catalog, catalog.native.files[-1]), b"RIFFfake"
    )
    started = asyncio.Event()

    async def wait(*args, **kwargs):
        kwargs["before_write"]()
        started.set()
        await asyncio.Event().wait()

    with patch(
        "custom_components.reolink_audio_library.audio_services.async_edit_recording", AsyncMock(side_effect=wait)
    ):
        task = asyncio.create_task(manage(hass, audio_setup, selection))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not lib.lock.locked() and item.token not in store.items
    assert hass.states.get(STATUS).attributes["last_recording_error"]["outcome_uncertain"]


@pytest.mark.parametrize("kind", ["rename", "delete"])
async def test_change_action_without_response_still_completes(hass, audio_setup, supported_library, selection, kind):
    lib = supported_library.library
    catalog = supported_library.catalog
    fields = {"new_name": "Changed"} if kind == "rename" else {"confirm_delete": True}
    with patch(
        "custom_components.reolink_audio_library.audio_services.async_edit_recording", successful(lib, catalog, kind)
    ):
        result = await action(
            hass, audio_setup, kind + "_audio", {"media_content_id": selection, **fields}, response=False
        )
    assert result is None
    assert hass.states.get(STATUS).attributes["last_recording_change"]["action"] == kind
