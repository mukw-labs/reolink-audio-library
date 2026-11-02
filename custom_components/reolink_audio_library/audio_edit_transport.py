"""One-attempt custom recording metadata/delete packets, never audio playback.

Wire fields were independently reconstructed from the official SDK and exact
firmware. See docs/RENAME_DELETE_VALIDATION.md. Caller owns the authenticated
Host/session and must verify identity, provenance and persistence separately.
"""

from __future__ import annotations

import asyncio
from xml.etree import ElementTree as ET

from .audio_upload_transport import (
    COMMAND_TIMEOUT,
    _body,
    _extension,
    _PinnedConnection,
    _reserve_id,
    _send,
    _valid_xml_text,
    _xml,
    validate_recording_name,
)
from .stored_audio import StoredAudioError

CMD_RENAME_AUDIO = 368
CMD_DELETE_AUDIO = 348


def rename_body(channel: int, file_id: int, name: str, external_id: str) -> bytes:
    """SDK metadata-list form, one entry; no replacement bytes or usage changes."""
    root = ET.Element("body")
    table = ET.SubElement(root, "audioFileInfoList", {"version": "1.1"})
    item = ET.SubElement(table, "audioFileInfo")
    for key, value in (("channelId", str(channel)), ("id", str(file_id)), ("fileName", name)):
        ET.SubElement(item, key).text = value
    if external_id:
        ET.SubElement(item, "extId").text = external_id
    return _xml(root)


async def async_change_recording(
    bc,
    channel: int,
    file_id: int,
    *,
    new_name: str | None = None,
    external_id: str = "",
    device_write_authorized: bool = False,
    pin=None,
) -> None:
    """One command on a pinned TCP session. None means delete, not an empty name."""
    if device_write_authorized is not True:
        raise StoredAudioError("An explicit recording action is required for camera writes")
    if type(channel) is not int or channel != 0 or type(file_id) is not int or not 3 <= file_id <= 14:
        raise StoredAudioError("Only existing custom recordings on the verified channel may be changed")
    if new_name is not None:
        validate_recording_name(new_name)
        if (
            not isinstance(external_id, str)
            or not _valid_xml_text(external_id)
            or len(external_id.encode("utf-8")) > 63
        ):
            raise StoredAudioError("The recording UUID cannot be preserved safely")
    async with asyncio.timeout(COMMAND_TIMEOUT):
        if pin is not None:
            pin.check(bc, "recording change")
        await bc.login()
        if pin is None:
            pin = _PinnedConnection.capture(bc)
        pin.check(bc, "recording change")
        command = CMD_RENAME_AUDIO if new_name is not None else CMD_DELETE_AUDIO
        body = (
            rename_body(channel, file_id, new_name, external_id)
            if new_name is not None
            else _body({"channelId": str(channel), "id": str(file_id)})
        )
        await _send(
            bc,
            pin,
            channel,
            command,
            _reserve_id(bc, "recording change"),
            "rename" if new_name is not None else "delete",
            extension=_extension(channel),
            body=body,
        )
