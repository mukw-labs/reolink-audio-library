"""Bounded command-410 download on an exclusively owned authenticated session.

The caller owns/closes the Host and holds the physical-camera operation lock.
Only this connection's callback is adapted; the official HA Host is never changed.
The observed download EOF (201) does not relax upload acknowledgement rules.
"""

from __future__ import annotations

import asyncio
import struct
from xml.etree import ElementTree as ET

from .audio_upload_transport import MAX_AUDIO_BYTES, _body, _extension, _PinnedConnection, _reserve_id
from .stored_audio import StoredAudioError

COMMAND = 410
EXPORT_TIMEOUT = 30
MAX_CHUNK_BYTES = 38_000
MAX_XML_BYTES = 16_384


def _xml(value: str) -> ET.Element:
    if not value or len(value) > MAX_XML_BYTES or "<!" in value:
        raise StoredAudioError("Invalid recording export XML")
    return ET.fromstring(value)


async def async_export_audio(bc, channel: int, file_id: int, expected_size: int) -> bytes:
    """Download one known recording, without playback, reconnect or retries."""
    if (
        type(channel) is not int
        or not 0 <= channel <= 63
        or type(file_id) is not int
        or not 0 <= file_id <= 14
        or type(expected_size) is not int
        or not 1 <= expected_size <= MAX_AUDIO_BYTES
    ):
        raise StoredAudioError("Invalid recording export selection or size")
    try:
        async with asyncio.timeout(EXPORT_TIMEOUT):
            await bc.login()
            pin = _PinnedConnection.capture(bc)
            protocol = pin.protocol
            original = protocol._push_callback
            message_id = _reserve_id(bc, "export")
            full_id = (message_id << 8) | (channel + 1)
            extension = bc._aes_encrypt(_extension(channel))
            body = bc._aes_encrypt(_body({"channelId": str(channel), "id": str(file_id)}))
            wire = extension + body
            packet = (
                bytes.fromhex("f0debc0a")
                + struct.pack("<III", COMMAND, len(wire), full_id)
                + b"\0\0\x14\x64"
                + struct.pack("<I", len(extension))
                + wire
            )
            finished = asyncio.Event()
            chunks = bytearray()
            failure: StoredAudioError | None = None
            eof = False
            frames = 0

            def receive(raw, header_length, payload, *, metadata=False):
                nonlocal failure, eof, frames
                if failure is not None:
                    return
                try:
                    pin.check(bc, "export")
                    if (
                        not isinstance(raw, bytes)
                        or header_length != 24
                        or not 24 <= len(raw) <= 24 + MAX_XML_BYTES
                        or not isinstance(payload, bytes)
                        or len(payload) > MAX_CHUNK_BYTES
                        or raw[:4] != bytes.fromhex("f0debc0a")
                        or int.from_bytes(raw[4:8], "little") != COMMAND
                        or int.from_bytes(raw[12:16], "little") != full_id
                        or raw[18:20] not in (b"\0\0", b"\x14\x64")
                        or int.from_bytes(raw[8:12], "little") != len(raw) - 24 + len(payload)
                        or int.from_bytes(raw[20:24], "little") not in (0, len(raw) - 24)
                        or (payload and int.from_bytes(raw[20:24], "little") == 0)
                    ):
                        raise StoredAudioError("Recording export response does not match this request")
                    status = int.from_bytes(raw[16:18], "little")
                    root = _xml(bc._decrypt(raw, header_length, COMMAND))
                    if metadata:
                        info = root.find("audioFileInfo") if root.tag == "body" else None
                        if (
                            status != 200
                            or payload
                            or info is None
                            or info.findtext("fileSize") != str(expected_size)
                            or info.findtext("fileFormat") != "adpcm"
                        ):
                            raise StoredAudioError("Recording export size or format changed; browse again")
                        return
                    frames += 1
                    if (
                        eof
                        or frames > 128
                        or root.tag != "Extension"
                        or root.findtext("binaryData") != "1"
                        # The verified firmware sends raw ADPCM. Fail closed on
                        # other encryption modes until separately established.
                        or root.findtext("encryptLen") not in (None, "0")
                        or root.findtext("channelId") not in (None, str(channel))
                    ):
                        raise StoredAudioError("Unsupported recording export stream")
                    if status == 201 and not payload:
                        if len(chunks) != expected_size:
                            raise StoredAudioError("Recording export ended before its announced size")
                        eof = True
                        finished.set()
                    elif status == 200 and payload and len(chunks) + len(payload) <= expected_size:
                        chunks.extend(payload)
                    else:
                        raise StoredAudioError("Invalid recording export chunk or status")
                except Exception as error:
                    failure = (
                        error
                        if isinstance(error, StoredAudioError)
                        else StoredAudioError(f"Could not decode recording export ({type(error).__name__})")
                    )
                    finished.set()

            def push(command, raw, header_length, payload):
                if command == COMMAND:
                    receive(raw, header_length, payload)
                elif original is not None:
                    original(command, raw, header_length, payload)

            mutex = getattr(pin.connection, "_mutex", None)
            if (mutex is not None and mutex.locked()) or pin.connection.receive_futures:
                raise StoredAudioError("Recording export connection is busy")
            protocol._push_callback = push
            try:
                # The first response is metadata. TCP parsing can dispatch all
                # subsequent chunks/EOF before this await resumes; buffer those
                # against inventory size, then independently check metadata.
                response = await pin.connection.send(packet, COMMAND, full_id, channel)
                receive(*response, metadata=True)
                await finished.wait()
                pin.check(bc, "export")
                if failure is not None:
                    raise failure
                return bytes(chunks)
            finally:
                protocol._push_callback = original
    except StoredAudioError:
        raise
    except Exception as error:
        raise StoredAudioError(f"Could not export recording ({type(error).__name__})") from None
