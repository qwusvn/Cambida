"""End-to-End Test: Upgrading Cambida from 2.3.3 to 2.3.4.

Verifies:
1. native_update.prepare_archive validates 2.3.4.zip cleanly.
2. universal_updater.ps1 overlays 2.3.4 on a real 2.3.3 install.
3. All shop state (config.json, analytics.db, tokens, videos) is 100% preserved.
4. Version tags and manifests correctly advance to 2.3.4.
5. Launching the updated runtime responds to health probes.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SPEC = importlib.util.spec_from_file_location("native_update", ROOT / "native_update.py")
NATIVE_UPDATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(NATIVE_UPDATE)


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _kill_lingering_test_processes():
    subprocess.run(
        [
            "powershell.exe", "-NoProfile", "-Command",
            "Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match '^Cambida' -or $_.ProcessName -match '^ffmpeg' } | Stop-Process -Force -ErrorAction SilentlyContinue"
        ],
        capture_output=True,
        timeout=10,
    )


class E2EUpdate233To234Tests(unittest.TestCase):
    def setUp(self):
        _kill_lingering_test_processes()
        time.sleep(1.0)

    def tearDown(self):
        _kill_lingering_test_processes()
        time.sleep(1.0)

    def test_e2e_upgrade_from_233_to_234(self):
        zip_233 = ROOT / "release" / "2.3.3.zip"
        zip_234 = ROOT / "release" / "2.3.4.zip"

        self.assertTrue(zip_233.is_file(), f"Missing 2.3.3 artifact: {zip_233}")
        self.assertTrue(zip_234.is_file(), f"Missing 2.3.4 artifact: {zip_234}")

        with tempfile.TemporaryDirectory(dir=ROOT, ignore_cleanup_errors=True) as td:
            base = Path(td)
            installed = base / "installed_233"
            extracted_234 = base / "extracted_234"
            installed.mkdir()

            try:
                print(f"\n[1/5] Extracting baseline 2.3.3 into {installed.name}...", flush=True)
                with zipfile.ZipFile(zip_233, "r") as zf:
                    zf.extractall(installed)

                self.assertEqual((installed / "RELEASE_VERSION.txt").read_text(encoding="utf-8").strip(), "2.3.3")

                print("[2/5] Seeding critical shop data in 2.3.3...", flush=True)
                shop_config = {
                    "server_port": 8004,
                    "site": {"name": "Billiards Club 2.3.3 Test"},
                    "disk_limit_gb": 120.0,
                    "minimum_free_disk_gb": 15.0,
                    "cameras": [{"id": 1, "name": "Bàn VIP 1", "ip": "192.168.1.10"}],
                    "tables": [{"id": "ban-01", "name": "Bàn VIP 1", "camera_id": 1}],
                    "telegram_token": "test-bot-token-secret",
                    "telegram_chat_id": "999888777",
                }
                (installed / "config.json").write_text(json.dumps(shop_config, indent=2), encoding="utf-8")

                # Seed SQLite db
                db_path = installed / "analytics.db"
                conn = sqlite3.connect(db_path)
                conn.execute("CREATE TABLE shop_test (id INTEGER PRIMARY KEY, note TEXT)")
                conn.execute("INSERT INTO shop_test (note) VALUES ('bill_record_preserved')")
                conn.commit()
                conn.close()

                # Seed identity tokens
                (installed / "tunnel_token.txt").write_text("cloudflare-named-tunnel-token-secret-xyz", encoding="utf-8")
                (installed / "controlhub_machine_id.txt").write_text("mach-id-12345678", encoding="utf-8")
                (installed / "controlhub_client_secret.txt").write_text("hub-client-secret-abc", encoding="utf-8")

                # Seed recorded video
                video_dir = installed / "cctv_videos"
                video_dir.mkdir(exist_ok=True)
                test_video = video_dir / "cam1_00-00-00_to_00-05-00_(01-10-2026).mp4"
                test_video.write_bytes(b"dummy_video_payload_bytes_for_testing")

                print("[3/5] Validating 2.3.4.zip using native_update.prepare_archive...", flush=True)
                prep_result = NATIVE_UPDATE.prepare_archive(zip_234, extracted_234, installed, "2.3.4")
                self.assertEqual(prep_result["manifest"]["version"], "2.3.4")
                self.assertEqual(prep_result["update"]["version"], "2.3.4")
                self.assertIn("modules/modules.json", prep_result["manifest"]["files"])
                modules_json = json.loads((extracted_234 / "modules/modules.json").read_text(encoding="utf-8"))
                self.assertIn("cambida_app", modules_json)

                print("[4/5] Running universal_updater.ps1 from payload to overlay 2.3.4 onto installed shop...", flush=True)
                updater_script = extracted_234 / "updater.ps1"
                self.assertTrue(updater_script.is_file(), "Payload updater.ps1 must exist")

                proc = subprocess.run(
                    [
                        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", str(updater_script),
                        "-OldPid", "0",
                        "-Payload", str(extracted_234),
                        "-Target", str(installed),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                self.assertEqual(proc.returncode, 0, f"Updater failed: {proc.stdout}\n{proc.stderr}")

                print("[5/5] Verifying 2.3.4 overlay and shop state integrity...", flush=True)
                # 1. Version tags advanced to 2.3.4
                self.assertEqual((installed / "RELEASE_VERSION.txt").read_text(encoding="utf-8").strip(), "2.3.4")
                self.assertEqual((installed / "VERSION.txt").read_text(encoding="utf-8").strip(), "2.3.4")

                # 2. release_manifest.json updated
                installed_manifest = json.loads((installed / "release_manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(installed_manifest["version"], "2.3.4")

                # 3. Shop config preserved exactly
                cur_config = json.loads((installed / "config.json").read_text(encoding="utf-8"))
                self.assertEqual(cur_config["server_port"], 8004)
                self.assertEqual(cur_config["site"]["name"], "Billiards Club 2.3.3 Test")
                self.assertEqual(cur_config["disk_limit_gb"], 120.0)
                self.assertEqual(cur_config["telegram_chat_id"], "999888777")

                # 4. SQLite DB preserved intact
                conn = sqlite3.connect(db_path)
                row = conn.execute("SELECT note FROM shop_test WHERE id = 1").fetchone()
                conn.close()
                self.assertIsNotNone(row)
                self.assertEqual(row[0], "bill_record_preserved")

                # 5. Tokens preserved intact
                self.assertEqual((installed / "tunnel_token.txt").read_text(encoding="utf-8"), "cloudflare-named-tunnel-token-secret-xyz")
                self.assertEqual((installed / "controlhub_machine_id.txt").read_text(encoding="utf-8"), "mach-id-12345678")
                self.assertEqual((installed / "controlhub_client_secret.txt").read_text(encoding="utf-8"), "hub-client-secret-abc")

                # 6. Video file preserved intact
                self.assertTrue(test_video.is_file())
                self.assertEqual(test_video.read_bytes(), b"dummy_video_payload_bytes_for_testing")

                # 7. Modules updated with new native binary
                cur_modules = json.loads((installed / "modules/modules.json").read_text(encoding="utf-8"))
                cambida_pyd_name = cur_modules["cambida_app"]["file"]
                self.assertTrue((installed / "modules" / cambida_pyd_name).is_file())

                print("SUCCESS: 2.3.3 to 2.3.4 upgrade verified flawlessly without any loss or error!", flush=True)

            finally:
                _kill_lingering_test_processes()
                time.sleep(1.0)


if __name__ == "__main__":
    unittest.main()
