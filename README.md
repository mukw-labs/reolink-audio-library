<p align="center">
  <img src="docs/assets/logo.png" alt="Reolink Audio Library" width="160">
</p>

# Reolink Audio Library

Manage the audio recordings saved on your Reolink camera from Home Assistant.
Upload a custom reply, rename or delete a recording, check available slots, and
listen through your browser or another speaker.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Tests](https://github.com/mukw-labs/reolink-audio-library/actions/workflows/tests.yaml/badge.svg)](https://github.com/mukw-labs/reolink-audio-library/actions/workflows/tests.yaml)
[![HACS: Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://www.hacs.xyz/docs/faq/custom_repositories/)

An independent custom integration. Requires the official Reolink integration;
works alongside standard Reolink Talk. Audio Library does not play audio on the
camera or add a live Talk media player.

## Features

| Feature | What you get |
| --- | --- |
| Upload custom audio | Choose a camera, local HA media file and recording name. Save into a free slot. |
| Browse and list | See factory and custom recordings, durations and recording identifiers. |
| Rename | Change a custom recording's name while preserving its ID, UUID and audio. |
| Delete | Remove one selected custom recording with explicit confirmation. |
| Preview elsewhere | Listen through HA's Media browser on your browser or another compatible speaker. |
| Slot reporting | Custom recording count, free slots and attributes describing factory, occupied and free slots. |
| Status | Upload progress and the latest operation result on the existing camera device. |

Factory recordings are protected. Uploads create new recordings and never
replace existing ones. Every write verifies the intended result and preservation
of the other recordings after reconnecting.

## Supported device and requirements

Support is currently restricted to this physically validated profile:

| Model | Hardware | Firmware | Connection |
| --- | --- | --- | --- |
| Reolink Video Doorbell WiFi-W | `DB_566128M5MP_W_W` | `v3.0.0.6460_2605211912` | Standalone, channel 0 |

Other models, firmware versions and NVR-connected channels are not enabled for
recording operations. General two-way audio support does not establish upload
support. If your profile differs, please open a compatibility request with the
model, hardware and firmware versions; do not include credentials or recordings.

- Home Assistant **2026.9.0 or newer**.
- The official **Reolink** integration already configured for the camera.
- FFmpeg for upload conversion, normally supplied by Home Assistant.
- A Home Assistant administrator account for upload, rename and delete.

No additional camera password, streaming service or frontend card is needed.
Reolink Talk is optional.

## Install

### HACS custom repository

[![Open your Home Assistant instance and add this repository to HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=mukw-labs&repository=reolink-audio-library&category=integration)

1. In **HACS → ⋮ → Custom repositories**, add
   `https://github.com/mukw-labs/reolink-audio-library` with type **Integration**.
2. Find **Reolink Audio Library** in HACS and download it.
3. Restart Home Assistant.
4. Open **Settings → Devices & services → Add integration → Reolink Audio Library**.
5. Select the existing Reolink camera you want to manage.

This is a HACS custom repository; it is not listed in the default HACS catalog.
See the [HACS custom repository guide](https://www.hacs.xyz/docs/faq/custom_repositories/).

### Manual installation

Download `reolink-audio-library-0.2.0.zip` from the
[latest release](https://github.com/mukw-labs/reolink-audio-library/releases/latest).
Extract its `custom_components/reolink_audio_library` directory into your Home
Assistant configuration directory, keeping that folder structure. Restart HA,
then add the integration as described above.

Use the integration's **Configure** option to change the camera selection later.
The sensors appear on the existing official Reolink device.

## Upload a recording

1. Add a short audio file under **Media → My media → Manage → Add media**.
2. Open **Developer tools → Actions → Reolink Audio Library: Upload custom audio**.
3. Select **Camera**, pick the local media file, enter a unique name and run.
4. Follow the camera's **Upload status** sensor. Success means the new recording
   was verified after reconnect, with the previous recordings preserved.

Uploads accept up to **10 seconds** of audio and **32 MiB** of input. WAV, MP3 and
other formats FFmpeg can decode are converted automatically. Recording names
may contain up to **127 UTF-8 bytes**. The camera assigns a free custom slot;
use the confirmed delete action to free one when full.

The advanced `media_content_id` field is an alternative to the media picker.
Provide one source. Actions return a direct response dictionary, including the
receipt and `inventory_refreshed`. If storage succeeds but display refresh fails,
the action remains successful with `inventory_refreshed: false`; refresh the list
before trying anything again. An uncertain write is never retried automatically.

## Rename or delete

Open **Developer tools → Actions → Reolink Audio Library**, then select **Rename
custom recording** or **Delete custom recording**. Choose the **Camera** and pick
its saved custom recording under **Reolink Audio Library** in the media picker.
The picker selects metadata without playing the recording.

Rename requires a new name. Delete requires **Confirm deletion**. A recording
configured as the automatic reply cannot be deleted, even when that reply is
disabled; select a different reply in the Reolink app first.

For scripts, the list action returns a current recording identifier accepted by
the advanced `media_content_id` field. Re-select the recording after changing it:
old identifiers become invalid when its metadata or UUID changes. Operation
responses include `changed`, `acknowledged`, `verified_after_reconnect` and
`inventory_refreshed`; the latest result is also on the status sensor.

Avoid simultaneous recording management through the vendor app or another
integration. See [management validation and limits](docs/RENAME_DELETE_VALIDATION.md).

## Listen on another speaker

In **Media**, choose **Web browser** or another speaker as the output, then open
**Reolink Audio Library → camera → recording**. Selecting a recording exports it
read-only and serves a temporary PCM WAV to the chosen output. This never invokes
camera playback. Reolink Talk players cannot be selected as preview outputs.

The output needs WAV support and access to HA's media URL. Preview audio is cached
privately in memory for up to ten minutes, with a 16-entry / 8 MiB limit. It is
not written into public `/local`, My media or permanent files.

## Coexistence and privacy

The `reolink_audio_library` domain has separate actions, sensors, cache and API
paths. The integration uses owned camera sessions with the official integration's
existing credentials; it does not replace its shared connection or change camera
settings. It does not manage Reolink Talk's services or entities.

If moving from an experimental Talk build with recording features, follow the
[migration guide](docs/MIGRATION.md). Standard Talk can remain installed for live
audio.

No telemetry is added. Camera communication stays local; conversion uses local
FFmpeg and previews use authenticated or time-limited signed HA URLs. See
[security guidance](SECURITY.md) before sharing diagnostics.

## Validation and development

The 0.2.0 backend passed a physical WiFi-W test: create one new silent recording,
rename it with identical exported audio bytes, then delete it. All pre-existing
recordings and automatic-reply settings were preserved. Nothing played on the
camera. The HA boundary passes **533 tests in each of three tested HA versions**.
See [test results](docs/TEST_RESULTS.md), [architecture](docs/ARCHITECTURE.md) and
[protocol evidence](docs/AUDIO_PROTOCOL.md).

```sh
uv venv --python python3.14 .venv
uv pip install --python .venv/bin/python -r requirements-test-ha.txt
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest -q
python scripts/build_release.py
```

Automated tests use fake cameras and synthetic audio. They never contact hardware.
Support for additional camera profiles requires independent protocol validation.

## License and credits

[MIT](LICENSE). Derived from the saved-audio feature work in
[magicx78/reolink_talk](https://github.com/magicx78/reolink_talk), itself based on
[joeblack2k/reolink_talk](https://github.com/joeblack2k/reolink_talk). Original
attribution is retained in [NOTICE.md](NOTICE.md).

This project is not affiliated with or endorsed by Reolink, Home Assistant or the
earlier maintainers. The original Audio Library logo was generated with OpenAI's
image-generation tool and is included under the repository's MIT license.
