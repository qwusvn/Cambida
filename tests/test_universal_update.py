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


def write_release(folder, version, files, *, start="server.exe", args=None, health=None):
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
    (folder / "release_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    (folder / "update.json").write_text(
        json.dumps(update, indent=2) + "\n", encoding="utf-8"
    )
    return manifest, update


class UniversalUpdateTests(unittest.TestCase):
    def test_prepare_archive_accepts_layout_independent_full_release(self):
        with tempfile.TemporaryDirectory() as td:
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

    def test_prepare_archive_rejects_shop_state_inside_release(self):
        with tempfile.TemporaryDirectory() as td:
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

    def test_updater_overlays_new_layout_replaces_itself_and_preserves_shop_data(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            target = base / "target"
            payload = base / "payload"
            target.mkdir()
            cmd = Path(os.environ["WINDIR"]) / "System32" / "cmd.exe"

            old_updater = target / "updater.ps1"
            old_updater.write_text("# old updater\n", encoding="utf-8")
            (target / "old-layout.bin").write_bytes(b"old")
            old_manifest = {
                "schema": 2,
                "version": "2.3.0",
                "files": {
                    "old-layout.bin": digest(target / "old-layout.bin"),
                    "updater.ps1": digest(old_updater),
                },
            }
            (target / "release_manifest.json").write_text(
                json.dumps(old_manifest), encoding="utf-8"
            )
            (target / "update.json").write_text(
                json.dumps({
                    "schema": 2,
                    "kind": "full",
                    "version": "2.3.0",
                    "start": {"path": "old-layout.bin", "args": []},
                }),
                encoding="utf-8",
            )

            (target / "config.json").write_text('{"shop":true}', encoding="utf-8")
            (target / "analytics.db").write_bytes(b"db")
            (target / "tunnel_token.txt").write_text("secret-token", encoding="utf-8")
            (target / "controlhub_client_secret.txt").write_text("local-client-state", encoding="utf-8")
            media = target / "cctv_videos"
            media.mkdir()
            (media / "keep.mp4").write_bytes(b"video")

            new_updater = b"# updater v2\n"
            write_release(
                payload,
                "2.4.0",
                {
                    "server.exe": cmd,
                    "new-layout.bin": b"new",
                    "updater.ps1": new_updater,
                },
                start="server.exe",
                args=["/d", "/c", "echo started>started.marker"],
            )

            script = ROOT / "scripts" / "universal_updater.ps1"
            completed = subprocess.run(
                [
                    "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(script),
                    "-OldPid", "0",
                    "-Payload", str(payload),
                    "-Target", str(target),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse((target / "old-layout.bin").exists())
            self.assertEqual((target / "new-layout.bin").read_bytes(), b"new")
            self.assertEqual((target / "updater.ps1").read_bytes(), new_updater)
            self.assertEqual(
                json.loads((target / "config.json").read_text(encoding="utf-8")),
                {"shop": True},
            )
            self.assertEqual((target / "analytics.db").read_bytes(), b"db")
            self.assertEqual(
                (target / "tunnel_token.txt").read_text(encoding="utf-8"),
                "secret-token",
            )
            self.assertEqual(
                (target / "controlhub_client_secret.txt").read_text(encoding="utf-8"),
                "local-client-state",
            )
            self.assertEqual((media / "keep.mp4").read_bytes(), b"video")
            marker = target / "started.marker"
            for _ in range(40):
                if marker.exists():
                    break
                time.sleep(0.05)
            self.assertTrue(marker.exists())
            self.assertEqual(
                json.loads((target / "update.json").read_text(encoding="utf-8"))["version"],
                "2.4.0",
            )


    def test_updater_health_uses_preserved_runtime_server_port(self):
        script = (ROOT / "scripts" / "universal_updater.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("$runtimeConfig.server_port", script)
        self.assertIn("Wait-Health $update $Target", script)
        self.assertIn("controlhub_client_secret.txt", script)

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
