"""Regression coverage for the standalone updater and TXT exclusion contract."""
import ast
from contextlib import ExitStack
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import logging
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch
import zipfile

import psutil

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("standalone_updater_tested", ROOT / "scripts/updater.py")
UPDATER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(UPDATER)


def release(folder, files=None, args=None):
    folder.mkdir(parents=True, exist_ok=True)
    files = files or {"server.exe": b"fake"}
    files = {**files, "update_exclude.txt": b"update_exclude.txt\nconfig.json\nlogs/\n"}
    for name, data in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    manifest = {"version": "2.4.0", "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    (folder / "release_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (folder / "update.json").write_text(json.dumps({"version": "2.4.0", "start": {"path": "server.exe", "args": args or []}}), encoding="utf-8")


class StandaloneUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "test_data")
        self.base = Path(self.temp.name)
        self.target = self.base / "shop with spaces"
        self.payload = self.base / "payload"
        self.target.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_txt_is_only_copy_policy_and_local_copy_wins(self):
        release(self.payload, {"server.exe": b"new", "config.json": b"new-config",
                "CUSTOM/keep.txt": b"replace", "custom/deep/new.txt": b"new",
                "custom2/change.txt": b"new", "logs/new.log": b"new"})
        policy = "update_exclude.txt\nCUSTOM\\\n"
        (self.target / "update_exclude.txt").write_text(policy)
        (self.target / "custom").mkdir()
        (self.target / "custom/keep.txt").write_bytes(b"keep")
        (self.target / "config.json").write_bytes(b"old-config")
        UPDATER.overlay(self.payload, self.target)
        self.assertEqual((self.target / "custom/keep.txt").read_bytes(), b"keep")
        self.assertFalse((self.target / "custom/deep").exists())
        self.assertEqual((self.target / "config.json").read_bytes(), b"new-config")
        self.assertEqual((self.target / "custom2/change.txt").read_bytes(), b"new")
        self.assertTrue((self.target / "logs/new.log").exists())
        self.assertEqual((self.target / "update_exclude.txt").read_text(), policy)

    def test_first_install_seeds_txt_and_preserves_listed_files(self):
        release(self.payload, {"server.exe": b"new", "config.json": b"replace", "logs/new.log": b"new"})
        (self.target / "config.json").write_bytes(b"keep")
        UPDATER.overlay(self.payload, self.target)
        self.assertEqual((self.target / "config.json").read_bytes(), b"keep")
        self.assertFalse((self.target / "logs").exists())
        self.assertEqual((self.target / "update_exclude.txt").read_bytes(), (self.payload / "update_exclude.txt").read_bytes())

    def test_unlisted_txt_itself_can_be_overwritten(self):
        release(self.payload)
        (self.target / "update_exclude.txt").write_text("config.json\n")
        UPDATER.overlay(self.payload, self.target)
        self.assertEqual((self.target / "update_exclude.txt").read_bytes(), (self.payload / "update_exclude.txt").read_bytes())

    def test_missing_txt_refuses_copy(self):
        self.payload.mkdir()
        (self.payload / "server.exe").write_bytes(b"new")
        with self.assertRaises(FileNotFoundError):
            UPDATER.overlay(self.payload, self.target)
        self.assertFalse((self.target / "server.exe").exists())

    def test_readonly_file_is_overwritten(self):
        release(self.payload)
        dest = self.target / "server.exe"
        dest.write_bytes(b"old")
        dest.chmod(stat.S_IREAD)
        UPDATER.overlay(self.payload, self.target)
        self.assertEqual(dest.read_bytes(), b"fake")

    def test_busy_file_retries_and_permanent_failure_names_file(self):
        source = self.base / "source"; source.write_bytes(b"new")
        dest = self.base / "dest"
        real_copy = shutil.copy2
        attempts = []
        def busy_copy(*args):
            attempts.append(1)
            if len(attempts) < 3:
                raise PermissionError("busy")
            return real_copy(*args)
        with patch.object(UPDATER.shutil, "copy2", side_effect=busy_copy):
            UPDATER.copy_with_retry(source, dest, delay=0)
        self.assertEqual(dest.read_bytes(), b"new")
        with patch.object(UPDATER.shutil, "copy2", side_effect=PermissionError("busy")):
            with self.assertRaisesRegex(OSError, "Cannot overwrite.*dest"):
                UPDATER.copy_with_retry(source, dest, attempts=2, delay=0)

    def test_zip_traversal_duplicate_and_symlink_are_rejected(self):
        for names in (["../escaped.txt"], ["same.bin", "SAME.bin"], ["bad/link"]):
            with self.subTest(names=names):
                archive = self.base / "bad.zip"
                with zipfile.ZipFile(archive, "w") as bundle:
                    for name in names:
                        info = zipfile.ZipInfo(name)
                        if name == "bad/link":
                            info.external_attr = (stat.S_IFLNK | 0o777) << 16
                        bundle.writestr(info, b"bad")
                with self.assertRaises(ValueError):
                    UPDATER.extract_archive(archive, self.base / "out", "2.4.0")
        self.assertFalse((self.base / "escaped.txt").exists())

    def test_target_link_is_rejected(self):
        elsewhere = self.base / "elsewhere"; elsewhere.mkdir()
        try:
            (self.target / "escape").symlink_to(elsewhere, target_is_directory=True)
        except OSError:
            self.skipTest("Windows symlink privilege unavailable")
        with self.assertRaises(ValueError):
            UPDATER.safe_path(self.target, "escape/new.txt")

    def test_listed_directory_link_is_skipped_without_touching_external_data(self):
        release(self.payload, {"server.exe": b"new", "videos/keep.mp4": b"replace"})
        elsewhere = self.base / "recordings"; elsewhere.mkdir()
        (elsewhere / "keep.mp4").write_bytes(b"keep")
        try:
            (self.target / "videos").symlink_to(elsewhere, target_is_directory=True)
        except OSError:
            self.skipTest("Windows symlink privilege unavailable")
        (self.target / "update_exclude.txt").write_text("update_exclude.txt\nvideos/\n")
        UPDATER.overlay(self.payload, self.target)
        self.assertEqual((elsewhere / "keep.mp4").read_bytes(), b"keep")

    def test_wrapped_zip_and_checksum_validation(self):
        release(self.payload)
        archive = self.base / "good.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            for path in self.payload.rglob("*"):
                if path.is_file():
                    bundle.write(path, "version/" + path.relative_to(self.payload).as_posix())
        payload = UPDATER.extract_archive(archive, self.base / "out", "2.4.0")
        self.assertEqual(payload.name, "version")
        (payload / "server.exe").write_bytes(b"corrupted")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            UPDATER.validate_payload(payload, "2.4.0")

    def test_download_failure_restarts_old_app_without_overlay(self):
        (self.target / "server.exe").write_bytes(b"old")
        with patch.object(UPDATER, "remove_watchdog"), patch.object(UPDATER, "stop_processes"), \
             patch.object(UPDATER, "download", side_effect=OSError("offline")), \
             patch.object(UPDATER, "launch_application", return_value=123) as launch:
            result = UPDATER.run_update(self.target, "2.4.0", url="https://example.invalid/release.zip")
        self.assertEqual(result, 1)
        launch.assert_called_once_with(self.target)
        self.assertEqual((self.target / "server.exe").read_bytes(), b"old")
        self.assertIn("Original application restarted", (self.target / "logs/updater.log").read_text())

    def test_corrupt_payload_does_not_overwrite_old_app(self):
        release(self.payload)
        (self.payload / "server.exe").write_bytes(b"corrupt")
        (self.target / "server.exe").write_bytes(b"old")
        with patch.object(UPDATER, "remove_watchdog"), patch.object(UPDATER, "stop_processes"), \
             patch.object(UPDATER, "launch_application", return_value=123):
            result = UPDATER.run_update(self.target, "2.4.0", payload=self.payload)
        self.assertEqual(result, 1)
        self.assertEqual((self.target / "server.exe").read_bytes(), b"old")

    def test_copy_failure_is_failure_and_never_launches_or_reports_complete(self):
        release(self.payload)
        (self.target / "update_exclude.txt").write_text("update_exclude.txt\nlogs/\n")
        with patch.object(UPDATER, "remove_watchdog"), patch.object(UPDATER, "stop_processes"), \
             patch.object(UPDATER, "copy_with_retry", side_effect=OSError("Cannot overwrite server.exe")), \
             patch.object(UPDATER, "launch_application") as launch:
            result = UPDATER.run_update(self.target, "2.4.0", payload=self.payload)
        self.assertEqual(result, 1)
        launch.assert_not_called()
        self.assertNotIn("complete:", (self.target / "logs/updater.log").read_text())

    def test_updater_stops_before_downloading_then_overlays_and_restarts(self):
        release(self.payload)
        events = []
        def fake_download(url, archive):
            events.append("download")
        with patch.object(UPDATER, "remove_watchdog"), \
             patch.object(UPDATER, "stop_processes", side_effect=lambda *a: events.append("stop")), \
             patch.object(UPDATER, "download", side_effect=fake_download), \
             patch.object(UPDATER, "extract_archive", return_value=self.payload), \
             patch.object(UPDATER, "launch_application", side_effect=lambda *a: events.append("start") or 123):
            result = UPDATER.run_update(self.target, "2.4.0", url="https://example.invalid/release.zip")
        self.assertEqual(result, 0)
        self.assertEqual(events, ["stop", "download", "start"])
        self.assertEqual((self.target / "server.exe").read_bytes(), b"fake")
        self.assertFalse((self.target / ".watchdog-update.json").exists())

    def test_handoff_leaves_app_alive_and_keeps_token_out_of_argv(self):
        tree = ast.parse((ROOT / "1.py").read_text(encoding="utf-8-sig"))
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "apply_github_update")
        updater = self.target / "updater.exe"; updater.write_bytes(b"fake")
        popen = Mock(return_value=Mock(pid=123, poll=Mock(return_value=None)))
        namespace = dict(re=__import__("re"), os=os, tempfile=tempfile, shutil=shutil, sys=sys,
                         time=types.SimpleNamespace(sleep=lambda _: None),
                         threading=threading, subprocess=types.SimpleNamespace(Popen=popen, DEVNULL=subprocess.DEVNULL),
                         BASE_DIR=str(self.target), CONFIG={}, json=json, datetime=__import__("datetime").datetime, APP_VERSION="2.3.4", logger=logging.getLogger("test"),
                         _UPDATE_HANDOFF_LOCK=threading.Lock())
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "<actual handoff>", "exec"), namespace)
        with patch.object(os, "_exit") as exit_app:
            self.assertTrue(namespace["apply_github_update"]("https://example.invalid/zip", "2.4.0", token="fake-secret"))
        exit_app.assert_not_called()
        argv = popen.call_args.args[0]
        self.assertNotIn("fake-secret", " ".join(argv))
        self.assertEqual(popen.call_args.kwargs["env"]["CAMBIDA_UPDATE_TOKEN"], "fake-secret")
        self.assertTrue(Path(argv[0]).is_relative_to(self.base))
        self.assertFalse(Path(argv[0]).is_relative_to(self.target))
        self.assertFalse(namespace["apply_github_update"]("https://example.invalid/zip", "2.4.0"))

    def test_handoff_spawn_failure_releases_guard_without_stopping_app(self):
        tree = ast.parse((ROOT / "1.py").read_text(encoding="utf-8-sig"))
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "apply_github_update")
        (self.target / "updater.exe").write_bytes(b"fake")
        lock = threading.Lock()
        namespace = dict(re=__import__("re"), os=os, tempfile=tempfile, shutil=shutil, sys=sys,
                         time=time, subprocess=types.SimpleNamespace(Popen=Mock(side_effect=OSError("spawn failure")), DEVNULL=subprocess.DEVNULL),
                         BASE_DIR=str(self.target), CONFIG={}, json=json, datetime=__import__("datetime").datetime, APP_VERSION="2.3.4", logger=logging.getLogger("test"), _UPDATE_HANDOFF_LOCK=lock)
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "<actual handoff>", "exec"), namespace)
        with patch.object(os, "_exit") as exit_app, self.assertRaisesRegex(OSError, "spawn failure"):
            namespace["apply_github_update"]("https://example.invalid/zip", "2.4.0")
        self.assertFalse(lock.locked())
        exit_app.assert_not_called()

    @unittest.skipUnless(sys.platform == "win32", "Windows process isolation")
    def test_only_installation_processes_are_killed(self):
        other = self.base / "other app"; other.mkdir()
        owned_exe = self.target / "ffmpeg.exe"
        outside_exe = other / "ffmpeg.exe"
        cmd = Path(os.environ["WINDIR"]) / "System32/cmd.exe"
        shutil.copy2(cmd, owned_exe); shutil.copy2(cmd, outside_exe)
        processes = [subprocess.Popen([str(p), "/d", "/c", "ping -n 30 127.0.0.1 >nul"],
                                    creationflags=0x08000000) for p in (owned_exe, outside_exe)]
        try:
            UPDATER.stop_processes(self.target)
            self.assertIsNotNone(processes[0].poll())
            self.assertIsNone(processes[1].poll())
        finally:
            for process in processes:
                try:
                    parent = psutil.Process(process.pid)
                    children = parent.children(recursive=True)
                    for child in children: child.kill()
                    parent.kill()
                    psutil.wait_procs([parent, *children], timeout=5)
                except psutil.NoSuchProcess:
                    pass
                process.wait(timeout=5)

    def test_old_app_bootloader_is_stopped_but_updater_bootloader_survives(self):
        app = Mock(pid=110, exe=Mock(return_value=str(self.target / "Cambida.exe")))
        app.info = {"exe": str(self.target / "Cambida.exe")}
        runner = Mock(pid=210, exe=Mock(return_value=str(self.base / "runner/updater.exe")))
        runner.info = {"exe": str(self.base / "runner/updater.exe")}
        own = Mock(pid=211, parents=Mock(return_value=[runner, app]))
        app.children.return_value = [runner, own]
        with patch.object(UPDATER.psutil, "Process", return_value=own), \
             patch.object(UPDATER.psutil, "process_iter", return_value=[app, runner]), \
             patch.object(UPDATER.psutil, "wait_procs", return_value=([], [])), \
             patch.object(UPDATER, "legacy_watchdog_root", return_value=None):
            UPDATER.stop_processes(self.target, old_pid=110)
        app.kill.assert_called_once()
        runner.kill.assert_not_called()
        own.kill.assert_not_called()

    def test_watchdog_retirement_is_scoped_to_its_recorded_installation(self):
        local = self.base / "local"
        folder = local / "CambidaWatchdog"; folder.mkdir(parents=True)
        state = folder / "watchdog.json"
        with patch.dict(os.environ, {"LOCALAPPDATA": str(local)}):
            state.write_text(json.dumps({"target": str(self.base / "other shop")}))
            self.assertIsNone(UPDATER.legacy_watchdog_root(self.target))
            state.write_text(json.dumps({"target": str(self.target / "old test")}))
            self.assertEqual(UPDATER.legacy_watchdog_root(self.target), folder.resolve())

    @unittest.skipUnless(sys.platform == "win32", "Windows executable restart")
    def test_background_download_overlay_restart_using_real_runner(self):
        cmd = Path(os.environ["WINDIR"]) / "System32/cmd.exe"
        data = cmd.read_bytes()
        release(self.payload, {"server.exe": data, "config.json": b"replace", "new.bin": b"new"},
                args=["/d", "/c", "ping -n 30 127.0.0.1 >nul"])
        archive = self.base / "release.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            for path in self.payload.rglob("*"):
                if path.is_file(): bundle.write(path, path.relative_to(self.payload).as_posix())
        zip_bytes = archive.read_bytes()
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.send_header("Content-Length", str(len(zip_bytes)))
                self.end_headers(); self.wfile.write(zip_bytes)
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        (self.target / "config.json").write_bytes(b"keep")
        env = os.environ.copy(); env.update(LOCALAPPDATA=str(self.base / "isolated-local"), TEMP=str(self.base), TMP=str(self.base))
        runner = os.environ.get("CAMBIDA_TEST_UPDATER_EXE")
        command = [runner] if runner else [sys.executable, "-B", str(ROOT / "scripts/updater.py")]
        try:
            result = subprocess.run(command + ["--target", str(self.target), "--version", "2.4.0",
                                    "--url", f"http://127.0.0.1:{server.server_port}/release.zip"],
                                    env=env, capture_output=True, text=True, timeout=40,
                                    creationflags=0x08000000)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((self.target / "config.json").read_bytes(), b"keep")
            self.assertEqual((self.target / "new.bin").read_bytes(), b"new")
            text = (self.target / "logs/updater.log").read_text()
            self.assertIn("complete:", text)
            app_pid = int(text.rsplit("application PID ", 1)[1].splitlines()[0])
            self.assertTrue(psutil.pid_exists(app_pid))
        finally:
            server.shutdown();server.server_close();thread.join(timeout=2)
            for process in psutil.process_iter(["exe"]):
                try:
                    if process.info["exe"] and Path(process.info["exe"]).resolve() == (self.target / "server.exe").resolve():
                        children = process.children(recursive=True)
                        for child in children: child.kill()
                        process.kill(); psutil.wait_procs([process, *children], timeout=5)
                except psutil.NoSuchProcess: pass


    @unittest.skipUnless(sys.platform == "win32", "Windows path semantics")
    def test_filesystem_root_targets_are_recognised(self):
        self.assertTrue(UPDATER.is_filesystem_root("D:\\"))
        self.assertFalse(UPDATER.is_filesystem_root("D:\\Cambida"))
        self.assertFalse(UPDATER.is_filesystem_root(self.target))

    def test_drive_root_installation_accepts_staging_on_the_same_drive(self):
        # D:\ contains every path on the volume; a staging directory outside
        # the program files must not be rejected as "inside the installation".
        with patch.object(UPDATER, "remove_watchdog"), \
             patch.object(UPDATER, "stop_processes"), \
             patch.object(UPDATER, "log"), \
             patch.object(UPDATER, "read_exclusions", return_value=([], Path("policy"))), \
             patch.object(UPDATER, "validate_payload"), \
             patch.object(UPDATER, "overlay", return_value=0) as overlay, \
             patch.object(UPDATER, "launch_application", return_value=321), \
             patch.object(UPDATER.tempfile, "mkdtemp", return_value=str(self.base / "work")):
            result = UPDATER.run_update("D:\\", "2.4.0", payload=r"D:\Temp\payload")
        self.assertEqual(result, 0)
        overlay.assert_called_once()

    def test_payload_inside_a_subdirectory_installation_is_still_rejected(self):
        inside = self.target / "payload"
        inside.mkdir()
        with patch.object(UPDATER, "remove_watchdog"), \
             patch.object(UPDATER, "stop_processes"), \
             patch.object(UPDATER, "log"), \
             patch.object(UPDATER, "launch_application", return_value=321), \
             patch.object(UPDATER.tempfile, "mkdtemp", return_value=str(self.base / "work")):
            result = UPDATER.run_update(self.target, "2.4.0", payload=inside)
        self.assertEqual(result, 1)


if __name__ == "__main__":
    unittest.main()
