"""Real HA media-source/HTTP delivery; all camera sessions are offline fakes."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

from homeassistant.components import media_source  # noqa: E402
from homeassistant.components.http.auth import async_sign_path  # noqa: E402
from homeassistant.components.media_player import BrowseError  # noqa: E402
from homeassistant.components.media_player.browse_media import async_process_play_media_url  # noqa: E402
from homeassistant.components.media_source import Unresolvable  # noqa: E402
from test_audio_services_ha import audio_setup as audio_setup, supported_library as supported_library  # noqa: E402

from custom_components.reolink_audio_library import audio_preview as preview  # noqa: E402

ROOT = "media-source://reolink_audio_library"


@pytest.fixture
async def recording(hass, audio_setup, supported_library):
    library = supported_library.library
    catalog = supported_library.catalog
    api = SimpleNamespace(
        baichuan=SimpleNamespace(
            get_info=AsyncMock(
                return_value={
                    "type": catalog.profile.model,
                    "hardwareVersion": catalog.profile.hardware,
                    "firmwareVersion": catalog.profile.firmware,
                }
            )
        )
    )
    closed = Mock()

    @asynccontextmanager
    async def connect():
        try:
            yield api
        finally:
            closed()

    library.connect = Mock(return_value=connect)
    with (
        patch(
            "custom_components.reolink_audio_library.media_source.async_read_native_inventory",
            AsyncMock(return_value=catalog.native),
        ) as native,
        patch(
            "custom_components.reolink_audio_library.media_source.async_read_inventory",
            AsyncMock(return_value=catalog.custom),
        ),
        patch(
            "custom_components.reolink_audio_library.media_source.async_export_audio",
            AsyncMock(return_value=bytes(4000)),
        ) as export,
    ):
        root = await media_source.async_browse_media(hass, ROOT)
        assert len(root.children) == 1
        camera = await media_source.async_browse_media(hass, root.children[0].media_content_id)
        yield SimpleNamespace(
            uri=camera.children[-1].media_content_id,
            camera=root.children[0].media_content_id,
            export=export,
            native=native,
            closed=closed,
            library=library,
            catalog=catalog,
            store=preview.get_preview_store(hass),
            entry=audio_setup.entry,
        )
        for shared in audio_setup.apis.values():
            shared.play_quick_reply.assert_not_awaited()
        supported_library.api.baichuan.QuickReplyPlay.assert_not_awaited()


async def resolve(hass, recording, target=None):
    return await media_source.async_resolve_media(hass, recording.uri, target)


async def test_global_browser_is_metadata_only_and_preserves_camera_folder(hass, audio_setup, recording):
    root = await media_source.async_browse_media(hass, None)
    assert ROOT in [item.media_content_id for item in root.children]
    camera = await media_source.async_browse_media(hass, recording.camera)
    assert [item.title for item in camera.children] == [
        "Stock 0",
        "Stock 1",
        "Stock 2",
        "Existing reply",
        "Uploaded reply",
    ]
    assert all(item.can_play and item.media_content_type == "audio/wav" for item in camera.children)
    recording.export.assert_not_awaited()


async def test_resolve_uses_owned_session_cache_and_refreshes_inventory(hass, recording):
    first = await resolve(hass, recording)
    second = await resolve(hass, recording)
    assert first.url == second.url and first.mime_type == "audio/wav"
    recording.export.assert_awaited_once()
    assert recording.closed.call_count == 1
    assert recording.library.async_read.await_count == 3  # browse + two resolves
    assert len(recording.store.items) == 1
    cached = next(iter(recording.store.items.values()))
    assert cached.wav[:4] == b"RIFF" and len(cached.wav) == 16044


@pytest.mark.parametrize("change", ["name", "size", "external_id", "removed"])
async def test_stale_slot_metadata_and_uuid_never_resolve_old_selection(hass, recording, change):
    await resolve(hass, recording)
    old = recording.catalog

    def changed(items):
        return (
            tuple(
                replace(item, **{change: {"name": "Changed", "size": 8000, "external_id": "different"}[change]})
                if item.id == 4
                else item
                for item in items
            )
            if change != "removed"
            else tuple(item for item in items if item.id != 4)
        )

    catalog = replace(
        old,
        native=replace(old.native, files=changed(old.native.files)),
        custom=replace(old.custom, files=changed(old.custom.files)),
    )
    recording.library.async_read.return_value = catalog
    with pytest.raises(Unresolvable, match="changed or was removed"):
        await resolve(hass, recording)
    assert not recording.store.items
    recording.export.assert_awaited_once()


@pytest.mark.parametrize(
    "identifier",
    ["../x", "bad", "0" * 24 + "/04/" + "a" * 64, "0" * 24 + "/4/" + "A" * 64, "0" * 24 + "/4/" + "a" * 64 + "?x=y"],
)
async def test_invalid_identifiers_never_connect(hass, recording, identifier):
    recording.library.connect.reset_mock()
    with pytest.raises(Unresolvable):
        await media_source.async_resolve_media(hass, ROOT + "/" + identifier, None)
    recording.library.connect.assert_not_called()


async def test_camera_output_is_rejected_before_export(hass, recording):
    with pytest.raises(Unresolvable, match="another speaker"):
        await resolve(hass, recording, "media_player.reolink_talk_front")
    recording.export.assert_not_awaited()


async def test_concurrent_resolutions_export_once_under_camera_lock(hass, recording):
    first, second = await asyncio.gather(resolve(hass, recording), resolve(hass, recording))
    assert first.url == second.url
    recording.export.assert_awaited_once()


async def test_changed_during_download_does_not_publish_cache(hass, recording):
    recording.native.side_effect = [recording.catalog.native, replace(recording.catalog.native, files=())]
    with pytest.raises(Unresolvable, match="changed during"):
        await resolve(hass, recording)
    assert not recording.store.items
    assert recording.closed.call_count == 1


async def test_export_cancellation_closes_owned_session_and_releases_lock(hass, recording):
    started = asyncio.Event()

    async def wait(*args):
        started.set()
        await asyncio.Event().wait()

    recording.export.side_effect = wait
    task = asyncio.create_task(resolve(hass, recording))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert recording.closed.call_count == 1
    assert not recording.library.lock.locked()
    assert not recording.store.items


async def test_errors_are_sanitized(hass, recording):
    recording.export.side_effect = RuntimeError("secret camera password")
    with pytest.raises(Unresolvable, match=r"\(RuntimeError\)") as error:
        await resolve(hass, recording)
    assert "secret" not in str(error.value)
    assert not recording.store.items


async def test_unload_revokes_cached_url(hass, recording, hass_client):
    media = await resolve(hass, recording)
    client = await hass_client()
    assert (await client.get(media.url)).status == 200
    assert await hass.config_entries.async_unload(recording.entry.entry_id)
    assert not recording.store.items
    assert (await client.get(media.url)).status == 404
    with pytest.raises(BrowseError):
        await media_source.async_browse_media(hass, recording.camera)


async def test_cache_expiration_and_eviction_are_bounded(hass, recording, monkeypatch):
    await resolve(hass, recording)
    store = recording.store
    now = preview.time.monotonic()
    with patch.object(preview.time, "monotonic", return_value=now + preview.CACHE_TTL + 1):
        store.prune()
    assert not store.items
    monkeypatch.setattr(preview, "MAX_CACHE_ENTRIES", 2)
    monkeypatch.setattr(preview, "MAX_CACHE_BYTES", 7)
    old = store.put(recording.entry.entry_id, recording.library, "a", b"1234")
    new = store.put(recording.entry.entry_id, recording.library, "b", b"1234")
    assert list(store.items) == [new.token] and old.token != new.token
    with pytest.raises(ValueError):
        store.put(recording.entry.entry_id, recording.library, "c", bytes(8))


async def test_http_auth_signed_urls_head_ranges_and_expiration(hass, recording, hass_client_no_auth):
    media = await resolve(hass, recording)
    client = await hass_client_no_auth()
    assert (await client.get(media.url)).status == 401
    signed = async_sign_path(hass, media.url, timedelta(minutes=5))
    response = await client.get(signed)
    data = await response.read()
    assert response.status == 200 and data[:4] == b"RIFF"
    assert response.headers["Content-Type"] == "audio/wav"
    assert response.headers["Cache-Control"] == "private, no-store"
    response = await client.head(signed)
    assert response.status == 200 and await response.read() == b""
    assert response.headers["Content-Length"] == str(len(data))
    for value, expected, offset in [
        ("bytes=0-3", b"RIFF", 0),
        ("bytes=-4", data[-4:], len(data) - 4),
        ("bytes=44-", data[44:], 44),
    ]:
        response = await client.get(signed, headers={"Range": value})
        assert response.status == 206 and await response.read() == expected
        assert response.headers["Content-Range"] == f"bytes {offset}-{offset + len(expected) - 1}/{len(data)}"
    for value in ["bytes=999999-", "bytes=1-0", "bytes=0-1,3-5", "bytes=-0", "items=1-2", "bytes=-"]:
        assert (await client.get(signed, headers={"Range": value})).status == 416
    assert (await client.get(signed + "tampered")).status == 401
    expired = async_sign_path(hass, media.url, timedelta(seconds=-1))
    assert (await client.get(expired)).status == 401
    recording.export.assert_awaited_once()


async def test_websocket_resolve_signs_for_web_browser_without_playback(
    hass, recording, hass_ws_client, hass_client_no_auth
):
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "media_source/resolve_media", "media_content_id": recording.uri})
    result = await ws.receive_json()
    assert result["success"]
    assert result["result"]["mime_type"] == "audio/wav"
    assert "authSig=" in result["result"]["url"]
    client = await hass_client_no_auth()
    response = await client.get(result["result"]["url"])
    assert response.status == 200 and (await response.read()).startswith(b"RIFF")


async def test_selected_player_url_is_absolute_and_signed(hass, recording):
    hass.config.internal_url = "http://homeassistant.local:8123"
    media = await resolve(hass, recording, "media_player.lounge")
    url = async_process_play_media_url(hass, media.url)
    assert url.startswith("http://homeassistant.local:8123/api/reolink_audio_library/recording/")
    assert "authSig=" in url
