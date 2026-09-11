import importlib.util
import json
import os
import unittest
from unittest.mock import patch


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location("cctv_app", os.path.join(ROOT, "1.py"))
APP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(APP)


class CameraTestEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = APP.app.test_client()
        with open(os.path.join(ROOT, "config.json"), "r", encoding="utf-8") as f:
            self.config = json.load(f)
        self.nvr_config = self.config.get("playback_source", {}).get("nvr", {})

    def _auth_client(self):
        with self.client.session_transaction() as sess:
            sess["admin_authenticated"] = True

    def test_unauthenticated_request_is_rejected(self):
        response = self.client.post("/api/admin/test-camera", json={
            "camera": {"name": "Bàn 1", "playback_source": "local"}
        })
        self.assertEqual(response.status_code, 401)
        self.assertFalse(response.get_json()["ok"])

    def test_missing_camera_payload_returns_400(self):
        self._auth_client()
        response = self.client.post("/api/admin/test-camera", json={})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])
        self.assertIn("Thiếu cấu hình camera", response.get_json()["message"])

    def test_invalid_source_mode_returns_400(self):
        self._auth_client()
        response = self.client.post("/api/admin/test-camera", json={
            "camera": {"name": "Bàn 1", "playback_source": "invalid_mode"}
        })
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["ok"])
        self.assertIn("Local hoặc NVR", response.get_json()["message"])

    def test_local_mode_missing_credentials(self):
        self._auth_client()
        response = self.client.post("/api/admin/test-camera", json={
            "camera": {"name": "Bàn 1", "playback_source": "local", "ip": "192.168.1.208"}
        })
        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["mode"], "local")
        self.assertIn("cần IP, tài khoản và mật khẩu", payload["message"])
        self.assertIn("local", payload["sources"])
        self.assertFalse(payload["sources"]["local"]["ok"])

    def test_local_mode_tests_only_rtsp_config(self):
        self._auth_client()
        camera = {
            "name": "Bàn 1",
            "playback_source": "local",
            "ip": "192.168.1.208",
            "user": "admin",
            "pass": "121212qW",
            "port": 554,
        }
        with patch.object(APP, "test_camera_connection", return_value={"ok": True, "message": "Kết nối thành công"}):
            response = self.client.post("/api/admin/test-camera", json={"camera": camera})
            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["mode"], "local")
            self.assertIn("Kết nối thành công", payload["message"])
            self.assertIn("local", payload["sources"])
            self.assertNotIn("nvr", payload["sources"])

    def test_nvr_mode_requires_valid_channel(self):
        self._auth_client()
        response = self.client.post("/api/admin/test-camera", json={
            "camera": {
                "name": "Bàn 1",
                "playback_source": "nvr",
                "vendor": "dahua",
                "host": "192.168.1.100",
                "user": "admin",
                "pass": "123",
                "nvr_channel": 0,
            }
        })
        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertFalse(payload["ok"])
        self.assertIn("Kênh NVR phải từ 1 trở lên", payload["message"])

    def test_nvr_mode_tests_exact_card_and_channel(self):
        self._auth_client()
        camera = {
            "name": "Bàn 1",
            "playback_source": "nvr",
            "vendor": "dahua",
            "host": "192.168.1.100",
            "http_port": 81,
            "user": "admin",
            "pass": "123",
            "nvr_channel": 2,
        }
        with patch.object(APP, "test_nvr_connection", return_value={"ok": True, "message": "NVR OK"}):
            with patch.object(APP, "_search_dahua_camera", return_value=[]) as mock_search:
                response = self.client.post("/api/admin/test-camera", json={"camera": camera})
                self.assertEqual(response.status_code, 200)
                payload = response.get_json()
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["mode"], "nvr")
                self.assertEqual(payload["channel"], 2)
                self.assertIn("Kênh 2", payload["message"])
                self.assertIn("nvr", payload["sources"])
                self.assertNotIn("local", payload["sources"])
                mock_search.assert_called_once()
                self.assertEqual(mock_search.call_args[0][0], 2)

    def test_hybrid_mode_is_rejected(self):
        self._auth_client()
        camera = {
            "name": "Bàn 1",
            "playback_source": "hybrid",
            "ip": "192.168.1.208",
            "user": "admin",
            "pass": "121212qW",
            "port": 554,
            "nvr_channel": 1,
        }
        response = self.client.post("/api/admin/test-camera", json={"camera": camera})
        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertFalse(payload["ok"])
        self.assertIn("Local hoặc NVR", payload["message"])

    def test_nvr_mode_mock_fallback_on_unreachable_recorder(self):
        self._auth_client()
        camera = {
            "name": "Bàn 1",
            "playback_source": "nvr",
            "vendor": "hikvision",
            "host": "192.168.1.200",
            "http_port": 80,
            "user": "admin",
            "pass": "secret",
            "nvr_channel": 1,
        }
        with patch.object(APP, "test_nvr_connection", return_value={"ok": False, "message": "Không kết nối được"}):
            response = self.client.post("/api/admin/test-camera", json={"camera": camera})
            self.assertEqual(response.status_code, 400)
            payload = response.get_json()
            self.assertFalse(payload["ok"])
            self.assertIn("Không kết nối được", payload["message"])


if __name__ == "__main__":
    unittest.main()
