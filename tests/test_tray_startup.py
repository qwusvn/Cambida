import importlib.util
import os
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location("cctv_app_tray", os.path.join(ROOT, "1.py"))
APP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(APP)


class TrayStartupTests(unittest.TestCase):
    def test_frozen_build_always_enables_tray(self):
        with patch.object(APP.sys, "frozen", True, create=True):
            with patch.dict(APP.CONFIG, {"run_in_tray": "no"}, clear=False):
                self.assertTrue(APP._should_run_tray())

    def test_setup_tray_starts_detached_icon(self):
        fake_icon = unittest.mock.MagicMock()
        with patch.object(APP, "create_tray_icon", return_value=object()):
            with patch.object(APP.pystray, "Icon", return_value=fake_icon):
                result = APP.setup_tray()

        self.assertIs(result, fake_icon)
        fake_icon.run_detached.assert_called_once_with()

    def test_source_build_respects_run_in_tray_setting(self):
        with patch.object(APP.sys, "frozen", False, create=True):
            with patch.dict(APP.CONFIG, {"run_in_tray": "yes"}, clear=False):
                self.assertTrue(APP._should_run_tray())

            with patch.dict(APP.CONFIG, {"run_in_tray": "no"}, clear=False):
                self.assertFalse(APP._should_run_tray())


if __name__ == "__main__":
    unittest.main()
