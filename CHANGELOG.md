# Changelog

## 0.2.0 — 2026-11-02

- Add admin-only Rename custom recording and confirmed Delete custom recording
  actions using native device/saved-recording selectors.
- Bind selection to camera/metadata/UUID, preserve all other files and verify
  changes after reconnect. Protect factory and configured automatic-reply files.
- Reconcile uncertain ACKs only from exact fresh inventory, with no mutation retry.
- Invalidate cached previews across physical-camera aliases and expose safe
  result/error diagnostics while retaining the existing three sensor identities.
- Validate exact SDK/firmware paths and offline firmware execution. Pass the
  approved WiFi-W create/rename/delete test with identical renamed audio and the
  original inventory/settings preserved; no camera playback.

## 0.1.0 — 2026-11-02

Initial independent Reolink Audio Library release, separated from the experimental
stored-audio work in Reolink Talk with MIT attribution retained.

- New domain, config flow, device-based upload/list actions and recording sensors.
- Keep verified create-only upload, slot inventory and private previews on other
  outputs; move upload progress/results to a dedicated status sensor.
- Remove live Talk, TTS, media-player entities, volume, native camera playback,
  old WebRTC cleanup and hardware operator scripts.
- Keep existing Reolink/Talk services, entities and configuration untouched.
- Pass 431 tests in each of three real HA harnesses and include migration guidance.
