"""Device-based recording actions; no media-player or camera playback service."""

from __future__ import annotations

import asyncio
from dataclasses import asdict

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr, selector, service
from homeassistant.helpers.update_coordinator import UpdateFailed

from .audio_edit import RecordingChangeError, async_edit_recording
from .audio_preparation import async_prepare_media
from .audio_preview import get_preview_store
from .recording_identity import camera_key, parse_recording_selection, recording_media_id
from .audio_upload import AudioUploadError, async_upload_prepared
from .audio_upload_transport import validate_recording_name
from .const import DOMAIN
from .stored_audio import StoredAudioError

AUDIO_SERVICES = ("list_audio", "upload_audio", "rename_audio", "delete_audio")


def _media_source(value):
    value = cv.string(value)
    if not value.startswith("media-source://media_source/"):
        raise vol.Invalid("Choose a file from Home Assistant's local media library")
    return value


def _selected_media(value):
    media = selector.MediaSelector({"accept": ["audio/*"]})(value)
    _media_source(media["media_content_id"])
    return media


def _saved_media_id(value):
    value = cv.string(value)
    try:
        parse_recording_selection(value)
    except StoredAudioError as error:
        raise vol.Invalid(str(error)) from None
    return value


def _saved_recording(value):
    media = selector.MediaSelector({"accept": ["audio/*"]})(value)
    _saved_media_id(media["media_content_id"])
    return media


def _library_for_device(hass, device_id):
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise ServiceValidationError("Choose an existing Reolink device")
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is not ConfigEntryState.LOADED or not isinstance(getattr(entry, "runtime_data", None), dict):
            continue
        for library in entry.runtime_data.values():
            if library.entry_id not in device.config_entries:
                continue
            info = library.device_info
            if info is None or not device.identifiers.intersection(info["identifiers"]):
                continue
            try:
                if library.profile is None:
                    raise ServiceValidationError("No validated recording profile matches this camera and firmware")
            except StoredAudioError as error:
                raise ServiceValidationError(str(error)) from None
            return library
    raise ServiceValidationError(
        "Select this camera in Reolink Audio Library's configuration and load its Reolink integration"
    )


