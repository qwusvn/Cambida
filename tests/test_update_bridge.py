"""Exercise the payload bridge used by older clients, with isolated processes."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import psutil

from test_universal_update import write_release

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = os.environ.get("CAMBIDA_TEST_POWERSHELL", "powershell.exe")
ROOT_LETTERS = "ZYXWVUTSRQPONMLKJ"


@unittest.skipUnless(sys.platform == "win32", "Windows compatibility bridge")
class UpdateBridgeTests(unittest.TestCase):
    def _bridge_exe(self):
        runner = Path(os.environ.get("CAMBIDA_TEST_UPDATER_EXE", str(ROOT / "updater.exe")))
        self.assertTrue(runner.is_file(), "Build updater.exe before running bridge integration")
        return runner

    def _run_bridge(self, payload, target, cleanup=None, timeout=120):
        command = [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass",
                   "-File", str(ROOT / "scripts/universal_updater.ps1"),
                   "-OldPid", "0", "-Payload", str(payload), "-Target", str(target)]
        if cleanup:
            command += ["-CleanupRoot", str(cleanup)]
        return subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                              creationflags=0x08000000)

    def _map_drive(self, target):
        # subst exposes an ordinary directory as a drive root, so the bridge
        # sees exactly the "D:\" layout without touching a real system drive.
        for letter in ROOT_LETTERS:
            root = letter + ":\\"
            if os.path.exists(root):
                continue
            result = subprocess.run(["subst", letter + ":", str(target)], capture_output=True, text=True)
            if result.returncode == 0:
                return root
        self.skipTest("No free drive letter for a root-installation test")
        return None

    def _kill(self, executable):
        for process in psutil.process_iter(["exe"]):
            try:
                if process.info["exe"] and Path(process.info["exe"]).resolve() == Path(executable).resolve():
                    children = process.children(recursive=True)
                    for child in children:
                        child.kill()
                    process.kill()
                    psutil.wait_procs([process, *children], timeout=5)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

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


    def test_bridge_handles_an_installation_at_a_drive_root(self):
        runner = self._bridge_exe()
        with tempfile.TemporaryDirectory(dir=ROOT / "test_data") as td:
            base = Path(td)
            target = base / "shop"
            target.mkdir()
            (target / "config.json").write_bytes(b'{"shop":true}')
            (target / "cctv_videos").mkdir()
            (target / "cctv_videos" / "keep.mp4").write_bytes(b"video")
            staging = base / "staging"
            payload = staging / "payload"
            files = {"server.exe": Path(os.environ["WINDIR"]) / "System32/cmd.exe",
                     "updater.exe": runner,
                     "update_exclude.txt": ROOT / "update_exclude.txt",
                     "new.bin": b"new"}
            write_release(payload, "2.4.0", files, args=["/d", "/c", "ping -n 20 127.0.0.1 >nul"])
            drive = self._map_drive(target)
            try:
                result = self._run_bridge(payload, drive, cleanup=staging)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual((target / "config.json").read_bytes(), b'{"shop":true}')
                self.assertEqual((target / "cctv_videos" / "keep.mp4").read_bytes(), b"video")
                self.assertEqual((target / "new.bin").read_bytes(), b"new")
                self.assertEqual(json.loads((target / "update.json").read_text())["version"], "2.4.0")
                log = (target / "logs/updater.log").read_text(encoding="utf-8")
                self.assertIn("Update 2.4.0 completed", log)
                self.assertFalse(staging.exists(), "staging directory was not removed")
            finally:
                subprocess.run(["subst", drive.rstrip("\\"), "/D"], capture_output=True)
                self._kill(target / "server.exe")

    def test_bridge_restarts_the_application_when_the_payload_is_invalid(self):
        with tempfile.TemporaryDirectory(dir=ROOT / "test_data") as td:
            base = Path(td)
            target = base / "shop"
            target.mkdir()
            shutil.copy2(Path(os.environ["WINDIR"]) / "System32/WindowsPowerShell/v1.0/powershell.exe",
                         target / "Cambida.exe")
            (target / "update.json").write_text(json.dumps({
                "schema": 2, "kind": "full", "version": "2.3.4",
                "start": {"path": "Cambida.exe", "args": ["-NoProfile", "-Command", "Start-Sleep -Seconds 30"]},
            }), encoding="utf-8")
            payload = base / "payload"
            write_release(payload, "2.4.0", {"dummy.bin": b"x"})  # no updater.exe on purpose
            try:
                result = self._run_bridge(payload, target, timeout=90)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                log = (target / "logs/updater.log").read_text(encoding="utf-8")
                self.assertIn("Missing standalone updater.exe", log)
                self.assertIn("Original application restarted by the bridge", log)
                self.assertFalse((target / "dummy.bin").exists())
            finally:
                self._kill(target / "Cambida.exe")


if __name__ == "__main__":
    unittest.main()
