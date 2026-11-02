"""Encode/decode the headerless IMA stream used by a verified stored-audio profile.

This is not the live-talk DVI block layout. The inspected WiFi-W firmware reads
the first sample from the high nibble, starts predictor/index at zero and keeps
that state for the entire file. See docs/STORED_AUDIO_FORMAT.md.

The caller must select a verified device profile, validate its sample format and
duration limits, and run this synchronous CPU work outside HA's event loop.
"""

from __future__ import annotations

import struct

from .ima_adpcm import _ima_encode_nibble


def encode_stored_ima_adpcm(pcm_s16le: bytes, *, max_output_bytes: int) -> bytes:
    """Return continuous high-nibble-first IMA ADPCM without file/block headers.

    Input is mono signed 16-bit little-endian PCM at the verified profile's
    sample rate. No sample is consumed as a predictor header. An odd number of
    samples is padded with one zero PCM sample so each output byte is complete;
    callers must include this extra sample in their duration validation.

    The output cap comes from the caller's device profile and is checked before
    allocating or encoding. Authentication, packet framing and device writes are
    outside this codec's scope.
    """
    if not isinstance(pcm_s16le, bytes):
        raise TypeError("PCM audio must be bytes")
    if not pcm_s16le:
        raise ValueError("PCM audio must contain at least one sample")
    if len(pcm_s16le) % 2:
        raise ValueError("PCM audio must contain complete 16-bit samples")
    if type(max_output_bytes) is not int:
        raise TypeError("The stored-audio byte limit must be an integer")
    if max_output_bytes < 1:
        raise ValueError("The stored-audio byte limit must be positive")

    output_size = (len(pcm_s16le) + 3) // 4
    if output_size > max_output_bytes:
        raise ValueError("Stored audio exceeds the device profile's byte limit")

    output = bytearray(output_size)
    predictor = step_index = 0
    for index, offset in enumerate(range(0, len(pcm_s16le), 4)):
        first = struct.unpack_from("<h", pcm_s16le, offset)[0]
        second = struct.unpack_from("<h", pcm_s16le, offset + 2)[0] if offset + 2 < len(pcm_s16le) else 0
        high, predictor, step_index = _ima_encode_nibble(first, predictor, step_index)
        low, predictor, step_index = _ima_encode_nibble(second, predictor, step_index)
        output[index] = (high << 4) | low
    return bytes(output)


def stored_ima_to_wav(audio: bytes, *, sample_rate: int, max_input_bytes: int) -> bytes:
    """Decode a complete verified high-first raw IMA file to mono PCM16 WAV.

    Predictor/index start at zero and remain continuous across the entire file.
    Run outside HA's event loop. Size validation precedes output allocation.
    """
    from .ima_adpcm import _IMA_INDEX_TABLE, _IMA_STEP_TABLE

    if not isinstance(audio, bytes) or not audio:
        raise ValueError("Stored audio must contain bytes")
    if type(max_input_bytes) is not int or not 1 <= len(audio) <= max_input_bytes <= 184_320:
        raise ValueError("Stored audio exceeds the verified byte limit")
    if type(sample_rate) is not int or sample_rate != 16_000:
        raise ValueError("Unsupported stored audio sample rate")
    pcm = bytearray(len(audio) * 4)
    predictor = index = offset = 0
    for value in audio:
        for code in (value >> 4, value & 15):
            step = _IMA_STEP_TABLE[index]
            delta = (
                (step >> 3)
                + (step if code & 4 else 0)
                + (step >> 1 if code & 2 else 0)
                + (step >> 2 if code & 1 else 0)
            )
            predictor = max(-32768, min(32767, predictor + (-delta if code & 8 else delta)))
            index = max(0, min(88, index + _IMA_INDEX_TABLE[code]))
            struct.pack_into("<h", pcm, offset, predictor)
            offset += 2
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(pcm),
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        sample_rate,
        sample_rate * 2,
        2,
        16,
        b"data",
        len(pcm),
    )
    return header + pcm
