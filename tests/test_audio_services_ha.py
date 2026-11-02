"""Real HA device actions, setup and coexistence with a foreign Talk component."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

pytest.importorskip("pytest_homeassistant_custom_component")

import voluptuous as vol  # noqa: E402
from homeassistant.config_entries import ConfigEntryState  # noqa: E402
from homeassistant.exceptions import ServiceValidationError  # noqa: E402
from homeassistant.helpers import device_registry as dr, entity_registry as er  # noqa: E402
from pytest_homeassistant_custom_component.common import MockConfigEntry  # noqa: E402

from custom_components.reolink_audio_library.audio_library import AudioCatalog  # noqa: E402
from custom_components.reolink_audio_library.audio_preparation import PreparedAudio  # noqa: E402
from custom_components.reolink_audio_library.audio_upload import RESEARCHED_WIFI_W, AudioUploadError, UploadReceipt  # noqa: E402
from custom_components.reolink_audio_library.const import DOMAIN, CONF_REOLINK_ENTRY_IDS  # noqa: E402
from custom_components.reolink_audio_library.stored_audio import AudioInventory, AudioRecord  # noqa: E402

STATUS = "sensor.reolink_audio_library_front_upload_status"


def catalog():
    stock = tuple(AudioRecord(index, f"Stock {index}", "alarm", 8000, 1) for index in range(3))
    custom = (
        AudioRecord(3, "Existing reply", "reply", 16000, 2, "old-id"),
        AudioRecord(4, "Uploaded reply", "reply", 4000, 0, "upload-uuid"),
    )
    return AudioCatalog(AudioInventory(custom, 12), AudioInventory(stock + custom), RESEARCHED_WIFI_W)


@pytest.fixture
async def audio_setup(hass, enable_custom_integrations):
    apis = {}
    official_entries = []
    devices = []
    for title in ("Front", "Back"):
        profile = RESEARCHED_WIFI_W
        api = SimpleNamespace(
            channels=[0],
            camera_model=Mock(return_value=profile.model if title == "Front" else "Unsupported"),
            camera_hardware_version=Mock(return_value=profile.hardware),
            camera_sw_version=Mock(return_value=profile.firmware),
            baichuan=SimpleNamespace(send=AsyncMock(), QuickReplyPlay=AsyncMock()),
            play_quick_reply=AsyncMock(),
            logout=AsyncMock(),
        )
        apis[title] = api
        entry = MockConfigEntry(
            domain="reolink",
            title=title,
            data={
                "host": title.lower() + ".invalid",
                "username": "fake",
                "password": "fake",
                "port": 443,
                "use_https": True,
            },
        )
        entry.runtime_data = SimpleNamespace(host=SimpleNamespace(api=api, unique_id=title.lower() + "-physical-id"))
        entry.add_to_hass(hass)
        official_entries.append(entry)
        devices.append(
            dr.async_get(hass).async_get_or_create(
                config_entry_id=entry.entry_id, identifiers={("reolink", title.lower() + "-physical-id")}, name=title
            )
        )
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Reolink Audio Library",
        data={CONF_REOLINK_ENTRY_IDS: [e.entry_id for e in official_entries]},
        unique_id=DOMAIN,
    )
    entry.add_to_hass(hass)
    with (
        patch("homeassistant.components.reolink.async_setup_entry", AsyncMock(return_value=True)),
        patch("homeassistant.components.reolink.async_unload_entry", AsyncMock(return_value=True)),
        patch("homeassistant.components.reolink.async_migrate_entry", AsyncMock(return_value=True), create=True),
        patch(
            "custom_components.reolink_audio_library.audio_library.AudioLibrary.async_read",
            AsyncMock(return_value=catalog()),
        ),
    ):
        for official in official_entries:
            if official.state is ConfigEntryState.NOT_LOADED:
                assert await hass.config_entries.async_setup(official.entry_id)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield SimpleNamespace(entry=entry, official_entries=official_entries, devices=devices, apis=apis)
        if entry.state is ConfigEntryState.LOADED:
            assert await hass.config_entries.async_unload(entry.entry_id)
        for official in official_entries:
            if official.state is ConfigEntryState.LOADED:
                assert await hass.config_entries.async_unload(official.entry_id)
        await hass.async_block_till_done()
        for api in apis.values():
            api.logout.assert_not_awaited()
            api.play_quick_reply.assert_not_awaited()
            api.baichuan.QuickReplyPlay.assert_not_awaited()


@pytest.fixture
def supported_library(audio_setup):
    library = audio_setup.entry.runtime_data[audio_setup.official_entries[0].entry_id]
    library.async_read = AsyncMock(return_value=catalog())
    library.connect = Mock(return_value=object())
    # Register a foreign Talk entity without loading or importing its code.
    foreign = MockConfigEntry(domain="reolink_talk", title="Existing Talk", data={})
    foreign.add_to_hass(library.hass)
    er.async_get(library.hass).async_get_or_create(
        "media_player", "reolink_talk", "original-front", suggested_object_id="reolink_talk_front", config_entry=foreign
    )
    library.hass.states.async_set("media_player.reolink_talk_front", "idle")
    return SimpleNamespace(library=library, catalog=catalog(), api=audio_setup.apis["Front"], profile=RESEARCHED_WIFI_W)


async def action(hass, setup, name, data=None, *, response=True, context=None):
    return await hass.services.async_call(
        DOMAIN,
        name,
        {"device_id": setup.devices[0].id, **(data or {})},
        blocking=True,
        return_response=response,
        context=context,
    )


def receipt():
    return UploadReceipt(5, "Delivery", 0.5, 4000, "11111111-2222-4333-8444-555555555555", True, True)


async def test_only_recording_sensors_and_own_actions_are_created(hass, audio_setup):
    entries = er.async_entries_for_config_entry(er.async_get(hass), audio_setup.entry.entry_id)
    assert len(entries) == 3
    assert all(
        item.domain == "sensor" and item.platform == DOMAIN and item.unique_id.startswith(DOMAIN + ":")
        for item in entries
    )
    assert set(hass.services.async_services()[DOMAIN]) == {"list_audio", "upload_audio", "rename_audio", "delete_audio"}
    assert hass.states.get("sensor.reolink_audio_library_front_custom_recordings").state == "2"
    assert hass.states.get("sensor.reolink_audio_library_front_free_recording_slots").state == "10"
    assert hass.states.get(STATUS).state == "idle"
    assert len(hass.states.get("sensor.reolink_audio_library_front_custom_recordings").attributes["slots"]) == 15
    assert not any(item.domain == "media_player" for item in entries)


async def test_list_action_targets_existing_device_without_talk(hass, audio_setup):
    result = await action(hass, audio_setup, "list_audio")
    assert [item["id"] for item in result["files"]] == [0, 1, 2, 3, 4]
    assert result["free_slots"] == 10 and result["preview_supported"]
    assert "native_playback_supported" not in result


@pytest.mark.parametrize("picker", [False, True])
async def test_upload_uses_device_and_preparation_without_media_player(hass, audio_setup, supported_library, picker):
    fields = (
        {"media": {"media_content_id": "media-source://media_source/local/file.wav", "media_content_type": "audio/wav"}}
        if picker
        else {"media_content_id": "media-source://media_source/local/file.wav"}
    )
    with (
        patch(
            "custom_components.reolink_audio_library.audio_services.async_prepare_media",
            AsyncMock(return_value=PreparedAudio(bytes(4000), 0.5, 100)),
        ) as prepare,
        patch(
            "custom_components.reolink_audio_library.audio_services.async_upload_prepared",
            AsyncMock(return_value=receipt()),
        ) as upload,
    ):
        result = await action(hass, audio_setup, "upload_audio", {"name": "Delivery", **fields})
    assert result["id"] == 5 and result["inventory_refreshed"]
    assert "entity_id" not in prepare.await_args.kwargs
    assert upload.await_args.kwargs == {"device_write_authorized": True}
    assert hass.states.get(STATUS).state == "completed"
    assert hass.states.get(STATUS).attributes["last_uploaded_recording"]["id"] == 5


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"media_content_id": "https://bad/file.wav"},
        {"media_content_id": "/tmp/file.wav"},
        {"media_content_id": "media-source://tts/x"},
        {"media_content_id": "media-source://media_source/local/f", "overwrite": True},
        {"media_content_id": "media-source://media_source/local/f", "file_id": 3},
        {
            "media_content_id": "media-source://media_source/local/f",
            "media": {"media_content_id": "media-source://media_source/local/g", "media_content_type": "audio/wav"},
        },
    ],
)
async def test_schema_rejects_unsupported_or_ambiguous_input(hass, audio_setup, fields):
    with pytest.raises(vol.Invalid):
        await action(hass, audio_setup, "upload_audio", {"name": "Test", **fields})


@pytest.mark.parametrize("case", ["missing", "other", "unsupported", "unselected", "unloaded"])
async def test_wrong_devices_fail_before_media_access(hass, audio_setup, supported_library, case):
    device_id = "missing"
    if case == "other":
        foreign = MockConfigEntry(domain="other", data={})
        foreign.add_to_hass(hass)
        device_id = (
            dr.async_get(hass)
            .async_get_or_create(config_entry_id=foreign.entry_id, identifiers={("other", "other")})
            .id
        )
    elif case == "unsupported":
        device_id = audio_setup.devices[1].id
    elif case == "unselected":
        device_id = audio_setup.devices[0].id
        audio_setup.entry.runtime_data.clear()
    elif case == "unloaded":
        device_id = audio_setup.devices[0].id
        await hass.config_entries.async_unload(audio_setup.official_entries[0].entry_id)
    with patch("custom_components.reolink_audio_library.audio_services.async_prepare_media", AsyncMock()) as prepare:
        with pytest.raises(ServiceValidationError):
            await action(
                hass,
                audio_setup,
                "upload_audio",
                {"device_id": device_id, "name": "Test", "media_content_id": "media-source://media_source/local/f"},
            )
    prepare.assert_not_awaited()


async def test_successful_save_survives_failed_display_refresh(hass, audio_setup, supported_library):
    supported_library.library.async_read.side_effect = RuntimeError("private details")
    with (
        patch(
            "custom_components.reolink_audio_library.audio_services.async_prepare_media",
            AsyncMock(return_value=PreparedAudio(bytes(4000), 0.5, 100)),
        ),
        patch(
            "custom_components.reolink_audio_library.audio_services.async_upload_prepared",
            AsyncMock(return_value=receipt()),
        ),
    ):
        result = await action(
            hass,
            audio_setup,
            "upload_audio",
            {"name": "Delivery", "media_content_id": "media-source://media_source/local/f"},
        )
    assert result["success"] and not result["inventory_refreshed"]
    assert hass.states.get(STATUS).state == "completed"
    assert hass.states.get("sensor.reolink_audio_library_front_custom_recordings").state == "unavailable"


async def test_uncertain_error_is_sanitized_and_never_retried(hass, audio_setup, supported_library):
    with (
        patch(
            "custom_components.reolink_audio_library.audio_services.async_prepare_media",
            AsyncMock(return_value=PreparedAudio(bytes(4000), 0.5, 100)),
        ),
        patch(
            "custom_components.reolink_audio_library.audio_services.async_upload_prepared",
            AsyncMock(side_effect=AudioUploadError("Check inventory", outcome_uncertain=True)),
        ) as upload,
    ):
        with pytest.raises(ServiceValidationError, match="Check inventory"):
            await action(
                hass,
                audio_setup,
                "upload_audio",
                {"name": "Delivery", "media_content_id": "media-source://media_source/local/f"},
            )
    upload.assert_awaited_once()
    assert hass.states.get(STATUS).state == "failed_check_inventory"
    assert hass.states.get(STATUS).attributes["last_upload_error"]["outcome_uncertain"]


async def test_cancel_releases_lock_and_updates_status(hass, audio_setup, supported_library):
    started = asyncio.Event()

    async def wait(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    with patch(
        "custom_components.reolink_audio_library.audio_services.async_prepare_media", AsyncMock(side_effect=wait)
    ):
        task = asyncio.create_task(
            action(
                hass,
                audio_setup,
                "upload_audio",
                {"name": "Test", "media_content_id": "media-source://media_source/local/f"},
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not supported_library.library.lock.locked()
    assert hass.states.get(STATUS).state == "cancelled_check_inventory"


async def test_foreign_talk_services_registry_and_data_survive_unload(hass, audio_setup):
    foreign_data = {"sentinel": object()}
    hass.data["reolink_talk"] = foreign_data
    handler = AsyncMock()
    hass.services.async_register("reolink_talk", "upload_audio", handler)
    registry = er.async_get(hass)
    foreign = registry.async_get_or_create(
        "media_player", "reolink_talk", "foreign-existing", suggested_object_id="reolink_talk_existing"
    )
    snapshot = foreign
    assert await hass.config_entries.async_unload(audio_setup.entry.entry_id)
    assert hass.data["reolink_talk"] is foreign_data
    assert hass.services.has_service("reolink_talk", "upload_audio")
    assert registry.async_get(foreign.entity_id) == snapshot
    handler.assert_not_called()
    # Actions remain registered and give a useful unloaded error.
    with pytest.raises(ServiceValidationError, match="configuration"):
        await action(hass, audio_setup, "list_audio")


async def test_actions_do_not_mutate_on_invalid_name(hass, audio_setup):
    with patch("custom_components.reolink_audio_library.audio_services.async_prepare_media", AsyncMock()) as prepare:
        with pytest.raises(ServiceValidationError, match="name"):
            await action(
                hass,
                audio_setup,
                "upload_audio",
                {"name": "x" * 128, "media_content_id": "media-source://media_source/local/f"},
            )
    prepare.assert_not_awaited()


async def test_owned_library_login_accepts_local_device_self_signed_https(
    hass, audio_setup, socket_enabled, aiohttp_server, tmp_path
):
    """Exercise the real Host/session callback against a local HTTPS server."""
    from datetime import UTC, datetime, timedelta
    from ipaddress import ip_address
    import ssl
    from aiohttp import web
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from homeassistant.helpers.aiohttp_client import async_get_clientsession

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-local-device")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip_address("127.0.0.1"))]), critical=False)
        .sign(key, hashes.SHA256())
    )
    certfile, keyfile = tmp_path / "test-cert.pem", tmp_path / "test-key.pem"
    certfile.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile, keyfile)
    commands = []

    async def handle(request):
        body = await request.json()
        command = body[0]["cmd"]
        commands.append(command)
        value = {"Token": {"name": "TEST-ONLY-token", "leaseTime": 3600}} if command == "Login" else {}
        return web.json_response([{"cmd": command, "code": 0, "value": value}], content_type="text/html")

    app = web.Application()
    app.router.add_post("/cgi-bin/api.cgi", handle)
    server = await aiohttp_server(app, ssl=context)
    entry = audio_setup.official_entries[0]
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, "host": "127.0.0.1", "port": server.port, "use_https": True}
    )
    library = audio_setup.entry.runtime_data[entry.entry_id]
    default_session = async_get_clientsession(hass)
    async with library.connect()() as api:
        assert api._aiohttp_session is not default_session
        await api.login()
    assert "Login" in commands
    assert not default_session.closed


async def test_upload_requires_admin_before_media_or_camera_access(hass, audio_setup, hass_read_only_user):
    from homeassistant.core import Context
    from homeassistant.exceptions import Unauthorized

    with patch("custom_components.reolink_audio_library.audio_services.async_prepare_media", AsyncMock()) as prepare:
        with pytest.raises(Unauthorized):
            await action(
                hass,
                audio_setup,
                "upload_audio",
                {"name": "Test", "media_content_id": "media-source://media_source/local/f"},
                context=Context(user_id=hass_read_only_user.id),
            )
    prepare.assert_not_awaited()


async def test_alias_devices_share_our_camera_operation_lock(hass, audio_setup):
    from custom_components.reolink_audio_library.audio_library import camera_lock

    first, second = audio_setup.official_entries
    second.runtime_data.host.unique_id = first.runtime_data.host.unique_id
    assert camera_lock(hass, first.entry_id) is camera_lock(hass, second.entry_id)


async def test_options_selection_does_not_implicitly_include_other_cameras(hass, audio_setup):
    from custom_components.reolink_audio_library.audio_library import libraries_for_entry

    selected = audio_setup.official_entries[0].entry_id
    hass.config_entries.async_update_entry(audio_setup.entry, data={CONF_REOLINK_ENTRY_IDS: [selected]})
    assert list(libraries_for_entry(hass, audio_setup.entry)) == [selected]


async def test_upload_without_response_completes_with_status_sensor(hass, audio_setup):
    with (
        patch(
            "custom_components.reolink_audio_library.audio_services.async_prepare_media",
            AsyncMock(return_value=PreparedAudio(bytes(4000), 0.5, 100)),
        ),
        patch(
            "custom_components.reolink_audio_library.audio_services.async_upload_prepared",
            AsyncMock(return_value=receipt()),
        ),
    ):
        assert (
            await action(
                hass,
                audio_setup,
                "upload_audio",
                {"name": "Delivery", "media_content_id": "media-source://media_source/local/f"},
                response=False,
            )
            is None
        )
    assert hass.states.get(STATUS).state == "completed"
