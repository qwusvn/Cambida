import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location("cctv_no_license_app", os.path.join(ROOT, "1.py"))
APP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(APP)


class NoLicenseGateTests(unittest.TestCase):
    def test_license_enforcement_is_removed(self):
        self.assertFalse(hasattr(APP, "LICENSE_ENFORCEMENT_ENABLED"))
        self.assertFalse(hasattr(APP, "get_machine_license_key"))
        self.assertFalse(hasattr(APP, "refresh_license_state"))

    def test_replay_is_not_blocked_by_license(self):
        original_camera_list = APP.CAMERA_LIST
        APP.CAMERA_LIST = [
            {
                "name": "Synthetic camera",
                "playback_source": "local",
                "local_transport": "rtsp",
                "ip": "127.0.0.1",
                "user": "unit-user",
                "pass": "unit-pass",
                "port": 554,
            }
        ]
        try:
            response = APP.app.test_client().get("/replay/cam1")
            self.assertEqual(response.status_code, 200)
        finally:
            APP.CAMERA_LIST = original_camera_list


if __name__ == "__main__":
    unittest.main()
