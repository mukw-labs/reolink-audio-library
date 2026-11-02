"""Native HTTP inventory boundary tests using synthetic recording names."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from reolink_aio.exceptions import ApiError, CredentialsInvalidError

from custom_components.reolink_audio_library.native_audio import (
    async_read_native_inventory,
    parse_native_audio_inventory,
)
from custom_components.reolink_audio_library.stored_audio import (
    AudioInventory,
    AudioProtocolError,
    AudioRecord,
    StoredAudioError,
)

SECRET = "private-password https://private.example/audio?token=secret"


def response(entries=None):
    """Exact observed HTTP envelope; names and optional identifiers are synthetic."""
    if entries is None:
        entries = [
            {
                "id": 0,
                "fileName": "Synthetic stock reply",
                "fileSize": 30720,
                "audioLen": 3,
                "type": "alarm",
                "sampleRate": 16000,
                "samplePrecision": 16,
            },
            {"id": 8, "fileName": "Synthetic custom reply", "extId": "synthetic-id"},
        ]
    return [{"cmd": "GetAudioFileList", "code": 0, "value": {"AudioFileList": entries}}]


def test_preserves_stock_zero_sparse_ids_raw_metadata_and_unknown_ownership():
    parsed = parse_native_audio_inventory(response())
    assert parsed == AudioInventory(
        files=(
            AudioRecord(id=0, name="Synthetic stock reply", type="alarm", size=30720, audio_length_raw=3),
            AudioRecord(id=8, name="Synthetic custom reply", external_id="synthetic-id"),
        )
    )
    assert all(record.duration is None and record.is_builtin is None for record in parsed.files)
    with pytest.raises(FrozenInstanceError):
        parsed.files[0].name = "changed"


def test_ignores_duplicate_initial_entries_and_untrusted_range_metadata():
    payload = response()
    payload[0]["initial"] = {"AudioFileList": payload[0]["value"]["AudioFileList"] * 2}
    payload[0]["range"] = {"AudioFileList": {"id": {"min": 0, "max": 15}, "fileNum": 3}}
    parsed = parse_native_audio_inventory(payload)
    assert [record.id for record in parsed.files] == [0, 8]
    assert parsed.max_files is None


def test_recognized_empty_list_succeeds():
    assert parse_native_audio_inventory(response([])) == AudioInventory(files=())


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        [],
        [None],
        response() * 2,
        [{"cmd": "WrongCommand", "code": 0, "value": {"AudioFileList": []}}],
        [{"cmd": "GetAudioFileList", "code": False, "value": {"AudioFileList": []}}],
        [{"cmd": "GetAudioFileList", "code": "0", "value": {"AudioFileList": []}}],
        [{"cmd": "GetAudioFileList", "value": {"AudioFileList": []}}],
        [{"cmd": "GetAudioFileList", "code": 0}],
        [{"cmd": "GetAudioFileList", "code": 0, "value": None}],
        [{"cmd": "GetAudioFileList", "code": 0, "value": {"AudioFileList": {}}}],
        [{"cmd": "GetAudioFileList", "code": 0, "initial": {"AudioFileList": []}}],
        response([None]),
        response([{}]),
        response([{"id": 0, "name": "Wrong name field"}]),
        response([{"id": 1, "fileName": "A"}, {"id": 1, "fileName": "B"}]),
    ],
)
def test_malformed_response_never_becomes_an_empty_inventory(payload):
    with pytest.raises(AudioProtocolError):
        parse_native_audio_inventory(payload)


@pytest.mark.parametrize("field", ["id", "fileSize", "audioLen"])
@pytest.mark.parametrize("invalid", [True, False, -1, 0.0, "0", None])
def test_numeric_record_fields_require_nonnegative_json_integers(field, invalid):
    entry = {"id": 0, "fileName": "Synthetic reply", field: invalid}
    with pytest.raises(AudioProtocolError, match="nonnegative integer"):
        parse_native_audio_inventory(response([entry]))


@pytest.mark.parametrize("field", ["fileName", "type", "extId"])
@pytest.mark.parametrize("invalid", [True, 1, [], {}, None])
def test_string_record_fields_reject_invalid_values(field, invalid):
    entry = {"id": 0, "fileName": "Synthetic reply", field: invalid}
    with pytest.raises(AudioProtocolError, match="must be a string"):
        parse_native_audio_inventory(response([entry]))


def test_status_error_preserves_only_numeric_code():
    payload = [{"cmd": "GetAudioFileList", "code": 1, "error": {"detail": SECRET, "rspCode": 401}}]
    with pytest.raises(AudioProtocolError, match="device code=1") as raised:
        parse_native_audio_inventory(payload)
    assert SECRET not in str(raised.value)


async def test_reads_fresh_value_without_mutating_shared_host_cache_or_lifecycle():
    payload = response()
    cache = {0: [{"id": 19, "fileName": "Cached old reply"}]}
    original_cache = deepcopy(cache)
    api = SimpleNamespace(
        send=AsyncMock(return_value=payload),
        _audio_file_list=cache,
        get_state=AsyncMock(),
        play_quick_reply=AsyncMock(),
        login=AsyncMock(),
        logout=AsyncMock(),
        close=AsyncMock(),
    )
    parsed = await async_read_native_inventory(api, 4)
    api.send.assert_awaited_once_with([{"cmd": "GetAudioFileList", "action": 0, "param": {"channel": 4}}])
    assert [record.id for record in parsed.files] == [0, 8]
    assert api._audio_file_list == original_cache
    for method in (api.get_state, api.play_quick_reply, api.login, api.logout, api.close):
        method.assert_not_awaited()
    payload[0]["value"]["AudioFileList"][0]["fileName"] = "Changed later"
    assert parsed.files[0].name == "Synthetic stock reply"


@pytest.mark.parametrize("error", [ApiError(SECRET), CredentialsInvalidError(SECRET), OSError(SECRET)])
async def test_transport_error_omits_arbitrary_exception_text(error):
    error.rspCode = 401
    api = SimpleNamespace(send=AsyncMock(side_effect=error))
    with pytest.raises(StoredAudioError, match="device rspCode=401") as raised:
        await async_read_native_inventory(api, 0)
    assert SECRET not in str(raised.value)
    assert raised.value.__suppress_context__


async def test_timeout_is_bounded_and_does_not_retry():
    async def delayed(_body):
        await asyncio.sleep(1)

    api = SimpleNamespace(send=AsyncMock(side_effect=delayed))
    with pytest.raises(StoredAudioError, match="TimeoutError"):
        await async_read_native_inventory(api, 0, timeout=0.001)
    assert api.send.await_count == 1


async def test_cancellation_propagates():
    api = SimpleNamespace(send=AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await async_read_native_inventory(api, 0)


@pytest.mark.parametrize("channel", [-1, True, 0.0, "0"])
async def test_invalid_channel_sends_nothing(channel):
    api = SimpleNamespace(send=AsyncMock())
    with pytest.raises(StoredAudioError, match="channel"):
        await async_read_native_inventory(api, channel)
    api.send.assert_not_awaited()


@pytest.mark.parametrize("timeout", [0, -1, True, float("nan"), float("inf"), "15"])
async def test_invalid_timeout_sends_nothing(timeout):
    api = SimpleNamespace(send=AsyncMock())
    with pytest.raises(StoredAudioError, match="timeout"):
        await async_read_native_inventory(api, 0, timeout=timeout)
    api.send.assert_not_awaited()
