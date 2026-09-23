import importlib.util
import os
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location("cctv_license_app", os.path.join(ROOT, "1.py"))
APP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(APP)


class HardDriveLicenseGateTests(unittest.TestCase):
    def setUp(self):
        self.old_enabled = APP.LICENSE_ENFORCEMENT_ENABLED
        self.old_state = APP.get_license_snapshot()
        self.client = APP.app.test_client()

    def tearDown(self):
        APP.LICENSE_ENFORCEMENT_ENABLED = self.old_enabled
        with APP.LICENSE_LOCK:
            APP.LICENSE_STATE.clear()
            APP.LICENSE_STATE.update(self.old_state)

    def test_drive_volume_serial_key(self):
        with patch.object(APP, "_get_drive_volume_serial", return_value="00E1-1D9A"):
            key = APP.get_machine_license_key()
        self.assertEqual(key, "00E1-1D9A")

    def test_guid_fallback_when_drive_serial_unavailable(self):
        with patch.object(APP, "_get_drive_volume_serial", return_value=None), patch.object(
            APP, "_read_windows_machine_guid", return_value="ABC-123"
        ):
            key = APP.get_machine_license_key()
        self.assertRegex(key, r"^[0-9A-F]{16}$")

    def test_pinned_message_exact_and_no_dash_match(self):
        key = "00E1-1D9A"
        # Match with dash
        self.assertTrue(APP._pinned_message_has_key(f"KHACH A\n{key}\nACTIVE", key))
        # Match without dash
        self.assertTrue(APP._pinned_message_has_key("KHACH A\n00E11D9A\nACTIVE", key))
        # Case insensitive
        self.assertTrue(APP._pinned_message_has_key("khach a 00e1-1d9a active", key))
        # Not substring match
        self.assertFalse(APP._pinned_message_has_key(f"PREFIX{key}SUFFIX", key))
        self.assertFalse(APP._pinned_message_has_key("PREFIX00E11D9ASUFFIX", key))

    def test_record_license_activation_idempotent(self):
        test_key = "TEST-VOL-9999"
        first = APP.record_license_activation(test_key)
        self.assertIsNotNone(first)
        second = APP.record_license_activation(test_key)
        self.assertEqual(first, second)
        self.assertEqual(APP.get_license_activated_at(test_key), first)

    def test_refresh_license_state_records_activation_on_first_match(self):
        key = "00E1-1D9A"
        with patch.object(APP, "get_machine_license_key", return_value=key), patch.object(
            APP, "_telegram_pinned_text", return_value=f"Danh sach key\n{key}"
        ), patch.object(APP, "send_telegram_alert"):
            self.assertTrue(APP.refresh_license_state())
            snapshot = APP.get_license_snapshot()
            self.assertTrue(snapshot["active"])
            self.assertEqual(snapshot["key"], key)
            self.assertIsNotNone(snapshot["activated_at"])

    def test_unlicensed_allows_home_admin_and_replay_ui_but_gates_media_and_merge(self):
        APP.LICENSE_ENFORCEMENT_ENABLED = True
        with APP.LICENSE_LOCK:
            APP.LICENSE_STATE.update(
                active=False,
                key="00E1-1D9A",
                activated_at=None,
                reason="Mã ổ cứng chưa có trong tin nhắn ghim Telegram.",
            )

        # Trang chủ truy cập bình thường
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)

        # Trang admin truy cập bình thường (hoặc redirect nếu chưa đăng nhập)
        admin = self.client.get("/admin")
        self.assertIn(admin.status_code, (200, 302))

        # Trang xem lại truy cập được UI, nhưng có cờ LICENSE_ACTIVE = false và hiện mã kích hoạt
        replay = self.client.get("/replay/cam1")
        self.assertEqual(replay.status_code, 200)
        content = replay.get_data(as_text=True)
        self.assertIn("LICENSE_ACTIVE = false", content)
        self.assertIn("00E1-1D9A", content)
        self.assertIn("licenseModal", content)

        # Media xem lại và merge bị chặn 403
        video_res = self.client.get("/video/test.mp4")
        self.assertEqual(video_res.status_code, 403)
        self.assertFalse(video_res.get_json()["ok"])
        self.assertEqual(video_res.get_json()["license_key"], "00E1-1D9A")

        merge_res = self.client.get("/merge")
        self.assertEqual(merge_res.status_code, 403)
        self.assertIn("00E1-1D9A", merge_res.get_data(as_text=True))

        # API license status
        status_res = self.client.get("/api/license/status")
        self.assertEqual(status_res.status_code, 200)
        data = status_res.get_json()
        self.assertTrue(data["ok"])
        self.assertFalse(data["active"])
        self.assertEqual(data["license_key"], "00E1-1D9A")

    def test_licensed_state_allows_replay_and_media(self):
        APP.LICENSE_ENFORCEMENT_ENABLED = True
        with APP.LICENSE_LOCK:
            APP.LICENSE_STATE.update(
                active=True,
                key="00E1-1D9A",
                activated_at="23/09/2026 15:00:00",
                reason="Hợp lệ",
            )
        replay = self.client.get("/replay/cam1")
        self.assertEqual(replay.status_code, 200)
        self.assertIn("LICENSE_ACTIVE = true", replay.get_data(as_text=True))

        status_res = self.client.get("/api/license/status")
        self.assertEqual(status_res.status_code, 200)
        self.assertTrue(status_res.get_json()["active"])


if __name__ == "__main__":
    unittest.main()