def async_setup_audio_services(hass):
    async def list_audio(call):
        library = _library_for_device(hass, call.data["device_id"])
        try:
            async with library.lock:
                catalog = await library.async_read()
                library.async_set_updated_data(catalog)
        except Exception as error:
            raise HomeAssistantError(f"Could not list recordings ({type(error).__name__})") from None
        result = catalog.as_dict()
        for item, record in zip(result["files"], catalog.native.files, strict=True):
            item["media_content_id"] = recording_media_id(library.library_entry_id, library, catalog, record)
            item["can_rename"] = item["can_delete"] = (
                catalog.profile.custom_id_min <= record.id <= catalog.profile.custom_id_max
            )
        return {"device_id": call.data["device_id"], "channel": library.channel, **result}

    async def upload_audio(call):
        library = _library_for_device(hass, call.data["device_id"])
        result = await async_upload_for_library(hass, library, **call.data)
        return result if call.return_response else None

    hass.services.async_register(
        DOMAIN,
        "list_audio",
        list_audio,
        schema=vol.Schema({vol.Required("device_id"): cv.string}),
        supports_response=SupportsResponse.ONLY,
    )
    service.async_register_admin_service(
        hass,
        DOMAIN,
        "upload_audio",
        upload_audio,
        schema=vol.All(
            vol.Schema(
                {
                    vol.Required("device_id"): cv.string,
                    vol.Required("name"): vol.All(cv.string, vol.Length(min=1)),
                    vol.Optional("media_content_id"): _media_source,
                    vol.Optional("media"): _selected_media,
                }
            ),
            cv.has_at_least_one_key("media_content_id", "media"),
            cv.has_at_most_one_key("media_content_id", "media"),
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def manage_recording(call):
        library = _library_for_device(hass, call.data["device_id"])
        result = await async_manage_recording(
            hass, library, action="rename" if call.service == "rename_audio" else "delete", **call.data
        )
        return result if call.return_response else None

    for name in ("rename_audio", "delete_audio"):
        fields = {
            vol.Required("device_id"): cv.string,
            vol.Optional("recording"): _saved_recording,
            vol.Optional("media_content_id"): _saved_media_id,
        }
        if name == "rename_audio":
            fields[vol.Required("new_name")] = vol.All(cv.string, vol.Length(min=1))
        else:
            fields[vol.Required("confirm_delete")] = vol.All(cv.boolean, vol.In([True]))
        service.async_register_admin_service(
            hass,
            DOMAIN,
            name,
            manage_recording,
            schema=vol.All(
                vol.Schema(fields),
                cv.has_at_least_one_key("recording", "media_content_id"),
                cv.has_at_most_one_key("recording", "media_content_id"),
            ),
            supports_response=SupportsResponse.OPTIONAL,
        )


async def async_manage_recording(
    hass, library, *, device_id, action, recording=None, media_content_id=None, new_name=None, confirm_delete=False
):
    selected = recording["media_content_id"] if recording is not None else media_content_id
    try:
        selection = parse_recording_selection(selected)
        if selection.camera_key != camera_key(library.library_entry_id, library.entry_id, library.channel):
            raise StoredAudioError("Choose a recording belonging to the selected camera")
        if not 3 <= selection.file_id <= 14:
            raise StoredAudioError("Factory recordings cannot be renamed or deleted")
        if action == "rename":
            validate_recording_name(new_name)
        elif confirm_delete is not True:
            raise StoredAudioError("Confirm deletion of the selected custom recording")
    except StoredAudioError as error:
        raise ServiceValidationError(str(error)) from None
    store = get_preview_store(hass)
    async with library.lock:
        library.last_recording_error = None
        try:
            receipt = await async_edit_recording(
                library.connect(),
                library.channel,
                selection,
                action=action,
                new_name=new_name,
                confirm_delete=confirm_delete,
                device_write_authorized=True,
                before_write=lambda: store.invalidate_recording(library, selection.fingerprint),
            )
            result = {"device_id": device_id, "channel": library.channel, "success": True, **asdict(receipt)}
            library.last_recording_change = result
            try:
                catalog = await library.async_read()
                library.async_set_updated_data(catalog)
                store.invalidate_catalog(library, catalog)
                result["inventory_refreshed"] = True
            except Exception:
                result["inventory_refreshed"] = False
                library.async_set_update_error(
                    UpdateFailed("Recording change verified; displayed inventory needs refresh")
                )
            return result
        except asyncio.CancelledError:
            store.invalidate_recording(library, selection.fingerprint)
            library.last_recording_error = {"action": action, "type": "CancelledError", "outcome_uncertain": True}
            library.async_set_update_error(
                UpdateFailed("Recording change cancelled; inspect inventory before retrying")
            )
            raise
        except Exception as error:
            uncertain = error.outcome_uncertain if isinstance(error, RecordingChangeError) else True
            library.last_recording_error = {
                "action": action,
                "type": type(error).__name__,
                "outcome_uncertain": uncertain,
            }
            if uncertain:
                store.invalidate_recording(library, selection.fingerprint)
                library.async_set_update_error(
                    UpdateFailed("Recording outcome uncertain; inspect inventory before retrying")
                )
            if isinstance(error, StoredAudioError):
                raise ServiceValidationError(str(error)) from None
            raise HomeAssistantError(f"Could not change recording ({type(error).__name__})") from None
        finally:
            library.async_update_listeners()


async def async_upload_for_library(hass, library, *, device_id, name, media_content_id=None, media=None):
    """Create a recording, verify it after reconnect and never play it."""
    profile = library.profile
    if profile is None:
        raise ServiceValidationError("No validated recording profile matches this camera and firmware")
    try:
        validate_recording_name(name)
    except StoredAudioError as error:
        raise ServiceValidationError(str(error)) from None
    selected = media["media_content_id"] if media is not None else media_content_id
    async with library.lock:
        library.last_upload_error = None
        library.upload_status = "preparing"
        library.async_update_listeners()
        try:
            prepared = await async_prepare_media(
                hass, selected, max_duration=profile.max_duration, max_payload_bytes=profile.max_payload_bytes
            )
            library.upload_status = "uploading"
            library.async_update_listeners()
            receipt = await async_upload_prepared(
                library.connect(), library.channel, prepared, name, device_write_authorized=True
            )
            result = {"device_id": device_id, "channel": library.channel, **asdict(receipt)}
            library.last_upload_result = result
            library.upload_status = "completed"
            try:
                library.async_set_updated_data(await library.async_read())
                result["inventory_refreshed"] = True
            except Exception:
                result["inventory_refreshed"] = False
                library.async_set_update_error(UpdateFailed("Upload saved; could not refresh displayed recordings"))
            return result
        except asyncio.CancelledError:
            library.upload_status = "cancelled_check_inventory"
            raise
        except Exception as error:
            library.upload_status = "failed_check_inventory"
            library.last_upload_error = {"type": type(error).__name__}
            if isinstance(error, AudioUploadError):
                library.last_upload_error.update(
                    external_id=error.external_id,
                    outcome_uncertain=error.outcome_uncertain,
                    transport_failure=error.transport_failure,
                )
            if isinstance(error, StoredAudioError):
                raise ServiceValidationError(str(error)) from None
            raise HomeAssistantError(f"Could not upload recording ({type(error).__name__})") from None
        finally:
            library.async_update_listeners()
