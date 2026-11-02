# Stored recording protocol and evidence

This standalone package retains the specifically verified stored-audio protocol
from the earlier MIT Talk feature work. No new camera write or playback was
performed for the source split. New device-action/setup boundaries are tested
with fake cameras; this is distinct from the historical physical evidence below.

## Enabled profile

Reolink Video Doorbell WiFi-W, hardware DB_566128M5MP_W_W, firmware
v3.0.0.6460_2605211912, standalone channel 0. Identity is checked against the
live device before writing or exporting. Other models/firmware fail closed.

| Operation | Native request | Boundary |
| --- | --- | --- |
| Device identity | Baichuan 80 | Live model/hardware/firmware validation |
| Custom catalog | Baichuan 347 | Capacity and user UUIDs, no slot guessing |
| Full native catalog | HTTP GetAudioFileList, action 0 | Factory and custom metadata |
| Encoding ability | Baichuan 267 | Additional format guard, not upload proof |
| Import | Baichuan 261 | Begin then 2,048-byte raw chunks, one pinned request ID |
| Save new reply | Baichuan 262 | New request ID, unique extId, no replacement |
| Rename custom recording | Baichuan 368 | One metadata entry, original UUID preserved |
| Delete custom recording | Baichuan 348 | Explicit confirmation and configured-reply protection |
| Export recording | Baichuan 410 | Existing ID, metadata/data 200, empty EOF 201 |

Factory IDs are 0–2; the verified custom range is 3–14 with capacity 12. The
camera allocates a free ID. Duplicate names/UUIDs, inconsistent catalogs, a full
camera or unsafe capacity metadata prevent uploading. Byte sizes and truncated
raw audioLen values are preserved; duration is derived from ADPCM size.

## Codec and mutation transport

The stored file is continuous headerless IMA ADPCM, high nibble first, mono
16 kHz, initially zero predictor/index, with no state reset between chunks.
The raw payload is bounded to 184,320 bytes and uploads to 10 seconds. This is
different from live Talk block framing, which is not part of Audio Library.
See [format evidence](STORED_AUDIO_FORMAT.md).

The owned TCP session remains pinned through begin/chunks/save. XML extension
and body are separately encrypted with library authentication; raw ADPCM is
appended to binary-marked extensions. Import/save acknowledgements require 200
and matching command/full correlation/header. No mutation is retried or resumed
after a reconnect. Owned-session cleanup closes staging on interruption; a
separate fresh session verifies the saved UUID/name/ID/size/type and every prior
record. A save ACK alone is not persistent-storage proof. See
[firmware facts](FIRMWARE_AUDIO_UPLOAD.md) and
[Android client facts](ANDROID_UPLOAD_EVIDENCE.md).

## Read-only export

Command 410 takes audioFileInfo version 1.1 with channelId and recording id.
Omit type for normal Quick Reply files. No destination path or playback command
is sent. Metadata reports fileSize/fileFormat=adpcm, status 200. Correlated
Extension/binaryData=1 packets contain at most 38,000 raw bytes each, status 200.
Empty status 201 terminates the file. Only export accepts this EOF; upload ACKs
remain strict 200. Unexpected payload encryption is rejected.

The library's generic send_payload does not aggregate command 410 in the tested
versions. The adapter changes a callback only on its exclusively owned connection,
restores it on all exits, bounds early data against inventory size and requires
valid metadata plus complete EOF. Real library TCP parsing can dispatch chunks
before the initial metadata send await resumes, so the checks are independent.

Before and after export, both native and custom inventories must agree with the
fresh catalog. Decode to PCM16 WAV off-loop. The global media source returns a
signed, authenticated HA URL for the chosen other output; it never calls native
camera playback. See [export evidence](AUDIO_EXPORT_FEASIBILITY.md).

## Provenance and proof

The earlier work examined the official Reolink Android application and matching
firmware as private research inputs. Only independently reconstructed protocol
facts and MIT implementations are retained; no proprietary binaries, voice
recordings, GPL/AGPL implementation or disassembly is shipped. The detailed
Android/firmware/format notes are historical research provenance, not installation
instructions or permission to repeat a hardware write.

The create-only transaction was physically stored, checked after reconnect and
manually confirmed by the user, preserving pre-existing recordings. Read-only
exports of a synthetic custom file and one factory file matched their known
source bytes; synthetic WAV decoding matched its expected hash. See
[physical result summary](UPLOAD_RESULT_2026-10-31.md). A later standalone-backend test also created, renamed and deleted one new silent
recording, preserving the original catalog and audio bytes; see
[management validation](RENAME_DELETE_VALIDATION.md). Replacement, power-loss
recovery and other models/codecs remain unsupported or unverified.

The transport is built on the already installed MIT
[reolink_aio library](https://github.com/starkillerOG/reolink_aio/tree/e3a5591d009a60fe36471bbf29ac7fa5bfe225c5).
HA integration boundaries follow its
[action guidance](https://contributors.home-assistant.io/core/integrations/actions/)
and [media-source platform](https://contributors.home-assistant.io/core/integrations/media-source/).
