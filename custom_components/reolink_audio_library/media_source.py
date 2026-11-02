"""Camera recordings as WAV media for Web browser or another HA speaker."""

from __future__ import annotations

import asyncio
import re

from homeassistant.components.media_player import BrowseError, MediaClass, MediaType
from homeassistant.components.media_source import BrowseMediaSource, MediaSource, PlayMedia, Unresolvable
from homeassistant.helpers import entity_registry as er

from .audio_export import async_export_audio
from .audio_preview import active_libraries, get_preview_store, recording_fingerprint
from .audio_upload import profile_for_device_info
from .const import DOMAIN
from .native_audio import async_read_native_inventory
from .stored_audio import StoredAudioError, async_read_inventory
from .stored_audio_codec import stored_ima_to_wav


async def async_get_media_source(hass):
    return ReolinkRecordingSource(hass)


class ReolinkRecordingSource(MediaSource):
    name = "Reolink Audio Library"

    def __init__(self, hass):
        super().__init__(DOMAIN)
        self.hass = hass
        self.store = get_preview_store(hass)

    def _camera(self, identifier):
        if not re.fullmatch(r"[0-9a-f]{24}(?:/(?:[0-9]|1[0-4])/[0-9a-f]{64})?", identifier):
            raise StoredAudioError("Invalid recording identifier; browse Reolink recordings again")
        camera_id = identifier.split("/")[0]
        for key, library_id, library in active_libraries(self.hass):
            if key == camera_id:
                return key, library_id, library
        raise StoredAudioError("This camera is unavailable or its recording format is unsupported")

    @staticmethod
    def _folder(identifier, title, children):
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=identifier,
            media_class=MediaClass.DIRECTORY,
            media_content_type=MediaType.MUSIC,
            title=title,
            can_play=False,
            can_expand=True,
            children=children,
        )

    def _title(self, library):
        entry = self.hass.config_entries.async_get_entry(library.entry_id)
        return entry.title if entry else "Camera"

    async def async_browse_media(self, item):
        try:
            if not item.identifier:
                return self._folder(
                    None,
                    self.name,
                    [self._folder(key, self._title(library), []) for key, _, library in active_libraries(self.hass)],
                )
            key, _, library = self._camera(item.identifier)
            if "/" in item.identifier:
                raise StoredAudioError("A recording is not a directory")
            async with library.lock:
                catalog = await library.async_read()
                library.async_set_updated_data(catalog)
            self.store.invalidate_catalog(library, catalog)
            return self._folder(
                key,
                self._title(library),
                [
                    BrowseMediaSource(
                        domain=DOMAIN,
                        identifier=f"{key}/{record.id}/{recording_fingerprint(catalog, record)}",
                        media_class=MediaClass.MUSIC,
                        media_content_type="audio/wav",
                        title=record.name,
                        can_play=True,
                        can_expand=False,
                    )
                    for record in catalog.native.files
                ],
            )
        except StoredAudioError as error:
            raise BrowseError(str(error)) from None

    async def async_resolve_media(self, item):
        try:
            if item.target_media_player and (
                (target := er.async_get(self.hass).async_get(item.target_media_player)) is not None
                and target.platform == "reolink_talk"
            ):
                raise StoredAudioError("Choose Web browser or another speaker to listen to Reolink recordings")
            _, library_id, library = self._camera(item.identifier)
            parts = item.identifier.split("/")
            if len(parts) != 3:
                raise StoredAudioError("Choose a recording inside the camera folder")
            async with library.lock:
                async with asyncio.timeout(90):
                    catalog = await library.async_read()
                    library.async_set_updated_data(catalog)
                    self.store.invalidate_catalog(library, catalog)
                    record = next((record for record in catalog.native.files if record.id == int(parts[1])), None)
                    if record is None or recording_fingerprint(catalog, record) != parts[2]:
                        raise StoredAudioError("This recording changed or was removed; browse the camera again")
                    if (cached := self.store.find(library_id, library, parts[2])) is not None:
                        return PlayMedia(cached.url, "audio/wav")
                    async with library.connect()() as api:
                        profile = profile_for_device_info(await api.baichuan.get_info(), library.channel)
                        if (
                            await async_read_native_inventory(api, library.channel) != catalog.native
                            or await async_read_inventory(api, library.channel) != catalog.custom
                        ):
                            raise StoredAudioError("Camera recordings changed; browse the camera again")
                        audio = await async_export_audio(api.baichuan, library.channel, record.id, record.size)
                        if (
                            await async_read_native_inventory(api, library.channel) != catalog.native
                            or await async_read_inventory(api, library.channel) != catalog.custom
                        ):
                            raise StoredAudioError("Camera recordings changed during export; browse again")
                    wav = await self.hass.async_add_executor_job(
                        lambda: stored_ima_to_wav(
                            audio, sample_rate=profile.sample_rate, max_input_bytes=profile.max_payload_bytes
                        )
                    )
                    # An unload can run while export/decoding awaits. Never
                    # publish another URL after the owning entry is unloaded.
                    if not any(
                        entry == library_id and active is library for _, entry, active in active_libraries(self.hass)
                    ):
                        raise StoredAudioError("This camera was unloaded during recording export")
                    return PlayMedia(self.store.put(library_id, library, parts[2], wav).url, "audio/wav")
        except StoredAudioError as error:
            raise Unresolvable(str(error)) from None
        except Exception as error:
            raise Unresolvable(f"Could not prepare recording preview ({type(error).__name__})") from None
