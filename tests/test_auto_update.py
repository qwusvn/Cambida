import importlib.util
import json
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location("cctv_app", os.path.join(ROOT, "1.py"))
APP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(APP)


class AutoUpdateAndPrimaryLocalTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_config = dict(APP.CONFIG)
        self.old_camera_list = list(APP.CAMERA_LIST)
        self.old_base_dir = APP.BASE_DIR
        APP.BASE_DIR = self.temp_dir.name

    def tearDown(self):
        APP.CONFIG = self.old_config
        APP.CAMERA_LIST = self.old_camera_list
        APP.BASE_DIR = self.old_base_dir
        self.temp_dir.cleanup()

    def test_parse_version_tuple(self):
        self.assertEqual(APP._parse_version_tuple("v2.1.1"), (2, 1, 1))
        self.assertEqual(APP._parse_version_tuple("2.1.2"), (2, 1, 2))
        self.assertEqual(APP._parse_version_tuple("v3.0.0-beta"), (3, 0, 0))
        self.assertTrue(APP._parse_version_tuple("v2.1.2") > APP._parse_version_tuple("2.1.1"))
        self.assertFalse(APP._parse_version_tuple("2.1.1") > APP._parse_version_tuple("2.1.1"))
        self.assertFalse(APP._parse_version_tuple("2.1.0") > APP._parse_version_tuple("2.1.1"))

    def test_check_github_update_finds_newer_version(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "tag_name": "v2.1.2",
            "body": "Fixes and improvements",
            "assets": [
                {
                    "name": "CCTV_2.1.2.zip",
                    "browser_download_url": "https://github.com/qwusvn/Cambida/releases/download/v2.1.2/CCTV_2.1.2.zip"
                }
            ]
        }

        with patch("requests.get", return_value=mock_response):
            with patch.object(APP, "APP_VERSION", "2.1.1"):
                res = APP.check_github_update(repo="qwusvn/Cambida")
                self.assertTrue(res["has_update"])
                self.assertEqual(res["new_version"], "2.1.2")
                self.assertEqual(res["download_url"], "https://github.com/qwusvn/Cambida/releases/download/v2.1.2/CCTV_2.1.2.zip")

    def test_check_github_update_already_latest(self):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "tag_name": "v2.1.1",
            "assets": []
        }

        with patch("requests.get", return_value=mock_response):
            with patch.object(APP, "APP_VERSION", "2.1.1"):
                res = APP.check_github_update(repo="qwusvn/Cambida")
                self.assertFalse(res["has_update"])

    def test_check_and_notify_pending_update_sends_telegram_and_removes_marker(self):
        marker_path = os.path.join(self.temp_dir.name, ".pending_update_notification")
        with open(marker_path, "w", encoding="utf-8") as f:
            json.dump({
                "previous_version": "2.1.1",
                "new_version": "2.1.2",
                "updated_at": "22/09/2026 23:00:00"
            }, f)

        with patch.object(APP, "send_telegram_alert") as mock_telegram:
            APP.check_and_notify_pending_update()
            mock_telegram.assert_called_once()
            call_msg = mock_telegram.call_args[0][0]
            self.assertIn("TỰ ĐỘNG CẬP NHẬT THÀNH CÔNG", call_msg)
            self.assertIn("v2.1.2", call_msg)
            self.assertIn("v2.1.1", call_msg)

        # Ensure marker file was deleted
        self.assertFalse(os.path.exists(marker_path))

    def test_local_primary_with_nvr_backup_mode_and_recording(self):
        cam = {
            "name": "Bàn 1 (Local + NVR Backup)",
            "playback_source": "nvr",
            "vendor": "dahua",
            "host": "192.168.1.100",
            "user": "admin",
            "pass": "pass123",
            "nvr_channel": 1,
            "backup_local": True,
        }
        APP.CAMERA_LIST = [cam]

        # Camera has NVR
        self.assertTrue(APP.camera_has_nvr(cam))
        # Should record locally as primary
        self.assertTrue(APP.should_record_locally(cam))
        # Playback mode defaults to local (primary)
        self.assertEqual(APP._camera_playback_mode(1), "local")

    def test_replay_route_provides_has_nvr_flag(self):
        cam = {
            "name": "Bàn 1",
            "playback_source": "nvr",
            "vendor": "hikvision",
            "host": "192.168.1.200",
            "user": "admin",
            "pass": "pass123",
            "nvr_channel": 2,
            "backup_local": True,
        }
        APP.CAMERA_LIST = [cam]
        client = APP.app.test_client()
        resp = client.get("/replay/cam1")
        self.assertEqual(resp.status_code, 200)
        body = resp.get_data(as_text=True)
        # Verify source toggle UI exists for NVR-backed camera
        self.assertIn("sourceToggleBtn", body)
        self.assertIn("Local (Chính)", body)
        self.assertIn('value="local"', body)

    def test_api_status_includes_version(self):
        client = APP.app.test_client()
        with client.session_transaction() as sess:
            sess["admin_authenticated"] = True
        resp = client.get("/api/status")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("version", data)
        self.assertEqual(data["version"], APP.APP_VERSION)


if __name__ == "__main__":
    unittest.main()
