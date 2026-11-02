# Standalone recording integration

`reolink_audio_library` registers `list_audio`, `upload_audio`, `rename_audio` and `delete_audio` actions.
They select an existing official Reolink device directly. Mutations are admin-only. Upload is create-only; rename/delete bind a saved
recording fingerprint through the native media picker, with explicit delete confirmation. They are registered in
async_setup, remain available while unloaded, and then return a clear validation
error. No fake media player is needed to target a camera.

Configuration selects existing official Reolink entries, with no credentials
copied into this integration's config entry. Only standalone channel 0 is
currently validated. Device targeting checks registry identifiers, official
entry membership, configured selection, loaded state and the validated profile.
Selecting an unrelated/NVR child device or unloaded/unsupported camera fails
before media access or upload. Empty selections never mean every camera.

Owned Host sessions use the existing official credentials without closing or
reconfiguring its shared API. Automatic port discovery/opening is disabled.
Mutations pin one TCP session and never retry uncertain append/save operations.
Pre/post inventory preservation and fresh-session storage verification remain
unchanged from the previously physically tested transaction. Preview uses its
own read-only session with command 410 and exact-size/format/EOF validation.
All operations within Audio Library serialize by physical camera identity,
including aliased official entries; third-party integrations have separate locks.

## Modules

- audio_library.py: immutable catalog, coordinator, physical-camera locks and
  transient upload status/receipt/error data. Native/custom inventories are
  cross-validated on owned sessions. Capacity refreshes every five minutes.
- sensor.py: used/free slots and upload status on the existing official device.
- audio_services.py: device validation, local media preparation and upload/list
  actions, with metadata-bound rename/delete. No entity-service registration or native playback path exists.
- audio_preparation.py: bounded local media/FFmpeg conversion; no camera IO.
- stored_audio_codec.py / ima_adpcm.py: stored raw IMA encoding/decoding only.
  The codec has no live-talk imports or network/audio-output transport.
- audio_upload.py / audio_upload_transport.py: verified create-only transaction,
  capacity/profile guards, strict 200 ACKs and independent reconnect verification.
- audio_export.py: command-410 download on an owned pinned TCP session, bounded
  early buffering, metadata/data 200 and empty EOF 201. No shared callback patch.
- media_source.py / audio_preview.py: Audio Library browse tree, metadata/UUID
  fingerprints, private WAV conversion/cache and authenticated/signed delivery.
- stored_audio.py / native_audio.py: bounded parsers and read operations only.

Preview URLs use `/api/reolink_audio_library/recording/` with opaque tokens.
Memory cache is bounded to 16 entries / 8 MiB and ten-minute fixed lifetime,
cleared on owning-entry unload/HA stop and changed metadata. HEAD and single
byte ranges are supported. CPU work runs outside HA's event loop.

No media_player.py, talk.py, live DVI block encoder, TalkConfig, TalkReset,
QuickReplyPlay, TTS handler, volume control, automatic-reply selector, legacy
cleanup or vendor binaries are shipped. A compatibility guard rejects a Talk
player as preview output; it does not import, mutate or call that integration.
Namespaced services, sensor IDs, data keys and API paths permit coexistence.

## Recording management (0.2.0)

recording_identity.py shares camera keys and fingerprints without importing HA.
audio_edit_transport.py sends exact SDK-derived 368/348 packets using the existing
pinned strict-ACK sender. audio_edit.py validates the current catalogs, guards
factory/automatic-reply records, sends once and verifies on a distinct Host.
Preview invalidation groups aliases by their shared operation lock, not just an
official config-entry ID. See RENAME_DELETE_VALIDATION.md for evidence and limits.
