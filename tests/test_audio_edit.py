"""Owned in-memory camera, production edit orchestration and wire sender."""

from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from xml.etree import ElementTree as ET
import asyncio

import pytest

from test_audio_upload_transport import Connection, crypt, ack
from custom_components.reolink_audio_library.audio_edit import (
    async_edit_recording,
    RecordingChangeError,
    RecordingState,
)
from custom_components.reolink_audio_library.audio_upload import RESEARCHED_WIFI_W
from custom_components.reolink_audio_library.recording_identity import RecordingSelection, recording_fingerprint
from custom_components.reolink_audio_library.stored_audio import AudioInventory, AudioRecord, StoredAudioError

SECRET = "private-password and signed?token=private"


class Camera:
    def __init__(self):
        self.stock = tuple(AudioRecord(i, f"Factory {i}", "alarm", 8000, 1) for i in range(3))
        self.custom = [
            AudioRecord(3, "Existing", "reply", 8000, 1, ""),
            AudioRecord(4, "Synthetic", "reply", 4000, 0, "synthetic-uuid"),
        ]
        self.info = {
            "type": RESEARCHED_WIFI_W.model,
            "hardwareVersion": RESEARCHED_WIFI_W.hardware,
            "firmwareVersion": RESEARCHED_WIFI_W.firmware,
        }
        self.capacity = 12
        self.auto = (0, -1, 2)
        self.auto_xml = None
        self.apis = []
        self.closed = []
        self.packets = []
        self.status = 200
        self.apply = True
        self.lost_ack = False
        self.bad_effect = None
        self.on_close = None
        self.reuse = None
        self.cancel = False

    def state(self):
        return RecordingState(
            AudioInventory(tuple(self.custom), self.capacity),
            AudioInventory(self.stock + tuple(self.custom)),
            RESEARCHED_WIFI_W,
        )

    def selection(self, file_id=4):
        record = next(r for r in self.state().native.files if r.id == file_id)
        return RecordingSelection("a" * 24, file_id, recording_fingerprint(self.state(), record))

    @asynccontextmanager
    async def connect(self):
        if self.reuse is not None and self.apis:
            api = self.apis[0]
        else:
            connection = Connection()

            async def read(*, cmd_id, channel, **kwargs):
                assert channel == 0
                if cmd_id == 427:
                    return (
                        self.auto_xml
                        or f"<body><AutoReply><enable>{self.auto[0]}</enable><audioId>{self.auto[1]}</audioId><timeout>{self.auto[2]}</timeout></AutoReply></body>"
                    )
                assert cmd_id == 347
                root = ET.Element("body")
                table = ET.SubElement(root, "audioFileInfoList")
                ET.SubElement(table, "maxFileNumber").text = str(self.capacity)
                for r in self.custom:
                    item = ET.SubElement(table, "audioFileInfo")
                    for key, value in (
                        ("id", r.id),
                        ("fileName", r.name),
                        ("type", r.type),
                        ("fileSize", r.size),
                        ("audioLen", r.audio_length_raw),
                        ("extId", r.external_id),
                    ):
                        if value is not None:
                            ET.SubElement(item, key).text = str(value)
                return ET.tostring(root, encoding="unicode")

            async def native(request):
                assert request == [{"cmd": "GetAudioFileList", "action": 0, "param": {"channel": 0}}]
                return [
                    {
                        "cmd": "GetAudioFileList",
                        "code": 0,
                        "value": {
                            "AudioFileList": [
                                {
                                    "id": r.id,
                                    "fileName": r.name,
                                    "type": r.type,
                                    "fileSize": r.size,
                                    "audioLen": r.audio_length_raw,
                                    **({"extId": r.external_id} if r.external_id is not None else {}),
                                }
                                for r in (*self.stock, *self.custom)
                            ]
                        },
                    }
                ]

            def mutate(packet, count):
                self.packets.append(packet)
                command = int.from_bytes(packet[4:8], "little")
                offset = int.from_bytes(packet[20:24], "little")
                root = ET.fromstring(crypt(packet[24 + offset :]))
                file_id = int(root.findtext(".//id"))
                assert command in (348, 368) and 3 <= file_id <= 14
                if self.apply and self.status == 200:
                    if command == 348:
                        self.custom = [r for r in self.custom if r.id != file_id]
                    else:
                        item = root.find("audioFileInfoList/audioFileInfo")
                        self.custom = [
                            replace(r, name=item.findtext("fileName"), external_id=item.findtext("extId", ""))
                            if r.id == file_id
                            else r
                            for r in self.custom
                        ]
                    if self.bad_effect:
                        self.bad_effect(self)
                if self.cancel:
                    raise asyncio.CancelledError
                if self.lost_ack:
                    raise TimeoutError(SECRET)
                return ack(packet, self.status), 24, b""

            connection.hook = mutate
            bc = SimpleNamespace(
                _connection=connection,
                _logged_in=True,
                _aes_key=b"key",
                _mess_id=0,
                _aes_encrypt=crypt,
                login=AsyncMock(),
                send=AsyncMock(side_effect=read),
                get_info=AsyncMock(side_effect=lambda: self.info.copy()),
            )
            api = SimpleNamespace(baichuan=bc, send=AsyncMock(side_effect=native))
        self.apis.append(api)
        try:
            yield api
        finally:
            self.closed.append(api)
            if self.on_close:
                self.on_close(self)


