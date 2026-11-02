# Android client upload evidence

Protocol research from the earlier MIT Talk feature work. Current validation
results are documented in [TEST_RESULTS.md](TEST_RESULTS.md).

Research updated 2026-10-31 (Australia/Sydney). This records independently
described protocol facts from static analysis of the official Android client.
No proprietary source, disassembly, SDK binary or audio recording is included
in the integration. No camera mutation, playback or application execution was
performed for this analysis.

This advances the original command-catalog research: the vendor client actually
implements a two-step import sender, a separate metadata commit, and a different
ADPCM layout from this integration's live-talk stream. It is still not a
successful hardware upload test on the selected WiFi-W camera.

## Provenance and reproducibility

The [official download page](https://reolink.com/software-and-manual/) links the
Android app distributed from Reolink's own CDN:

`https://home-cdn.reolink.us/wp-content/assets/app/android/reolink.apk`

The downloaded APK's manifest reports version `4.63.0.3.20261023`. The CDN's
Last-Modified header was 2026-10-23. The artifact is 396,136,466 bytes.

| Artifact | SHA-256 |
| --- | --- |
| Official APK | `95013498bc170898b2199644a4d04395b3980ed41b2abfb886dd5100a3ca66b4` |
| APK member `lib/arm64-v8a/libBCSDKWrapper.so` | `e0088744cdabaa847fe9052cd100fc72f4ad891d962c73e47a5f99ed97a3080b` |

These are research artifacts, not integration dependencies. The relevant ELF is
AArch64, contains dynamic symbols, and was inspected using `readelf`,
`llvm-objdump`, strings, ELF relocations and read-only data tables. The virtual
addresses below refer only to this exact hashed binary. Most unsymbolized
callbacks are misleadingly labelled by disassemblers with the nearest unrelated
C++ symbol; use their addresses and relocation/caller links, not that label.

Proprietary research inputs and disassembly are retained privately and are not
distributed with the source or release.

## Relevant paths in the real vendor implementation

| Address | Symbol or independently identified role |
| --- | --- |
| `0x8f2d2c` | `JsonAPI_Request_ImportAudioFile`: local JSON API wrapper |
| `0x3ef898` | `BCSDKChannelConfigurator::remoteImportRingtone`: queues import context `0x8a7` |
| `0x46de8c` | `BaichuanDevice::__importRingtone`: sets up a two-step import session |
| `0x481c20` | Import request callback, identified through the function object's relocated vtable |
| `0x4827b8` | Import response callback, identified through the paired relocated vtable |
| `0x5d3148` | `ALG_AdpcmEncode`: continuous IMA encoder, high nibble first |
| `0x8f3308` | `JsonAPI_Request_SaveAudioFile`: local JSON API wrapper |
| `0x3efbbc` | `BCSDKChannelConfigurator::remoteSaveAudioFile`: queues commit context `0x904` |
| `0x49c684` | `BaichuanConfigurator::configWrite` case for context `0x904`; sends command 262 |
| `0x4e4948` | Commit XML callback, reached through the case's relocated function object |
| `0x4882a0` | `BCNetSession::response`: negative result fails; continue result repeats the current step |

The local JSON endpoint strings are `/json_api/audio/importAudioFile` and
`/json_api/audio/saveAudioFile`. They belong to the app/SDK wrapper and must not
be treated as HTTP endpoints implemented by the camera.

## Import transaction: command 261

The SDK import context creates a two-step `BCNetSession`. The first callback
invocation prepares/reads the local recording and sends metadata. The next step
sends binary chunks and repeats until all prepared bytes have been acknowledged.

### Initial metadata

The initial command is 261. The header constant at `0x2518c0` contains the normal
Baichuan magic and command 261. The callback constructs a channel extension and
a `body/audioFileInfo` element with version `1.1`, containing:

- `channelId` when the channel is nonnegative;
- `fileSize`, equal to the prepared encoded byte length;
- `fileFormat` for recognized explicit format modes (`adpcm`, `aac`, or `pcm`).

No binary audio is attached to this first request. The payload offset separates
the extension from the command XML body. Authentication/encryption and header
assembly should continue to use the existing reolink_aio sender, rather than
copying SDK implementation.

The local import wrapper accepts source/output file paths and format metadata.
These are local SDK inputs: do not send `sourceFileName` or `adpcmFile` paths to
the camera. The actual wire body is the metadata above.

### Binary continuation

The callback's second step constructs `Extension` version `1.1` with
`binaryData=1` and `channelId` when present. It reads **at most 2048 audio bytes**
per request, using `fread` at `0x481fe8`; a shorter final read is sent at its
actual byte length. Each request remains **command 261**, uses the **same
import-session message identifier**, and appends the raw encoded bytes after
the extension. No offset/sequence number, WAV header or live-talk `BcMedia`
packet is added inside the binary audio by this callback.

The header body length counts extension plus binary bytes; the payload offset
counts the extension. This is a stateful sequence, not a one-shot metadata call
followed by an unrelated stream of command 202 packets. The SDK's 2048-byte
continuations also remain below the exact camera firmware's separately
discovered fixed binary-buffer boundary. Do not use the generic transport's
whole-message limit as the allowable audio chunk length.

### Acknowledgement and completion

The response callback requires a response at least 24 bytes long and checks the
header's status at offset 16 for **200**. It rejects other statuses. After a
successful chunk response it continues while the sent-byte counter is below
`fileSize`; completion is not inferred from socket transmission alone.

The sending callback advances its byte counter by the actual chunk length. The
response handler returns the session's continue result while more data remains;
`BCNetSession::response` repeats the same step on that result. It advances to
the next step on a completed response and fails the session on a negative one.

This establishes client sequencing. It does not establish safe automatic retry
after an ambiguous timeout: the camera may already have accepted a chunk. A
future implementation must abort the transaction on uncertain acknowledgement
and avoid replaying the chunk automatically.

## Stored audio encoding

The SDK's default/ADPCM import path reads raw signed PCM16 input in 2048-byte
batches. It initializes predictor and step index to zero once and preserves
them across batches. `ALG_AdpcmEncode` uses the conventional IMA step/index
tables, predictor clipping and index range 0–88.

Each input sample becomes a four-bit nibble. The **first sample occupies the
high nibble**, the second the low nibble. The importer writes only the encoded
nibble bytes to its intermediate `adpcmFile`, then sends that file's bytes.
Although the callback constructs an eight-byte temporary block header in memory,
the actual file-write source begins eight bytes later: the constructed header
is **not written**. There is no RIFF/WAV container, no DVI predictor/index block
header and no command-202 `BcMedia` framing in this stored representation.

This differs materially from the existing live-talk encoder, which has per-block
predictor state and low-nibble-first packing. Reusing the talk encoder unchanged
would produce a different byte stream.

The SDK loop processes full PCM batches and drops an incomplete final PCM batch
in this path. That is an observed client implementation quirk, not a requirement
to truncate user audio. An independent implementation should validate its own
tail/padding policy against the exact firmware decoder and meaningful offline
fixtures. Do not silently lose the end of a recording.

The SDK's explicit format enum maps `1=adpcm`, `2=aac`, `3=pcm`; the wire strings
are those names. The selected firmware's internal enum differs, so protocol
XML must use the established strings, not client-side enum integers. Current SDK
AAC/PCM paths are not evidence that all formats should be exposed on the selected
camera. Its reported encoding capability and firmware implementation must gate
the production feature.

## Commit transaction: command 262

`remoteSaveAudioFile` queues a new command/request session (context `0x904`).
The `configWrite` jump table at `0x25a0f8` maps that context to case `0x49c684`;
that case calls the channel XML sender with command **262** and the callback at
`0x4e4948`. The callback builds `body/audioFileInfo` version `1.1` with:

| Field | Source-confirmed serialization |
| --- | --- |
| `type` | Usage string, including `reply` |
| `fileName` | Requested recording name |
| `channelId` | Included when nonnegative |
| `extId` | Optional nonempty external identifier |
| `fileFormat` | Optional recognized encoding string |
| `audioTags` | Optional modern SDK metadata; applicability to the target firmware is not established |

The callback does **not** serialize `id`, `fileSize`, `audioLen`, `customName`,
or a `save` boolean. Do not invent `save=true` to commit or `save=false` to cancel.
The selected firmware's independently inspected save parser also has no such
boolean. Its field name is `fileName`, contradicting older generic field
descriptions that alone had suggested `customName`.

The current SDK type table at `0x26bd70` is `alarm`, `reply`, `default`,
`festival`, `doorbell`. For this task only an explicitly requested **reply**
belongs in the eventual commit. Omitting it can select legacy alarm behaviour;
the existence of other type strings is not permission to change factory sounds.

Import and commit have different SDK request sessions, so command 262 can have
a new message identifier. They still use the **same authenticated camera
connection/session**. These two notions of session must not be confused: the
exact firmware separately verifies ownership of the imported buffer against
the camera-side session. Never reconnect and then attempt to commit a buffer
owned by the old connection.

The legacy `remoteSaveRingtone` method is a different context (`0x8a9`) that also
sends command 262 but without a command XML body. It is not a suitable substitute
for custom Quick Reply commit and must not be used as a generic cleanup command.

## What remains to prove

No explicit safe cancel/rollback command was established in these client paths.
No guaranteed assigned-recording-ID response was established either. After a
future approved commit, re-list actual device inventory, confirm exactly the
new ID/name, check previous records are unchanged, reconnect and list again.
Only then can physical persistence and separately approved audible native
playback demonstrate the final acceptance criteria.

Any eventual upload implementation should therefore:

1. Use the exact model/firmware evidence profile, not generic command names.
2. Prepare bounded native bytes with an independently written high-nibble-first
   continuous IMA encoder and confirmed sample-rate/channel requirements.
3. Reuse the established authenticated transport, reserve an import message ID,
   and serialize the entire transaction on that camera connection.
4. Send command 261 metadata, await success, send sequential chunks of at most
   2048 bytes with the same import identifier, and await every response.
5. Require the connection to remain the same; abort on timeout/disconnection or
   uncertain acknowledgement without automatic mutation retries.
6. Send command 262 with `type=reply` and `fileName` using a new request ID on
   the same authenticated connection, then verify storage by listing.
7. Keep replacement, deletion and factory sound changes unavailable until their
   independent protocol and recovery behaviour are established.

These are implementation requirements derived from static evidence, not a claim
that an unapproved device write has been executed successfully.

## Other primary sources checked

The [official Reolink CLI distribution](https://github.com/reolink/reolink-cli/tree/8efa6eb12ecff5e5bfb408d8ab45b8282f8c2e18)
explicitly states its binary source is unpublished. Its published audio command
surface includes replies and live talk, and its
[voice-alert reference](https://github.com/reolink/reolink-cli/blob/8efa6eb12ecff5e5bfb408d8ab45b8282f8c2e18/skills/reolink-cli/references/voice-alert.md)
describes command 201/202 streaming rather than persistent import. It does not
provide an alternative public upload implementation.

The native-Linux project's
[protocol research dossier](https://github.com/TodesengelX/reolink-native-linux/blob/main/docs/research/protocols.md)
is a useful overview but contains no concrete persistent import/save sequence.

A historical [primary user report](https://github.com/mnpg/Reolink_api_documentations/issues/1)
contains HTTP `GetAudioFileList` data distinguishing three existing `alarm`
entries and a self-recorded `reply` entry, with an ID range. That is evidence
from that user's firmware, not a universal allocation algorithm. Later
[reolink_aio discussion](https://github.com/starkillerOG/reolink_aio/issues/10#issuecomment-2044329242)
asked whether `SetAudioFileList` uploaded bytes; the
[follow-up](https://github.com/starkillerOG/reolink_aio/issues/10#issuecomment-2119202462)
only identified naming metadata and did not establish upload.
