"""Verified create-only stored-audio upload with fresh-session verification.

Mutations are single-attempt. The caller serializes by physical camera and owns
all sessions; closing an interrupted session lets firmware clean its staging.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
import math
from typing import Callable
from uuid import uuid4

from .audio_preparation import PreparedAudio
from .audio_upload_transport import AudioUploadError as AudioTransportError
from .audio_upload_transport import async_import_and_commit
from .native_audio import async_read_native_inventory
from .stored_audio import (
    CMD_AUDIO_ENCODE_ABILITY,
    AudioInventory,
    AudioRecord,
    StoredAudioError,
    async_read_inventory,
    parse_audio_capabilities,
)


@dataclass(frozen=True)
class UploadProfile:
    """A specifically validated model/hardware/firmware and its native limits."""

    model: str = "Reolink Video Doorbell WiFi-W"
    hardware: str = "DB_566128M5MP_W_W"
    firmware: str = "v3.0.0.6460_2605211912"
    sample_rate: int = 16000
    max_duration: float = 10.0
    max_payload_bytes: int = 184320
    custom_id_min: int = 3
    custom_id_max: int = 14


RESEARCHED_WIFI_W = UploadProfile()


class AudioUploadError(StoredAudioError):
    """An upload failure that retains a safe correlation ID for investigation."""

    def __init__(
        self,
        message: str,
        *,
        external_id: str | None = None,
        outcome_uncertain: bool = False,
        status: int | None = None,
    ):
        super().__init__(message)
        self.external_id = external_id
        self.outcome_uncertain = outcome_uncertain
        self.rspCode = status
        self.transport_failure: dict | None = None


@dataclass(frozen=True)
class UploadReceipt:
    id: int
    name: str
    duration: float
    size: int
    external_id: str
    commit_acknowledged: bool
    success: bool = True
    status: str = "stored_verified_after_reconnect"
    verified_after_reconnect: bool = True
    native_playback_verified: bool = False


def profile_for_device_info(info: dict, channel: int = 0) -> UploadProfile:
    profile = RESEARCHED_WIFI_W
    if type(channel) is not int or channel != 0:
        raise AudioUploadError("The researched upload profile supports a standalone camera on channel 0 only")
    if not isinstance(info, dict) or (
        info.get("type"),
        info.get("hardwareVersion"),
        info.get("firmwareVersion"),
    ) != (
        profile.model,
        profile.hardware,
        profile.firmware,
    ):
        raise AudioUploadError("No researched stored-audio upload profile matches this camera and firmware")
    return profile


def _validate_prepared(audio: PreparedAudio, profile: UploadProfile) -> None:
    if not isinstance(audio, PreparedAudio) or not isinstance(audio.payload, bytes) or not audio.payload:
        raise AudioUploadError("Prepare a nonempty native audio payload before importing")
    if any(type(value) is not int for value in (audio.sample_rate, audio.precision, audio.channels)) or (
        audio.sample_rate,
        audio.precision,
        audio.channels,
        audio.encoding,
    ) != (
        profile.sample_rate,
        16,
        1,
        "ima_adpcm_raw_msb",
    ):
        raise AudioUploadError("Prepared audio does not match the researched native format")
    duration = len(audio.payload) * 2 / profile.sample_rate
    if (
        isinstance(audio.duration, bool)
        or not isinstance(audio.duration, (int, float))
        or not math.isfinite(audio.duration)
        or not math.isclose(audio.duration, duration, rel_tol=0, abs_tol=1e-9)
    ):
        raise AudioUploadError("Prepared duration does not match the native payload sample count")
    if duration > profile.max_duration or len(audio.payload) > profile.max_payload_bytes:
        raise AudioUploadError("Prepared audio exceeds the researched duration or byte limit")


def _assert_preserved(before: AudioInventory, after: AudioInventory) -> None:
    actual = {record.id: record for record in after.files}
    if any(actual.get(record.id) != record for record in before.files):
        raise AudioUploadError("An existing recording changed; do not retry the upload or delete recordings")


def validate_inventory_consistency(custom: AudioInventory, native: AudioInventory, profile: UploadProfile) -> None:
    """Conflicting inventories are unsafe preflight evidence, not a free slot."""
    if any(
        not profile.custom_id_min <= item.id <= profile.custom_id_max or item.type != "reply" for item in custom.files
    ):
        raise AudioUploadError("The custom inventory does not match this firmware's researched ownership rules")
    if any(not 0 <= item.id <= profile.custom_id_max for item in native.files):
        raise AudioUploadError("Native recording IDs do not match the researched firmware profile")
    if not set(range(profile.custom_id_min)).issubset({item.id for item in native.files}):
        raise AudioUploadError("The native inventory is missing the researched firmware's stock recording baseline")
    if any(item.size is None or item.size <= 0 or item.audio_length_raw is None for item in native.files):
        raise AudioUploadError("Native inventory lacks the metadata needed to verify recording preservation")
    native_custom = {item.id: item for item in native.files if item.id >= profile.custom_id_min}
    if set(native_custom) != {item.id for item in custom.files}:
        raise AudioUploadError("Native and custom inventories disagree about occupied user slots; nothing was imported")
    for item in custom.files:
        native_item = native_custom[item.id]
        if (item.name, item.size, item.type) != (native_item.name, native_item.size, native_item.type):
            raise AudioUploadError("Native and custom recording metadata conflict; nothing was imported")
        if item.audio_length_raw != native_item.audio_length_raw or (
            native_item.external_id is not None and item.external_id != native_item.external_id
        ):
            raise AudioUploadError(
                "Native and custom recording duration/identity metadata conflict; nothing was imported"
            )


def _confirm_new(
    before: AudioInventory,
    after: AudioInventory,
    *,
    profile: UploadProfile,
    name: str,
    external_id: str,
    size: int,
) -> AudioRecord:
    _assert_preserved(before, after)
    matches = [record for record in after.files if record.external_id == external_id]
    if len(matches) != 1:
        raise AudioUploadError("The camera did not confirm exactly one recording with this upload's external ID")
    record = matches[0]
    if (
        record.id in {item.id for item in before.files}
        or not profile.custom_id_min <= record.id <= profile.custom_id_max
        or record.type != "reply"
        or record.name != name
        or record.size != size
        or len(after.files) != len(before.files) + 1
    ):
        raise AudioUploadError("Saved recording metadata does not confirm a new intact user recording")
    return record


async def _identity(api, channel: int) -> UploadProfile:
    # Established, read-only public API (command 80). It initializes the owned
    # Host's device identity without enabling ports or loading unrelated state.
    info = await api.baichuan.get_info()
    return profile_for_device_info(info, channel)


async def async_upload_prepared(
    connect: Callable[[], AbstractAsyncContextManager],
    channel: int,
    audio: PreparedAudio,
    name: str,
    *,
    overwrite: bool = False,
    file_id: int | None = None,
    device_write_authorized: bool = False,
) -> UploadReceipt:
    """Import once and verify actual storage through a fresh owned session.

    `connect` must create and always close an independent library-owned Host.
    Never pass the official HA integration's borrowed Host: closing an interrupted
    upload session is the proven staging-cleanup path. No mutation is retried.
    """
    if device_write_authorized is not True:
        raise AudioUploadError(
            "Camera writes require explicit operator approval or an explicit Home Assistant upload action"
        )
    if overwrite is not False or file_id is not None:
        raise AudioUploadError("Replacement is not established; only a new device-allocated recording is supported")
    if type(channel) is not int or channel != 0:
        raise AudioUploadError("The researched upload profile supports a standalone camera on channel 0 only")
    _validate_prepared(audio, RESEARCHED_WIFI_W)
    external_id = str(uuid4())
    commit_acknowledged = False
    transfer_error: Exception | None = None
    try:
        async with connect() as api:
            profile = await _identity(api, channel)
            before_native = await async_read_native_inventory(api, channel)
            before = await async_read_inventory(api, channel)
            if before.max_files is None or before.max_files <= 0:
                raise AudioUploadError("The camera did not report usable custom-audio capacity")
            if before.max_files > profile.custom_id_max - profile.custom_id_min + 1:
                raise AudioUploadError("Reported capacity exceeds the researched custom recording ID space")
            if len(before.files) >= before.max_files:
                raise AudioUploadError("The camera's reported custom-audio inventory is full; nothing was imported")
            if any(record.name == name for record in (*before.files, *before_native.files)):
                raise AudioUploadError("This recording name already exists; no recording was overwritten")
            validate_inventory_consistency(before, before_native, profile)
            xml = await api.baichuan.send(cmd_id=CMD_AUDIO_ENCODE_ABILITY, channel=channel)
            encoders = parse_audio_capabilities(xml)["formats"]
            if not any(
                (entry["audio_type"], entry["sample_rate"], entry["sample_precision"], entry["sound_track"])
                == ("adpcm", 16000, 16, "mono")
                for entry in encoders
            ):
                raise AudioUploadError("The camera's reported audio capability does not match the researched profile")
            original_api, original_bc = api, api.baichuan
            original_transport = getattr(getattr(original_bc, "_connection", None), "_transport", None)
            try:
                await async_import_and_commit(
                    api.baichuan,
                    channel,
                    audio.payload,
                    name,
                    external_id,
                    device_write_authorized=True,
                )
                commit_acknowledged = True
                first_saved = _confirm_new(
                    before,
                    await async_read_inventory(api, channel),
                    profile=profile,
                    name=name,
                    external_id=external_id,
                    size=audio.size,
                )
            except Exception as error:
                # Do not repeat append chunks or a possibly completed commit.
                # A fresh read can still establish an uncertain commit outcome.
                transfer_error = error
                first_saved = None
        # The old owned session has now closed, triggering cleanup of any unsaved
        # buffer. Never reconnect and continue its import/commit transaction.
        async with connect() as verification_api:
            if verification_api is original_api or verification_api.baichuan is original_bc:
                raise AudioUploadError("Persistence verification requires a new independent camera session")
            if await _identity(verification_api, channel) != profile:
                raise AudioUploadError("Camera identity changed during verification")
            current_transport = getattr(getattr(verification_api.baichuan, "_connection", None), "_transport", None)
            if original_transport is not None and current_transport is original_transport:
                raise AudioUploadError("Persistence verification reused the original camera connection")
            saved = _confirm_new(
                before,
                await async_read_inventory(verification_api, channel),
                profile=profile,
                name=name,
                external_id=external_id,
                size=audio.size,
            )
            if first_saved is not None and saved != first_saved:
                raise AudioUploadError("The saved recording changed after reconnecting")
            native_after = await async_read_native_inventory(verification_api, channel)
            _assert_preserved(before_native, native_after)
            native_matches = [item for item in native_after.files if item.id == saved.id]
            if len(native_after.files) != len(before_native.files) + 1 or len(native_matches) != 1:
                raise AudioUploadError("Native Quick Reply inventory does not confirm exactly one new recording")
            native_saved = native_matches[0]
            if (native_saved.name, native_saved.size, native_saved.type) != (name, audio.size, "reply"):
                raise AudioUploadError("Native Quick Reply metadata does not match the stored upload")
    except AudioUploadError as error:
        error.external_id = external_id
        error.outcome_uncertain = commit_acknowledged or transfer_error is not None
        if transfer_error is not None:
            code = getattr(transfer_error, "rspCode", None)
            numeric_code = code if type(code) is int else None
            status = f", device rspCode={numeric_code}" if numeric_code is not None else ""
            error.args = (f"{error}; original import/commit failure ({type(transfer_error).__name__}{status})",)
            error.rspCode = numeric_code
            if isinstance(transfer_error, AudioTransportError):
                error.transport_failure = {
                    "phase": transfer_error.phase,
                    "response_header": transfer_error.response_header,
                }
        raise
    except Exception as error:
        # Library exceptions can contain credentials or media URLs: never expose
        # their arbitrary text. Preserve only type and a numeric device status.
        code = getattr(error, "rspCode", None)
        status = f", device rspCode={code}" if isinstance(code, int) else ""
        failure = AudioUploadError(
            f"Upload/verification failed ({type(error).__name__}{status}); inspect inventory before retrying",
            external_id=external_id,
            outcome_uncertain=commit_acknowledged or transfer_error is not None,
            status=code if type(code) is int else None,
        )
        if isinstance(transfer_error, AudioTransportError):
            failure.transport_failure = {
                "phase": transfer_error.phase,
                "response_header": transfer_error.response_header,
            }
        raise failure from None
    return UploadReceipt(
        id=saved.id,
        name=saved.name,
        duration=audio.duration,
        size=audio.size,
        external_id=external_id,
        commit_acknowledged=commit_acknowledged,
    )
