"""Independently observed SDK XML fields over real pinned transport helpers."""

from xml.etree import ElementTree as ET

import pytest

from test_audio_upload_transport import native_bc as native_bc, crypt, ack
from custom_components.reolink_audio_library.audio_edit_transport import async_change_recording
from custom_components.reolink_audio_library.stored_audio import StoredAudioError


@pytest.mark.parametrize("new_name", ["Renamed <reply> & café", None])
async def test_exact_sdk_wire_fields_and_one_request(native_bc, new_name):
    await async_change_recording(
        native_bc, 0, 4, new_name=new_name, external_id="preserve-uuid", device_write_authorized=True
    )
    assert len(native_bc._connection.packets) == 1
    packet = native_bc._connection.packets[0]
    assert int.from_bytes(packet[4:8], "little") == (368 if new_name is not None else 348)
    assert int.from_bytes(packet[12:16], "little") == 0x123501
    offset = int.from_bytes(packet[20:24], "little")
    extension = ET.fromstring(crypt(packet[24 : 24 + offset]))
    assert extension.findtext("channelId") == "0" and extension.find("binaryData") is None
    root = ET.fromstring(crypt(packet[24 + offset :]))
    if new_name is not None:
        table = root.find("audioFileInfoList")
        assert table.attrib == {"version": "1.1"}
        item = table.find("audioFileInfo")
        assert item.attrib == {}
        assert len(table) == 1
        assert {field.tag: field.text for field in item} == {
            "channelId": "0",
            "id": "4",
            "fileName": new_name,
            "extId": "preserve-uuid",
        }
    else:
        item = root.find("audioFileInfo")
        assert item.attrib == {"version": "1.1"}
        assert {field.tag: field.text for field in item} == {"channelId": "0", "id": "4"}
    native_bc.send.assert_not_awaited()
    native_bc.logout.assert_not_awaited()


async def test_legacy_empty_uuid_omitted_exactly_like_sdk(native_bc):
    await async_change_recording(native_bc, 0, 3, new_name="New name", external_id="", device_write_authorized=True)
    packet = native_bc._connection.packets[0]
    offset = int.from_bytes(packet[20:24], "little")
    assert ET.fromstring(crypt(packet[24 + offset :])).find(".//extId") is None


@pytest.mark.parametrize(
    "arguments",
    [
        {"file_id": 0},
        {"file_id": 1},
        {"file_id": 2},
        {"file_id": 15},
        {"file_id": True},
        {"channel": 1},
        {"channel": True},
        {"new_name": ""},
        {"new_name": "x" * 128},
        {"new_name": "bad\0name"},
        {"new_name": "rename", "external_id": "x" * 64},
    ],
)
async def test_invalid_inputs_never_login_or_write(native_bc, arguments):
    args = {"channel": 0, "file_id": 4, "new_name": "A name", "external_id": "uuid", **arguments}
    with pytest.raises(StoredAudioError):
        await async_change_recording(native_bc, **args, device_write_authorized=True)
    native_bc.login.assert_not_awaited()
    assert not native_bc._connection.packets


@pytest.mark.parametrize("gate", [False, None, 1, "yes"])
async def test_default_write_gate_precedes_access(gate):
    with pytest.raises(StoredAudioError):
        await async_change_recording(object(), 0, 4, device_write_authorized=gate)


@pytest.mark.parametrize("status", [0, 201, 300, 400, 401, 500])
async def test_non_200_is_not_acknowledgement_and_never_retries(native_bc, status):
    native_bc._connection.hook = lambda packet, count: (ack(packet, status), 24, b"")
    with pytest.raises(StoredAudioError):
        await async_change_recording(native_bc, 0, 4, device_write_authorized=True)
    assert len(native_bc._connection.packets) == 1


async def test_real_library_aes_for_rename_metadata(native_bc):
    from reolink_aio.baichuan.baichuan import Baichuan

    bc = Baichuan("127.0.0.1", "fake", "fake", None)
    bc._connection = native_bc._connection
    bc._logged_in = True
    bc._aes_key = b"0123456789abcdef"
    await async_change_recording(bc, 0, 4, new_name="New reply", external_id="uuid", device_write_authorized=True)
    packet = bc._connection.packets[0]
    offset = int.from_bytes(packet[20:24], "little")
    body = bc._aes_decrypt(packet[24 + offset :], b"", decode=False)
    assert ET.fromstring(body).findtext(".//fileName") == "New reply"
