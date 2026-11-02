# Exact WiFi-W firmware: stored-audio findings

Protocol research from the earlier MIT Talk feature work. Current validation
results are documented in [TEST_RESULTS.md](TEST_RESULTS.md).

Investigated 2026-10-30 and resumed 2026-10-31 (Australia/Sydney).
This is read-only static analysis of an official downloaded firmware image.
No firmware was flashed, no camera filesystem was accessed, and no import,
save, delete or playback command was sent to the physical camera for this work.

These findings materially extend the earlier firmware command catalogue. They
establish executable handler behaviour for the selected firmware, but do not
prove a complete integration upload succeeds on hardware. A short, separately
approved persistence/playback test is still required before claiming support.

## Provenance and reproduction

The exact installed build is Video Doorbell WiFi-W,
hardware `DB_566128M5MP_W_W`, version `v3.0.0.6460_2605211912`.
The [firmware archive index](https://github.com/AT0myks/reolink-fw-archive)
links this build to an [official Reolink CDN download](https://home-cdn.reolink.us/wp-content/uploads/2026/06/08112134a78ff9f74b81e365.zip?download_name=Reolink_Video_Doorbell_WiFi_W6460_2605211912.zip).
The current [official WiFi doorbell support page](https://support.reolink.com/c/video-doorbell-wifi/)
lists the same hardware variant; this investigation does not recommend an update.

Archive SHA-256:
`a2fed3a376df267bb1207046cf6961d5d8713e542d35720b47c3a8b6999bd4ff`.

The archive contains
`DB_566128M5MP_W_W.6460_2605211912.Reolink-Video-Doorbell-WiFi-W.OV05A10.5MP.WIFI8812.REOLINK.pak`.
Its local extraction was performed with [reolinkfw](https://github.com/AT0myks/reolink-fw)
2.0.0, with `ubi-reader==0.8.9` because a newer installed dependency had removed
an extraction helper expected by that tool. The GPL extraction tool was used
separately; no extraction-tool or vendor implementation code was incorporated
into the MIT integration.

The relevant executables are stripped ARM32 little-endian ELF images under the
extracted `rootfs/mnt/app/`. Evidence uses ELF virtual addresses, not file offsets.
`llvm-objdump`, ELF segment mapping and literal-pool/string cross references were
used. No extracted executable was run as a process; isolated codec routines were
later emulated offline as described in STORED_AUDIO_FORMAT.md.

| Artifact | SHA-256 |
| --- | --- |
| `netserver` | `5fccdb0b4e9378cf9407735ca6761ad5a6476b2a3aad6da9a9514304cb0c44c0` |
| `device` | `b46009ec0bc5ca73c9a038d075e217c1e0b1d95acae0f29cbe8a1104f729d964` |
| `libnetpublic.so` | `551b6f1860661cb0b41628ffdb0016d7ac24960c288b1901392b8f33c2d41d59` |

Vendor binaries and disassembly are private research inputs, not integration
source or distributable test fixtures.

## Command and parameter handlers

`netserver`'s command records associate the names, IDs and callbacks below.
The parameter table at virtual address `0x138b24` has 178 records of 52 bytes;
the record for `audioFileInfo` (ID `0x1b3b`) points to processor `0x94a58`.
The binary parameter ID is `0x1451` (5201), processor `0x8b33c`.

| Operation | Handler / relevant addresses |
| --- | --- |
| 260: legacy persistent alarm info (not staging) | `0x9a958` |
| 261: import command acknowledgement | `0x96368` |
| 261: begin metadata processing | `0x94b98` within `0x94a58` |
| 261: raw binary processing | `0x8b33c` dispatches to `0x8a770` |
| 262: commit/save | `0x963c4`, with metadata branch `0x9517c` |
| 347: custom inventory | `0x9ac88` |
| Device: begin custom audio | `0x5c270` |
| Device: byte append | `0x5c39c` |
| Device: final file placement | `0x5c3f0` |
| Device: cancel temporary import | `0x5c548` |
| Device: save RPC / allocate reply slot | `0x62d34`, `0x632c0` |

## Metadata differs from the older catalogue

The exact `libnetpublic.so` XML parser symbol
`nets_param_audio_file_info_x2s` starts at `0x38420`. It recognizes these fields:

| XML field | Internal offset / relevant constraint |
| --- | --- |
| `channelId` | `0x00` |
| `fileSize` | `0x04`; when supplied it must be greater than zero |
| `audioLen` | `0x08` |
| `fileName` | `0x0c`; 128-byte buffer |
| `id` | `0x8c` |
| `timeout` | `0x90` |
| `type` | `0x94`; 32-byte buffer |
| `extId` | `0xb4`; 64-byte buffer |
| `fileFormat` | `0xf4`; `adpcm` → 0, `aac` → 1, `pcm` → 2; unknown value rejects |

In particular, this build expects **`fileName`**, rather than assuming the older
catalogue's `customName` request field. The parser reads optional fields; parser
optionality does not imply every transaction field can be omitted safely.

For commit, the netserver branch at `0x951b0` copies the staging handle, name,
external identifier and file format into the device save RPC. It maps `type`:

| String | Device category |
| --- | --- |
| `reply` | 1: custom Quick Reply |
| `default` | 2: standalone default audio |
| `festival` | 3: standalone festival audio |
| `doorbell` | 4: button audio |
| Missing/other | 0: legacy custom alarm path |

An upload implementation must explicitly set `type=reply`. Allowing the missing
type to fall through to category zero targets a different persistent sound.
Device-side validation at `0x62d4c` requires a reply name of 1–127 bytes. Enforce
UTF-8 byte length before encoding XML, not merely Python character count.

The commit path does not use the request `id` to select a replacement slot.
Do not promise arbitrary ID replacement from this command's metadata parser.

## Session, import and commit sequence

The following describes independently reconstructed firmware behaviour. Native
client-side evidence is documented separately and should be checked against it.

1. Import metadata processing rejects an already active transfer. It requests a
   new device-side temporary recording using `fileSize` and `fileFormat`.
2. The device returns an internal handle. Netserver records the owning session,
   handle, expected byte count, received byte count zero, and an active flag.
   The handle is internal to the firmware; it is not an allocated recording ID.
3. Binary data remains command 261. The generic dispatcher places bytes after a
   binary-marked extension into parameter 5201. Processor `0x8b33c` routes those
   bytes to `0x8a770`; no live-talk command or BcMedia packetization is involved.
4. Binary processing requires an active import owned by the same session. It
   appends the bytes through the device write RPC and adds their count to the
   received total. At total >= expected size, it marks transfer complete and
   clears the active flag. The client must send **exactly** the announced size;
   the comparison itself is not a safe overrun check.
5. Command 262 checks transfer-complete and same-owner conditions before commit.
   It processes metadata and sends the save RPC, then clears all transfer state.
   Returning status 200 alone is not proof that a reply slot was saved.

Keep the same authenticated connection/session throughout begin, chunks and
commit. Reconnection in the middle is not a supported continuation mechanism.
The binary RPC buffer has room for 4500 audio bytes (`0x119c` total structure:
handle, 4500 data bytes, and length). Import's copy does not visibly clamp the
length at that site. Therefore do not treat the library's much larger generic
message guard as a valid audio chunk size.

Independent native SDK analysis reports **2048-byte** file chunks, same command
261/message ID, an acknowledgement after every chunk, and no XML recording body
on the chunk packets. Follow that smaller observed client boundary rather than
probing the firmware buffer limit.

## Recording ownership and device-assigned slots

The device save path scans its 15-entry metadata table (stride 208), selecting an
enabled entry with an empty recording name. It takes that entry's existing ID;
the caller does not calculate the ID. Existing nonempty entries are skipped.
The reply file-placement routine then requires an ID from **3 through 14**.
IDs 0–2 are the existing stock replies in the observed HTTP inventory; the
firmware's native allocator and commit range provide stronger ownership evidence
than the names alone.

Command 347's serializer (`0x9ae90` onward) excludes the stock part of the table
and reports `maxFileNumber=12`. Thus the observed empty command 347 inventory is
consistent with three HTTP Quick Replies and no custom reply recordings. It is
not evidence of an unsupported command or an empty stock inventory.

These IDs and ranges are **specific to this analysed firmware**. Generic models
must not hardcode them as universal camera capacity. Prefer device allocation
and a post-commit read, preserving the actual returned IDs.

When no free reply entry exists, the save path can remove the temporary file
and still return success from the placement helper. Consequently an ACK-only
implementation can claim a false success. Use a fresh unique `extId` (up to
63 bytes), requery custom inventory, find exactly one matching new record, and
verify every old record is unchanged. Return the device's actual new ID.
Then reconnect and verify the same record again. No automatic overwrite or
destructive cleanup is justified by this allocator.

## File size, length and raw audio

Device begin routine `0x5c270` checks a positive `fileSize` no greater than
**184320 bytes**, and an audio-length argument no greater than **10**. It opens
the temporary recording as an appendable raw file. However, the Baichuan begin
route forwards only size/format and passes zero for that length argument; it
does not itself establish an accurate ten-second duration from the data.

The duration helper at `0x5aa90` uses integer `fileSize/8000` for ADPCM and
`fileSize/32000` for PCM; AAC uses ADTS parsing (with a fallback estimate).
This establishes **whole seconds, truncated**, for ADPCM `audioLen` on this build.
For example the stock byte sizes 30720, 32640 and 38720 correspond to 3, 4 and
4 seconds, matching the physical HTTP inventory. Do not apply this unit rule
unconditionally to unanalysed firmware.

The stored audio files have no WAV header; the raw bytes are appended as supplied.
Separate analysis of the native encoder and firmware decoder establishes
continuous IMA ADPCM, high nibble first, initially zero predictor/index, with no
per-block talk header. See the audio-format evidence for bit-exact verification.
Do not reuse live-talk DVI block framing merely because both codecs are ADPCM.

The preparation layer should independently enforce the vendor's ten-second
recording limit and verified encoded-size bound. Capability `maxCapacity=128`
still has no established relationship to these limits and must not substitute
for either limit.

## Interrupted uploads and cleanup

Netserver session removal at `0x7d53c` checks whether the departing session owns
an **active** import. It sends `MSG_AUDIO_CUSTOM_CANCEL` with the staging handle
and clears owner/flags/expected/received state. The device cancel routine at
`0x5c548` flushes and closes that handle and removes its temporary import file.
It does not delete an existing committed reply.

A fully received but unsaved transfer has complete=1 and active=0. The periodic
server handler at `0x805c8` checks whether its owning session still exists. If it
does not, it sends the same temporary-import cancellation. Success callback
`0x70f34` clears the owner and transfer flags. The timing and failure cases of
this periodic cleanup have not been measured on hardware.

Implications for implementation:

- Do not retry a chunk after an uncertain acknowledgement: duplicate bytes are
  appended, not placed at an idempotent file offset.
- Do not retry save after an uncertain acknowledgement: it may already have
  allocated/persisted a recording. Query inventory and correlate `extId` first.
- Cancellation/recovery must not close or log out HA's shared authenticated Host.
  Use an owned session if recovery requires closing an upload connection, while
  reusing the library's authentication rather than duplicating it.
- A device may be busy because another app owns an import. Report that condition;
  do not cancel somebody else's recording or use deletion as rollback.
- After an interrupted transfer, document uncertain device state until a fresh
  inventory and the normal firmware cleanup establish what remains.

## Staging and process-crash limitation

Command 260 reads the legacy persistent `/mnt/para/custom_alarm.adpcm`; its
fileSize=0 response cannot establish import-idle or staging-empty state.
Netserver rejects an active import (`0x94b98`, active flag at 0x168), and a
completed transfer belonging to another session (`0x94bf4`, complete at 0x16c).
The same owner can cancel its old handle before a new begin. Device begin rejects
a non-null global FILE pointer at `0x5c2d8` before opening in append mode at
`0x5c30c`/`0x5c314`. Thus ordinary delayed cleanup should reject a new transfer
as busy, rather than silently append to an open prior transfer.

However, no create-time truncate/unlink or startup cleanup was found. Staging
`/mnt/tmp/custom_alarm.adpcm` lives on a 12 MiB tmpfs, cleared by a full reboot.
A process-only crash may leave an orphan file after its FILE pointer is lost;
a later begin could append. Postcommit size verification would detect a mismatch,
but a bad new recording might already exist. This recovery case is unproven;
do not probe it on the user's device or claim disconnect cleanup covers it.

## Remaining validation gates

The static analysis preceded hardware writes. The separately approved 2026-10-31
22:49 test subsequently passed native upload, UUID/name/size/type verification
after reconnect and preservation of all prior parsed records. See
`UPLOAD_RESULT_2026-10-31.md`. Audible native playback, broader metadata charset
handling and recovery fault cases remain untested. Power-loss recovery between file placement and metadata update
is not established. Explicit overwrite/replacement remains unsupported.

This exact-firmware analysis is a basis for an offline implementation and a
reviewable first test, not permission to perform the test or to generalize
upload support to all Reolink cameras.
