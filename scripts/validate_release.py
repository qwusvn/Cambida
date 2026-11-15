"""Offline full-package validation; never starts camera workers or services."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile


def validate(archive):
    archive = Path(archive)
    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
        assert len(names) == len(set(names)), "Duplicate archive paths"
        forbidden = {"config.json", "go2rtc.yaml", "analytics.db", "tunnel_token.txt",
                     "device_id.key", "controlhub_machine_id.txt", "controlhub_client_secret.txt"}
        for name in names:
            path = Path(name)
            assert not path.is_absolute() and ".." not in path.parts and ":" not in name, name
            assert not set(path.parts) & forbidden, name
            assert path.parts[0] not in {"logs", "cctv_videos", "nvr_cache"}, name
        manifest = json.loads(z.read("release_manifest.json"))
        update = json.loads(z.read("update.json"))
        assert manifest["schema"] == update["schema"] == 2
        assert manifest["version"] == update["version"] == archive.stem
        assert update["kind"] == "full" and update["start"]["path"] == "Cambida.exe"
        covered = set(manifest["files"])
        assert set(names) - {"release_manifest.json", "update.json"} == covered, "Manifest coverage mismatch"
        for name, expected in manifest["files"].items():
            assert hashlib.sha256(z.read(name)).hexdigest() == expected, name
        required = {"Cambida.exe", "updater.exe", "go2rtc.exe", "ffmpeg.exe", "index.html",
                    "static/hevc-inline.mjs", "static/hevc-mp4.mjs", "modules/modules.json",
                    "static/vendor/go2rtc-LICENSE.txt", "static/vendor/libde265-web-0.1.4/NOTICE.md",
                    "static/vendor/libde265-web-0.1.4/LICENSE"}
        assert required <= set(names), sorted(required - set(names))
        assert any(n.endswith(".wasm") for n in names), "No decoder binary"
        assert any("third_party/libde265/" in n and n.endswith(".cc") for n in names), "No LGPL source"
        build = json.loads(z.read("BUILD_INFO.json"))
        root = Path(__file__).resolve().parents[1]
        for name, expected in build["source"].items():
            assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected, "Source drift: " + name
        return {"version": manifest["version"], "files": len(names), "manifest_hashes": len(covered),
                "zip_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "tests": build["tests"], "status": "PASS"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = validate(args.archive)
    text = json.dumps(result, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
