"""Fresh native HTTP recording inventory without changing the Host's caches.

The HTTP list includes stock recordings omitted by Baichuan command 347. Read
only ``value``: an action-1 response can repeat recordings in ``initial``.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

from .stored_audio import AudioInventory, AudioProtocolError, AudioRecord, StoredAudioError

COMMAND = "GetAudioFileList"


def _integer(entry: dict[str, Any], field: str, *, required: bool = False) -> int | None:
    if field not in entry and not required:
        return None
    value = entry.get(field)
    if type(value) is not int or value < 0:
        raise AudioProtocolError(f"Native audio metadata {field} must be a nonnegative integer")
    return value


def _string(entry: dict[str, Any], field: str, *, required: bool = False) -> str | None:
    if field not in entry and not required:
        return None
    value = entry.get(field)
    if not isinstance(value, str):
        raise AudioProtocolError(f"Native audio metadata {field} must be a string")
    return value


def parse_native_audio_inventory(response: Any) -> AudioInventory:
    """Validate one native response, preserving actual IDs and unknown fields.

    Duration units, recording ownership and maximum capacity are not inferred
    from this response. In particular ID zero is a valid native recording ID.
    """
    if not isinstance(response, list) or len(response) != 1 or not isinstance(response[0], dict):
        raise AudioProtocolError("Camera returned an invalid native audio inventory response")
    result = response[0]
    if result.get("cmd") != COMMAND:
        raise AudioProtocolError("Camera returned an unexpected native audio inventory command")
    code = result.get("code")
    if type(code) is not int:
        raise AudioProtocolError("Camera returned an invalid native audio inventory status")
    if code != 0:
        # Never include error/detail strings or the response in user-facing text.
        raise AudioProtocolError(f"Native audio inventory failed (device code={code})")
    value = result.get("value")
    if not isinstance(value, dict) or not isinstance(value.get("AudioFileList"), list):
        raise AudioProtocolError("Camera response does not contain a native audio inventory")

    records = []
    ids: set[int] = set()
    for entry in value["AudioFileList"]:
        if not isinstance(entry, dict):
            raise AudioProtocolError("Native audio inventory contains an invalid recording")
        file_id = _integer(entry, "id", required=True)
        name = _string(entry, "fileName", required=True)
        assert file_id is not None and name is not None
        if file_id in ids:
            raise AudioProtocolError("Camera returned duplicate native audio recording IDs")
        ids.add(file_id)
        records.append(
            AudioRecord(
                id=file_id,
                name=name,
                type=_string(entry, "type"),
                size=_integer(entry, "fileSize"),
                audio_length_raw=_integer(entry, "audioLen"),
                external_id=_string(entry, "extId"),
            )
        )
    return AudioInventory(files=tuple(records))


async def async_read_native_inventory(api, channel: int, *, timeout: float = 15.0) -> AudioInventory:
    """Read a fresh HTTP snapshot through the existing authenticated Host.

    Calling ``send`` directly avoids updating the shared native playback cache.
    This performs no playback, upload, authentication or lifecycle operations.
    """
    from reolink_aio.exceptions import ReolinkError

    if type(channel) is not int or channel < 0:
        raise StoredAudioError("Native audio inventory channel must be a nonnegative integer")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise StoredAudioError("Native audio inventory timeout must be positive and finite")
    try:
        async with asyncio.timeout(timeout):
            response = await api.send([{"cmd": COMMAND, "action": 0, "param": {"channel": channel}}])
    except (ReolinkError, TimeoutError, OSError) as error:
        code = getattr(error, "rspCode", None)
        status = f", device rspCode={code}" if type(code) is int else ""
        raise StoredAudioError(f"Native audio inventory failed ({type(error).__name__}{status})") from None
    return parse_native_audio_inventory(response)
