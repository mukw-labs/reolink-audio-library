# Historical physical storage result

The earlier feature work completed one explicitly approved create-only upload
on the supported WiFi-W firmware on 2026-10-31. A half-second synthetic 440 Hz
tone was encoded to 4,000 raw ADPCM bytes. Import/save succeeded on one pinned
TCP session, and a fresh session verified the new slot, name, size, type and
unique external identifier. All three factory files and the pre-existing user
recording retained their parsed metadata. No existing recording was overwritten.

The user manually tested the saved recording and reported it working, then
explicitly prohibited further actual-doorbell playback. This is user-reported
audible/native success, not an agent-observed playback test. Do not run or request
another camera playback as part of source cleanup or integration migration.

The synthetic ADPCM later exported read-only with command 410 matched the original
upload exactly, SHA-256
`ca277c2514f1af11d316bb3f556136bda20cc5f9f7ee5343cf3c8ea42f8c7e2a`.
The independent WAV and factory export evidence are in AUDIO_EXPORT_FEASIBILITY.md.

This result proves that specifically tested protocol/model/firmware combination.
It does not establish generic Reolink support, replacement/deletion, power-loss
recovery or a new physical write through Audio Library's standalone device action.
The latter is exercised with fakes in real HA harnesses. Private receipts, device
identifiers, personal recording names and backup locations are deliberately not
part of this source repository or release archive.
