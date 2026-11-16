import importlib.util
import os
import threading
import unittest
from unittest.mock import Mock, patch


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location("cctv_app_launcher", os.path.join(ROOT, "1.py"))
APP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(APP)


class LauncherTests(unittest.TestCase):
    def test_packaged_double_click_launcher_reuses_port_before_starting_exe(self):
        launchers = [
            ("CCTV_2.1.0.launcher.cmd", r"release\2.1.0\CCTV_2.1.0.exe"),
            ("CCTV_2.1.1.launcher.cmd", r"release\2.1.1\CCTV_2.1.1.exe"),
        ]
        if os.path.exists(os.path.join(ROOT, "CCTV_2.1.2.launcher.cmd")):
            launchers.append(("CCTV_2.1.2.launcher.cmd", r"release\2.1.2\CCTV_2.1.2.exe"))

        for launcher_file, expected_exe in launchers:
            if not os.path.exists(os.path.join(ROOT, launcher_file)):
                continue
            with open(os.path.join(ROOT, launcher_file), encoding="utf-8") as handle:
                launcher = handle.read()
            self.assertIn(expected_exe, launcher)
            self.assertIn("call :port_ready", launcher)
            self.assertTrue(
                'if errorlevel 1 start "Cambida CCTV" "%EXE%"' in launcher
                or 'if errorlevel 1 start "Cambida CCTV" /D "%EXE_DIR%" "%EXE%"' in launcher
            )
            self.assertIn('start "" "%URL%"', launcher)
            self.assertNotIn("taskkill", launcher.lower())

    def test_open_server_page_waits_then_uses_default_browser_reuse_mode(self):
        calls = []

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        def connector(address, timeout):
            calls.append((address, timeout))
            return Connection()

        def opener(url, **kwargs):
            calls.append((url, kwargs))
            return True

        self.assertTrue(APP._open_server_page(8004, opener=opener, connector=connector))
        self.assertEqual(calls[0][0], ("127.0.0.1", 8004))
        self.assertEqual(calls[1], ("http://127.0.0.1:8004/", {"new": 0, "autoraise": True}))

    def test_second_double_click_reuses_existing_server_without_restart(self):
        with patch.object(APP, "_try_take_instance_mutex", return_value=False), \
                patch.object(APP, "_open_server_page", return_value=True) as open_page, \
                patch.object(APP.ctypes.windll.user32, "MessageBoxW") as restart_prompt:
            self.assertFalse(APP.acquire_single_instance())
        open_page.assert_called_once_with()
        restart_prompt.assert_not_called()


    def test_start_windows_integration_starts_sleep_blocker_and_firewall_worker(self):
        called = threading.Event()
        with patch.object(APP, "start_keep_awake") as keep, \
                patch.object(APP, "ensure_firewall_exception", side_effect=lambda *a, **k: called.set()):
            APP._start_windows_integration()
        keep.assert_called_once()
        self.assertTrue(called.wait(2), "firewall worker did not run")

    def test_firewall_worker_targets_the_frozen_executable(self):
        with patch.object(APP.sys, "frozen", True, create=True), \
                patch.object(APP, "ensure_firewall_exception") as ensure:
            APP._ensure_firewall_exception()
        self.assertEqual(ensure.call_args.args[0], APP.sys.executable)
        self.assertEqual(ensure.call_args.kwargs["base_dir"], APP.BASE_DIR)

    def test_firewall_worker_skips_source_mode(self):
        with patch.object(APP.sys, "frozen", False, create=True), \
                patch.object(APP, "ensure_firewall_exception") as ensure:
            APP._ensure_firewall_exception()
        self.assertIsNone(ensure.call_args.args[0])

    def test_quit_releases_the_sleep_blocker_before_exit(self):
        icon = Mock()
        with patch.object(APP, "prompt_exit_password", return_value=True), \
                patch.object(APP, "_stop_all_recordings"), \
                patch.object(APP, "stop_cloudflared_tunnel"), \
                patch("services.go2rtc_manager.stop_go2rtc"), \
                patch.object(APP, "stop_keep_awake") as stop_awake, \
                patch.object(APP, "_release_single_instance"), \
                patch.object(APP.os, "_exit") as exit_app:
            APP.on_quit(icon, None)
        stop_awake.assert_called_once()
        exit_app.assert_called_once_with(0)

    def test_rejected_cancelled_or_failed_password_never_stops_application(self):
        for result in (False, None, RuntimeError("dialog unavailable")):
            with self.subTest(result=result), \
                    patch.object(APP, "prompt_exit_password") as prompt, \
                    patch.object(APP, "_quit_application") as quit_app:
                if isinstance(result, Exception):
                    prompt.side_effect = result
                else:
                    prompt.return_value = result
                APP.on_quit(Mock(), None)
                quit_app.assert_not_called()
                self.assertFalse(APP._QUIT_LOCK.locked())

    def test_only_one_exit_dialog_can_be_open(self):
        with patch.object(APP, "prompt_exit_password") as prompt:
            APP._QUIT_LOCK.acquire()
            try:
                APP.on_quit(Mock(), None)
            finally:
                APP._QUIT_LOCK.release()
        prompt.assert_not_called()

    def test_autostart_targets_native_exe_even_when_legacy_launcher_exists(self):
        registry = Mock()
        registry.HKEY_CURRENT_USER = 1
        registry.KEY_SET_VALUE = 2
        registry.REG_SZ = 1
        registry.DeleteValue.side_effect = FileNotFoundError
        with patch.object(APP, "winreg", registry), \
                patch.object(APP.sys, "frozen", True, create=True), \
                patch.object(APP.sys, "executable", r"C:\Program Files\Cambida\Cambida.exe"), \
                patch.object(APP.os.path, "isfile", return_value=True):
            APP.set_autostart(True)
        self.assertEqual(registry.SetValueEx.call_args.args[-1],
                         '"C:\\Program Files\\Cambida\\Cambida.exe" --no-browser')
        registry.CloseKey.assert_called_once_with(registry.CreateKeyEx.return_value)

    def test_source_autostart_includes_interpreter_and_absolute_script(self):
        registry = Mock()
        registry.DeleteValue.side_effect = FileNotFoundError
        with patch.object(APP, "winreg", registry), \
                patch.object(APP.sys, "frozen", False, create=True), \
                patch.object(APP.os.path, "isfile", return_value=False):
            APP.set_autostart(True)
        expected = APP.subprocess.list2cmdline([
            APP.sys.executable, os.path.abspath(APP.__file__), "--no-browser",
        ])
        self.assertEqual(registry.SetValueEx.call_args.args[-1], expected)

    def test_autostart_closes_registry_handle_after_write_error(self):
        registry = Mock()
        registry.SetValueEx.side_effect = OSError("access denied")
        with patch.object(APP, "winreg", registry):
            APP.set_autostart(True)
        registry.CloseKey.assert_called_once_with(registry.CreateKeyEx.return_value)

    def test_source_autostart_can_be_disabled_without_setting_a_value(self):
        registry = Mock()
        registry.DeleteValue.side_effect = FileNotFoundError
        with patch.object(APP, "winreg", registry):
            APP.set_autostart(False)
        registry.SetValueEx.assert_not_called()
        registry.DeleteValue.assert_any_call(registry.CreateKeyEx.return_value, "CameraHighlight")
        registry.CloseKey.assert_called_once_with(registry.CreateKeyEx.return_value)

    def test_duplicate_autostart_does_not_open_browser(self):
        with patch.object(APP.sys, "argv", ["Cambida.exe", "--no-browser"]), \
                patch.object(APP, "_try_take_instance_mutex", return_value=False), \
                patch.object(APP, "_open_server_page") as open_page:
            self.assertFalse(APP.acquire_single_instance())
        open_page.assert_not_called()

    def test_current_server_startup_is_headless_and_does_not_open_browser(self):
        for launcher_file in ("Chay_CCTV.cmd", "run.cmd"):
            with open(os.path.join(ROOT, launcher_file), encoding="utf-8") as handle:
                launcher = handle.read().lower()
            self.assertNotIn('start "" "%url%"', launcher)
            self.assertNotIn("mo trinh duyet", launcher)

        with open(os.path.join(ROOT, "1.py"), encoding="utf-8") as handle:
            source = handle.read()
        main_source = source[source.index("def main():"):]
        self.assertNotIn("_delayed_open", main_source)
        self.assertNotIn("_open_server_page(server_port)", main_source)


if __name__ == "__main__":
    unittest.main()
