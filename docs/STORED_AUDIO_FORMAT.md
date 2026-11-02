# Native stored-audio format: exact WiFi-W firmware

Protocol research from the earlier MIT Talk feature work. Current validation
results are documented in [TEST_RESULTS.md](TEST_RESULTS.md).

Analysis completed 2026-10-31 (Australia/Sydney). These observations apply to
Video Doorbell WiFi-W, hardware `DB_566128M5MP_W_W`, firmware
`v3.0.0.6460_2605211912`. They are static/offline codec evidence, not a successful
camera import, persistent storage or audible playback test.

## Source and licence boundary

The exact vendor firmware was downloaded from the public
[Reolink CDN archive](https://home-cdn.reolink.us/wp-content/uploads/2026/06/08112134a78ff9f74b81e365.zip?download_name=Reolink_Video_Doorbell_WiFi_W6460_2605211912.zip)
and extracted locally without connecting to or flashing a camera. The firmware
is a proprietary research input. No firmware code, complete recordings or
decompiled implementation is copied into this MIT repository.

The extracted `device` binary has SHA-256
`b46009ec0bc5ca73c9a038d075e217c1e0b1d95acae0f29cbe8a1104f729d964`.
Addresses below are ELF virtual addresses in that binary, not camera-memory
accesses. The integration implementation reuses its existing MIT standard IMA
quantizer and independently implements the established byte layout.

## Confirmed byte layout

The stored default recordings are **raw, continuous IMA/DVI ADPCM**, with these
properties on the inspected playback path:

- Mono, 16,000 samples/second, signed 16-bit PCM before quantization, as reported
  by the physical device's native HTTP inventory.
- Two 4-bit coded samples per stored byte: **first sample in the high nibble**.
- Predictor starts at 0 and IMA step index starts at 0 for each opened file.
- Predictor/index continue across file reads. There are no per-chunk state resets.
- No RIFF/WAV container, BcMedia packet header, or embedded predictor/index header.
- Every source sample is quantized; the first PCM sample is not removed to form
  a predictor header.

Evidence: standard 89-entry IMA step table at `0x3209b4`, adjacent IMA index table
at `0x320b18`; raw decoder at `0xf8330`; encoder at `0xf8204`. The decoder consumes
the high nibble before the low nibble. File opening at `0x5f2e4` clears predictor
and index at `0x5f388`–`0x5f394`. The raw file reader at `0x5a580` reads file bytes
and passes them directly to this decoder, preserving state across reads.

The existing Talk encoder emits predictor-bearing blocks and packs its sample
nibbles low-first. Its *quantization math* is reusable; its block layout is not.
`stored_audio_codec.encode_stored_ima_adpcm` provides the separate stored layout.

## Default resource cross-check

The extracted stock-resource sizes exactly match the physical HTTP
`GetAudioFileList` response. Only hashes and metadata are recorded here.

| Resource | Bytes | SHA-256 | Samples after decode | Duration at 16 kHz | Device audioLen |
| --- | ---: | --- | ---: | ---: | ---: |
| `default_audio_0` | 30720 | `ab7858ac42837a187d3e0ef19d0558dc43c2c1d0f0140cdda776af050650c0f9` | 61440 | 3.84 s | 3 |
| `default_audio_1` | 32640 | `ca5bf74c92fb85a8de9827f4f82f681fd8adea7af56c5766bf87901bdc15b842` | 65280 | 4.08 s | 4 |
| `default_audio_2` | 38720 | `f27feeaabfeea903e3550a6adaee75e17e965c9e42295e05decafea7c305474c` | 77440 | 4.84 s | 4 |

Duration helper `0x5aa90` uses integer `file_size / 8000` for the default raw
ADPCM case, consistent with the reported lengths above. This establishes whole
seconds with truncation for that firmware path. Other devices/profiles and client
provided duration metadata must not inherit this interpretation automatically.

Path formatter `0x5a510` accepts IDs 0–14, mapping 0–2 to the firmware stock
resources and 3–14 to the user-recording path. This explains the twelve user-slot
capacity. It is supporting static evidence; upload allocation must use the
complete verified firmware transaction and a fresh inventory, not just this
range calculation.

## Offline binary verification

The actual ARM decoder was run in a local Unicorn 2.1.4 emulator, with no device
access or audible playback. For all three stock resources, an independently
written standard IMA decoder with high-first nibbles matched the firmware's PCM
output **and final predictor/index state exactly**. Decoded audio was not retained.
The private research directory contains `audio-format-verify.py` and
`audio-format-results.json`; these require the locally extracted proprietary input
and are not CI dependencies.

Synthetic PCM vectors were also run through the firmware encoder at `0xf8204`.
The tests commit only tiny synthetic expected vectors and a synthetic waveform
hash. In particular, a 16,000-sample, amplitude-20,000, 500 Hz sine at 16 kHz yields
exactly 8,000 ADPCM bytes with SHA-256
`830086021d09f059bebe55c5019504f7a8139dec681aab9ed8e95162075e3307`.
CI runs entirely with the independent integration code and synthetic inputs.

## Encoder contract and limits

`encode_stored_ima_adpcm(pcm_s16le, max_output_bytes=profile_limit)` accepts
nonempty, complete mono 16-bit PCM samples. The external device profile supplies
the mandatory positive byte cap, checked before encoding or output allocation.
The caller validates the source sample rate, channel count and duration, and runs
the synchronous encoder in an executor to avoid blocking HA.

Output is `ceil(sample_count / 2)` bytes. If the number of PCM samples is odd,
the independent encoder quantizes one extra zero PCM sample to fill the final
low nibble. Duration validation must include that extra sample. This explicit
padding choice is covered by tests; it does not assert a native-client padding
convention.

This codec module performs no media resolution, network operation or camera
write. Establishing a stored format does not independently establish packet
framing, commit/rollback behaviour or safe replacement. Those remain separate
protocol and hardware-validation gates.