@pytest.fixture
def camera():
    return Camera()


async def change(camera, action="rename", **kwargs):
    selection = kwargs.pop("selection", None)
    if selection is None:
        selection = camera.selection()
    return await async_edit_recording(
        camera.connect,
        0,
        selection,
        action=action,
        new_name=kwargs.pop("new_name", "Renamed" if action == "rename" else None),
        confirm_delete=kwargs.pop("confirm_delete", action == "delete"),
        device_write_authorized=True,
        **kwargs,
    )


@pytest.mark.parametrize("action", ["rename", "delete"])
async def test_production_wire_and_reconnect_preserve_every_other_record(camera, action):
    before = camera.state()
    receipt = await change(camera, action)
    assert receipt.changed and receipt.acknowledged and receipt.verified_after_reconnect
    assert len(camera.packets) == 1 and len(camera.apis) == len(camera.closed) == 2
    assert camera.apis[0] is not camera.apis[1]
    assert camera.stock == before.native.files[:3]
    assert camera.custom[0] == before.custom.files[0]
    if action == "rename":
        assert camera.custom[1] == replace(before.custom.files[1], name="Renamed")
    else:
        assert [r.id for r in camera.custom] == [3]


async def test_empty_legacy_uuid_is_preserved(camera):
    await change(camera, selection=camera.selection(3))
    assert camera.custom[0].external_id == ""


@pytest.mark.parametrize("kind", ["name", "size", "uuid", "removed"])
async def test_stale_slot_selection_is_rejected_before_write(camera, kind):
    selection = camera.selection()
    if kind == "removed":
        camera.custom.pop()
    else:
        field = {"name": "name", "size": "size", "uuid": "external_id"}[kind]
        value = {"name": "Changed", "size": 8000, "uuid": "changed"}[kind]
        camera.custom[-1] = replace(camera.custom[-1], **{field: value})
    with pytest.raises(StoredAudioError):
        await change(camera, selection=selection)
    assert not camera.packets


@pytest.mark.parametrize("file_id", [0, 1, 2, 15, True])
async def test_factory_and_invalid_ids_rejected_before_connect(camera, file_id):
    selection = RecordingSelection("a" * 24, file_id, "a" * 64)
    with pytest.raises(StoredAudioError):
        await change(camera, selection=selection)
    assert not camera.apis


@pytest.mark.parametrize("enabled", [0, 1])
async def test_configured_auto_reply_is_protected_even_when_disabled(camera, enabled):
    camera.auto = (enabled, 4, 2)
    with pytest.raises(RecordingChangeError, match="configured automatic reply"):
        await change(camera, "delete")
    assert not camera.packets


@pytest.mark.parametrize(
    "xml",
    [
        "<body/>",
        "<!DOCTYPE body><body/>",
        "<body><AutoReply><enable>0</enable><audioId>-1</audioId></AutoReply></body>",
        "<body><AutoReply><enable>2</enable><audioId>-1</audioId><timeout>2</timeout></AutoReply></body>",
        "<body><AutoReply><enable>0</enable><enable>1</enable><audioId>-1</audioId><timeout>2</timeout></AutoReply></body>",
    ],
)
async def test_unknown_auto_reply_cannot_be_deleted(camera, xml):
    camera.auto_xml = xml
    with pytest.raises(StoredAudioError):
        await change(camera, "delete")
    assert not camera.packets


