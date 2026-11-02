# Read-only export evidence

The earlier feature work verified command 410 on the exact supported WiFi-W
firmware on 2026-11-01. This document records that evidence; the standalone
Audio Library 0.1.0 package has not been physically installed or write-tested.

The official Android SDK's remoteExportAudioFile/downloadAudioFile path sends
command 410 with audioFileInfo/channelId/id. The matching firmware looks up that
ID and opens the existing recording in read mode. Normal factory/custom replies
omit type; the vendor's type=doorbell path is a separate button-ringtone mode.
This protocol is distinct from import 261, save 262 and native playback 349.

Observed responses use 24-byte modern headers, class 0000, and preserve the full
request correlation. Metadata status 200 announces fileSize/fileFormat=adpcm.
Binary-marked Extension packets carry raw ADPCM in chunks up to 38,000 bytes,
status 200. An empty status 201 packet ends the file. The import/save path must
not adopt that EOF status as a successful ACK.

| Physical read | Result |
| --- | --- |
| Synthetic custom recording | 4,000 bytes, exactly matching the original uploaded synthetic bytes |
| Factory recording | 38,000 + 720 bytes, exactly matching the corresponding firmware resource |

Synthetic ADPCM SHA-256:
`ca277c2514f1af11d316bb3f556136bda20cc5f9f7ee5343cf3c8ea42f8c7e2a`.
Factory ADPCM SHA-256:
`f27feeaabfeea903e3550a6adaee75e17e965c9e42295e05decafea7c305474c`.
The native/custom inventories were compared before and after and remained
unchanged. No user's voice recording was downloaded for this export proof.

Independent standard IMA decoding of the synthetic file produces mono PCM16
WAV, 16 kHz, 8,000 samples / 0.5 seconds / 16,044 bytes. Its 220 positive zero
crossings match the known 440 Hz tone. WAV SHA-256:
`2951430f7852a5f595989762b18a207c84e0888175eaa7e8c2ae9833df1b6b1c`.
The later combined Talk feature build's real HA media source delivered this same
WAV through authenticated GET/HEAD/ranges. That is historical delivery evidence,
not a standalone Audio Library deployment claim. No output was played by the agent.

Library releases 0.21.14, 0.21.15 and 0.21.18 do not aggregate command 410 through
their generic payload sender. Audio Library therefore adapts only its own pinned
TCP connection, buffers bounded early chunks, validates metadata/size/codec/EOF,
rejects unexpected encryption and restores the callback on success/error/cancel.
There are no shared-Host callback changes, global patches or download retries.

Current source, URL/cache lifecycle and software tests are documented in
[architecture](ARCHITECTURE.md) and [tests](TEST_RESULTS.md). Raw evidence,
firmware/application inputs and disassembly remain private outside this repository.
