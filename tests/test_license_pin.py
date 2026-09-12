import importlib.util
import os
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location("cctv_license_app", os.path.join(ROOT, "1.py"))
APP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(APP)


class TelegramPinnedLicenseTests(unittest.TestCase):
    def setUp(self):
        self.old_enabled = APP.LICENSE_ENFORCEMENT_ENABLED
        self.old_state = APP.get_license_snapshot()
        self.client = APP.app.test_client()

    def tearDown(self):
        APP.LICENSE_ENFORCEMENT_ENABLED = self.old_enabled
        with APP.LICENSE_LOCK:
            APP.LICENSE_STATE.clear()
            APP.LICENSE_STATE.update(self.old_state)

    def test_machine_guid_key_uses_stable_legacy_style_hex(self):
        with patch.object(APP, "_read_windows_machine_guid", return_value="ABC-123"):
            key = APP.get_machine_license_key()
        self.assertRegex(key, r"^[0-9A-F]{16}$")

    def test_pinned_message_exact_key_match(self):
        key = "0123456789ABCDEF"
        self.assertTrue(APP._pinned_message_has_key(f"KHACH A\n{key}\nACTIVE", key))
        self.assertFalse(APP._pinned_message_has_key(f"X{key}Y", key))
        self.assertFalse(APP._pinned_message_has_key("FEDCBA9876543210", key))

    def test_refresh_license_state_uses_only_machine_key_and_pin(self):
        key = "0123456789ABCDEF"
        with patch.object(APP, "get_machine_license_key", return_value=key), patch.object(
            APP, "_telegram_pinned_text", return_value=f"Danh sach key\n{key}"
        ):
            self.assertTrue(APP.refresh_license_state())
            self.assertTrue(APP.get_license_snapshot()["active"])
        with patch.object(APP, "get_machine_license_key", return_value=key), patch.object(
            APP, "_telegram_pinned_text", return_value="Danh sach key khac"
        ):
            self.assertFalse(APP.refresh_license_state())
            self.assertFalse(APP.get_license_snapshot()["active"])

    def test_replay_is_blocked_but_home_is_not_when_unlicensed(self):
        APP.LICENSE_ENFORCEMENT_ENABLED = True
        with APP.LICENSE_LOCK:
            APP.LICENSE_STATE.update(
                active=False,
                key="0123456789ABCDEF",
                reason="Key chưa có trong tin ghim.",
            )
        replay = self.client.get("/replay/cam1")
        self.assertEqual(replay.status_code, 403)
        self.assertIn("0123456789ABCDEF", replay.get_data(as_text=True))
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)

    def test_replay_passes_gate_when_licensed(self):
        APP.LICENSE_ENFORCEMENT_ENABLED = True
        with APP.LICENSE_LOCK:
            APP.LICENSE_STATE.update(active=True, key="0123456789ABCDEF", reason="ok")
        response = self.client.get("/replay/cam1")
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
