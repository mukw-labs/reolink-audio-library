"""Exercise real bounded FFmpeg preparation without camera access or playback."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import io
import math
import os
from pathlib import Path
import shutil
import struct
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import wave

import pytest

from custom_components.reolink_audio_library import audio_preparation
from custom_components.reolink_audio_library.audio_preparation import (
    AudioPreparationError,
    PreparedAudio,
    async_prepare_audio_bytes,
    async_prepare_media,
)

FFMPEG = shutil.which("ffmpeg")
HAS_HA = importlib.util.find_spec("pytest_homeassistant_custom_component") is not None
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="FFmpeg is needed for real conversion tests")
needs_ha = pytest.mark.skipif(not HAS_HA, reason="Native media resolution requires the HA test harness")


def _wav(
    *, samples: int = 3200, sample_rate: int = 16000, channels: int = 1, frequency: int = 400, amplitude: int = 12000
) -> bytes:
    frames = [
        int(amplitude * math.sin(2 * math.pi * (frequency + 200 * channel) * index / sample_rate))
        for index in range(samples)
        for channel in range(channels)
    ]
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(struct.pack(f"<{len(frames)}h", *frames))
    return output.getvalue()


@pytest.fixture(scope="module")
def mp3_audio() -> bytes:
    if FFMPEG is None:
        pytest.skip("FFmpeg is needed to generate an MP3 fixture")
    # Synthetic input and pipe-only conversion; no fixture recording is retained.
    result = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-i", "pipe:0", "-c:a", "libmp3lame", "-f", "mp3", "pipe:1"],
        input=_wav(),
        capture_output=True,
        check=True,
    )
    return result.stdout


@needs_ffmpeg
async def test_wav_becomes_native_raw_adpcm_with_exact_duration_and_size():
    source = _wav()
    prepared = await async_prepare_audio_bytes(source, max_duration=1, max_payload_bytes=8000)
    assert prepared.duration == 0.2
    assert prepared.input_size == len(source)
    assert prepared.size == len(prepared.payload) == 1600
    assert prepared.sample_rate == 16000
    assert prepared.channels == 1
    assert prepared.precision == 16
    assert prepared.encoding == "ima_adpcm_raw_msb"


@needs_ffmpeg
async def test_pcm_preservation_and_native_format_match_firmware_verified_waveform():
    prepared = await async_prepare_audio_bytes(
        _wav(samples=16000, frequency=500, amplitude=20000), max_duration=1, max_payload_bytes=8000
    )
    # Same synthetic source passed through the actual firmware encoder offline.
    # This catches gain, sample-order, resampling and framing regressions, rather
    # than accepting any payload of the correct size.
    assert hashlib.sha256(prepared.payload).hexdigest() == (
        "830086021d09f059bebe55c5019504f7a8139dec681aab9ed8e95162075e3307"
    )


@needs_ffmpeg
async def test_stereo_other_sample_rate_is_downmixed_and_resampled():
    prepared = await async_prepare_audio_bytes(
        _wav(samples=4410, sample_rate=44100, channels=2), max_duration=1, max_payload_bytes=8000
    )
    assert prepared.channels == 1
    assert prepared.sample_rate == 16000
    assert prepared.duration == 0.1
    assert prepared.size == 800


@needs_ffmpeg
async def test_mp3_is_decoded_instead_of_requiring_a_wav_container(mp3_audio):
    prepared = await async_prepare_audio_bytes(mp3_audio, max_duration=1, max_payload_bytes=8000)
    # MP3 over a nonseekable pipe can include encoder delay/padding.
    assert 0.2 <= prepared.duration < 0.5
    assert prepared.input_size == len(mp3_audio)
    assert prepared.size == round(prepared.duration * 8000)


@needs_ffmpeg
async def test_exact_duration_limit_is_accepted():
    prepared = await async_prepare_audio_bytes(_wav(samples=1600), max_duration=0.1, max_payload_bytes=800)
    assert prepared.duration == 0.1
    assert prepared.size == 800


@needs_ffmpeg
async def test_one_sample_over_duration_limit_is_rejected_instead_of_trimmed():
    with pytest.raises(AudioPreparationError, match="duration limit"):
        await async_prepare_audio_bytes(_wav(samples=1601), max_duration=0.1, max_payload_bytes=800)


@needs_ffmpeg
async def test_odd_sample_padding_is_included_in_duration_and_payload():
    prepared = await async_prepare_audio_bytes(_wav(samples=1601), max_duration=1, max_payload_bytes=8000)
    assert prepared.duration == 1602 / 16000
    assert prepared.size == 801
    with pytest.raises(AudioPreparationError, match="padding sample"):
        await async_prepare_audio_bytes(_wav(samples=1601), max_duration=1601 / 16000, max_payload_bytes=8000)


@needs_ffmpeg
async def test_device_payload_cap_is_enforced_after_conversion():
    with pytest.raises(AudioPreparationError, match="payload limit"):
        await async_prepare_audio_bytes(_wav(), max_duration=1, max_payload_bytes=1599)


@needs_ffmpeg
async def test_invalid_audio_gets_safe_error_without_ffmpeg_stderr():
    marker = b"PRIVATE_CONTENT_OR_LOCATION"
    with pytest.raises(AudioPreparationError, match="could not decode") as caught:
        await async_prepare_audio_bytes(marker, max_duration=1, max_payload_bytes=8000)
    assert marker.decode() not in str(caught.value)


@needs_ffmpeg
async def test_playlist_cannot_read_another_file_through_ffmpeg(tmp_path):
    referenced = tmp_path / "PRIVATE_LOCAL_AUDIO.wav"
    referenced.write_bytes(_wav())
    playlist = f"ffconcat version 1.0\nfile '{referenced}'\n".encode()
    with pytest.raises(AudioPreparationError, match="could not decode") as caught:
        await async_prepare_audio_bytes(playlist, max_duration=1, max_payload_bytes=8000)
    assert str(referenced) not in str(caught.value)


@pytest.mark.parametrize("source", [b"", None, "audio"])
async def test_empty_or_nonbyte_input_is_rejected_before_process_creation(source, monkeypatch):
    start = AsyncMock()
    monkeypatch.setattr(audio_preparation.asyncio, "create_subprocess_exec", start)
    with pytest.raises(AudioPreparationError, match="nonempty"):
        await async_prepare_audio_bytes(source, max_duration=1, max_payload_bytes=8000)
    start.assert_not_awaited()


async def test_input_resource_guard_is_checked_before_process_creation(monkeypatch):
    monkeypatch.setattr(audio_preparation, "MAX_INPUT_BYTES", 1024)
    start = AsyncMock()
    monkeypatch.setattr(audio_preparation.asyncio, "create_subprocess_exec", start)
    with pytest.raises(AudioPreparationError, match="resource limit"):
        await async_prepare_audio_bytes(bytes(1025), max_duration=1, max_payload_bytes=8000)
    start.assert_not_awaited()


@pytest.mark.parametrize("duration", [0, -1, math.inf, math.nan, True, "10"])
async def test_invalid_duration_profile_is_rejected_before_process_creation(duration, monkeypatch):
    start = AsyncMock()
    monkeypatch.setattr(audio_preparation.asyncio, "create_subprocess_exec", start)
    with pytest.raises(AudioPreparationError, match="[Dd]uration"):
        await async_prepare_audio_bytes(_wav(), max_duration=duration, max_payload_bytes=8000)
    start.assert_not_awaited()


@pytest.mark.parametrize("size", [0, -1, True, 8000.0])
async def test_invalid_payload_profile_is_rejected(size):
    with pytest.raises(AudioPreparationError, match="payload limit"):
        await async_prepare_audio_bytes(_wav(), max_duration=1, max_payload_bytes=size)


@pytest.mark.parametrize("rate", [8000, 44100, True])
async def test_unresearched_sample_rate_is_rejected(rate):
    with pytest.raises(AudioPreparationError, match="16000 Hz"):
        await async_prepare_audio_bytes(_wav(), max_duration=1, max_payload_bytes=8000, sample_rate=rate)


async def test_missing_ffmpeg_has_actionable_error(monkeypatch):
    monkeypatch.setattr(audio_preparation.shutil, "which", lambda _name: None)
    with pytest.raises(AudioPreparationError, match="FFmpeg.*not found"):
        await async_prepare_audio_bytes(_wav(), max_duration=1, max_payload_bytes=8000)


@needs_ffmpeg
async def test_cpu_encoding_does_not_run_on_the_event_loop_thread(monkeypatch):
    loop_thread = threading.get_ident()
    encoder_threads = []
    original = audio_preparation.encode_stored_ima_adpcm

    def encode(*args, **kwargs):
        encoder_threads.append(threading.get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(audio_preparation, "encode_stored_ima_adpcm", encode)
    await async_prepare_audio_bytes(_wav(), max_duration=1, max_payload_bytes=8000)
    assert encoder_threads and all(value != loop_thread for value in encoder_threads)


def _hung_converter(tmp_path: Path) -> tuple[Path, Path]:
    pid_file = tmp_path / "converter.pid"
    executable = tmp_path / "ffmpeg"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import os,time\n"
        "from pathlib import Path\n"
        f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "while True: time.sleep(1)\n"
    )
    executable.chmod(0o755)
    return executable, pid_file


async def _wait_pid(pid_file: Path) -> int:
    async with asyncio.timeout(5):
        while True:
            try:
                return int(pid_file.read_text())
            except (OSError, ValueError):
                pass
            await asyncio.sleep(0.01)


async def test_cancelling_conversion_kills_and_reaps_its_process(tmp_path, monkeypatch):
    executable, pid_file = _hung_converter(tmp_path)
    monkeypatch.setattr(audio_preparation.shutil, "which", lambda _name: str(executable))
    operation = asyncio.create_task(async_prepare_audio_bytes(_wav(), max_duration=1, max_payload_bytes=8000))
    pid = await _wait_pid(pid_file)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_conversion_timeout_kills_and_reaps_its_process(tmp_path, monkeypatch):
    executable, pid_file = _hung_converter(tmp_path)
    monkeypatch.setattr(audio_preparation.shutil, "which", lambda _name: str(executable))
    monkeypatch.setattr(audio_preparation, "TRANSCODE_TIMEOUT", 1.0)
    with pytest.raises(AudioPreparationError, match="time limit"):
        await async_prepare_audio_bytes(_wav(), max_duration=1, max_payload_bytes=8000)
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_prepared_record_does_not_display_audio_payload():
    prepared = PreparedAudio(payload=b"PRIVATE_AUDIO_BYTES", duration=0.1, input_size=100)
    assert "PRIVATE_AUDIO_BYTES" not in repr(prepared)
    assert "payload=" not in repr(prepared)
    assert prepared.size == 19


def test_unavailable_unrelated_media_root_does_not_block_a_valid_file(tmp_path):
    local = tmp_path / "local"
    local.mkdir()
    media = local / "recording.wav"
    media.write_bytes(b"test-media")
    assert audio_preparation._read_media_file(media, (tmp_path / "offline-storage", local)) == b"test-media"


@needs_ha
@needs_ffmpeg
async def test_media_resolves_through_ha_and_reads_the_local_file(hass, tmp_path):
    local = tmp_path / "local"
    local.mkdir()
    media = local / "recording.wav"
    media.write_bytes(_wav())
    hass.config.media_dirs = {"local": str(local)}
    identifier = "media-source://media_source/local/recording.wav"
    with patch(
        "homeassistant.components.media_source.async_resolve_media", AsyncMock(return_value=SimpleNamespace(path=media))
    ) as resolve:
        prepared = await async_prepare_media(
            hass,
            identifier,
            max_duration=1,
            max_payload_bytes=8000,
        )
    resolve.assert_awaited_once_with(hass, identifier, target_media_player=None)
    assert prepared.duration == 0.2
    assert prepared.size == 1600


@needs_ha
@pytest.mark.parametrize("selection", ["https://example.invalid/private.wav?token=SECRET", "/tmp/recording.wav"])
async def test_media_requires_ha_identifier_before_resolution(hass, selection):
    with patch("homeassistant.components.media_source.async_resolve_media", AsyncMock()) as resolve:
        with pytest.raises(AudioPreparationError, match="media-source identifier"):
            await async_prepare_media(
                hass,
                selection,
                max_duration=1,
                max_payload_bytes=8000,
            )
    resolve.assert_not_awaited()


@needs_ha
async def test_resolved_url_is_not_fetched_when_no_local_path_exists(hass):
    media = SimpleNamespace(path=None, url="https://example.invalid/private.wav?token=SECRET")
    with patch("homeassistant.components.media_source.async_resolve_media", AsyncMock(return_value=media)):
        with pytest.raises(AudioPreparationError, match="local media library") as caught:
            await async_prepare_media(
                hass,
                "media-source://provider/recording.wav",
                max_duration=1,
                max_payload_bytes=8000,
            )
    assert "SECRET" not in str(caught.value)


@needs_ha
@pytest.mark.parametrize("case", ["outside", "symlink", "directory", "missing", "empty"])
async def test_local_media_cannot_escape_roots_or_read_invalid_files(hass, tmp_path, case):
    local = tmp_path / "local"
    local.mkdir()
    outside = tmp_path / "PRIVATE_OUTSIDE.wav"
    outside.write_bytes(_wav())
    selected = local / "recording.wav"
    if case == "outside":
        selected = outside
    elif case == "symlink":
        selected.symlink_to(outside)
    elif case == "directory":
        selected.mkdir()
    elif case == "empty":
        selected.touch()
    hass.config.media_dirs = {"local": str(local)}
    with patch(
        "homeassistant.components.media_source.async_resolve_media",
        AsyncMock(return_value=SimpleNamespace(path=selected)),
    ):
        with pytest.raises(AudioPreparationError) as caught:
            await async_prepare_media(
                hass,
                "media-source://media_source/local/recording.wav",
                max_duration=1,
                max_payload_bytes=8000,
            )
    assert str(selected) not in str(caught.value)


@needs_ha
async def test_ha_resolver_exception_does_not_expose_signed_url(hass):
    with patch(
        "homeassistant.components.media_source.async_resolve_media",
        AsyncMock(side_effect=RuntimeError("https://example.invalid/private.wav?token=SECRET")),
    ):
        with pytest.raises(AudioPreparationError, match="could not resolve") as caught:
            await async_prepare_media(
                hass,
                "media-source://provider/recording.wav",
                max_duration=1,
                max_payload_bytes=8000,
            )
    assert "SECRET" not in str(caught.value)


@needs_ha
async def test_local_file_resource_cap_is_enforced_on_actual_read(hass, tmp_path, monkeypatch):
    local = tmp_path / "local"
    local.mkdir()
    selected = local / "recording.wav"
    selected.write_bytes(bytes(1025))
    hass.config.media_dirs = {"local": str(local)}
    monkeypatch.setattr(audio_preparation, "MAX_INPUT_BYTES", 1024)
    with patch(
        "homeassistant.components.media_source.async_resolve_media",
        AsyncMock(return_value=SimpleNamespace(path=selected)),
    ):
        with pytest.raises(AudioPreparationError, match="resource limit"):
            await async_prepare_media(
                hass,
                "media-source://media_source/local/recording.wav",
                max_duration=1,
                max_payload_bytes=8000,
            )
