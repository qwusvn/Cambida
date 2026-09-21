import importlib.util
import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch, MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location("cctv_app", os.path.join(ROOT, "1.py"))
APP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(APP)


class NvrPerCameraTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.old_video_dir = APP.VIDEO_DIR
        self.old_db_path = APP.DB_PATH
        self.old_camera_list = APP.CAMERA_LIST
        APP.VIDEO_DIR = self.temp.name
        APP.DB_PATH = os.path.join(self.temp.name, "analytics.db")
        APP.CAMERA_LIST = [
            {
                "name": "Bàn 1 (Local)",
                "playback_source": "local",
                "ip": "192.168.1.101",
                "user": "admin",
                "pass": "pass101",
                "port": 554,
                "record_path": "h264/ch1/main/av_stream",
                "preview_path": "h264/ch1/sub/av_stream",
            },
            {
                "name": "Bàn 2 (NVR no backup)",
                "playback_source": "nvr",
                "vendor": "dahua",
                "host": "192.168.1.200",
                "http_port": 81,
                "rtsp_port": 554,
                "user": "admin",
                "pass": "dahuaPass",
                "nvr_channel": 2,
                "stream": "main",
                "backup_local": False,
            },
            {
                "name": "Bàn 3 (NVR with backup)",
                "playback_source": "nvr",
                "vendor": "hikvision",
                "host": "192.168.1.201",
                "http_port": 80,
                "rtsp_port": 554,
                "user": "admin",
                "pass": "hikPass",
                "nvr_channel": 5,
                "stream": "main",
                "backup_local": True,
            },
        ]
        APP.init_db()
        self.client = APP.app.test_client()

    def tearDown(self):
        APP.VIDEO_DIR = self.old_video_dir
        APP.DB_PATH = self.old_db_path
        APP.CAMERA_LIST = self.old_camera_list
        self.temp.cleanup()

    def touch(self, filename):
        with open(os.path.join(self.temp.name, filename), "wb") as handle:
            handle.write(b"mp4 content")

    def test_legacy_hybrid_config_migrates_to_per_camera_nvr(self):
        legacy_raw = {
            "cameras": [
                {"name": "Cam 1", "ip": "192.168.1.10", "user": "u1", "pass": "p1"},
                {"name": "Cam 2", "ip": "192.168.1.11", "user": "u2", "pass": "p2"},
                {"name": "Cam 3", "ip": "192.168.1.12", "user": "u3", "pass": "p3", "playback_source": "hybrid"},
            ],
            "playback_source": {
                "mode": "hybrid",
                "nvr": {
                    "vendor": "dahua",
                    "host": "192.168.1.250",
                    "http_port": 81,
                    "rtsp_port": 554,
                    "username": "nvrUser",
                    "password": "nvrPass",
                    "stream": "main",
                    "channel_map": {"1": 1},
                },
            },
        }
        migrated = APP.migrate_config_data(legacy_raw)
        self.assertNotIn("playback_source", migrated)
        cams = migrated["cameras"]
        self.assertEqual(cams[0]["playback_source"], "nvr")
        self.assertTrue(cams[0]["backup_local"])
        self.assertEqual(cams[0]["vendor"], "dahua")
        self.assertEqual(cams[0]["host"], "192.168.1.250")
        self.assertEqual(cams[0]["user"], "nvrUser")
        self.assertEqual(cams[0]["pass"], "nvrPass")
        self.assertEqual(cams[0]["nvr_channel"], 1)
        self.assertEqual(cams[1]["playback_source"], "local")
        self.assertEqual(cams[2]["playback_source"], "nvr")
        self.assertTrue(cams[2]["backup_local"])

    def test_clean_config_for_saving_strips_nvr_from_local_and_direct_from_nvr(self):
        candidate = {
            "video_dir": "cctv_videos",
            "db_path": "analytics.db",
            "log_dir": "logs",
            "playback_source": {"mode": "nvr"},
            "retention_days": 30,
            "cameras": [
                {
                    "name": "Bàn 1",
                    "playback_source": "local",
                    "ip": "192.168.1.10",
                    "port": 554,
                    "user": "admin",
                    "pass": "123",
                    "record_path": "h264/ch1/main/av_stream",
                    "preview_path": "h264/ch1/sub/av_stream",
                    "nvr_channel": 1,
                    "vendor": "dahua",
                    "host": "192.168.1.200",
                    "backup_local": True,
                },
                {
                    "name": "Bàn 2",
                    "playback_source": "nvr",
                    "ip": "stale_ip",
                    "port": 999,
                    "host": "192.168.1.200",
                    "http_port": 81,
                    "rtsp_port": 554,
                    "vendor": "dahua",
                    "user": "admin",
                    "pass": "123",
                    "nvr_channel": 2,
                    "stream": "main",
                    "backup_local": True,
                },
            ],
        }
        cleaned = APP.clean_config_for_saving(candidate)
        self.assertNotIn("playback_source", cleaned)
        self.assertNotIn("retention_days", cleaned)

        local_cam = cleaned["cameras"][0]
        self.assertEqual(local_cam["playback_source"], "local")
        self.assertEqual(local_cam["vendor"], "dahua")
        self.assertNotIn("host", local_cam)
        self.assertNotIn("nvr_channel", local_cam)
        self.assertNotIn("backup_local", local_cam)
        self.assertEqual(local_cam["ip"], "192.168.1.10")

        nvr_cam = cleaned["cameras"][1]
        self.assertEqual(nvr_cam["playback_source"], "nvr")
        self.assertNotIn("ip", nvr_cam)
        self.assertNotIn("port", nvr_cam)
        self.assertEqual(nvr_cam["host"], "192.168.1.200")
        self.assertEqual(nvr_cam["nvr_channel"], 2)
        self.assertTrue(nvr_cam["backup_local"])

    def test_validate_config_rejects_hybrid_mode(self):
        candidate_root_hybrid = {
            "admin_auth": {"username": "admin", "password": "123"},
            "playback_source": {"mode": "hybrid"},
            "cameras": [],
        }
        err = APP.validate_config(candidate_root_hybrid)
        self.assertIn("Hybrid", err)

        candidate_cam_hybrid = {
            "admin_auth": {"username": "admin", "password": "123"},
            "cameras": [{"name": "Cam 1", "playback_source": "hybrid"}],
        }
        err = APP.validate_config(candidate_cam_hybrid)
        self.assertIn("Hybrid", err)

    def test_nvr_backup_worker_builds_rtsp_from_recorder_fields(self):
        dahua_cam = {
            "name": "Bàn test Dahua",
            "playback_source": "nvr",
            "vendor": "dahua",
            "host": "192.168.1.88",
            "rtsp_port": 554,
            "user": "dUser",
            "pass": "dPass",
            "nvr_channel": 4,
            "stream": "main",
            "ip": "stale.ip.1.1",
            "port": 9999,
        }
        url = APP.build_rtsp_url(dahua_cam, "record")
        self.assertIn("192.168.1.88:554", url)
        self.assertIn("dUser:dPass", url)
        self.assertIn("channel=4&subtype=0", url)
        self.assertNotIn("stale.ip.1.1", url)
        self.assertNotIn("9999", url)

        dahua_cam["stream"] = "sub"
        url_sub = APP.build_rtsp_url(dahua_cam, "record")
        self.assertIn("channel=4&subtype=1", url_sub)

        hik_cam = {
            "name": "Bàn test Hik",
            "playback_source": "nvr",
            "vendor": "hikvision",
            "host": "192.168.1.99",
            "rtsp_port": 1554,
            "user": "hUser",
            "pass": "hPass",
            "nvr_channel": 3,
            "stream": "main",
            "ip": "stale.ip.2.2",
        }
        url_hik = APP.build_rtsp_url(hik_cam, "record")
        self.assertIn("192.168.1.99:1554", url_hik)
        self.assertIn("hUser:hPass", url_hik)
        self.assertIn("/Streaming/Channels/301", url_hik)
        self.assertNotIn("stale.ip.2.2", url_hik)

        hik_cam["stream"] = "sub"
        url_hik_sub = APP.build_rtsp_url(hik_cam, "record")
        self.assertIn("/Streaming/Channels/302", url_hik_sub)

    def test_source_mode_records_nvr_only_without_local_backup(self):
        self.assertFalse(APP.RTSP_LOCAL_TIMELINE_ONLY)
        self.assertTrue(APP.should_record_locally({"playback_source": "local"}))
        self.assertFalse(APP.should_record_locally({"playback_source": "nvr", "backup_local": False}))
        self.assertTrue(APP.should_record_locally({"playback_source": "nvr", "backup_local": True}))

    def test_timeline_mode_follows_persistent_camera_source(self):
        self.assertEqual(APP.CAMERA_LIST[1]["playback_source"], "nvr")
        self.assertEqual(APP._timeline_playback_mode(2), "nvr")
        self.assertEqual(APP.CAMERA_LIST[1]["playback_source"], "nvr")

    def test_local_camera_replay_does_not_show_source_selector(self):
        response = self.client.get("/replay/cam1")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Bàn 1 (Local)", body)
        self.assertNotIn('id="srcServer1"', body)
        self.assertNotIn("Server 1 - NVR", body)

    def test_nvr_configured_camera_replay_uses_nvr_timeline(self):
        response = self.client.get("/replay/cam2")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Bàn 2 (NVR no backup)", body)
        self.assertNotIn('name="replaySource"', body)
        self.assertNotIn("Server 1 - NVR", body)
        self.assertNotIn("Server 2 - Local", body)
        self.assertIn('const CAMERA_MODE = "nvr";', body)

    def test_nvr_backup_camera_replay_keeps_nvr_as_primary_source(self):
        response = self.client.get("/replay/cam3")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Bàn 3 (NVR with backup)", body)
        self.assertNotIn('name="replaySource"', body)
        self.assertNotIn("Server 1 - NVR", body)
        self.assertNotIn("Server 2 - Local", body)
        self.assertIn('const CAMERA_MODE = "nvr";', body)

    def test_list_videos_allows_backup_local_but_keeps_nvr_isolated(self):
        filename = "cam3_10-00-00_to_10-05-00_(11-09-2026).mp4"
        other_day = "cam3_10-00-00_to_10-05-00_(12-09-2026).mp4"
        self.touch(filename)
        self.touch(other_day)

        res_local = self.client.get("/list/cam3?source=local&date=2026-09-11")
        self.assertEqual(res_local.status_code, 200)
        names = [item["name"] for item in res_local.get_json()]
        self.assertIn(filename, names)
        self.assertNotIn(other_day, names)

        segment = {
            "filename": "nvr_cam3_10-00-00",
            "play_url": "/nvr/video/token-3",
            "download_url": "/nvr/download/token-3",
            "started_at": "2026-09-11T10:00:00",
            "end_at": "2026-09-11T10:05:00",
            "duration_sec": 300,
        }
        with patch.object(APP, "_search_hikvision_camera", return_value=[segment]) as nvr_search:
            res_nvr = self.client.get("/list/cam3?source=nvr&date=2026-09-11")
        self.assertEqual(res_nvr.status_code, 200)
        nvr_search.assert_called_once()
        items = res_nvr.get_json()
        self.assertEqual([item["name"] for item in items], [segment["filename"]])
        self.assertEqual(items[0]["source"], "nvr")
        self.assertTrue(items[0]["url"].startswith("/nvr/video/"))

    def test_nvr_replay_page_keeps_timeline_only_ui(self):
        response = self.client.get("/replay/cam2")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn('const CAMERA_MODE = "nvr";', body)
        self.assertNotIn('type="time"', body)
        self.assertNotIn('name="replaySource"', body)
        self.assertNotIn("Server 1", body)
        self.assertNotIn("Server 2", body)
        self.assertNotIn("thumbnail", body.lower())
        self.assertIn('class="timeline-marker"', body)
        self.assertIn("left:50%", body)
        self.assertIn("background:var(--blue)", body)
        self.assertIn("function seekTimelineProgress", body)
        self.assertIn("pendingProgress", body)
        self.assertIn("clipStartAt", body)
        self.assertIn("selectionWidthMs", body)
        self.assertIn("const base=nvr?requested:(range?.start||requested);", body)
        self.assertIn('player.dataset.baseAt=base?formatLocalSecond(base):""', body)
        self.assertNotIn("clipStartSec", body)
        self.assertIn('renderTimelineCoverage("filmstrip")', body)
        self.assertIn('renderTimeline("replay",timelineStates.replay.progress)', body)
        self.assertIn('if($("filterDate").value)url+=', body)

    def test_prepare_nvr_merge_parts_starts_at_exact_requested_time(self):
        segment = {
            "started_at": "2026-09-12T22:00:00",
            "end_at": "2026-09-12T22:29:01",
            "play_url": "/nvr/video/token-2200",
        }
        nvr = {"vendor": "dahua", "playback_chunk_sec": 300}
        reference = {
            "vendor": "dahua",
            "cam_id": 2,
            "started_at": segment["started_at"],
            "ended_at": segment["end_at"],
        }
        with patch.object(APP, "get_camera_recorder_config", return_value=nvr), patch.object(
            APP, "_search_dahua_camera", return_value=[segment]
        ), patch.object(APP, "_get_nvr_reference", return_value=reference):
            _, parts, missing = APP._prepare_nvr_merge_parts(
                2,
                datetime.fromisoformat("2026-09-12T22:09:00"),
                datetime.fromisoformat("2026-09-12T22:19:00"),
            )
        self.assertEqual(missing, 0)
        self.assertEqual(len(parts), 2)
        self.assertEqual(parts[0]["start"], datetime.fromisoformat("2026-09-12T22:09:00"))
        self.assertEqual(parts[0]["end"], datetime.fromisoformat("2026-09-12T22:14:00"))
        self.assertEqual(parts[1]["start"], datetime.fromisoformat("2026-09-12T22:14:00"))
        self.assertEqual(parts[1]["end"], datetime.fromisoformat("2026-09-12T22:19:00"))

    def test_merge_route_uses_nvr_when_camera_source_is_nvr(self):
        with patch.object(APP, "_camera_playback_mode", return_value="nvr"), patch.object(
            APP, "_merge_nvr_response", return_value=APP.Response("nvr", status=200)
        ) as merge_nvr, patch.object(APP.glob, "glob", return_value=[]):
            response = self.client.get(
                "/merge?cam_id=2&start=2026-09-12T22:09:00&end=2026-09-12T22:10:00"
            )
        self.assertEqual(response.status_code, 200)
        merge_nvr.assert_called_once()
        self.assertEqual(response.get_data(as_text=True), "nvr")


if __name__ == "__main__":
    unittest.main()
