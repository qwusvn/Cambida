import importlib.util
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location("cctv_go2rtc_app", os.path.join(ROOT, "1.py"))
APP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(APP)


class Go2rtcRoutesTests(unittest.TestCase):
    def setUp(self):
        self.old_camera_list = APP.CAMERA_LIST
        APP.CAMERA_LIST = [
            {"name": "Bàn test 1", "playback_source": "local", "sub_rtsp_url": "rtsp://test/1"},
            {"name": "Bàn test 2", "playback_source": "local", "sub_rtsp_url": "rtsp://test/2"},
        ]
        self.client = APP.app.test_client()

    def tearDown(self):
        APP.CAMERA_LIST = self.old_camera_list

    def test_go2rtc_status_endpoint(self):
        with patch("services.go2rtc_manager.get_go2rtc_status", return_value={"running": True, "healthy": True, "api_port": 1984}):
            res = self.client.get("/api/go2rtc/status")
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertTrue(data["running"])
            self.assertTrue(data["healthy"])

    def test_go2rtc_webrtc_proxy_missing_params(self):
        # Missing src
        res = self.client.post("/api/go2rtc/webrtc", data="v=0")
        self.assertEqual(res.status_code, 400)

        # Missing body
        res = self.client.post("/api/go2rtc/webrtc?src=cam1_sub")
        self.assertEqual(res.status_code, 400)

    def test_go2rtc_webrtc_proxy_invalid_camera(self):
        res = self.client.post("/api/go2rtc/webrtc?src=cam99_sub", data="v=0")
        self.assertEqual(res.status_code, 404)

    def test_arbitrary_sources_never_reach_sidecar(self):
        for src in ["rtsp://example.invalid/live", "cam1_extra", "cam0_sub", "cam1_sub?x=1", "exec:bad"]:
            with patch.object(APP.urllib.request, "urlopen") as upstream:
                res = self.client.post("/api/go2rtc/webrtc", query_string={"src": src}, data="v=0")
                self.assertEqual(res.status_code, 400)
                upstream.assert_not_called()

    def test_private_camera_never_reaches_sidecar(self):
        with patch.object(APP, "is_admin", return_value=False), \
             patch.object(APP, "is_camera_private", return_value=True), \
             patch.object(APP.urllib.request, "urlopen") as upstream:
            res = self.client.post("/api/go2rtc/webrtc?src=cam1_sub", data="v=0")
            self.assertEqual(res.status_code, 403)
            upstream.assert_not_called()

    def test_go2rtc_webrtc_proxy_success(self):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"v=0\r\nsdp_answer"
        mock_resp.__enter__.return_value = mock_resp

        with patch.object(APP.urllib.request, "urlopen", return_value=mock_resp):
            res = self.client.post("/api/go2rtc/webrtc?src=cam1_sub", data="v=0\r\nsdp_offer", content_type="text/plain")
            self.assertEqual(res.status_code, 200)
            self.assertIn(b"sdp_answer", res.data)

    def test_go2rtc_restart_requires_admin(self):
        res = self.client.post("/api/go2rtc/restart")
        self.assertEqual(res.status_code, 401)


if __name__ == "__main__":
    unittest.main()
