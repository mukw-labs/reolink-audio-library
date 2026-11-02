"""Stable metadata-bound selection shared by browsing and recording actions."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re

from .const import DOMAIN
from .stored_audio import StoredAudioError

RECORDING_ID = re.compile(r"([0-9a-f]{24})/([0-9]|1[0-4])/([0-9a-f]{64})")


@dataclass(frozen=True)
class RecordingSelection:
    camera_key: str
    file_id: int
    fingerprint: str


def camera_key(library_entry_id: str, entry_id: str, channel: int) -> str:
    return hashlib.sha256(f"{library_entry_id}:{entry_id}:{channel}".encode()).hexdigest()[:24]


def recording_fingerprint(catalog, record) -> str:
    """Bind to ID/name/type/size/raw length and custom UUID, including empty UUIDs."""
    custom = next((item for item in catalog.custom.files if item.id == record.id), record)
    return hashlib.sha256(
        json.dumps(
            [record.id, record.name, record.type, record.size, record.audio_length_raw, custom.external_id],
            ensure_ascii=True,
        ).encode()
    ).hexdigest()


def recording_media_id(library_entry_id, library, catalog, record) -> str:
    return (
        f"media-source://{DOMAIN}/{camera_key(library_entry_id, library.entry_id, library.channel)}"
        f"/{record.id}/{recording_fingerprint(catalog, record)}"
    )


def parse_recording_selection(media_id: str) -> RecordingSelection:
    prefix = f"media-source://{DOMAIN}/"
    if not isinstance(media_id, str) or not media_id.startswith(prefix):
        raise StoredAudioError("Choose a saved recording from Reolink Audio Library")
    match = RECORDING_ID.fullmatch(media_id[len(prefix) :])
    if match is None:
        raise StoredAudioError("Invalid recording selection; browse the camera again")
    return RecordingSelection(match[1], int(match[2]), match[3])
