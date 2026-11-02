"""Read-only saved-recording catalog and one operation lock per physical camera."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, replace
from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .audio_connection import owned_host_factory
from .audio_upload import AudioUploadError, UploadProfile, profile_for_device_info, validate_inventory_consistency
from .const import DOMAIN
from .native_audio import async_read_native_inventory
from .stored_audio import AudioInventory, StoredAudioError, async_read_inventory

_LOGGER = logging.getLogger(__name__)


def shared_api(hass, entry_id: str, channel: int):
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry.state is not ConfigEntryState.LOADED:
        raise StoredAudioError("Load this camera's official Reolink integration first")
    api = getattr(getattr(getattr(entry, "runtime_data", None), "host", None), "api", None)
    if api is None:
        raise StoredAudioError("The official Reolink integration has no authenticated camera connection available")
    if channel not in api.channels:
        raise StoredAudioError("The selected channel is not present on this Reolink host")
    return api


def cached_profile(api, channel: int) -> UploadProfile | None:
    """Cached identity is a UI hint only; every upload verifies live identity."""
    try:
        return profile_for_device_info(
            {
                "type": api.camera_model(None),
                "hardwareVersion": api.camera_hardware_version(None),
                "firmwareVersion": api.camera_sw_version(None),
            },
            channel,
        )
    except (AttributeError, AudioUploadError):
        return None


def camera_lock(hass, entry_id: str) -> asyncio.Lock:
    entry = hass.config_entries.async_get_entry(entry_id)
    data = entry.data if entry is not None else {}
    # Aliased entries and channels on the same physical device must serialize.
    host = getattr(getattr(entry, "runtime_data", None), "host", None)
    identifier = getattr(host, "unique_id", None)
    key = (
        ("device", identifier)
        if isinstance(identifier, str) and identifier
        else ("endpoint", str(data.get("host", entry_id)).casefold(), int(data.get("baichuan_port", 9000)))
    )
    return hass.data.setdefault(DOMAIN, {}).setdefault("camera_locks", {}).setdefault(key, asyncio.Lock())


@dataclass(frozen=True)
class AudioCatalog:
    custom: AudioInventory
    native: AudioInventory
    profile: UploadProfile

    def as_dict(self) -> dict:
        profile = self.profile
        records = []
        custom_by_id = {record.id: record for record in self.custom.files}
        for record in self.native.files:
            own = custom_by_id.get(record.id, record)
            records.append(
                replace(
                    record,
                    external_id=own.external_id,
                    duration=record.size / (profile.sample_rate / 2) if record.size is not None else None,
                    is_builtin=record.id < profile.custom_id_min,
                )
            )
        occupied = {record.id: record for record in records}
        slots = []
        if self.custom.max_files == profile.custom_id_max - profile.custom_id_min + 1:
            for slot_id in range(profile.custom_id_max + 1):
                item = occupied.get(slot_id)
                slots.append(
                    {
                        "id": slot_id,
                        "status": "factory" if slot_id < profile.custom_id_min else "occupied" if item else "available",
                        "name": item.name if item else None,
                    }
                )
        used = len(self.custom.files)
        capacity = self.custom.max_files
        return {
            "supported": True,
            "files": [asdict(record) for record in records],
            "max_files": capacity,
            "used_slots": used,
            "free_slots": max(0, capacity - used) if capacity is not None else None,
            "slots": slots,
            "slot_selection_supported": False,
            "available_slot_id": None,
            "slot_allocation_verified": True,
            "device_upload_supported": True,
            "upload_supported": True,
            "audio_length_unit": "seconds_truncated",
            "max_duration": profile.max_duration,
            "preview_supported": True,
        }


class AudioLibrary(DataUpdateCoordinator[AudioCatalog]):
    """Keep browser and capacity sensor synchronized without changing HA's Host."""

    def __init__(self, hass, library_entry, entry_id: str, channel: int):
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} recordings {entry_id}:{channel}",
            config_entry=library_entry,
            update_interval=timedelta(minutes=5),
        )
        self.library_entry_id = library_entry.entry_id
        self.last_recording_change = None
        self.last_recording_error = None
        self.entry_id = entry_id
        self.channel = channel
        self.upload_status = "idle"
        self.last_upload_result = None
        self.last_upload_error = None
        self.lock = camera_lock(hass, entry_id)

    @property
    def api(self):
        return shared_api(self.hass, self.entry_id, self.channel)

    @property
    def device_info(self):
        # Associate with the existing official device, using its stable ID.
        from homeassistant.helpers.device_registry import DeviceInfo

        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        host = getattr(getattr(entry, "runtime_data", None), "host", None)
        identifier = getattr(host, "unique_id", None)
        return DeviceInfo(identifiers={("reolink", identifier)}) if isinstance(identifier, str) else None

    @property
    def profile(self):
        return cached_profile(self.api, self.channel)

    def connect(self):
        entry = self.hass.config_entries.async_get_entry(self.entry_id)
        if entry is None:
            raise StoredAudioError("The camera's official Reolink entry was removed")
        # Reolink devices use self-signed HTTPS certificates. Match the library's
        # own local-device TLS policy without changing HA's default session.
        return owned_host_factory(
            entry.data, session_callback=lambda: async_get_clientsession(self.hass, verify_ssl=False)
        )

    async def async_read(self) -> AudioCatalog:
        """Caller holds the camera lock; each read uses its own library session."""
        try:
            async with asyncio.timeout(45):
                async with self.connect()() as api:
                    profile = profile_for_device_info(await api.baichuan.get_info(), self.channel)
                    native = await async_read_native_inventory(api, self.channel)
                    custom = await async_read_inventory(api, self.channel)
                    validate_inventory_consistency(custom, native, profile)
                    if (
                        custom.max_files is None
                        or not 0 < custom.max_files <= profile.custom_id_max - profile.custom_id_min + 1
                    ):
                        raise StoredAudioError("Camera reported an unrecognized custom recording capacity")
                    catalog = AudioCatalog(custom, native, profile)
                    if store := self.hass.data.get(DOMAIN, {}).get("preview_store"):
                        store.invalidate_catalog(self, catalog)
                    return catalog
        except StoredAudioError:
            raise
        except Exception as error:
            raise StoredAudioError(f"Could not read saved recordings ({type(error).__name__})") from None

    async def _async_update_data(self):
        async with self.lock:
            try:
                return await self.async_read()
            except Exception as error:
                # Never expose arbitrary library errors, credentials or tokens.
                raise UpdateFailed(f"Could not read saved recordings ({type(error).__name__})") from None


def libraries_for_entry(hass, library_entry) -> dict:
    from .const import CONF_REOLINK_ENTRY_IDS, DEFAULT_CHANNEL

    channel = DEFAULT_CHANNEL
    selected = library_entry.options.get(CONF_REOLINK_ENTRY_IDS, library_entry.data.get(CONF_REOLINK_ENTRY_IDS, []))
    return {
        entry.entry_id: AudioLibrary(hass, library_entry, entry.entry_id, channel)
        for entry in hass.config_entries.async_entries("reolink")
        if entry.entry_id in selected and all(key in entry.data for key in ("host", "username", "password"))
    }
