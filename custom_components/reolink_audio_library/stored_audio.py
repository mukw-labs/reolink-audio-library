"""Stored-audio discovery, independent of live talk and Home Assistant.

Only established read commands are sent here. Upload metadata serializers are
not evidence of a working import/commit transaction; see docs/AUDIO_PROTOCOL.md.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree as ET

CMD_LIST_AUDIO = 347
CMD_AUDIO_ENCODE_ABILITY = 267
# Integration resource guard, not a camera file-size limit.
MAX_XML_BYTES = 1024 * 1024


class StoredAudioError(Exception):
    """A safe, user-facing error without credentials or response payloads."""


class AudioProtocolError(StoredAudioError):
    """An unrecognized or inconsistent read response."""


@dataclass(frozen=True)
class AudioRecord:
    """Actual inventory metadata; unknown duration units/ownership stay unknown."""

    id: int
    name: str
    type: str | None = None
    size: int | None = None
    audio_length_raw: int | None = None
    external_id: str | None = None
    duration: float | None = None
    is_builtin: bool | None = None


@dataclass(frozen=True)
class AudioInventory:
    """A successful list is distinct from confirmed arbitrary-upload support."""

    files: tuple[AudioRecord, ...]
    max_files: int | None = None


def _xml_root(xml: str) -> ET.Element:
    if not isinstance(xml, str) or len(xml.encode("utf-8")) > MAX_XML_BYTES:
        raise AudioProtocolError("Audio metadata response is invalid or exceeds the integration XML size limit")
    if "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
        raise AudioProtocolError("Audio metadata must not contain XML document types or entities")
    try:
        return ET.fromstring(xml)
    except ET.ParseError:
        raise AudioProtocolError("Camera returned malformed audio metadata XML") from None


def _number(element: ET.Element, field: str, *, required: bool = False) -> int | None:
    value = element.findtext(field)
    if value is None:
        if required:
            raise AudioProtocolError(f"Audio metadata is missing {field}")
        return None
    try:
        number = int(value)
    except ValueError:
        raise AudioProtocolError(f"Audio metadata {field} must be an integer") from None
    if number < 0:
        raise AudioProtocolError(f"Audio metadata {field} must not be negative")
    return number


def parse_audio_inventory(xml: str) -> AudioInventory:
    """Parse modern and firmware-described legacy lists; never allocate IDs.

    `audioFileInfo` entries are established by reolink_aio. Legacy `item` and
    capacity fields are firmware-derived, not claimed as hardware-tested.
    """
    root = _xml_root(xml)
    containers = list(root.iter("audioFileInfoList"))
    if len(containers) > 1:
        raise AudioProtocolError("Camera returned multiple audio lists for one channel")
    container = containers[0] if containers else root
    entries = list(container.iter("audioFileInfo"))
    if containers:
        entries.extend(container.findall("item"))
    if not containers and not entries:
        raise AudioProtocolError("Camera response does not contain a recognized audio inventory")
    records = []
    ids: set[int] = set()
    for entry in entries:
        file_id = _number(entry, "id", required=True)
        assert file_id is not None
        if file_id in ids:
            raise AudioProtocolError("Camera returned duplicate audio recording IDs")
        ids.add(file_id)
        name = entry.findtext("fileName")
        if name is None:
            name = entry.findtext("customName")
        if name is None:
            raise AudioProtocolError("Audio recording is missing its name")
        records.append(
            AudioRecord(
                id=file_id,
                name=name,
                type=entry.findtext("type"),
                size=_number(entry, "fileSize"),
                audio_length_raw=_number(entry, "audioLen"),
                external_id=entry.findtext("extId"),
            )
        )
    return AudioInventory(files=tuple(records), max_files=_number(container, "maxFileNumber"))


def parse_audio_capabilities(xml: str) -> dict[str, Any]:
    """Preserve capability fields without treating them as an import spec."""
    root = _xml_root(xml)
    abilities = list(root.iter("AudioEncodeAbility"))
    if len(abilities) != 1:
        raise AudioProtocolError("Camera response does not contain one AudioEncodeAbility")
    ability = abilities[0]
    formats = []
    for encoder in ability.findall("audioEncodeList/audioEncode"):
        formats.append(
            {
                "audio_type": encoder.findtext("audioType"),
                "sample_rate": _number(encoder, "sampleRate"),
                "sample_precision": _number(encoder, "samplePrecision"),
                "length_per_encoder": _number(encoder, "lengthPerEncoder"),
                "sound_track": encoder.findtext("soundTrack"),
            }
        )
    return {
        "status": "reported",
        "max_capacity_raw": _number(ability, "maxCapacity"),
        "max_capacity_unit": None,
        "formats": formats,
        "verified_for_upload": False,
    }


def _safe_failure(operation: str, error: Exception) -> StoredAudioError:
    """Keep actual numeric device status while omitting arbitrary exception text."""
    code = getattr(error, "rspCode", None)
    status = f", device rspCode={code}" if isinstance(code, int) else ""
    return StoredAudioError(f"{operation} failed ({type(error).__name__}{status})")


async def async_read_inventory(api, channel: int, *, timeout: float = 15.0) -> AudioInventory:
    """Use the existing authenticated Baichuan session and its reconnect logic."""
    from reolink_aio.exceptions import NotSupportedError, ReolinkError

    try:
        async with asyncio.timeout(timeout):
            xml = await api.baichuan.send(cmd_id=CMD_LIST_AUDIO, channel=channel)
    except NotSupportedError:
        raise StoredAudioError("This camera does not support stored-audio discovery (command 347)") from None
    except (ReolinkError, TimeoutError, OSError) as error:
        raise _safe_failure("Stored-audio discovery (command 347)", error) from None
    return parse_audio_inventory(xml)
