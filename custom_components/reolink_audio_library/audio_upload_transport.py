"""Native upload primitive, separate from live talk and HA's action boundary.

This primitive requires an explicit write gate and does not establish persistence:
its caller must verify inventory,
preservation and reconnection separately. See docs/ANDROID_UPLOAD_EVIDENCE.md.
No proprietary implementation code is included here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import struct
from uuid import UUID
from xml.etree import ElementTree as ET

from .stored_audio import StoredAudioError

CMD_IMPORT_AUDIO = 261
CMD_SAVE_AUDIO = 262
MAX_AUDIO_BYTES = 184_320
CHUNK_BYTES = 2048
COMMAND_TIMEOUT = 15.0
_MAGIC = bytes.fromhex("f0debc0a")
_CLASS = bytes.fromhex("1464")


class AudioUploadError(StoredAudioError):
    """Sanitized upload failure identifying phase and actual numeric device code."""

    def __init__(self, phase: str, reason: str, *, status: int | None = None) -> None:
        self.phase = phase
        self.rspCode = status
        self.response_header: dict | None = None
        suffix = f"; device rspCode={status}" if status is not None else ""
        super().__init__(f"Stored-audio {phase}: {reason}{suffix}. No automatic retry was attempted")


def _valid_xml_text(value: str) -> bool:
    return all(
        char in "\t\n\r"
        or 0x20 <= ord(char) <= 0xD7FF
        or 0xE000 <= ord(char) <= 0xFFFD
        or 0x10000 <= ord(char) <= 0x10FFFF
        for char in value
    )


def _validate(channel: int, audio_bytes: bytes, name: str, external_id: str) -> None:
    if type(channel) is not int or not 0 <= channel <= 63:
        raise AudioUploadError("validation", "channel must be an integer from 0 through 63")
    if not isinstance(audio_bytes, bytes) or not 1 <= len(audio_bytes) <= MAX_AUDIO_BYTES:
        raise AudioUploadError("validation", f"native audio must contain 1 through {MAX_AUDIO_BYTES} bytes")
    validate_recording_name(name)
    if not isinstance(external_id, str) or not _valid_xml_text(external_id):
        raise AudioUploadError("validation", "external identifier must be a canonical UUID")
    try:
        canonical = str(UUID(external_id))
    except (ValueError, AttributeError):
        raise AudioUploadError("validation", "external identifier must be a canonical UUID") from None
    if len(external_id.encode("utf-8")) > 63 or canonical != external_id.lower():
        raise AudioUploadError("validation", "external identifier must be a canonical UUID of at most 63 bytes")


def validate_recording_name(name: str) -> None:
    """Validate a camera recording name before media or network access."""
    if not isinstance(name, str) or not name.strip() or not _valid_xml_text(name):
        raise AudioUploadError("validation", "recording name must contain valid XML 1.0 text")
    if len(name.encode("utf-8")) > 127:
        raise AudioUploadError("validation", "recording name exceeds 127 UTF-8 bytes")


def _xml(element: ET.Element) -> bytes:
    return ET.tostring(element, encoding="utf-8", xml_declaration=True)


def _extension(channel: int, *, binary: bool = False) -> bytes:
    root = ET.Element("Extension", {"version": "1.1"})
    if binary:
        ET.SubElement(root, "binaryData").text = "1"
    ET.SubElement(root, "channelId").text = str(channel)
    return _xml(root)


def _body(fields: dict[str, str]) -> bytes:
    root = ET.Element("body")
    info = ET.SubElement(root, "audioFileInfo", {"version": "1.1"})
    for key, value in fields.items():
        ET.SubElement(info, key).text = value
    return _xml(root)


@dataclass(frozen=True)
class _PinnedConnection:
    connection: object
    transport: object
    protocol: object
    aes_key: object

    @classmethod
    def capture(cls, bc) -> _PinnedConnection:
        connection = getattr(bc, "_connection", None)
        pin = cls(
            connection,
            getattr(connection, "_transport", None),
            getattr(connection, "_protocol", None),
            getattr(bc, "_aes_key", None),
        )
        pin.check(bc, "connection")
        return pin

    def check(self, bc, phase: str) -> None:
        from reolink_aio.enums import ConnectionEnum

        if (
            self.connection is None
            or self.transport is None
            or self.protocol is None
            or getattr(bc, "_connection", None) is not self.connection
            or getattr(self.connection, "_transport", None) is not self.transport
            or getattr(self.connection, "_protocol", None) is not self.protocol
            or getattr(self.connection, "connection_open", False) is not True
            or getattr(self.connection, "con_type", None) is not ConnectionEnum.tcp
            or getattr(bc, "_logged_in", False) is not True
            or getattr(bc, "_aes_key", None) != self.aes_key
        ):
            raise AudioUploadError(phase, "the original authenticated TCP connection is unavailable or changed")


def _reserve_id(bc, phase: str) -> int:
    current = getattr(bc, "_mess_id", None)
    if type(current) is not int or not 0 <= current <= 0xFFFFFF:
        raise AudioUploadError(phase, "the Baichuan message counter is unavailable")
    # No await occurs between reading and reserving the counter. Chunk sends
    # reuse the reserved ID without resetting the Host's global counter.
    next_id = (current + 1) % 0x1000000
    bc._mess_id = next_id
    return next_id


def _check_ack(response, *, command: int, full_id: int, phase: str) -> None:
    if not isinstance(response, tuple) or len(response) != 3:
        raise AudioUploadError(phase, "the acknowledgement shape is invalid")
    raw, header_length, payload = response
    if not isinstance(raw, bytes) or header_length != 24 or len(raw) < 24 or payload != b"":
        raise AudioUploadError(phase, "a complete modern acknowledgement without binary data is required")
    if (
        raw[:4] != _MAGIC
        or int.from_bytes(raw[4:8], "little") != command
        or int.from_bytes(raw[12:16], "little") != full_id
        # Requests use 1464; reolink_aio also recognises 0000 as a modern
        # 24-byte RESPONSE. Physical read-only 347/267 replies use 0000 on
        # the researched WiFi-W. Do not require request and reply classes equal.
        or raw[18:20] not in (_CLASS, b"\0\0")
        or int.from_bytes(raw[8:12], "little") != len(raw) - 24
        or int.from_bytes(raw[20:24], "little") > len(raw) - 24
    ):
        raise AudioUploadError(phase, "the acknowledgement header does not match this request")
    status = int.from_bytes(raw[16:18], "little")
    if status != 200:
        # reolink_aio permits 201/300 generally. Import/save require the native
        # client's stricter acknowledgement and must not retry an append.
        raise AudioUploadError(phase, "the device did not confirm this request", status=status)


async def _send(
    bc,
    pin: _PinnedConnection,
    channel: int,
    command: int,
    message_id: int,
    phase: str,
    *,
    extension: bytes,
    body: bytes = b"",
    payload: bytes = b"",
) -> None:
    try:
        pin.check(bc, phase)
        encrypted_extension = bc._aes_encrypt(extension)
        encrypted_body = bc._aes_encrypt(body)
        if (
            not isinstance(encrypted_extension, bytes)
            or not isinstance(encrypted_body, bytes)
            or len(encrypted_extension) != len(extension)
            or len(encrypted_body) != len(body)
        ):
            raise AudioUploadError(phase, "the authenticated AES transport is incompatible")
        full_id = (message_id << 8) | (channel + 1)
        # Installed connection.send can wait before writing when another sender
        # owns its mutex or this correlation ID. Reject that situation so its
        # normal TCP write has no intervening await after our identity check.
        mutex = getattr(pin.connection, "_mutex", None)
        receive_futures = getattr(pin.connection, "receive_futures", {})
        if (mutex is not None and mutex.locked()) or full_id in receive_futures.get(command, {}):
            raise AudioUploadError(phase, "the authenticated TCP connection is busy")
        wire_body = encrypted_extension + encrypted_body + payload
        header = (
            _MAGIC
            + struct.pack("<III", command, len(wire_body), full_id)
            + b"\0\0"
            + _CLASS
            + struct.pack("<I", len(encrypted_extension))
        )
        async with asyncio.timeout(COMMAND_TIMEOUT):
            # The connection's send writes once and waits. Unlike high-level
            # Baichuan.send, it does not reconnect, reauthenticate or retry.
            response = await pin.connection.send(header + wire_body, command, full_id, channel)
        pin.check(bc, phase)
        try:
            _check_ack(response, command=command, full_id=full_id, phase=phase)
        except AudioUploadError as error:
            if isinstance(response, tuple) and len(response) == 3:
                raw, header_length, response_payload = response
                if isinstance(raw, bytes) and len(raw) >= 24:
                    # Header metadata only: never retain body, AES material or
                    # raw payload. Keep enough evidence to diagnose a failed
                    # one-attempt test without repeating its camera write.
                    error.response_header = {
                        "header_length": header_length if type(header_length) is int else None,
                        "class": raw[18:20].hex(),
                        "status": int.from_bytes(raw[16:18], "little"),
                        "command": int.from_bytes(raw[4:8], "little"),
                        "body_length": int.from_bytes(raw[8:12], "little"),
                        "raw_length": len(raw),
                        "payload_length": len(response_payload) if isinstance(response_payload, bytes) else None,
                    }
            raise
    except AudioUploadError:
        raise
    except asyncio.CancelledError:
        raise
    except Exception as error:
        code = getattr(error, "rspCode", None)
        raise AudioUploadError(
            phase,
            type(error).__name__,
            status=code if type(code) is int else None,
        ) from None


async def async_import_and_commit(
    bc,
    channel: int,
    audio_bytes: bytes,
    name: str,
    external_id: str,
    *,
    device_write_authorized: bool = False,
) -> None:
    """Import native ADPCM and commit a new reply on one pinned TCP session.

    Caller responsibilities: verified firmware/format/capacity and explicit
    operator write approval, transaction ownership, no concurrent camera writes,
    post-save inventory/reconnect/preservation checks, and cleanup of any OWNED
    session on failure. This function never logs out or closes a borrowed Host.
    A successful return confirms acknowledgements only, not persistent storage.
    """
    if device_write_authorized is not True:
        raise AudioUploadError("authorization", "explicit operator approval for device writes is required")
    _validate(channel, audio_bytes, name, external_id)
    normal_extension = _extension(channel)
    binary_extension = _extension(channel, binary=True)
    begin_body = _body({"channelId": str(channel), "fileSize": str(len(audio_bytes)), "fileFormat": "adpcm"})
    save_body = _body(
        {"type": "reply", "fileName": name, "channelId": str(channel), "extId": external_id, "fileFormat": "adpcm"}
    )
    lock = getattr(bc, "_reolink_audio_library_audio_upload_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        bc._reolink_audio_library_audio_upload_lock = lock
    async with lock:
        try:
            async with asyncio.timeout(COMMAND_TIMEOUT):
                await bc.login()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            code = getattr(error, "rspCode", None)
            raise AudioUploadError("login", type(error).__name__, status=code if type(code) is int else None) from None
        pin = _PinnedConnection.capture(bc)
        import_id = _reserve_id(bc, "begin")
        await _send(bc, pin, channel, CMD_IMPORT_AUDIO, import_id, "begin", extension=normal_extension, body=begin_body)
        for start in range(0, len(audio_bytes), CHUNK_BYTES):
            await _send(
                bc,
                pin,
                channel,
                CMD_IMPORT_AUDIO,
                import_id,
                f"chunk {start // CHUNK_BYTES + 1}",
                extension=binary_extension,
                payload=audio_bytes[start : start + CHUNK_BYTES],
            )
        pin.check(bc, "save")
        save_id = _reserve_id(bc, "save")
        await _send(bc, pin, channel, CMD_SAVE_AUDIO, save_id, "save", extension=normal_extension, body=save_body)
