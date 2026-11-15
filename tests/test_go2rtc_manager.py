import os
import sys
import unittest
from unittest.mock import MagicMock, patch

from services import go2rtc_manager


class Go2rtcManagerTests(unittest.TestCase):
    def test_generate_go2rtc_yaml_with_cameras(self):
        cameras = [
            {"id": 1, "sub_rtsp_url": "rtsp://user:pass@192.168.1.10:554/sub", "main_rtsp_url": "rtsp://user:pass@192.168.1.10:554/main"},
            {"id": 2, "rtsp_url": "rtsp://user:pass@192.168.1.11:554/live"},
        ]
        yaml_text = go2rtc_manager.generate_go2rtc_yaml(cameras, api_port=1984, rtsp_port=8554, webrtc_port=8555)

        self.assertIn('listen: "127.0.0.1:1984"', yaml_text)
        self.assertIn('listen: "127.0.0.1:8554"', yaml_text)
        self.assertIn('listen: ":8555"', yaml_text)
        self.assertIn('cam1_sub: "rtsp://user:pass@192.168.1.10:554/sub"', yaml_text)
        self.assertIn('cam1_main: "rtsp://user:pass@192.168.1.10:554/main"', yaml_text)
        self.assertIn('cam2: "rtsp://user:pass@192.168.1.11:554/live"', yaml_text)

    def test_generate_go2rtc_yaml_with_custom_builder(self):
        cameras = [{"name": "Table 1"}]
        def fake_builder(cam, stream):
            return f"rtsp://nvr.local/cam1/{stream}"

        yaml_text = go2rtc_manager.generate_go2rtc_yaml(cameras, rtsp_url_builder=fake_builder)
        self.assertIn('cam1_sub: "rtsp://nvr.local/cam1/sub"', yaml_text)
        self.assertIn('cam1_main: "rtsp://nvr.local/cam1/main"', yaml_text)

    def test_find_go2rtc_binary(self):
        with patch("os.path.isfile", side_effect=lambda p: "mock_go2rtc" in p):
            found = go2rtc_manager.find_go2rtc_binary(base_dir="C:/app/mock_go2rtc")
            self.assertIsNotNone(found)

    def test_start_and_stop_go2rtc_lifecycle(self):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.pid = 12345

        with patch.object(go2rtc_manager, "find_go2rtc_binary", return_value="C:/app/go2rtc.exe"), \
             patch("builtins.open", unittest.mock.mock_open()), \
             patch("subprocess.Popen", return_value=mock_proc):

            started = go2rtc_manager.start_go2rtc([{"rtsp_url": "rtsp://test"}])
            self.assertTrue(started)

            status = go2rtc_manager.get_go2rtc_status()
            self.assertTrue(status["running"])
            self.assertEqual(status["pid"], 12345)

            go2rtc_manager.stop_go2rtc()
            mock_proc.terminate.assert_called_once()
            self.assertIsNone(go2rtc_manager._go2rtc_proc)


if __name__ == "__main__":
    unittest.main()
