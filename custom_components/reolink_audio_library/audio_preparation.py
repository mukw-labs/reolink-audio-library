"""Bounded, offline preparation for the firmware-established stored format.

This module never opens a camera connection. The HA upload action remains gated
separately until an approved physical persistence test establishes support.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import math
from pathlib import Path
import shutil
import stat

from .stored_audio import StoredAudioError
from .stored_audio_codec import encode_stored_ima_adpcm

# Integration resource guards, distinct from camera import limits.
MAX_INPUT_BYTES = 32 * 1024 * 1024
TRANSCODE_TIMEOUT = 30.0


class AudioPreparationError(StoredAudioError):
    """A safe validation/conversion error without media locations or content."""


@dataclass(frozen=True)
class PreparedAudio:
    payload: bytes = field(repr=False)
    duration: float
    input_size: int
    sample_rate: int = 16000
    encoding: str = "ima_adpcm_raw_msb"
    channels: int = 1
    precision: int = 16

    @property
    def size(self) -> int:
        return len(self.payload)


def _limits(max_duration: float, max_payload_bytes: int, sample_rate: int) -> int:
    if isinstance(max_duration, bool) or not isinstance(max_duration, (int, float)):
        raise AudioPreparationError("A positive finite duration limit is required")
    if not math.isfinite(max_duration) or not 0 < max_duration <= 60:
        raise AudioPreparationError("Duration limit is outside the integration preparation bounds")
    if isinstance(max_payload_bytes, bool) or not isinstance(max_payload_bytes, int) or max_payload_bytes <= 0:
        raise AudioPreparationError("A positive integer camera payload limit is required")
    if sample_rate != 16000 or isinstance(sample_rate, bool):
        raise AudioPreparationError("Only the researched 16000 Hz stored-audio profile is implemented")
    samples = math.floor(max_duration * sample_rate)
    if samples < 2:
        raise AudioPreparationError("The duration limit is too short to encode audio")
    return samples


async def _decode_pcm(input_bytes: bytes, *, max_samples: int, sample_rate: int) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise AudioPreparationError("FFmpeg is required for audio preparation but was not found")
    # Decode one sample beyond the limit so long audio is rejected, not silently
    # trimmed. Restrict input protocols: playlists must not open files or URLs.
    process = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-protocol_whitelist",
        "pipe",
        "-i",
        "pipe:0",
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-t",
        f"{(max_samples + 1) / sample_rate:.9f}",
        "-c:a",
        "pcm_s16le",
        "-f",
        "s16le",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        async with asyncio.timeout(TRANSCODE_TIMEOUT):
            pcm, _stderr = await process.communicate(input_bytes)
    except BaseException:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
        raise
    if process.returncode != 0:
        raise AudioPreparationError("FFmpeg could not decode a supported audio stream from the selected file")
    if not pcm or len(pcm) % 2:
        raise AudioPreparationError("The selected file contains no complete PCM audio samples")
    if len(pcm) // 2 > max_samples:
        raise AudioPreparationError("Audio exceeds the camera profile's duration limit; shorten the recording")
    return pcm


async def async_prepare_audio_bytes(
    input_bytes: bytes,
    *,
    max_duration: float,
    max_payload_bytes: int,
    sample_rate: int = 16000,
) -> PreparedAudio:
    """Transcode a bounded input into raw, continuous, high-nibble IMA ADPCM."""
    max_samples = _limits(max_duration, max_payload_bytes, sample_rate)
    if not isinstance(input_bytes, bytes) or not input_bytes:
        raise AudioPreparationError("Select a nonempty, readable audio file")
    if len(input_bytes) > MAX_INPUT_BYTES:
        raise AudioPreparationError("Input exceeds the integration's 32 MiB media resource limit")
    try:
        pcm = await _decode_pcm(input_bytes, max_samples=max_samples, sample_rate=sample_rate)
    except TimeoutError:
        raise AudioPreparationError("Audio conversion exceeded the integration's time limit") from None
    # Two samples per byte; the codec pads an odd final sample with one zero.
    encoded_samples = ((len(pcm) // 2 + 1) // 2) * 2
    if encoded_samples > max_samples:
        raise AudioPreparationError("Audio including its final padding sample exceeds the duration limit")
    try:
        payload = await asyncio.to_thread(encode_stored_ima_adpcm, pcm, max_output_bytes=max_payload_bytes)
    except ValueError:
        raise AudioPreparationError("Converted audio exceeds the camera profile's payload limit") from None
    return PreparedAudio(
        payload=payload,
        duration=encoded_samples / sample_rate,
        input_size=len(input_bytes),
        sample_rate=sample_rate,
    )


def _read_media_file(path: Path, media_dirs: tuple[Path, ...]) -> bytes:
    try:
        resolved = path.resolve(strict=True)
        roots = tuple(root.resolve(strict=False) for root in media_dirs)
        if not any(resolved.is_relative_to(root) for root in roots):
            raise AudioPreparationError("Resolved audio must be inside a configured HA media directory")
        mode = resolved.stat().st_mode
        if not stat.S_ISREG(mode):
            raise AudioPreparationError("Select a regular audio file from the media library")
        with resolved.open("rb") as source:
            data = source.read(MAX_INPUT_BYTES + 1)
    except (OSError, RuntimeError):
        raise AudioPreparationError("The selected local media file does not exist or cannot be read") from None
    if not data:
        raise AudioPreparationError("The selected local media file is empty")
    if len(data) > MAX_INPUT_BYTES:
        raise AudioPreparationError("Input exceeds the integration's 32 MiB media resource limit")
    return data


async def async_prepare_media(
    hass,
    media_content_id: str,
    *,
    max_duration: float,
    max_payload_bytes: int,
) -> PreparedAudio:
    """Use HA media resolution; never accept arbitrary filesystem paths/URLs."""
    from homeassistant.components.media_source import async_resolve_media

    _limits(max_duration, max_payload_bytes, 16000)
    if not isinstance(media_content_id, str) or not media_content_id.startswith("media-source://"):
        raise AudioPreparationError("Select a Home Assistant media-source identifier")
    try:
        resolved = await async_resolve_media(hass, media_content_id, target_media_player=None)
    except Exception:
        raise AudioPreparationError("Home Assistant could not resolve the selected media item") from None
    if resolved.path is None:
        raise AudioPreparationError("Upload preparation currently requires a file in HA's local media library")
    media_dirs = tuple(Path(path) for path in hass.config.media_dirs.values())
    data = await hass.async_add_executor_job(_read_media_file, Path(resolved.path), media_dirs)
    return await async_prepare_audio_bytes(data, max_duration=max_duration, max_payload_bytes=max_payload_bytes)
