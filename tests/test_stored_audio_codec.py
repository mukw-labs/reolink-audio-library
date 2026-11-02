"""Stored codec vectors verified offline against the exact firmware routine.

Only synthetic PCM/vector hashes are committed. No test requires firmware,
Unicorn, device access or vendor recordings; no audible playback occurs.
"""

from __future__ import annotations

import hashlib
import math
import struct
from unittest.mock import Mock

import pytest

from custom_components.reolink_audio_library import stored_audio_codec
from custom_components.reolink_audio_library.stored_audio_codec import encode_stored_ima_adpcm


def _pcm(samples: list[int]) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


@pytest.mark.parametrize(
    ("samples", "expected"),
    [
        ([0] * 16, "0000000000000000"),
        ([0, 0, 1000, -1000, 30000, -30000, 12345, -12345, 0, 0, 0, 0, 0, 0, 0, 0], "007f7f7f28080808"),
        ([-32768, 32767] * 16, "f7f7f7f7f7f6f7f7f7f7f7f7f7f7f7f7"),
        (list(range(-32768, 32768, 2048)), "fffffffff91111121232333434333434"),
    ],
    ids=["silence", "steps", "clipping", "ramp"],
)
def test_matches_synthetic_vectors_from_emulated_firmware_encoder(samples, expected):
    # Firmware device VA 0xf8204, predictor/index 0; exact provenance in docs.
    payload = encode_stored_ima_adpcm(_pcm(samples), max_output_bytes=len(bytes.fromhex(expected)))
    assert payload == bytes.fromhex(expected)


def test_full_second_matches_native_encoder_without_block_state_resets():
    samples = [int(20000 * math.sin(2 * math.pi * 500 * n / 16000)) for n in range(16000)]
    payload = encode_stored_ima_adpcm(_pcm(samples), max_output_bytes=8000)
    assert len(payload) == 8000
    assert hashlib.sha256(payload).hexdigest() == "830086021d09f059bebe55c5019504f7a8139dec681aab9ed8e95162075e3307"
    # A new file begins with zero state; it cannot be used as a file-chunk API.
    split = encode_stored_ima_adpcm(_pcm(samples[:1024]), max_output_bytes=512)
    split += encode_stored_ima_adpcm(_pcm(samples[1024:]), max_output_bytes=7488)
    assert split != payload


def test_each_file_starts_with_fresh_predictor_and_index():
    pcm = _pcm([1000, -1000, 32000, -32000] * 32)
    expected = encode_stored_ima_adpcm(pcm, max_output_bytes=64)
    encode_stored_ima_adpcm(_pcm([-32000, 32000] * 128), max_output_bytes=128)
    assert encode_stored_ima_adpcm(pcm, max_output_bytes=64) == expected


def test_odd_sample_count_is_padded_with_one_zero_pcm_sample():
    pcm = _pcm([32767, -32768, 1234])
    padded = _pcm([32767, -32768, 1234, 0])
    assert encode_stored_ima_adpcm(pcm, max_output_bytes=2) == encode_stored_ima_adpcm(padded, max_output_bytes=2)
    assert len(encode_stored_ima_adpcm(pcm, max_output_bytes=2)) == 2


def test_one_sample_is_encoded_rather_than_dropped_as_predictor_header():
    assert encode_stored_ima_adpcm(_pcm([0]), max_output_bytes=1) == b"\x00"
    assert encode_stored_ima_adpcm(_pcm([1000]), max_output_bytes=1) == b"\x7a"


@pytest.mark.parametrize("pcm", [b"", b"\x00", b"\x00\x00\x00"])
def test_rejects_missing_or_incomplete_pcm_samples(pcm):
    with pytest.raises(ValueError, match="sample"):
        encode_stored_ima_adpcm(pcm, max_output_bytes=100)


@pytest.mark.parametrize("pcm", [None, "audio", bytearray(b"\x00\x00"), memoryview(b"\x00\x00")])
def test_rejects_nonbyte_pcm(pcm):
    with pytest.raises(TypeError, match="PCM audio must be bytes"):
        encode_stored_ima_adpcm(pcm, max_output_bytes=100)


@pytest.mark.parametrize("limit", [True, False, 1.0, "100", None])
def test_byte_limit_must_be_an_actual_integer(limit):
    with pytest.raises(TypeError, match="limit must be an integer"):
        encode_stored_ima_adpcm(_pcm([0, 0]), max_output_bytes=limit)


@pytest.mark.parametrize("limit", [0, -1])
def test_byte_limit_must_be_positive(limit):
    with pytest.raises(ValueError, match="limit must be positive"):
        encode_stored_ima_adpcm(_pcm([0, 0]), max_output_bytes=limit)


def test_rejects_oversize_before_quantizing_or_allocating_output(monkeypatch):
    quantizer = Mock(side_effect=AssertionError("Oversize data must not be encoded"))
    monkeypatch.setattr(stored_audio_codec, "_ima_encode_nibble", quantizer)
    with pytest.raises(ValueError, match="device profile's byte limit"):
        encode_stored_ima_adpcm(_pcm([0, 0, 0]), max_output_bytes=1)
    quantizer.assert_not_called()


def test_exact_payload_limit_accepts_without_extra_headers():
    assert encode_stored_ima_adpcm(_pcm([0] * 20), max_output_bytes=10) == bytes(10)


def test_decoder_high_first_continuous_state_and_wav_header():
    import io
    import wave
    from custom_components.reolink_audio_library.stored_audio_codec import stored_ima_to_wav

    # Independent standard IMA nibble vector: 7, F, 7, F. No block predictor.
    wav = stored_ima_to_wav(bytes.fromhex("7f7f"), sample_rate=16000, max_input_bytes=2)
    with wave.open(io.BytesIO(wav)) as stream:
        assert (stream.getnchannels(), stream.getsampwidth(), stream.getframerate(), stream.getnframes()) == (
            1,
            2,
            16000,
            4,
        )
        assert struct.unpack("<4h", stream.readframes(4)) == (11, -19, 44, -92)


def test_decoder_synthetic_tone_frequency_and_expected_wave_hash():
    from custom_components.reolink_audio_library.stored_audio_codec import stored_ima_to_wav

    pcm = _pcm(
        [int(2000 * min(1.0, n / 320, (7999 - n) / 320) * math.sin(2 * math.pi * 440 * n / 16000)) for n in range(8000)]
    )
    audio = encode_stored_ima_adpcm(pcm, max_output_bytes=4000)
    wav = stored_ima_to_wav(audio, sample_rate=16000, max_input_bytes=4000)
    samples = struct.unpack("<8000h", wav[44:])
    assert len(wav) == 16044
    assert sum(a <= 0 < b for a, b in zip(samples, samples[1:])) == 220
    assert 1990 < max(samples) < 2050
    assert hashlib.sha256(wav).hexdigest() == "2951430f7852a5f595989762b18a207c84e0888175eaa7e8c2ae9833df1b6b1c"


@pytest.mark.parametrize(
    "audio,rate,limit",
    [
        (b"", 16000, 1),
        ("bad", 16000, 3),
        (b"a", 8000, 1),
        (b"ab", 16000, 1),
        (b"a", 16000, 184321),
        (b"a", True, 1),
        (b"a", 16000, True),
    ],
)
def test_decoder_rejects_unverified_formats_and_bounds(audio, rate, limit):
    with pytest.raises(ValueError):
        stored_audio_codec.stored_ima_to_wav(audio, sample_rate=rate, max_input_bytes=limit)
