# Rename and delete validation

## Implemented behavior

`rename_audio` and `delete_audio` are administrator actions with native camera and
saved-recording selectors. Selection binds camera identity, custom slot ID,
metadata and UUID. Wrong-camera, factory, stale or reused selections fail before a
write. Delete requires explicit confirmation and rejects a recording configured
as the automatic reply, even when that reply is disabled.

Rename sends one metadata entry through Baichuan **368**, preserving the existing
UUID, or omitting `extId` when the original legacy UUID is empty. Names must be
nonblank, valid XML and at most 127 UTF-8 bytes. Duplicate names are rejected;
renaming to the current name is a no-op.

Delete sends Baichuan **348** containing one custom ID and channel, without a
guessed path. Automatic-reply selection is read through command **427** and
compared after deletion and reconnect. No camera setting is changed.

The mutation uses one pinned connection and strict matching 200 ACK checks. It
is never retried. Both native/custom inventories must establish the exact
intended change, unchanged identity/capacity and preservation of every unselected
record, including on an independent Host/transport. A missing ACK can only be
reconciled from that fresh state. Uncertain outcomes require inventory inspection.

Old preview tokens are revoked across aliases of the same camera. Results and
sanitized failures appear on the status sensor; a successful write remains
successful if a later display refresh fails.

## Protocol evidence

The official Android SDK metadata context `0x900` sends command 368; context
`0x901` sends 348. The metadata serializer uses `audioFileInfoList version=1.1`
with a single `audioFileInfo` entry; delete uses `audioFileInfo version=1.1`
with `channelId` and `id`. No proprietary implementation is included.

Exact-firmware analysis and private offline Unicorn execution with synthetic
memory verified custom name updates, UUID/other-record preservation, partial-list
semantics and factory/out-of-range rejection. Filesystem and persistence calls
were intercepted; no camera write occurred during emulation. The firmware can
acknowledge deletion without establishing successful file removal, which is why
fresh catalog verification is required. See the [Android](ANDROID_UPLOAD_EVIDENCE.md)
and [firmware](FIRMWARE_AUDIO_UPLOAD.md) provenance documents.

## Physical test on 2026-11-02

Target: Reolink Video Doorbell WiFi-W, hardware `DB_566128M5MP_W_W`, firmware
`v3.0.0.6460_2605211912`, standalone channel 0. Runtime: HA 2026.9.0,
Python 3.14.6, reolink_aio 0.21.14.

The operator approved one new temporary silent recording. A separate one-shot
process used the production 0.2.0 backend, without installing the integration,
playing audio, retrying mutations or modifying pre-existing recordings.

| Step | Result |
| --- | --- |
| Create | 800 stored bytes / 0.1 seconds of digital silence; unique UUID, newly allocated slot; acknowledged and verified after reconnect |
| Rename | Command 368 acknowledged; new name verified after reconnect, UUID and all other metadata preserved |
| Audio | Read-only exports before and after rename both exactly matched the original 800 bytes |
| Delete | Command 348 acknowledged; only the newly created recording removed, verified after reconnect |
| Preservation | Full native/custom inventories, identity and capacity matched the baseline; both pre-existing custom recordings remained |
| Further audit | Independent read-only session confirmed the temporary ID/UUID were absent and prior automatic-reply settings were unchanged |
| Playback | None |

Silent audio SHA-256:
`67042dfda5683aead81b6055d19c4dba238341f9dd82f49c0e7cc0c19c5f10d1`.
The tested pre-branding ZIP SHA-256 was
`900863e99c576eee1da76d07a29fbd680eab73f9e7421d57245bb0c4a25226aa`.
The public release has the same Python implementation with added PNG branding
and corrected HTTP dependency / polling-class manifest metadata;
its separate checksum is supplied with the release. Private receipts, device
identifiers, personal recording names and backup locations are excluded.

## Limits

The firmware mutates by ID without compare-and-swap. Fresh metadata checks and
Audio Library's camera lock reduce stale-slot risk but cannot exclude simultaneous
changes by the vendor app or another integration. Avoid concurrent management.
An identical-metadata replacement with an empty legacy UUID cannot be distinguished
by a metadata fingerprint.

Deletion proves removal from the recording catalogs, not secure erasure of raw
storage or the absence of orphan files after a firmware filesystem failure.
Persistence was verified across independent sessions, without power cycling the
camera. No automatic recovery write, undelete or replacement is implemented.
Only the exact supported profile is enabled. HA permissions, selectors and action
responses are covered separately by the real HA software harnesses.
