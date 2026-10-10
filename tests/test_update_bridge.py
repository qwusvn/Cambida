"""Exercise the payload bridge used by older clients, with isolated processes."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import psutil

from test_universal_update import write_release

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(sys.platform == "win32", "Windows compatibility bridge")
class UpdateBridgeTests(unittest.TestCase):
    def test_legacy_payload_bridge_uses_standalone_exe_and_txt(self):
        runner = Path(os.environ.get("CAMBIDA_TEST_UPDATER_EXE", str(ROOT / "updater.exe")))
        self.assertTrue(runner.is_file(), "Build updater.exe before running bridge integration")
        with tempfile.TemporaryDirectory(dir=ROOT / "test_data") as td:
            base = Path(td)
            target = base / "old shop with spaces"; target.mkdir()
            payload = base / "payload"
            protected = {"config.json": b'{"shop":true}', "analytics.db": b"db",
                         "tunnel_token.txt": b"fake-token", "cctv_videos/keep.mp4": b"video"}
            for name, data in protected.items():
                p = target / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(data)
            (target / "old-layout.bin").write_bytes(b"keep-absent-file")
            files = {name: b"replace" for name in protected}
            files.update({"server.exe": Path(os.environ["WINDIR"]) / "System32/cmd.exe",
                          "updater.exe": runner,
                          "update_exclude.txt": ROOT / "update_exclude.txt",
                          "updater.ps1": ROOT / "scripts/universal_updater.ps1",
                          "new.bin": b"new"})
            write_release(payload, "2.4.0", files, args=["/d", "/c", "ping -n 30 127.0.0.1 >nul"])
            env = os.environ.copy()
            env.update(LOCALAPPDATA=str(base / "isolated-local"), TEMP=str(base), TMP=str(base))
            try:
                result = subprocess.run([os.environ.get("CAMBIDA_TEST_POWERSHELL", "powershell.exe"), "-NoProfile", "-ExecutionPolicy", "Bypass",
                                         "-File", str(payload / "updater.ps1"), "-OldPid", "0",
                                         "-Payload", str(payload), "-Target", str(target)],
                                        env=env, capture_output=True, text=True, timeout=90,
                                        creationflags=0x08000000)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                for name, data in protected.items():
                    self.assertEqual((target / name).read_bytes(), data, name)
                self.assertEqual((target / "new.bin").read_bytes(), b"new")
                self.assertEqual((target / "old-layout.bin").read_bytes(), b"keep-absent-file")
                self.assertEqual(json.loads((target / "update.json").read_text())["version"], "2.4.0")
                self.assertFalse((target / ".watchdog-update.json").exists())
                log = (target / "logs/updater.log").read_text()
                pid = int(log.rsplit("application PID ", 1)[1].splitlines()[0])
                self.assertTrue(psutil.pid_exists(pid))
            finally:
                for process in psutil.process_iter(["exe"]):
                    try:
                        if process.info["exe"] and Path(process.info["exe"]).resolve() == (target / "server.exe").resolve():
                            children = process.children(recursive=True)
                            for child in children: child.kill()
                            process.kill()
                            psutil.wait_procs([process, *children], timeout=5)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass


if __name__ == "__main__":
    unittest.main()
