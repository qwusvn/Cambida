import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "cambida_watchdog_tested", ROOT / "scripts" / "cambida_watchdog.py"
)
WATCHDOG = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WATCHDOG)


class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/ping":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_args):
        pass


class WatchdogTests(unittest.TestCase):
    def test_safe_child_rejects_runtime_state_and_traversal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            with self.assertRaisesRegex(ValueError, "Protected"):
                WATCHDOG.safe_child(root, "config.json")
            with self.assertRaisesRegex(ValueError, "Unsafe"):
                WATCHDOG.safe_child(root, "../outside.bin")
            allowed = WATCHDOG.safe_child(
                root, ".update-backups/abc", allow_protected=True
            )
            self.assertTrue(allowed.is_relative_to(root.resolve()))

    def test_rollback_transaction_restores_program_files_only(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            backup = target / ".update-backups" / "tx1"
            backup.mkdir(parents=True)
            (target / "Cambida.exe").write_bytes(b"new")
            (target / "new-module.pyd").write_bytes(b"new-module")
            (target / "config.json").write_text(
                '{"server_port":8000,"keep":true}', encoding="utf-8"
            )
            (backup / "Cambida.exe").write_bytes(b"old")

            tx = {
                "schema": 1,
                "stage": "applying",
                "backup_rel": ".update-backups/tx1",
                "journal": [
                    {"relative": "Cambida.exe", "existed": True},
                    {"relative": "new-module.pyd", "existed": False},
                ],
            }
            self.assertTrue(WATCHDOG.rollback_transaction(target, tx, launch=False))
            self.assertEqual((target / "Cambida.exe").read_bytes(), b"old")
            self.assertFalse((target / "new-module.pyd").exists())
            self.assertEqual(
                json.loads((target / "config.json").read_text(encoding="utf-8")),
                {"server_port": 8000, "keep": True},
            )
            saved = json.loads(
                (target / WATCHDOG.TRANSACTION).read_text(encoding="utf-8")
            )
            self.assertEqual(saved["stage"], "rolled_back")

    def test_rollback_refuses_transaction_that_targets_config(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            backup = target / ".update-backups" / "tx2"
            backup.mkdir(parents=True)
            (target / "config.json").write_text('{"keep":true}', encoding="utf-8")
            (backup / "config.json").write_text('{"keep":false}', encoding="utf-8")
            tx = {
                "backup_rel": ".update-backups/tx2",
                "journal": [{"relative": "config.json", "existed": True}],
            }
            self.assertFalse(WATCHDOG.rollback_transaction(target, tx, launch=False))
            self.assertEqual(
                json.loads((target / "config.json").read_text(encoding="utf-8")),
                {"keep": True},
            )

    def test_http_health_uses_server_port_from_preserved_config(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            server = ThreadingHTTPServer(("127.0.0.1", 0), PingHandler)
            port = server.server_address[1]
            (target / "config.json").write_text(
                json.dumps({"server_port": port}), encoding="utf-8"
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                self.assertTrue(WATCHDOG.http_healthy(target))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_transaction_age_marks_old_update_as_stale(self):
        age = WATCHDOG.transaction_age({"updated_at": int(time.time()) - 300})
        self.assertGreaterEqual(age, 299)

    def test_cleanup_disk_artifacts_prunes_backups_and_temp(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td)
            backup_root = target / ".update-backups"
            backup_root.mkdir()
            b1 = backup_root / "backup_old"
            b1.mkdir()
            (b1 / "file.txt").write_text("old")
            time.sleep(0.01)
            b2 = backup_root / "backup_new"
            b2.mkdir()
            (b2 / "file.txt").write_text("new")

            WATCHDOG.cleanup_disk_artifacts(target)
            self.assertTrue(b2.is_dir())
            self.assertFalse(b1.is_dir())


if __name__ == "__main__":
    unittest.main()