@pytest.mark.parametrize("action", ["rename", "delete"])
async def test_lost_ack_reconciles_only_with_exact_fresh_read_without_retry(camera, action):
    camera.lost_ack = True
    receipt = await change(camera, action)
    assert not receipt.acknowledged and receipt.verified_after_reconnect
    assert len(camera.packets) == 1


@pytest.mark.parametrize("status", [200, 201, 300, 401, 500])
async def test_no_change_or_rejected_ack_is_failure_with_no_retry(camera, status):
    camera.status = status
    camera.apply = False
    with pytest.raises(RecordingChangeError) as error:
        await change(camera)
    assert error.value.outcome_uncertain
    assert SECRET not in str(error.value)
    assert len(camera.packets) == 1


@pytest.mark.parametrize("effect", ["other_name", "target_uuid", "target_size", "new_file", "auto_reply"])
async def test_unexpected_effects_fail_preservation_check(camera, effect):
    def bad(c):
        if effect == "other_name":
            c.custom[0] = replace(c.custom[0], name="Wrong")
        elif effect == "target_uuid":
            c.custom[-1] = replace(c.custom[-1], external_id="Wrong")
        elif effect == "target_size":
            c.custom[-1] = replace(c.custom[-1], size=8000)
        elif effect == "new_file":
            c.custom.append(AudioRecord(5, "Unexpected", "reply", 8000, 1, "extra"))
        else:
            c.auto = (1, 0, 3)

    camera.bad_effect = bad
    with pytest.raises(RecordingChangeError):
        await change(camera, "delete" if effect == "auto_reply" else "rename")
    assert len(camera.packets) == 1


async def test_changed_after_disconnect_does_not_claim_persistence(camera):
    camera.on_close = lambda c: setattr(
        c, "custom", [replace(r, name="Unexpected") if r.id == 4 else r for r in c.custom]
    )
    with pytest.raises(RecordingChangeError):
        await change(camera)
    assert len(camera.packets) == 1


async def test_reused_verification_host_rejected(camera):
    camera.reuse = True
    with pytest.raises(RecordingChangeError, match="fresh independent"):
        await change(camera)


async def test_cancel_closes_session_and_never_retries(camera):
    camera.cancel = True
    with pytest.raises(asyncio.CancelledError):
        await change(camera)
    assert len(camera.closed) == len(camera.apis) == len(camera.packets) == 1


async def test_unchanged_name_is_a_noop_and_does_not_invalidate_cache(camera):
    callback = Mock()
    receipt = await change(camera, new_name="Synthetic", before_write=callback)
    assert not receipt.changed and receipt.status == "unchanged"
    assert not camera.packets
    callback.assert_not_called()


async def test_duplicate_name_and_missing_uuid_fail_before_write(camera):
    with pytest.raises(RecordingChangeError, match="already has"):
        await change(camera, new_name="Existing")
    camera.custom[-1] = replace(camera.custom[-1], external_id=None)
    with pytest.raises(RecordingChangeError, match="UUID"):
        await change(camera)
    assert not camera.packets


async def test_default_gates_precede_all_io(camera):
    with pytest.raises(RecordingChangeError):
        await async_edit_recording(camera.connect, 0, camera.selection(), action="delete", confirm_delete=True)
    with pytest.raises(RecordingChangeError):
        await change(camera, "delete", confirm_delete=False)
    assert not camera.apis


async def test_live_identity_is_required_before_write(camera):
    camera.info["firmwareVersion"] = "unverified-firmware"
    with pytest.raises(RecordingChangeError) as error:
        await change(camera)
    assert not error.value.outcome_uncertain
    assert not camera.packets


async def test_uuid_and_other_metadata_survive_delete_of_legacy_record(camera):
    other = camera.custom[1]
    await change(camera, "delete", selection=camera.selection(3))
    assert camera.custom == [other]


async def test_preflight_does_not_invalidate_preview_for_active_auto_reply(camera):
    callback = Mock()
    camera.auto = (1, 4, 2)
    with pytest.raises(RecordingChangeError):
        await change(camera, "delete", before_write=callback)
    callback.assert_not_called()
    assert not camera.packets


@pytest.mark.parametrize("capacity", [0, 13, 1])
async def test_inconsistent_capacity_rejected_before_write(camera, capacity):
    camera.capacity = capacity
    with pytest.raises(RecordingChangeError):
        await change(camera)
    assert not camera.packets
