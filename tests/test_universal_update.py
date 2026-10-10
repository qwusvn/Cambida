import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]

SPEC = importlib.util.spec_from_file_location("native_update_tested", ROOT / "native_update.py")
NATIVE_UPDATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(NATIVE_UPDATE)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_release(folder, version, files, *, start="server.exe", args=None, health=None, health_v2=None):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    for name, source in files.items():
        target = folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(source, bytes):
            target.write_bytes(source)
        else:
            shutil.copy2(source, target)
    manifest = {
        "schema": 2,
        "version": version,
        "files": {
            name.replace("\\", "/"): digest(folder / name)
            for name in files
        },
    }
    update = {
        "schema": 2,
        "kind": "full",
        "version": version,
        "start": {"path": start, "args": list(args or [])},
    }
    if health:
        update["health"] = health
    if health_v2:
        update["health_v2"] = health_v2
    (folder / "release_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    (folder / "update.json").write_text(
        json.dumps(update, indent=2) + "\n", encoding="utf-8"
    )
    return manifest, update


class UniversalUpdateTests(unittest.TestCase):
    def test_prepare_archive_accepts_layout_independent_full_release(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "test_data") as td:
            base = Path(td)
            source = base / "source"
            installed = base / "installed"
            extracted = base / "extracted"
            installed.mkdir()
            cmd = Path(os.environ["WINDIR"]) / "System32" / "cmd.exe"
            write_release(
                source,
                "2.4.0",
                {"server.exe": cmd, "sub/new-layout.bin": b"new layout"},
                start="server.exe",
                args=["/d", "/c", "exit 0"],
            )
            archive = base / "2.4.0.zip"
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
                for path in source.rglob("*"):
                    if path.is_file():
                        zf.write(path, path.relative_to(source).as_posix())

            result = NATIVE_UPDATE.prepare_archive(
                archive, extracted, installed, "2.4.0"
            )
            self.assertEqual(result["manifest"]["schema"], 2)
            self.assertEqual(result["update"]["start"]["path"], "server.exe")

    def test_health_v2_config_port_metadata_is_valid(self):
        NATIVE_UPDATE._validate_local_health({
            "health_v2": {"mode": "config_port", "path": "/", "timeout_seconds": 90}
        })
        with self.assertRaisesRegex(ValueError, "Health path is invalid"):
            NATIVE_UPDATE._validate_local_health({
                "health_v2": {
                    "mode": "config_port",
                    "path": "https://bad.example/",
                    "timeout_seconds": 90,
                }
            })

    def test_prepare_archive_rejects_shop_state_inside_release(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "test_data") as td:
            base = Path(td)
            source = base / "source"
            source.mkdir()
            (source / "config.json").write_text("{}", encoding="utf-8")
            (source / "release_manifest.json").write_text(
                json.dumps({
                    "schema": 2,
                    "version": "2.4.0",
                    "files": {"config.json": digest(source / "config.json")},
                }),
                encoding="utf-8",
            )
            (source / "update.json").write_text(
                json.dumps({
                    "schema": 2,
                    "kind": "full",
                    "version": "2.4.0",
                    "start": {"path": "config.json", "args": []},
                }),
                encoding="utf-8",
            )
            archive = base / "bad.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                for path in source.iterdir():
                    zf.write(path, path.name)
            with self.assertRaisesRegex(ValueError, "Unsafe update path"):
                NATIVE_UPDATE.prepare_archive(
                    archive, base / "out", base / "installed", "2.4.0"
                )








    def test_new_package_uses_standalone_updater_and_txt(self):
        builder = (ROOT / "scripts/build_native.py").read_text(encoding="utf-8-sig")
        app = (ROOT / "1.py").read_text(encoding="utf-8-sig")
        launcher = (ROOT / "scripts/release_launcher.cmd").read_text(encoding="utf-8-sig")
        self.assertIn("updater_runtime()", builder)
        self.assertIn("'update_exclude.txt'", builder)
        self.assertNotIn("watchdog_runtime()", builder)
        self.assertNotIn("CambidaWatchdog.exe", builder + launcher)
        self.assertNotIn("_ensure_external_watchdog", app)

    def test_native_builder_ignores_invalid_sync_copy_module_names(self):
        spec = importlib.util.spec_from_file_location(
            "build_native_tested", ROOT / "scripts" / "build_native.py"
        )
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        modules = builder.source_modules()
        self.assertIn("camera_modules.config", modules)
        self.assertIn("controlhub_claim", modules)
        self.assertTrue(all(part.isidentifier() for name in modules for part in name.split(".")))
        self.assertFalse(any("(1)" in str(path) for path, _ in modules.values()))


if __name__ == "__main__":
    unittest.main()
