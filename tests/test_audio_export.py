"""Offline command-410 framing, bounds, cancellation and no-retry checks."""

import asyncio
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from reolink_aio.enums import ConnectionEnum

from custom_components.reolink_audio_library import audio_export as export
from custom_components.reolink_audio_library.stored_audio import StoredAudioError


def frame(full_id, xml, payload=b"", status=200):
    xml = xml.encode()
    raw = bytes.fromhex("f0debc0a") + struct.pack("<IIIH", 410, len(xml) + len(payload), full_id, status)
    return raw + bytes(2) + struct.pack("<I", len(xml)) + xml, 24, payload


@pytest.fixture
def download():
    original = Mock()
    connection = SimpleNamespace(
        _transport=object(),
        _protocol=SimpleNamespace(_push_callback=original),
        connection_open=True,
        con_type=ConnectionEnum.tcp,
        receive_futures={},
        _mutex=asyncio.Lock(),
    )
    state = SimpleNamespace(
        audio=b"\x70" * 38720,
        metadata=None,
        chunks=None,
        original=original,
        mutate=None,
        late=False,
        missing_eof=False,
        sends=0,
    )
    bc = SimpleNamespace(
        _connection=connection,
        _aes_key=b"key",
        _mess_id=4,
        _logged_in=True,
        login=AsyncMock(),
        _aes_encrypt=lambda value: value,
        _decrypt=lambda raw, hlen, cmd: raw[hlen:].decode(),
        send=AsyncMock(side_effect=AssertionError("No high-level resend")),
    )

    async def send(packet, command, full_id, channel):
        state.sends += 1
        assert command == 410 and channel == 0
        offset = int.from_bytes(packet[20:24], "little")
        assert b"<id>4</id>" in packet[24 + offset :]
        assert b"<type>" not in packet
        metadata = frame(
            full_id,
            state.metadata
            or (
                f'<body><audioFileInfo version="1.1"><fileSize>{len(state.audio)}</fileSize>'
                "<fileFormat>adpcm</fileFormat></audioFileInfo></body>"
            ),
        )

        def deliver():
            for item in (
                state.chunks
                if state.chunks is not None
                else [
                    frame(full_id, "<Extension><binaryData>1</binaryData></Extension>", part)
                    for start in range(0, len(state.audio), 38000)
                    if (part := state.audio[start : start + 38000])
                ]
            ):
                if state.mutate:
                    item = state.mutate(item)
                connection._protocol._push_callback(410, *item)
            if not state.missing_eof:
                connection._protocol._push_callback(
                    410, *frame(full_id, "<Extension><binaryData>1</binaryData></Extension>", status=201)
                )

        if state.late:
            asyncio.get_running_loop().call_soon(deliver)
        else:
            deliver()
        return metadata

    connection.send = AsyncMock(side_effect=send)
    state.bc = bc
    return state


@pytest.mark.parametrize("late", [False, True])
@pytest.mark.parametrize("size", [1, 4000, 38720, 184320])
async def test_exact_download_even_when_all_chunks_precede_metadata_return(download, late, size):
    download.audio = bytes(size)
    download.late = late
    assert await export.async_export_audio(download.bc, 0, 4, size) == download.audio
    assert download.sends == 1
    assert download.bc._connection._protocol._push_callback is download.original
    download.bc.send.assert_not_awaited()


@pytest.mark.parametrize(
    "metadata",
    [
        "<body><audioFileInfo><fileSize>5</fileSize><fileFormat>adpcm</fileFormat></audioFileInfo></body>",
        "<body><audioFileInfo><fileSize>38720</fileSize><fileFormat>aac</fileFormat></audioFileInfo></body>",
        "<!DOCTYPE body><body/>",
        "<body/>",
        "<broken",
    ],
)
async def test_metadata_must_confirm_exact_size_and_codec(download, metadata):
    download.metadata = metadata
    with pytest.raises(StoredAudioError):
        await export.async_export_audio(download.bc, 0, 4, len(download.audio))
    assert download.sends == 1
    assert download.bc._connection._protocol._push_callback is download.original


@pytest.mark.parametrize(
    "field,value",
    [
        (0, b"oops"),
        (4, struct.pack("<I", 349)),
        (8, bytes(4)),
        (12, bytes(4)),
        (16, struct.pack("<H", 300)),
        (18, b"\x14\x66"),
        (20, bytes(4)),
    ],
)
async def test_corrupted_chunk_header_rejected(download, field, value):
    def mutate(item):
        raw, hlen, payload = item
        return raw[:field] + value + raw[field + len(value) :], hlen, payload

    download.mutate = mutate
    with pytest.raises(StoredAudioError):
        await export.async_export_audio(download.bc, 0, 4, len(download.audio))
    assert download.bc._connection._protocol._push_callback is download.original


@pytest.mark.parametrize(
    "xml,payload,status",
    [
        ("<Extension><binaryData>1</binaryData></Extension>", b"a", 201),
        ("<Extension><binaryData>1</binaryData></Extension>", b"", 200),
        ("<Extension><binaryData>0</binaryData></Extension>", b"a", 200),
        ("<Extension><binaryData>1</binaryData><encryptLen>1</encryptLen></Extension>", b"a", 200),
        ("<Extension><binaryData>1</binaryData><channelId>1</channelId></Extension>", b"a", 200),
        ("<Extension><binaryData>1</binaryData></Extension>", bytes(38001), 200),
    ],
)
async def test_invalid_or_unverified_binary_stream_rejected(download, xml, payload, status):
    download.chunks = [frame(0x501, xml, payload, status)]
    with pytest.raises(StoredAudioError):
        await export.async_export_audio(download.bc, 0, 4, len(download.audio))


