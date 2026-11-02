"""Offline native upload framing and interruption tests; never access hardware."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from xml.etree import ElementTree as ET

import pytest
from reolink_aio.enums import ConnectionEnum

from custom_components.reolink_audio_library import audio_upload_transport as upload

EXTERNAL_ID = "fb391df9-17bb-4bd8-a063-7d66ccf404d8"


def crypt(data):
    """Reversible, length-preserving encryption substitute, not production AES."""
    assert isinstance(data, bytes)
    return bytes(value ^ 0xA5 for value in data)


def ack(packet, status=200):
    return packet[:8] + bytes(4) + packet[12:16] + status.to_bytes(2, "little") + bytes.fromhex("1464") + bytes(4)


class Connection:
    con_type = ConnectionEnum.tcp

    def __init__(self):
        self._transport = SimpleNamespace(closed=False)
        self._protocol = object()
        self._mutex = asyncio.Lock()
        self.receive_futures = {}
        self.packets = []
        self.hook = None

    @property
    def connection_open(self):
        return not self._transport.closed

    async def send(self, packet, command, full_id, channel):
        assert int.from_bytes(packet[4:8], "little") == command
        assert int.from_bytes(packet[12:16], "little") == full_id
        assert full_id & 0xFF == channel + 1
        self.packets.append(packet)
        await asyncio.sleep(0)
        if self.hook is not None:
            result = self.hook(packet, len(self.packets))
            if result is not None:
                return result
        return ack(packet), 24, b""


@pytest.fixture
def native_bc():
    bc = SimpleNamespace(
        _connection=Connection(),
        _logged_in=True,
        _aes_key=b"0123456789abcdef",
        _mess_id=0x1234,
        _aes_encrypt=crypt,
        login=AsyncMock(),
        logout=AsyncMock(),
        close=AsyncMock(),
        send=AsyncMock(side_effect=AssertionError("high-level send must not be used")),
        _connect_if_needed=AsyncMock(side_effect=AssertionError("reconnection must not occur")),
    )
    return bc


async def run(bc, data=b"\x98\x76", **kwargs):
    arguments = {"channel": 0, "audio_bytes": data, "name": "A reply", "external_id": EXTERNAL_ID}
    arguments.update(kwargs)
    return await upload.async_import_and_commit(bc, **arguments, device_write_authorized=True)


def unpack(packet):
    assert packet[:4] == bytes.fromhex("f0debc0a")
    assert packet[16:20] == bytes.fromhex("00001464")
    assert int.from_bytes(packet[8:12], "little") == len(packet) - 24
    offset = int.from_bytes(packet[20:24], "little")
    return ET.fromstring(crypt(packet[24 : 24 + offset])), packet[24 + offset :]


@pytest.mark.parametrize("authorization", [False, None, 1, "yes"])
async def test_default_gate_precedes_even_client_access(authorization):
    with pytest.raises(upload.AudioUploadError, match="authorization"):
        await upload.async_import_and_commit(
            object(), 0, b"a", "reply", EXTERNAL_ID, device_write_authorized=authorization
        )


@pytest.mark.parametrize(
    "arguments",
    [
        {"channel": -1},
        {"channel": 64},
        {"channel": True},
        {"channel": "0"},
        {"audio_bytes": b""},
        {"audio_bytes": bytes(184321)},
        {"audio_bytes": bytearray(b"a")},
        {"name": ""},
        {"name": "   "},
        {"name": "a" * 128},
        {"name": "é" * 64},
        {"name": "bad\x00name"},
        {"name": "bad\ud800"},
        {"name": "bad\ufffe"},
        {"external_id": "invalid"},
        {"external_id": EXTERNAL_ID.replace("-", "")},
        {"external_id": "\x00"},
    ],
)
async def test_invalid_inputs_never_login_or_send(native_bc, arguments):
    with pytest.raises(upload.AudioUploadError, match="validation"):
        await run(native_bc, **arguments)
    native_bc.login.assert_not_awaited()
    assert not native_bc._connection.packets


@pytest.mark.parametrize("size", [1, 2048, 2049, 184320])
async def test_chunks_are_raw_bounded_and_share_import_id(native_bc, size):
    data = bytes(index % 256 for index in range(size))
    await run(native_bc, data, channel=63)
    packets = native_bc._connection.packets
    assert len(packets) == 2 + (size + 2047) // 2048
    assert [int.from_bytes(packet[4:8], "little") for packet in packets] == [261] * (len(packets) - 1) + [262]
    assert {int.from_bytes(packet[12:16], "little") for packet in packets[:-1]} == {0x123540}
    assert int.from_bytes(packets[-1][12:16], "little") == 0x123640
    assert native_bc._mess_id == 0x1236
    raw_chunks = []
    for packet in packets[1:-1]:
        extension, payload = unpack(packet)
        assert extension.attrib == {"version": "1.1"}
        assert extension.findtext("binaryData") == "1"
        assert extension.findtext("channelId") == "63"
        assert 1 <= len(payload) <= 2048
        raw_chunks.append(payload)
    assert b"".join(raw_chunks) == data
    native_bc.login.assert_awaited_once()
    native_bc.send.assert_not_awaited()
    native_bc._connect_if_needed.assert_not_awaited()
    native_bc.close.assert_not_awaited()
    native_bc.logout.assert_not_awaited()


async def test_metadata_is_separately_encrypted_and_xml_escaped(native_bc):
    name = "Say <hello> & 'café'"
    await run(native_bc, name=name)
    begin_extension, begin_encrypted = unpack(native_bc._connection.packets[0])
    assert begin_extension.findtext("binaryData") is None
    begin = ET.fromstring(crypt(begin_encrypted)).find("audioFileInfo")
    assert begin.attrib == {"version": "1.1"}
    assert {element.tag: element.text for element in begin} == {
        "channelId": "0",
        "fileSize": "2",
        "fileFormat": "adpcm",
    }
    _, save_encrypted = unpack(native_bc._connection.packets[-1])
    saved = ET.fromstring(crypt(save_encrypted)).find("audioFileInfo")
    assert {element.tag: element.text for element in saved} == {
        "type": "reply",
        "fileName": name,
        "channelId": "0",
        "extId": EXTERNAL_ID,
        "fileFormat": "adpcm",
    }


async def test_real_library_aes_accepts_and_decrypts_separate_xml():
    from reolink_aio.baichuan.baichuan import Baichuan

    bc = Baichuan("127.0.0.1", "user", "pass", None)
    bc._aes_key = b"0123456789abcdef"
    bc._logged_in = True
    bc._connection = Connection()
    await run(bc)
    packet = bc._connection.packets[-1]
    offset = int.from_bytes(packet[20:24], "little")
    extension = bc._aes_decrypt(packet[24 : 24 + offset], b"", decode=False)
    body = bc._aes_decrypt(packet[24 + offset :], b"", decode=False)
    assert ET.fromstring(extension).findtext("channelId") == "0"
    assert ET.fromstring(body).findtext("audioFileInfo/type") == "reply"


async def test_message_id_wrap_uses_fresh_save_id(native_bc):
    native_bc._mess_id = 0xFFFFFF
    await run(native_bc)
    assert [int.from_bytes(packet[12:16], "little") for packet in native_bc._connection.packets] == [1, 1, 257]


@pytest.mark.parametrize("status", [0, 201, 300, 400, 401, 500])
async def test_non_200_ack_stops_without_retry(native_bc, status):
    native_bc._connection.hook = lambda packet, count: (ack(packet, status), 24, b"")
    with pytest.raises(upload.AudioUploadError) as caught:
        await run(native_bc)
    assert caught.value.rspCode == status
    assert caught.value.phase == "begin"
    assert len(native_bc._connection.packets) == 1


@pytest.mark.parametrize("failed_request, phase", [(2, "chunk 1"), (3, "save")])
async def test_chunk_or_save_rejection_is_not_retried(native_bc, failed_request, phase):
    native_bc._connection.hook = lambda packet, count: (ack(packet, 201 if count == failed_request else 200), 24, b"")
    with pytest.raises(upload.AudioUploadError) as caught:
        await run(native_bc)
    assert caught.value.phase == phase
    assert caught.value.rspCode == 201
    assert len(native_bc._connection.packets) == failed_request


@pytest.mark.parametrize("name", ["a" * 127, "é" * 63 + "a", "\U0001f514" * 31 + "abc"])
async def test_valid_names_at_utf8_boundary_are_accepted(native_bc, name):
    assert len(name.encode("utf-8")) == 127
    await run(native_bc, name=name)
    _, save_encrypted = unpack(native_bc._connection.packets[-1])
    assert ET.fromstring(crypt(save_encrypted)).findtext("audioFileInfo/fileName") == name


@pytest.mark.parametrize(
    "mutation", ["magic", "command", "message_id", "class", "length", "offset", "short", "legacy", "binary", "shape"]
)
async def test_malformed_ack_is_rejected(native_bc, mutation):
    def broken(packet, count):
        raw = bytearray(ack(packet))
        if mutation in {"magic", "command", "message_id", "class", "length", "offset"}:
            offset = {"magic": 0, "command": 4, "message_id": 12, "class": 18, "length": 8, "offset": 20}[mutation]
            raw[offset] ^= 1
        if mutation == "shape":
            return (bytes(raw),)
        return (
            bytes(raw[:20] if mutation == "short" else raw),
            20 if mutation == "legacy" else 24,
            b"data" if mutation == "binary" else b"",
        )

    native_bc._connection.hook = broken
    with pytest.raises(upload.AudioUploadError, match="acknowledgement"):
        await run(native_bc)
    assert len(native_bc._connection.packets) == 1


@pytest.mark.parametrize("identity", ["connection", "transport", "protocol", "closed", "authentication", "key"])
async def test_connection_change_stops_before_any_followup(native_bc, identity):
    connection = native_bc._connection

    def replace(packet, count):
        if identity == "connection":
            native_bc._connection = Connection()
        elif identity == "transport":
            connection._transport = SimpleNamespace(closed=False)
        elif identity == "protocol":
            connection._protocol = object()
        elif identity == "closed":
            connection._transport.closed = True
        elif identity == "authentication":
            native_bc._logged_in = False
        else:
            native_bc._aes_key = b"changed key"

    connection.hook = replace
    with pytest.raises(upload.AudioUploadError, match="connection"):
        await run(native_bc)
    assert len(connection.packets) == 1
    native_bc.login.assert_awaited_once()
    native_bc.logout.assert_not_awaited()


@pytest.mark.parametrize("condition", ["udp", "closed", "unauthenticated", "counter"])
async def test_invalid_initial_transport_never_sends(native_bc, condition):
    if condition == "udp":
        native_bc._connection.con_type = ConnectionEnum.udp
    elif condition == "closed":
        native_bc._connection._transport.closed = True
    elif condition == "unauthenticated":
        native_bc._logged_in = False
    else:
        native_bc._mess_id = -1
    with pytest.raises(upload.AudioUploadError):
        await run(native_bc)
    assert not native_bc._connection.packets


@pytest.mark.parametrize("stage", ["login", "begin", "chunk 1", "save"])
async def test_errors_are_phase_specific_and_redact_exception_text(native_bc, stage):
    class DeviceError(Exception):
        rspCode = 451

    error = DeviceError("SECRET password and audio payload")
    if stage == "login":
        native_bc.login.side_effect = error
    else:

        def fail(packet, count):
            if count == {"begin": 1, "chunk 1": 2, "save": 3}[stage]:
                raise error

        native_bc._connection.hook = fail
    with pytest.raises(upload.AudioUploadError) as caught:
        await run(native_bc)
    assert caught.value.phase == stage
    assert caught.value.rspCode == 451
    assert "SECRET" not in str(caught.value)
    assert "DeviceError" in str(caught.value)
    native_bc.close.assert_not_awaited()
    native_bc.logout.assert_not_awaited()


async def test_chunk_cancellation_propagates_without_save_or_cleanup(native_bc):
    def cancel(packet, count):
        if count == 2:
            raise asyncio.CancelledError

    native_bc._connection.hook = cancel
    with pytest.raises(asyncio.CancelledError):
        await run(native_bc, bytes(4096))
    assert len(native_bc._connection.packets) == 2
    native_bc.logout.assert_not_awaited()
    native_bc.close.assert_not_awaited()


@pytest.mark.parametrize("stage", ["login", "send"])
async def test_timeout_stops_without_retry(native_bc, monkeypatch, stage):
    monkeypatch.setattr(upload, "COMMAND_TIMEOUT", 0.001)
    if stage == "login":
        native_bc.login.side_effect = lambda: None

        async def wait_login():
            await asyncio.Event().wait()

        native_bc.login.side_effect = wait_login
    else:

        async def wait_send(*args):
            await asyncio.Event().wait()

        native_bc._connection.send = wait_send
    with pytest.raises(upload.AudioUploadError, match="TimeoutError"):
        await run(native_bc)
    native_bc.login.assert_awaited_once()
    native_bc.logout.assert_not_awaited()


async def test_same_client_transactions_do_not_interleave(native_bc):
    await asyncio.gather(run(native_bc, b"first"), run(native_bc, b"second"))
    packets = native_bc._connection.packets
    assert [int.from_bytes(packet[4:8], "little") for packet in packets] == [261, 261, 262, 261, 261, 262]
    assert native_bc.login.await_count == 2
    assert native_bc._mess_id == 0x1238


@pytest.mark.parametrize("condition", ["mutex", "future"])
async def test_busy_connection_rejected_before_send(native_bc, condition):
    if condition == "mutex":
        await native_bc._connection._mutex.acquire()
    else:
        native_bc._connection.receive_futures = {261: {0x123501: object()}}
    with pytest.raises(upload.AudioUploadError, match="busy"):
        await run(native_bc)
    assert not native_bc._connection.packets
    native_bc.logout.assert_not_awaited()


@pytest.mark.parametrize("message_class", [b"\0\0", bytes.fromhex("1464")])
async def test_modern_response_class_need_not_equal_request_class(native_bc, message_class):
    """0000 is observed in physical read replies; this upload fixture is synthetic."""

    def response(packet, count):
        raw = ack(packet)
        return raw[:18] + message_class + raw[20:], 24, b""

    native_bc._connection.hook = response
    await run(native_bc, bytes(4000))
    assert [int.from_bytes(packet[4:8], "little") for packet in native_bc._connection.packets] == [261, 261, 261, 262]


async def test_rejected_response_retains_only_safe_header_metadata(native_bc):
    def response(packet, count):
        raw = ack(packet)[:18] + bytes.fromhex("1465") + ack(packet)[20:]
        return raw, 24, b""

    native_bc._connection.hook = response
    with pytest.raises(upload.AudioUploadError) as caught:
        await run(native_bc)
    assert caught.value.response_header == {
        "header_length": 24,
        "class": "1465",
        "status": 200,
        "command": 261,
        "body_length": 0,
        "raw_length": 24,
        "payload_length": 0,
    }
    assert len(native_bc._connection.packets) == 1
