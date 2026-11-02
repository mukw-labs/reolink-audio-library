"""Private, bounded, temporary WAV delivery through HA authentication."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import timedelta
import re
import secrets
import time

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN
from .recording_identity import camera_key, recording_fingerprint

CACHE_TTL = 600
MAX_CACHE_BYTES = 8 * 1024 * 1024
MAX_CACHE_ENTRIES = 16
URL_PREFIX = "/api/reolink_audio_library/recording"


def active_libraries(hass):
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is not ConfigEntryState.LOADED or not isinstance(getattr(entry, "runtime_data", None), dict):
            continue
        for library in entry.runtime_data.values():
            try:
                if library.profile is None:
                    continue
            except Exception:
                continue
            key = camera_key(entry.entry_id, library.entry_id, library.channel)
            yield key, entry.entry_id, library


@dataclass(frozen=True)
class CachedPreview:
    token: str
    library_entry_id: str
    camera: tuple[str, int]
    fingerprint: str
    expires: float
    operation_lock: object = field(repr=False)
    wav: bytes = field(repr=False)

    @property
    def url(self):
        return f"{URL_PREFIX}/{self.token}.wav"


class PreviewStore:
    """Fixed-lifetime memory cache; downloads are serialized by the camera lock."""

    def __init__(self, hass):
        self.hass = hass
        self.items: OrderedDict[str, CachedPreview] = OrderedDict()

        @callback
        def cleanup(_now):
            self.prune()

        cancel = async_track_time_interval(hass, cleanup, timedelta(seconds=60))

        @callback
        def stop(_event):
            cancel()
            self.items.clear()

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop)

    def prune(self):
        now = time.monotonic()
        for token, item in tuple(self.items.items()):
            if item.expires <= now:
                del self.items[token]

    def find(self, library_entry_id, library, fingerprint):
        self.prune()
        return next(
            (
                item
                for item in self.items.values()
                if item.library_entry_id == library_entry_id
                and item.camera == (library.entry_id, library.channel)
                and item.fingerprint == fingerprint
            ),
            None,
        )

    def put(self, library_entry_id, library, fingerprint, wav):
        self.prune()
        if not wav or len(wav) > MAX_CACHE_BYTES:
            raise ValueError("Recording preview exceeds the cache limit")
        item = CachedPreview(
            secrets.token_urlsafe(32),
            library_entry_id,
            (library.entry_id, library.channel),
            fingerprint,
            time.monotonic() + CACHE_TTL,
            library.lock,
            wav,
        )
        while self.items and (
            len(self.items) >= MAX_CACHE_ENTRIES
            or sum(len(old.wav) for old in self.items.values()) + len(wav) > MAX_CACHE_BYTES
        ):
            self.items.popitem(last=False)
        self.items[item.token] = item
        return item

    def invalidate_catalog(self, library, catalog):
        valid = {recording_fingerprint(catalog, record) for record in catalog.native.files}
        for token, item in tuple(self.items.items()):
            if (
                item.operation_lock is library.lock
                and item.camera[1] == library.channel
                and item.fingerprint not in valid
            ):
                del self.items[token]

    def invalidate_recording(self, library, fingerprint):
        for token, item in tuple(self.items.items()):
            if (
                item.operation_lock is library.lock
                and item.camera[1] == library.channel
                and item.fingerprint == fingerprint
            ):
                del self.items[token]

    def unload(self, library_entry_id):
        for token, item in tuple(self.items.items()):
            if item.library_entry_id == library_entry_id:
                del self.items[token]


class RecordingPreviewView(HomeAssistantView):
    """HA signs this authenticated path for browser/selected-player fetching."""

    url = URL_PREFIX + "/{token}.wav"
    name = "api:reolink_audio_library:recording"
    requires_auth = True

    def __init__(self, store):
        self.store = store

    async def get(self, request, token):
        self.store.prune()
        item = self.store.items.get(token)
        active = {(entry, library.entry_id, library.channel) for _, entry, library in active_libraries(self.store.hass)}
        if item is None or (item.library_entry_id, *item.camera) not in active:
            raise web.HTTPNotFound()
        size = len(item.wav)
        start, end, status = 0, size - 1, 200
        headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
        if value := request.headers.get("Range"):
            match = re.fullmatch(r"bytes=([0-9]*)-([0-9]*)", value)
            if match is None or not any(match.groups()) or any(len(v) > 12 for v in match.groups()):
                raise web.HTTPRequestRangeNotSatisfiable(headers={"Content-Range": f"bytes */{size}"})
            first, last = match.groups()
            if first:
                start, end = int(first), min(int(last), size - 1) if last else size - 1
            else:
                start = max(0, size - int(last))
            if start > end or start >= size:
                raise web.HTTPRequestRangeNotSatisfiable(headers={"Content-Range": f"bytes */{size}"})
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            status = 206
        headers["Content-Length"] = str(end - start + 1)
        return web.Response(
            body=item.wav[start : end + 1] if request.method != "HEAD" else b"",
            status=status,
            content_type="audio/wav",
            headers=headers,
        )

    async def head(self, request, token):
        return await self.get(request, token)


def get_preview_store(hass) -> PreviewStore:
    data = hass.data.setdefault(DOMAIN, {})
    if (store := data.get("preview_store")) is None:
        store = data["preview_store"] = PreviewStore(hass)
        hass.http.register_view(RecordingPreviewView(store))
    return store
