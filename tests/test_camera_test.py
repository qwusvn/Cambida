import importlib.util
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location("cctv_app", os.path.join(ROOT, "1.py"))
APP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(APP)


class CameraTestEndpointTests(unittest.TestCase):
    def setUp(self):
        self.client = APP.app.test_client()
        with APP.RTSP_PROFILE_LOCK:
            self.profile_cache = dict(APP.RTSP_PROFILE_CACHE)
            APP.RTSP_PROFILE_CACHE.clear()
        with open(os.path.join(ROOT, "config.json"), "r", encoding="utf-8") as f:
            self.config = json.load(f)
        self.nvr_config = self.config.get("playback_source", {}).get("nvr", {})

    def tearDown(self):
        with APP.RTSP_PROFILE_LOCK:
            APP.RTSP_PROFILE_CACHE.clear()
            APP.RTSP_PROFILE_CACHE.update(self.profile_cache)

    def _local_camera(self, **overrides):
        camera = {
            "name": "Imou test",
            "playback_source": "local",
            "ip": "192.168.1.50",
            "port": 554,
            "user": "admin",
            "pass": "Safety@Code",
        }
        camera.update(overrides)
        return camera

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

    def test_imou_main_and_sub_urls_use_channel_and_subtype(self):
        camera = self._local_camera(
            vendor="imou",
            rtsp_channel=7,
            user="cam@user",
            **{"pass": "p:a/b?&"},
        )

        main_url = APP.build_rtsp_url(camera, "record")
        sub_url = APP.build_rtsp_url(camera, "preview")

        self.assertEqual(
            main_url,
            "rtsp://cam%40user:p%3Aa%2Fb%3F%26@192.168.1.50:554/"
            "cam/realmonitor?channel=7&subtype=0",
        )
        self.assertEqual(
            sub_url,
            "rtsp://cam%40user:p%3Aa%2Fb%3F%26@192.168.1.50:554/"
            "cam/realmonitor?channel=7&subtype=1",
        )
        candidates = APP.get_rtsp_candidates(camera, "record")
        self.assertIn("&unicast=true&proto=Onvif", candidates[1]["url"])

    def test_legacy_and_custom_paths_are_preserved_as_candidates(self):
        legacy_camera = self._local_camera()
        legacy_candidates = APP.get_rtsp_candidates(legacy_camera, "record")
        legacy_url = next(item["url"] for item in legacy_candidates if item["profile"] == "legacy")
        self.assertIn("/h264/ch1/main/av_stream?timeout=20000000", legacy_url)
        self.assertTrue(any(item["profile"] == "imou" for item in legacy_candidates))

        custom_camera = self._local_camera(
            record_path="custom/main?profile=low-latency",
            preview_path="custom/sub",
        )
        custom_url = APP.build_rtsp_url(custom_camera, "record")
        self.assertIn("/custom/main?profile=low-latency&timeout=20000000", custom_url)
        self.assertEqual(APP.get_rtsp_candidates(custom_camera, "record")[0]["profile"], "configured")
        self.assertTrue(
            any("/h264/ch1/main/av_stream?timeout=20000000" in item["url"]
                for item in APP.get_rtsp_candidates(custom_camera, "record"))
        )

    def test_connection_falls_back_and_caches_successful_profile_for_preview(self):
        camera = self._local_camera()
        results = [
            SimpleNamespace(returncode=1, stderr="404 Not Found"),
            SimpleNamespace(returncode=0, stderr=""),
        ]
        with patch.object(APP.os.path, "exists", return_value=True), patch.object(
            APP.subprocess, "run", side_effect=results
        ) as mock_run:
            result = APP.test_camera_connection(camera)

        self.assertTrue(result["ok"])
        self.assertEqual(result["profile"], "imou_onvif")
        self.assertNotIn("url", result)
        self.assertEqual(mock_run.call_count, 2)
        first_url = mock_run.call_args_list[0].args[0][mock_run.call_args_list[0].args[0].index("-i") + 1]
        second_url = mock_run.call_args_list[1].args[0][mock_run.call_args_list[1].args[0].index("-i") + 1]
        self.assertIn("subtype=0", first_url)
        self.assertNotIn("timeout=", first_url)
        self.assertIn("unicast=true&proto=Onvif", second_url)
        self.assertNotIn("timeout=", second_url)
        self.assertIn("unicast=true&proto=Onvif", APP.build_rtsp_url(camera, "preview"))
        self.assertIn("-map", mock_run.call_args_list[0].args[0])
        map_index = mock_run.call_args_list[0].args[0].index("-map")
        self.assertEqual(mock_run.call_args_list[0].args[0][map_index + 1], "0:v:0")

    def test_recording_fallback_remembers_working_profile(self):
        camera = self._local_camera()
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            old_video_dir = APP.VIDEO_DIR
            APP.VIDEO_DIR = temp_dir
            stop_event = _StopAfterOneWait()

            def fake_attempt(_cam_id, _url, temp_path, _stop_event):
                if not fake_attempt.failed:
                    fake_attempt.failed = True
                    return -1, "404 Not Found", False
                with open(temp_path, "wb") as handle:
                    handle.write(b"video")
                return 0, "", False

            fake_attempt.failed = False
            try:
                with patch.object(APP, "_run_recording_attempt", side_effect=fake_attempt) as mock_attempt, patch.object(
                    APP, "upsert_video_index"
                ):
                    APP.record_camera(1, camera, stop_event)
            finally:
                APP.VIDEO_DIR = old_video_dir

        self.assertEqual(mock_attempt.call_count, 2)
        self.assertEqual(APP._get_cached_rtsp_profile(camera), "imou_onvif")

    def test_explicit_direct_url_is_authoritative_and_not_fallback_probed(self):
        direct_url = "rtsp://admin:secret@192.168.1.50:554/direct/live"
        camera = self._local_camera(record_rtsp_url=direct_url)
        with patch.object(APP.os.path, "exists", return_value=True), patch.object(
            APP.subprocess, "run", return_value=SimpleNamespace(returncode=0, stderr="")
        ) as mock_run:
            result = APP.test_camera_connection(camera)

        self.assertTrue(result["ok"])
        self.assertEqual(result["profile"], "direct")
        self.assertEqual(APP.build_rtsp_url(camera, "record"), direct_url)
        self.assertEqual(mock_run.call_count, 1)
        self.assertEqual(mock_run.call_args.args[0][mock_run.call_args.args[0].index("-i") + 1], direct_url)
        self.assertNotIn("secret", json.dumps(result))

    def test_imou_auth_error_is_sanitized_and_mentions_device_safety_code(self):
        secret = "device-secret-123"
        camera = self._local_camera(vendor="imou", **{"pass": secret})
        with patch.object(APP.os.path, "exists", return_value=True), patch.object(
            APP.subprocess, "run", return_value=SimpleNamespace(returncode=1, stderr="401 Unauthorized")
        ) as mock_run:
            result = APP.test_camera_connection(camera)

        self.assertFalse(result["ok"])
        self.assertIn("Safety Code", result["message"])
        self.assertIn("không phải mật khẩu tài khoản Imou", result["message"])
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("rtsp://", serialized)
        self.assertEqual(mock_run.call_count, 1)

    def test_remote_rtsp_reset_is_classified_and_short_circuited(self):
        camera = self._local_camera(vendor="imou")
        with patch.object(APP.os.path, "exists", return_value=True), patch.object(
            APP.subprocess,
            "run",
            return_value=SimpleNamespace(
                returncode=-1094995529,
                stderr="Failed reading RTSP data: Error number -10054 occurred",
            ),
        ) as mock_run:
            result = APP.test_camera_connection(camera)

        self.assertFalse(result["ok"])
        self.assertIn("reset phiên RTSP", result["message"])
        self.assertIn("port 554", result["message"])
        self.assertEqual(mock_run.call_count, 1)

    def test_network_unreachable_does_not_probe_every_path(self):
        camera = self._local_camera()
        with patch.object(APP.os.path, "exists", return_value=True), patch.object(
            APP.subprocess, "run", return_value=SimpleNamespace(returncode=1, stderr="Connection refused")
        ) as mock_run:
            result = APP.test_camera_connection(camera)

        self.assertFalse(result["ok"])
        self.assertIn("không truy cập được IP/port", result["message"])
        self.assertEqual(mock_run.call_count, 1)

    def test_nvr_rtsp_generation_remains_single_unchanged_url(self):
        camera = {
            "playback_source": "nvr",
            "vendor": "dahua",
            "host": "nvr.example",
            "rtsp_port": 8554,
            "user": "nvr-user",
            "pass": "nvr-pass",
            "nvr_channel": 4,
            "stream": "sub",
            "record_path": "must-not-be-used",
            "record_rtsp_url": "rtsp://stale-local-override.invalid/should-not-win",
        }
        url = APP.build_rtsp_url(camera, "record")
        candidates = APP.get_rtsp_candidates(camera, "record")

        self.assertEqual(
            url,
            "rtsp://nvr-user:nvr-pass@nvr.example:8554/"
            "cam/realmonitor?channel=4&subtype=1",
        )
        self.assertEqual(candidates, [{"profile": "nvr", "url": url}])


    def test_cache_invalidates_when_local_profile_configuration_changes(self):
        camera = self._local_camera(vendor="imou", record_path="h264/ch1/main/av_stream")
        APP._remember_rtsp_profile(camera, "imou_onvif")
        self.assertEqual(APP._get_cached_rtsp_profile(camera), "imou_onvif")

        changed = dict(camera)
        changed["record_path"] = "custom/new-main"
        self.assertIsNone(APP._get_cached_rtsp_profile(changed))

    def test_custom_path_timeout_is_inserted_before_fragment_and_not_duplicated(self):
        camera = self._local_camera(record_path="custom/main?profile=low#frag")
        url = APP.build_rtsp_url(camera, "record")
        self.assertIn("/custom/main?profile=low&timeout=20000000#frag", url)

        camera["record_path"] = "custom/main?timeout=123&profile=low#frag"
        url = APP.build_rtsp_url(camera, "record")
        self.assertEqual(url.count("timeout="), 1)
        self.assertIn("?timeout=123&profile=low#frag", url)

    def test_local_netsdk_uses_37777_adapter_not_rtsp_probe(self):
        self._auth_client()
        camera = self._local_camera(
            local_transport="netsdk",
            netsdk_port=37777,
            netsdk_channel=1,
            netsdk_stream="main",
        )
        probe_result = SimpleNamespace(
            ok=True,
            media_ok=True,
            message="NetSDK 37777 da nhan duoc luong video.",
            channels=1,
            sdk_error=0,
        )
        fake_adapter = SimpleNamespace(probe=lambda **_kwargs: probe_result)
        with patch.object(APP.Dahua37777Adapter, "from_camera", return_value=fake_adapter) as mock_sdk, \
             patch.object(APP, "get_rtsp_candidates") as mock_rtsp:
            response = self.client.post("/api/admin/test-camera", json={"camera": camera})
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["sources"]["local"]["transport"], "netsdk")
        self.assertEqual(payload["sources"]["local"]["profile"], "netsdk37777")
        mock_sdk.assert_called_once()
        mock_rtsp.assert_not_called()

    def test_netsdk_snapshot_route_uses_private_adapter_not_rtsp(self):
        camera = self._local_camera(
            local_transport="netsdk",
            netsdk_port=37777,
            netsdk_channel=1,
            netsdk_stream="sub",
        )
        old_cameras = APP.CAMERA_LIST
        APP.CAMERA_LIST = [camera]
        fake_adapter = SimpleNamespace(capture_jpeg=lambda *_args, **_kwargs: b"\xff\xd8jpeg\xff\xd9")
        try:
            with patch.object(APP.Dahua37777Adapter, "from_camera", return_value=fake_adapter) as mock_sdk, \
                 patch.object(APP.cv2, "VideoCapture") as mock_rtsp:
                response = self.client.get("/snapshot/cam1?stream=sub")
        finally:
            APP.CAMERA_LIST = old_cameras
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/jpeg")
        self.assertEqual(response.data, b"\xff\xd8jpeg\xff\xd9")
        mock_sdk.assert_called_once()
        mock_rtsp.assert_not_called()

    def test_netsdk_live_route_uses_private_frame_generator(self):
        camera = self._local_camera(local_transport="netsdk", netsdk_port=37777)
        old_cameras = APP.CAMERA_LIST
        APP.CAMERA_LIST = [camera]
        multipart = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n\xff\xd8x\xff\xd9\r\n"
        try:
            with patch.object(APP, "gen_netsdk_frames", return_value=iter([multipart])) as mock_frames, \
                 patch.object(APP, "gen_frames") as mock_rtsp:
                response = self.client.get("/cam1?stream=main")
        finally:
            APP.CAMERA_LIST = old_cameras
        self.assertEqual(response.status_code, 200)
        self.assertIn(multipart, response.data)
        mock_frames.assert_called_once()
        mock_rtsp.assert_not_called()

    def test_validate_config_accepts_explicit_netsdk_37777_local_camera(self):
        candidate = dict(self.config)
        netsdk_camera = self._local_camera(
            local_transport="netsdk",
            netsdk_port=37777,
            netsdk_channel=1,
            netsdk_stream="main",
        )
        netsdk_camera.pop("port", None)
        candidate["cameras"] = [netsdk_camera]
        candidate["tables"] = []
        self.assertIsNone(APP.validate_config(candidate))

    def test_recording_errors_are_redacted_before_alerts(self):
        secret = "SafetySecret!"
        camera = self._local_camera(vendor="imou", **{"pass": secret})
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            old_video_dir = APP.VIDEO_DIR
            APP.VIDEO_DIR = temp_dir
            APP.CAM_LAST_ERROR.pop(99, None)
            stop_event = _StopAfterOneWait()
            try:
                with patch.object(
                    APP,
                    "_run_recording_attempt",
                    return_value=(-1, f"rtsp://admin:{secret}@192.168.1.50:554/live 401 Unauthorized", False),
                ), patch.object(APP, "send_telegram_alert") as mock_alert:
                    APP.record_camera(99, camera, stop_event)
            finally:
                APP.VIDEO_DIR = old_video_dir

        self.assertIn(99, APP.CAM_LAST_ERROR)
        self.assertNotIn(secret, APP.CAM_LAST_ERROR[99])
        self.assertNotIn("admin:", APP.CAM_LAST_ERROR[99])
        mock_alert.assert_called_once()
        self.assertNotIn(secret, mock_alert.call_args.args[0])
        self.assertNotIn("admin:", mock_alert.call_args.args[0])



class _StopAfterOneWait:
    def __init__(self):
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, _timeout):
        self.stopped = True
        return True


if __name__ == "__main__":
    unittest.main()