async def test_short_export_rejected(download):
    download.chunks = []
    with pytest.raises(StoredAudioError, match="ended"):
        await export.async_export_audio(download.bc, 0, 4, len(download.audio))


async def test_timeout_restores_callback_and_never_retries(download, monkeypatch):
    monkeypatch.setattr(export, "EXPORT_TIMEOUT", 0.02)
    download.missing_eof = True
    with pytest.raises(StoredAudioError, match="TimeoutError"):
        await export.async_export_audio(download.bc, 0, 4, len(download.audio))
    assert download.sends == 1
    assert download.bc._connection._protocol._push_callback is download.original


async def test_cancel_restores_callback(download):
    download.missing_eof = True
    task = asyncio.create_task(export.async_export_audio(download.bc, 0, 4, len(download.audio)))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert download.bc._connection._protocol._push_callback is download.original


@pytest.mark.parametrize(
    "channel,file_id,size", [(True, 4, 1), (64, 4, 1), (0, -1, 1), (0, 15, 1), (0, 4, 0), (0, 4, 184321)]
)
async def test_invalid_selection_never_contacts_camera(download, channel, file_id, size):
    with pytest.raises(StoredAudioError):
        await export.async_export_audio(download.bc, channel, file_id, size)
    download.bc.login.assert_not_awaited()


@pytest.mark.parametrize("fragmented", [False, True])
async def test_real_library_tcp_dispatch_and_aes_with_coalesced_eof(fragmented):
    from reolink_aio.baichuan.baichuan import Baichuan
    from reolink_aio.baichuan.tcp_protocol import BaichuanTcpConnection, BaichuanTcpClientProtocol

    bc = Baichuan("127.0.0.1", "fake-user", "fake-password", None)
    bc._aes_key = b"0123456789abcdef"
    bc._logged_in = True
    bc.login = AsyncMock()
    original = Mock()
    loop = asyncio.get_running_loop()
    connection = BaichuanTcpConnection("127.0.0.1", 9000)
    protocol = BaichuanTcpClientProtocol(loop, "127.0.0.1", original)
    bc._connection = connection
    connection._protocol = protocol
    audio = b"\x70" * 38720

    def encrypted_frame(full_id, xml, payload=b"", status=200):
        encrypted = bc._aes_encrypt(('<?xml version="1.0"?>' + xml).encode())
        raw = bytes.fromhex("f0debc0a") + struct.pack("<IIIH", 410, len(encrypted) + len(payload), full_id, status)
        return raw + bytes(2) + struct.pack("<I", len(encrypted)) + encrypted + payload

    def write(packet):
        full_id = int.from_bytes(packet[12:16], "little")
        wire = encrypted_frame(
            full_id,
            "<body><audioFileInfo><fileSize>38720</fileSize><fileFormat>adpcm</fileFormat></audioFileInfo></body>",
        )
        extension = "<Extension><binaryData>1</binaryData></Extension>"
        wire += encrypted_frame(full_id, extension, audio[:38000])
        wire += encrypted_frame(full_id, extension, audio[38000:])
        wire += encrypted_frame(full_id, extension, status=201)
        pieces = [wire[:13], wire[13:25], wire[25:38060], wire[38060:]] if fragmented else [wire]
        for piece in pieces:
            loop.call_soon(protocol.data_received, piece)

    connection._transport = SimpleNamespace(write=write, is_closing=lambda: False)
    assert await export.async_export_audio(bc, 0, 4, len(audio)) == audio
    assert not protocol.receive_futures
    assert protocol._push_callback is original
    original.assert_not_called()


async def test_changed_connection_is_rejected_and_callback_restored(download):
    def mutate(item):
        download.bc._connection._transport = object()
        return item

    download.mutate = mutate
    with pytest.raises(StoredAudioError, match="connection"):
        await export.async_export_audio(download.bc, 0, 4, len(download.audio))
    assert download.bc._connection._protocol._push_callback is download.original


async def test_unrelated_pushes_keep_original_dispatch(download):
    def mutate(item):
        download.bc._connection._protocol._push_callback(267, b"other", 24, b"")
        return item

    download.mutate = mutate
    assert await export.async_export_audio(download.bc, 0, 4, len(download.audio)) == download.audio
    assert download.original.call_count == 2


async def test_stream_overrun_is_rejected_before_buffer_growth(download):
    xml = "<Extension><binaryData>1</binaryData></Extension>"
    download.chunks = [frame(0x501, xml, bytes(38000)), frame(0x501, xml, bytes(721))]
    with pytest.raises(StoredAudioError, match="chunk"):
        await export.async_export_audio(download.bc, 0, 4, len(download.audio))


@pytest.mark.parametrize("busy", ["mutex", "future"])
async def test_busy_owned_session_is_not_modified(download, busy):
    if busy == "mutex":
        await download.bc._connection._mutex.acquire()
    else:
        download.bc._connection.receive_futures = {347: {1: object()}}
    with pytest.raises(StoredAudioError, match="busy"):
        await export.async_export_audio(download.bc, 0, 4, len(download.audio))
    assert download.sends == 0
    assert download.bc._connection._protocol._push_callback is download.original
