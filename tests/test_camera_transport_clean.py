import importlib.util
import json
import os
import unittest
from unittest.mock import patch


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location(
    "cambida_camera_transport_clean", os.path.join(ROOT, "1.py")
)
APP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(APP)


class CameraTransportCleanTests(unittest.TestCase):
    def setUp(self):
        self.client = APP.app.test_client()
        self.old_config = APP.CONFIG
        self.old_camera_list = APP.CAMERA_LIST

    def tearDown(self):
        APP.CONFIG = self.old_config
        APP.CAMERA_LIST = self.old_camera_list

    def _auth(self):
        with self.client.session_transaction() as session:
            session["admin_authenticated"] = True

    def _root(self, cameras=None, tables=None):
        return {
            "admin_auth": {"username": "admin", "password": "unit-test"},
            "server_port": 8004,
            "video_dir": "cctv_videos",
            "db_path": "analytics.db",
            "log_dir": "logs",
            "record_duration_sec": 300,
            "record_timeout_sec": 330,
            "cameras": [] if cameras is None else cameras,
            "tables": [] if tables is None else tables,
        }

    def _local(self, transport="rtsp", **extra):
        camera = {
            "name": "Unit camera",
            "playback_source": "local",
            "ip": "192.0.2.10",
            "user": "unit-user",
            "pass": "unit-pass",
            "local_transport": transport,
            "port": 554,
            "record_path": "h264/ch1/main/av_stream",
            "preview_path": "h264/ch1/sub/av_stream",
            "record_rtsp_url": "rtsp://192.0.2.10/stale-record",
            "preview_rtsp_url": "rtsp://192.0.2.10/stale-preview",
            "netsdk_port": 37777,
            "netsdk_channel": 1,
            "netsdk_stream": "main",
            "vendor": "dahua",
            "host": "stale-nvr.example",
            "nvr_channel": 9,
        }
        camera.update(extra)
        return camera

    def _nvr(self, **extra):
        camera = {
            "name": "Unit NVR camera",
            "playback_source": "nvr",
            "vendor": "dahua",
            "host": "nvr.example",
            "http_port": 81,
            "rtsp_port": 554,
            "user": "nvr-user",
            "pass": "nvr-pass",
            "nvr_channel": 2,
            "stream": "main",
            "backup_local": False,
            "ip": "stale-local.example",
            "port": 9999,
            "local_transport": "netsdk",
            "netsdk_port": 37777,
            "netsdk_channel": 3,
            "netsdk_stream": "sub",
            "record_path": "stale/local/main",
            "preview_path": "stale/local/sub",
            "record_rtsp_url": "rtsp://stale/local",
            "preview_rtsp_url": "rtsp://stale/local-sub",
            "username": "legacy-user",
            "password": "legacy-pass",
            "channel": 88,
            "nvr_vendor": "hikvision",
            "nvr": {"host": "stale-nested.example"},
        }
        camera.update(extra)
        return camera

    def test_migrate_prunes_stale_transport_fields_without_seeding_cameras(self):
        migrated = APP.migrate_config_data(self._root([self._local("netsdk")]))
        camera = migrated["cameras"][0]
        for key in (
            "port",
            "record_path",
            "preview_path",
            "record_rtsp_url",
            "preview_rtsp_url",
        ):
            self.assertNotIn(key, camera)
        self.assertEqual(camera["local_transport"], "netsdk")
        self.assertEqual(migrated["cameras"], [camera])

        empty = APP.migrate_config_data(self._root())
        self.assertEqual(empty["cameras"], [])
        self.assertEqual(empty["tables"], [])

    def test_clean_config_is_transport_disjoint(self):
        cleaned = APP.clean_config_for_saving(
            self._root([self._local("netsdk"), self._local("rtsp"), self._nvr()])
        )
        netsdk, rtsp, nvr = cleaned["cameras"]

        self.assertEqual(netsdk["local_transport"], "netsdk")
        self.assertEqual(netsdk["netsdk_port"], 37777)
        for key in (
            "port",
            "record_path",
            "preview_path",
            "record_rtsp_url",
            "preview_rtsp_url",
        ):
            self.assertNotIn(key, netsdk)

        self.assertEqual(rtsp["local_transport"], "rtsp")
        self.assertEqual(rtsp["port"], 554)
        for key in ("netsdk_port", "netsdk_channel", "netsdk_stream"):
            self.assertNotIn(key, rtsp)

        self.assertEqual(nvr["playback_source"], "nvr")
        for key in (
            "ip",
            "port",
            "local_transport",
            "netsdk_port",
            "netsdk_channel",
            "netsdk_stream",
            "record_path",
            "preview_path",
            "record_rtsp_url",
            "preview_rtsp_url",
            "username",
            "password",
            "channel",
            "nvr_vendor",
            "nvr",
        ):
            self.assertNotIn(key, nvr)

    def test_validate_rejects_mixed_transport_payloads(self):
        netsdk_error = APP.validate_config(self._root([self._local("netsdk")]))
        self.assertIsNotNone(netsdk_error)
        self.assertIn("RTSP", netsdk_error)

        rtsp_error = APP.validate_config(
            self._root([self._local("rtsp", netsdk_port=37777)])
        )
        self.assertIsNotNone(rtsp_error)
        self.assertIn("NetSDK", rtsp_error)

        nvr_error = APP.validate_config(self._root([self._nvr()]))
        self.assertIsNotNone(nvr_error)
        self.assertIn("Local", nvr_error)

        clean_netsdk = APP.clean_config_for_saving(self._root([self._local("netsdk")]))["cameras"][0]
        clean_netsdk["port"] = ""
        self.assertIsNotNone(APP.validate_config(self._root([clean_netsdk])))

        clean_rtsp = APP.clean_config_for_saving(self._root([self._local("rtsp")]))["cameras"][0]
        clean_rtsp["netsdk_port"] = ""
        self.assertIsNotNone(APP.validate_config(self._root([clean_rtsp])))

        clean_nvr = APP.clean_config_for_saving(self._root([self._nvr()]))["cameras"][0]
        clean_nvr["ip"] = ""
        self.assertIsNotNone(APP.validate_config(self._root([clean_nvr])))

    def test_admin_put_add_then_remove_ends_with_zero_channels(self):
        self._auth()
        local_camera = APP.clean_config_for_saving(
            self._root([self._local("rtsp")])
        )["cameras"][0]
        candidate = self._root(
            [local_camera],
            [{"id": "ban-01", "name": "Unit camera", "camera_id": 1}],
        )

        try:
            with patch.object(APP, "save_config") as save_config:
                added = self.client.put("/api/admin/config", json=candidate)
                self.assertEqual(added.status_code, 200)
                self.assertEqual(len(APP.CAMERA_LIST), 1)
                self.assertEqual(save_config.call_args.args[0]["cameras"][0]["playback_source"], "local")

                removed = self.client.put(
                    "/api/admin/config",
                    json=self._root(),
                )
                self.assertEqual(removed.status_code, 200)
                self.assertEqual(APP.CONFIG["cameras"], [])
                self.assertEqual(APP.CONFIG["tables"], [])
                self.assertEqual(APP.CAMERA_LIST, [])
                self.assertEqual(save_config.call_args.args[0]["cameras"], [])
                self.assertEqual(save_config.call_args.args[0]["tables"], [])
        finally:
            APP.CONFIG = self.old_config
            APP.CAMERA_LIST = self.old_camera_list

    def test_admin_get_zero_channels_does_not_create_demo_camera(self):
        self._auth()
        APP.CONFIG = self._root()
        APP.CAMERA_LIST = []
        response = self.client.get("/api/admin/config")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["cameras"], [])
        self.assertEqual(payload["tables"], [])

    def test_nvr_replay_and_list_follow_nvr_source(self):
        APP.CAMERA_LIST = [self._nvr()]
        self.assertEqual(APP._timeline_playback_mode(1), "nvr")

        segment = {
            "filename": "nvr-segment",
            "play_url": "/nvr/video/token",
            "download_url": "/nvr/download/token",
            "started_at": "2026-09-19T10:00:00",
            "end_at": "2026-09-19T10:05:00",
            "duration_sec": 300,
        }
        with patch.object(APP, "_search_dahua_camera", return_value=[segment]) as search:
            response = self.client.get("/list/cam1?source=nvr&date=2026-09-19")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()[0]["source"], "nvr")
        search.assert_called_once()

        replay = self.client.get("/replay/cam1")
        self.assertEqual(replay.status_code, 200)
        self.assertIn('const CAMERA_MODE = "nvr";', replay.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
