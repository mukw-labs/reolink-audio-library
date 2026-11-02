#!/usr/bin/env python3
"""Build an installable custom_components ZIP without research or cache files."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "reolink_audio_library"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, help="ZIP output; defaults to artifacts/release/reolink-audio-library-VERSION.zip"
    )
    args = parser.parse_args()
    version = json.loads((COMPONENT / "manifest.json").read_text())["version"]
    output = args.output or ROOT / "artifacts" / "release" / f"reolink-audio-library-{version}.zip"
    output.parent.mkdir(parents=True, exist_ok=True)
    files = {
        path.relative_to(ROOT).as_posix(): path.read_bytes()
        for path in sorted(COMPONENT.rglob("*"))
        if path.is_file() and path.suffix in (".py", ".json", ".yaml", ".png") and "__pycache__" not in path.parts
    }
    files["custom_components/reolink_audio_library/LICENSE"] = (ROOT / "LICENSE").read_bytes()
    files["custom_components/reolink_audio_library/NOTICE.md"] = (ROOT / "NOTICE.md").read_bytes()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            # Fixed timestamps make the same source yield the same ZIP hash.
            info = ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    with ZipFile(output) as archive:
        assert archive.testzip() is None, "Release ZIP failed its CRC check"
        assert all(name.startswith("custom_components/reolink_audio_library/") for name in archive.namelist())
        assert json.loads(archive.read("custom_components/reolink_audio_library/manifest.json"))["version"] == version
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(".zip.sha256").write_text(f"{digest}  {output.name}\n")
    print(json.dumps({"zip": str(output), "version": version, "files": len(files), "sha256": digest}))


if __name__ == "__main__":
    main()
