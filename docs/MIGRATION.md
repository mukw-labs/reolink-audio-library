# Moving from an experimental Reolink Talk build

Audio Library uses the independent `reolink_audio_library` domain. Standard Talk
keeps `reolink_talk` and remains responsible for live audio. Audio Library never
automatically migrates or removes another integration's entities, settings or code.

If using standard Reolink Talk, install Audio Library alongside it. No Talk
migration is needed.

If using an experimental Talk build that already contains upload and saved-audio
features:

1. Back up Home Assistant and the existing Talk directory. Record the Talk entity
   IDs and any automations using experimental recording actions.
2. Install Audio Library and select the existing official Reolink camera. Check
   its sensors and read-only list action.
3. Move recording callers to the actions below. Re-select saved recordings using
   Audio Library's Media browser; old identifiers and signed URLs cannot be reused.
4. Restore a reviewed standard Talk release if keeping live audio. Replace the
   complete component directory so experimental modules do not remain, then
   restart HA. Retain its config entry to preserve the existing player identities.
5. Verify Talk players and Audio Library sensors. Remove obsolete experimental
   sensor entries through HA's UI only after checking their consumers.

| Experimental action or feature | Audio Library equivalent |
| --- | --- |
| `reolink_talk.upload_audio`, targeting a Talk player | `reolink_audio_library.upload_audio`, selecting the official camera device |
| `reolink_talk.list_audio` | `reolink_audio_library.list_audio` |
| Upload attributes on the Talk player | Upload status sensor and action response |
| Experimental saved-recordings source | Reolink Audio Library in Media |
| Native camera playback | Outside Audio Library's scope |

Do not re-upload existing camera files during migration. Use one recording
management client at a time: the vendor app and other integrations do not share
Audio Library's camera lock. Configuration and entity registries should be managed
through Home Assistant, without editing `.storage` files directly.
