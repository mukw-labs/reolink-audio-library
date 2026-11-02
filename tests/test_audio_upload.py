"""Candidate upload orchestration against an in-memory device, never hardware.

The real transport is always replaced. Mock persistence is not evidence of a
successful firmware import, reconnect persistence or audible native playback.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import math
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID
from xml.etree import ElementTree as ET

import pytest
from reolink_aio.exceptions import ApiError, CredentialsInvalidError

from custom_components.reolink_audio_library import audio_upload
from custom_components.reolink_audio_library.audio_preparation import PreparedAudio
from custom_components.reolink_audio_library.audio_upload import (
    AudioUploadError,
    async_upload_prepared,
    profile_for_device_info,
)

IDENTITY = {
    "type": "Reolink Video Doorbell WiFi-W",
    "hardwareVersion": "DB_566128M5MP_W_W",
    "firmwareVersion": "v3.0.0.6460_2605211912",
}
EXTERNAL_ID = "11111111-2222-4333-8444-555555555555"
SECRET = "PRIVATE-password https://example.invalid/audio?token=PRIVATE-token"
NAME = "Delivery & welcome"
CAPABILITIES = (
    "<body><AudioEncodeAbility><audioEncodeList><audioEncode>"
    "<audioType>adpcm</audioType><sampleRate>16000</sampleRate>"
    "<samplePrecision>16</samplePrecision><soundTrack>mono</soundTrack>"
    "</audioEncode></audioEncodeList></AudioEncodeAbility></body>"
)


def _user_record(file_id=5, name="Existing user message") -> dict:
    return {
        "id": file_id,
        "fileName": name,
        "fileSize": 800,
        "audioLen": 0,
        "extId": "existing-recording-external-id",
        "type": "reply",
    }


class InMemoryCamera:
    """Two independent owned sessions observing one synthetic recording store."""

    def __init__(self):
        self.stock = [
            {"id": index, "fileName": f"Stock message {index}", "fileSize": 1600, "audioLen": 0, "type": "alarm"}
            for index in range(3)
        ]
        self.custom = []
        self.max_files = 12
        self.identity = IDENTITY.copy()
        self.capabilities = CAPABILITIES
        self.new_id = 3
        self.apis = []
        self.closed = []
        self.on_close = None
        self.native_transform = None
        self.reuse = None
        self.read_error = None
        self.info_error = None
        self.get_infos = []
        self.import_arguments = []

    def _xml(self) -> str:
        root = ET.Element("body")
        inventory = ET.SubElement(root, "audioFileInfoList")
        if self.max_files is not None:
            ET.SubElement(inventory, "maxFileNumber").text = str(self.max_files)
        for record in self.custom:
            item = ET.SubElement(inventory, "audioFileInfo")
            for key, value in record.items():
                ET.SubElement(item, key).text = str(value)
        return ET.tostring(root, encoding="unicode")

    def _new_api(self, session_index):
        async def get_info():
            self.get_infos.append(session_index)
            if self.info_error:
                raise self.info_error
            return self.identity.copy()

        async def baichuan_send(*, cmd_id, channel):
            assert channel == 0
            if self.read_error:
                raise self.read_error
            if cmd_id == 347:
                return self._xml()
            assert cmd_id == 267, "Orchestration must delegate all mutations to its separately patched transport"
            return self.capabilities

        async def native_send(request):
            assert request == [{"cmd": "GetAudioFileList", "action": 0, "param": {"channel": 0}}]
            files = [record.copy() for record in (*self.stock, *self.custom)]
            if self.native_transform is not None:
                files = self.native_transform(session_index, files)
            return [{"cmd": "GetAudioFileList", "code": 0, "value": {"AudioFileList": files}}]

        bc = SimpleNamespace(
            send=AsyncMock(side_effect=baichuan_send),
            get_info=AsyncMock(side_effect=get_info),
            _connection=SimpleNamespace(_transport=object()),
        )
        return SimpleNamespace(
            baichuan=bc,
            send=AsyncMock(side_effect=native_send),
            logout=AsyncMock(),
            play_quick_reply=AsyncMock(),
        )

    @asynccontextmanager
    async def session(self):
        session_index = len(self.apis)
        api = self._new_api(session_index)
        if self.apis:
            if self.reuse == "api":
                api = self.apis[0]
            elif self.reuse == "baichuan":
                api.baichuan = self.apis[0].baichuan
            elif self.reuse == "transport":
                api.baichuan._connection._transport = self.apis[0].baichuan._connection._transport
        self.apis.append(api)
        try:
            yield api
        finally:
            await api.logout()
            self.closed.append(session_index)
            if self.on_close:
                self.on_close(session_index)

    async def persist(self, bc, channel, payload, name, external_id, *, device_write_authorized):
        assert device_write_authorized is True
        assert bc is self.apis[0].baichuan
        assert channel == 0
        self.import_arguments.append((bc, channel, payload, name, external_id))
        self.custom.append(
            {
                "id": self.new_id,
                "fileName": name,
                "fileSize": len(payload),
                "audioLen": 0,
                "extId": external_id,
                "type": "reply",
            }
        )


@pytest.fixture
def candidate(monkeypatch):
    camera = InMemoryCamera()
    connect = Mock(side_effect=camera.session)
    transport = AsyncMock(side_effect=camera.persist)
    monkeypatch.setattr(audio_upload, "async_import_and_commit", transport)
    monkeypatch.setattr(audio_upload, "uuid4", lambda: UUID(EXTERNAL_ID))
    return SimpleNamespace(
        camera=camera,
        connect=connect,
        transport=transport,
        audio=PreparedAudio(payload=bytes(800), duration=0.1, input_size=3244),
    )


async def _upload(candidate, **kwargs):
    return await async_upload_prepared(
        candidate.connect, 0, candidate.audio, NAME, device_write_authorized=True, **kwargs
    )


@pytest.mark.parametrize("authorization", [None, False, 1, "true"])
async def test_write_gate_requires_literal_approval_before_any_connection(candidate, authorization):
    with pytest.raises(AudioUploadError, match="explicit operator approval"):
        await async_upload_prepared(candidate.connect, 0, candidate.audio, NAME, device_write_authorized=authorization)
    candidate.connect.assert_not_called()
    candidate.transport.assert_not_awaited()


async def test_write_gate_defaults_to_disabled(candidate):
    with pytest.raises(AudioUploadError, match="explicit operator approval"):
        await async_upload_prepared(candidate.connect, 0, candidate.audio, NAME)
    candidate.connect.assert_not_called()


@pytest.mark.parametrize("kwargs", [{"overwrite": True}, {"overwrite": 0}, {"file_id": 3}, {"file_id": 0}])
async def test_replacement_parameters_are_rejected_before_connection(candidate, kwargs):
    with pytest.raises(AudioUploadError, match="Replacement is not established"):
        await _upload(candidate, **kwargs)
    candidate.connect.assert_not_called()
    candidate.transport.assert_not_awaited()


@pytest.mark.parametrize("channel", [1, -1, True, 0.0, "0"])
async def test_unresearched_channel_is_rejected_before_connection(candidate, channel):
    with pytest.raises(AudioUploadError, match="channel 0 only"):
        await async_upload_prepared(candidate.connect, channel, candidate.audio, NAME, device_write_authorized=True)
    candidate.connect.assert_not_called()


@pytest.mark.parametrize("field", ["type", "hardwareVersion", "firmwareVersion"])
async def test_device_identity_must_match_the_exact_researched_profile(candidate, field):
    candidate.camera.identity[field] = "another-device-or-firmware"
    with pytest.raises(AudioUploadError, match="No researched") as caught:
        await _upload(candidate)
    assert caught.value.outcome_uncertain is False
    assert candidate.camera.closed == [0]
    candidate.transport.assert_not_awaited()


def test_profile_lookup_returns_only_the_exact_identity():
    assert profile_for_device_info(IDENTITY) is audio_upload.RESEARCHED_WIFI_W
    with pytest.raises(AudioUploadError):
        profile_for_device_info({})


@pytest.mark.parametrize("info", [None, [], "not device info"])
def test_profile_lookup_rejects_malformed_identity_without_attribute_errors(info):
    with pytest.raises(AudioUploadError):
        profile_for_device_info(info)


@pytest.mark.parametrize(
    "change",
    [
        {"payload": b""},
        {"payload": bytearray(800)},
        {"duration": math.inf},
        {"duration": math.nan},
        {"duration": "0.1"},
        {"duration": None},
        {"duration": True},
        {"duration": True, "payload": bytes(8000)},
        {"duration": 0.2},
        {"sample_rate": 8000},
        {"sample_rate": 16000.0},
        {"precision": 8},
        {"channels": 2},
        {"channels": True},
        {"encoding": "adpcm_ima_wav"},
        {"payload": bytes(80001), "duration": 80001 * 2 / 16000},
    ],
)
async def test_malformed_or_oversize_prepared_audio_is_rejected_without_connecting(candidate, change):
    candidate.audio = replace(candidate.audio, **change)
    with pytest.raises(AudioUploadError):
        await _upload(candidate)
    candidate.connect.assert_not_called()
    candidate.transport.assert_not_awaited()


async def test_success_requires_two_distinct_owned_sessions_and_reports_device_assigned_id(candidate):
    candidate.camera.custom.append(_user_record())
    candidate.camera.new_id = 9
    previous = candidate.camera.custom[0].copy()
    receipt = await _upload(candidate)
    assert receipt.id == 9
    assert receipt.name == NAME
    assert receipt.external_id == EXTERNAL_ID
    assert receipt.size == 800
    assert receipt.duration == 0.1
    assert receipt.commit_acknowledged is True
    assert receipt.success is True
    assert receipt.verified_after_reconnect is True
    assert receipt.native_playback_verified is False
    assert receipt.status == "stored_verified_after_reconnect"
    assert candidate.camera.closed == [0, 1]
    assert candidate.camera.get_infos == [0, 1]
    assert candidate.camera.apis[0] is not candidate.camera.apis[1]
    assert candidate.camera.custom[0] == previous
    assert candidate.connect.call_count == 2
    candidate.transport.assert_awaited_once_with(
        candidate.camera.apis[0].baichuan, 0, candidate.audio.payload, NAME, EXTERNAL_ID, device_write_authorized=True
    )
    for api in candidate.camera.apis:
        api.logout.assert_awaited_once()
        api.play_quick_reply.assert_not_awaited()


@pytest.mark.parametrize("capacity", [None, 0])
async def test_missing_or_unusable_capacity_stops_before_import(candidate, capacity):
    candidate.camera.max_files = capacity
    with pytest.raises(AudioUploadError, match="usable custom-audio capacity"):
        await _upload(candidate)
    assert candidate.camera.closed == [0]
    candidate.transport.assert_not_awaited()


async def test_full_custom_inventory_does_not_overwrite(candidate):
    candidate.camera.custom = [_user_record(index, f"Recording {index}") for index in range(3, 15)]
    before = [record.copy() for record in candidate.camera.custom]
    with pytest.raises(AudioUploadError, match="inventory is full"):
        await _upload(candidate)
    assert candidate.camera.custom == before
    assert candidate.camera.closed == [0]
    candidate.transport.assert_not_awaited()


@pytest.mark.parametrize("missing_id", [0, 1, 2])
async def test_preflight_requires_the_complete_stock_recording_baseline(candidate, missing_id):
    candidate.camera.stock = [record for record in candidate.camera.stock if record["id"] != missing_id]
    with pytest.raises(AudioUploadError) as caught:
        await _upload(candidate)
    assert caught.value.outcome_uncertain is False
    assert candidate.camera.closed == [0]
    candidate.transport.assert_not_awaited()


async def test_reported_capacity_cannot_exceed_the_researched_user_id_space(candidate):
    candidate.camera.max_files = 13
    with pytest.raises(AudioUploadError) as caught:
        await _upload(candidate)
    assert caught.value.outcome_uncertain is False
    assert candidate.camera.closed == [0]
    candidate.transport.assert_not_awaited()


@pytest.mark.parametrize("location", ["stock", "custom"])
async def test_existing_name_in_either_inventory_is_never_overwritten(candidate, location):
    if location == "stock":
        candidate.camera.stock[0]["fileName"] = NAME
    else:
        candidate.camera.custom.append(_user_record(name=NAME))
    with pytest.raises(AudioUploadError, match="name already exists"):
        await _upload(candidate)
    candidate.transport.assert_not_awaited()
    assert candidate.camera.closed == [0]


@pytest.mark.parametrize("change", [{"id": 0}, {"id": 15}, {"type": "alarm"}, {"type": "unknown"}])
async def test_custom_inventory_must_obey_researched_ownership_before_mutation(candidate, change):
    candidate.camera.custom.append({**_user_record(), **change})
    with pytest.raises(AudioUploadError):
        await _upload(candidate)
    candidate.transport.assert_not_awaited()


@pytest.mark.parametrize("mismatch", ["native_only", "custom_only", "metadata", "out_of_range"])
async def test_preflight_rejects_inconsistent_native_and_custom_inventories_before_writing(candidate, mismatch):
    if mismatch == "native_only":
        candidate.camera.stock.append(_user_record(3))
    elif mismatch == "out_of_range":
        candidate.camera.stock.append(_user_record(15))
    else:
        candidate.camera.custom.append(_user_record())

        def mismatched(index, files):
            if mismatch == "custom_only":
                return [item for item in files if item["id"] != 5]
            files[-1]["fileSize"] = 799
            return files

        candidate.camera.native_transform = mismatched
    with pytest.raises(AudioUploadError) as caught:
        await _upload(candidate)
    assert caught.value.outcome_uncertain is False
    assert candidate.camera.closed == [0]
    candidate.transport.assert_not_awaited()


async def test_consistent_existing_user_baseline_accepts_native_inventory_without_optional_external_id(candidate):
    candidate.camera.custom.append(_user_record())
    previous = candidate.camera.custom[0].copy()

    def without_optional_external_id(index, files):
        for record in files:
            record.pop("extId", None)
        return files

    candidate.camera.native_transform = without_optional_external_id
    receipt = await _upload(candidate)
    assert receipt.success is True
    assert candidate.camera.custom[0] == previous
    assert candidate.camera.closed == [0, 1]
    candidate.transport.assert_awaited_once()


@pytest.mark.parametrize("mismatch", ["duration_conflict", "identity_conflict", "missing_size", "missing_duration"])
async def test_preflight_requires_complete_consistent_preservation_metadata(candidate, mismatch):
    candidate.camera.custom.append(_user_record())

    def contradictory_or_missing(index, files):
        if mismatch == "duration_conflict":
            files[-1]["audioLen"] = 1
        elif mismatch == "identity_conflict":
            files[-1]["extId"] = "conflicting-native-external-id"
        elif mismatch == "missing_size":
            files[0].pop("fileSize")
        else:
            files[0].pop("audioLen")
        return files

    candidate.camera.native_transform = contradictory_or_missing
    with pytest.raises(AudioUploadError) as caught:
        await _upload(candidate)
    assert caught.value.outcome_uncertain is False
    assert candidate.camera.closed == [0]
    candidate.transport.assert_not_awaited()


@pytest.mark.parametrize(
    ("value", "replacement"), [("adpcm", "g711"), ("16000", "8000"), ("16", "8"), ("mono", "stereo")]
)
async def test_reported_codec_must_match_the_profile(candidate, value, replacement):
    candidate.camera.capabilities = CAPABILITIES.replace(f">{value}<", f">{replacement}<")
    with pytest.raises(AudioUploadError, match="reported audio capability"):
        await _upload(candidate)
    candidate.transport.assert_not_awaited()


@pytest.mark.parametrize(
    "change",
    [{"extId": "another-external-id"}, {"id": 15}, {"fileName": "Wrong"}, {"type": "alarm"}, {"fileSize": 799}],
)
async def test_post_reconnect_recording_metadata_must_match_all_confirmation_fields(candidate, change):
    candidate.camera.on_close = lambda index: candidate.camera.custom[-1].update(change) if index == 0 else None
    with pytest.raises(AudioUploadError) as caught:
        await _upload(candidate)
    assert caught.value.external_id == EXTERNAL_ID
    assert caught.value.outcome_uncertain is True
    assert candidate.camera.closed == [0, 1]
    candidate.transport.assert_awaited_once()


async def test_existing_user_recording_must_survive_unchanged(candidate):
    candidate.camera.custom.append(_user_record())
    candidate.camera.on_close = lambda index: (
        candidate.camera.custom[0].update(fileName="Changed") if index == 0 else None
    )
    with pytest.raises(AudioUploadError, match="existing recording changed"):
        await _upload(candidate)
    candidate.transport.assert_awaited_once()


async def test_existing_stock_native_recording_must_survive_unchanged(candidate):
    candidate.camera.on_close = lambda index: candidate.camera.stock[0].update(fileSize=1599) if index == 0 else None
    with pytest.raises(AudioUploadError, match="existing recording changed"):
        await _upload(candidate)
    candidate.transport.assert_awaited_once()


async def test_acknowledged_commit_without_persistent_recording_is_failure(candidate):
    candidate.transport.side_effect = None
    candidate.transport.return_value = None
    with pytest.raises(AudioUploadError, match="external ID") as caught:
        await _upload(candidate)
    assert caught.value.outcome_uncertain is True
    assert candidate.camera.closed == [0, 1]
    candidate.transport.assert_awaited_once()


async def test_lost_commit_ack_can_be_resolved_by_actual_reconnect_inventory(candidate):
    async def persist_then_lose_ack(*args, **kwargs):
        await candidate.camera.persist(*args, **kwargs)
        raise ApiError(SECRET, rspCode=421)

    candidate.transport.side_effect = persist_then_lose_ack
    receipt = await _upload(candidate)
    assert receipt.success is True
    assert receipt.commit_acknowledged is False
    assert receipt.id == 3
    assert receipt.external_id == EXTERNAL_ID
    assert candidate.camera.closed == [0, 1]
    candidate.transport.assert_awaited_once()
    assert SECRET not in repr(receipt)


async def test_rejected_import_retains_numeric_status_without_private_exception_text(candidate):
    candidate.transport.side_effect = ApiError(SECRET, rspCode=421)
    with pytest.raises(AudioUploadError) as caught:
        await _upload(candidate)
    assert caught.value.outcome_uncertain is True
    assert caught.value.rspCode == 421
    assert "rspCode=421" in str(caught.value)
    assert "ApiError" in str(caught.value)
    assert SECRET not in str(caught.value)
    assert candidate.camera.closed == [0, 1]
    candidate.transport.assert_awaited_once()


async def test_duplicate_external_ids_do_not_confirm_a_saved_recording(candidate):
    def duplicate(index):
        if index == 0:
            candidate.camera.custom.append({**candidate.camera.custom[-1], "id": 4})

    candidate.camera.on_close = duplicate
    with pytest.raises(AudioUploadError, match="exactly one recording"):
        await _upload(candidate)
    candidate.transport.assert_awaited_once()


async def test_unrelated_added_recording_also_invalidates_the_expected_inventory_change(candidate):
    def add_unrelated(index):
        if index == 0:
            candidate.camera.custom.append(_user_record(8, "Unrelated concurrent recording"))

    candidate.camera.on_close = add_unrelated
    with pytest.raises(AudioUploadError, match="new intact user recording"):
        await _upload(candidate)
    candidate.transport.assert_awaited_once()


async def test_recording_metadata_cannot_change_between_ack_and_reconnect(candidate):
    candidate.camera.on_close = lambda index: candidate.camera.custom[-1].update(audioLen=1) if index == 0 else None
    with pytest.raises(AudioUploadError, match="changed after reconnecting"):
        await _upload(candidate)
    assert candidate.camera.closed == [0, 1]


async def test_nonprepared_input_is_rejected_before_connecting(candidate):
    candidate.audio = None
    with pytest.raises(AudioUploadError, match="Prepare a nonempty"):
        await _upload(candidate)
    candidate.connect.assert_not_called()


@pytest.mark.parametrize("reuse", ["api", "baichuan", "transport"])
async def test_verification_cannot_reuse_original_session_or_connection(candidate, reuse):
    candidate.camera.reuse = reuse
    with pytest.raises(AudioUploadError, match="independent camera session|original camera connection") as caught:
        await _upload(candidate)
    assert caught.value.outcome_uncertain is True
    assert candidate.camera.closed == [0, 1]
    candidate.transport.assert_awaited_once()


async def test_identity_is_rechecked_after_reconnect(candidate):
    candidate.camera.on_close = lambda index: (
        candidate.camera.identity.update(firmwareVersion="changed-firmware") if index == 0 else None
    )
    with pytest.raises(AudioUploadError, match="No researched"):
        await _upload(candidate)
    assert candidate.camera.closed == [0, 1]


async def test_native_inventory_must_also_confirm_the_new_recording(candidate):
    candidate.camera.native_transform = lambda index, files: files[:-1] if index == 1 else files
    with pytest.raises(AudioUploadError, match="Native Quick Reply inventory"):
        await _upload(candidate)
    assert candidate.camera.closed == [0, 1]


async def test_native_metadata_must_match_even_when_custom_inventory_is_correct(candidate):
    def wrong_native_name(index, files):
        if index == 1:
            files[-1]["fileName"] = "Unrelated native message"
        return files

    candidate.camera.native_transform = wrong_native_name
    with pytest.raises(AudioUploadError, match="Native Quick Reply metadata"):
        await _upload(candidate)


@pytest.mark.parametrize("error", [CredentialsInvalidError(SECRET), ApiError(SECRET, rspCode=401)])
async def test_identity_failures_are_sanitized_and_owned_connection_is_closed(candidate, error):
    candidate.camera.info_error = error
    with pytest.raises(AudioUploadError) as caught:
        await _upload(candidate)
    assert SECRET not in str(caught.value)
    assert type(error).__name__ in str(caught.value)
    if isinstance(error, ApiError):
        assert "rspCode=401" in str(caught.value)
    assert caught.value.outcome_uncertain is False
    assert candidate.camera.closed == [0]
    candidate.transport.assert_not_awaited()


async def test_cancelled_import_closes_its_owned_context_without_retry_or_verification(candidate):
    started = asyncio.Event()

    async def interrupted(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    candidate.transport.side_effect = interrupted
    operation = asyncio.create_task(_upload(candidate))
    await started.wait()
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert candidate.camera.closed == [0]
    assert candidate.connect.call_count == 1
    candidate.transport.assert_awaited_once()


async def test_transport_phase_and_safe_header_survive_failed_reconciliation(candidate):
    from custom_components.reolink_audio_library.audio_upload_transport import AudioUploadError as TransportError

    failure = TransportError("begin", "the acknowledgement header does not match this request")
    failure.response_header = {"class": "0000", "status": 200, "command": 261}
    candidate.transport.side_effect = failure
    with pytest.raises(AudioUploadError) as caught:
        await _upload(candidate)
    assert caught.value.transport_failure == {
        "phase": "begin",
        "response_header": {"class": "0000", "status": 200, "command": 261},
    }
    candidate.transport.assert_awaited_once()
    assert candidate.camera.closed == [0, 1]
