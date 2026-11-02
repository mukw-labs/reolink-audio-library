# Attribution and provenance

Reolink Audio Library is distributed under the MIT license in LICENSE.

The source was separated on 2026-11-02 from the stored-recording feature work in
magicx78/reolink_talk. That project is an MIT-licensed maintained fork of
joeblack2k/reolink_talk. Credit for the original integration belongs to joeblack2k,
and credit for its maintained fork to magicx78 and contributors. The original
MIT copyright notice and permission text are retained unchanged in LICENSE.

The standard IMA tables/quantizer in ima_adpcm.py were extracted from that MIT
code to remove the live-talk dependency. Stored format/layout, transport and
export support were independently implemented during the feature work from
protocol observations. No proprietary firmware/application code or GPL/AGPL
implementation is included. Vendor binaries, disassembly, private credentials,
audio captures and hardware evidence artifacts are not part of this repository.

This is not an official Reolink or Home Assistant integration and does not imply
endorsement by either project or the earlier maintainers. It has a distinct
Home Assistant domain and a separate repository identity.
