import importlib.util
import os
import unittest
from unittest.mock import patch


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location("cctv_app_launcher", os.path.join(ROOT, "1.py"))
APP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(APP)


class LauncherTests(unittest.TestCase):
    def test_packaged_launcher_starts_server_without_opening_browser(self):
        with open(os.path.join(ROOT, "CCTV_2.1.0.launcher.cmd"), encoding="utf-8") as handle:
            launcher = handle.read()
        self.assertIn(r"release\2.1.0\CCTV_2.1.0.exe", launcher)
        self.assertIn("call :port_ready", launcher)
        self.assertIn('if errorlevel 1 start "Cambida CCTV" "%EXE%"', launcher)
        self.assertNotIn('start "" "%URL%"', launcher)
        self.assertNotIn("open_browser", launcher)
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

    def test_second_process_does_not_open_browser_or_restart_server(self):
        with patch.object(APP, "_try_take_instance_mutex", return_value=False), \
                patch.object(APP, "_open_server_page", return_value=True) as open_page, \
                patch.object(APP, "_show_restart_prompt") as restart_prompt:
            self.assertFalse(APP.acquire_single_instance())
        open_page.assert_not_called()
        restart_prompt.assert_not_called()

    def test_tray_default_action_opens_gui_and_startup_does_not(self):
        menu = APP.create_tray_menu()
        self.assertTrue(menu.items[0].default)
        self.assertEqual(menu.items[0].text, "Mở Cambida")
        with patch.object(APP, "_open_server_page", return_value=True) as open_page:
            APP.on_open(None, None)
        open_page.assert_called_once_with()

        with open(os.path.join(ROOT, "1.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("_open_server_page(server_port)", source)


if __name__ == "__main__":
    unittest.main()
