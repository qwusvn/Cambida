import importlib.util
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location("cctv_app", os.path.join(ROOT, "1.py"))
APP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(APP)


class CCTV20Tests(unittest.TestCase):
    def setUp(self):
        os.makedirs(os.path.join(ROOT, "test_data"), exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=os.path.join(ROOT, "test_data"))
        self.old_video_dir = APP.VIDEO_DIR
        self.old_db_path = APP.DB_PATH
        self.old_camera_list = APP.CAMERA_LIST
        APP.VIDEO_DIR = self.temp.name
        APP.DB_PATH = os.path.join(self.temp.name, "analytics.db")
        APP.CAMERA_LIST = [
            {"name": "Bàn 1", "playback_source": "local"},
            {"name": "Bàn 2", "playback_source": "local"},
        ]
        APP.init_db()

    def tearDown(self):
        APP.VIDEO_DIR = self.old_video_dir
        APP.DB_PATH = self.old_db_path
        APP.CAMERA_LIST = self.old_camera_list
        self.temp.cleanup()

    def test_parse_current_and_legacy_names(self):
        current = APP.parse_video_metadata("cam2_23-58-00_to_00-03-00_(03-08-2026).mp4", 300)
        self.assertEqual(current["cam_id"], 2)
        self.assertEqual(current["format"], "range")
        self.assertEqual((current["end"] - current["start"]).total_seconds(), 300)
        legacy = APP.parse_video_metadata("cam1_segment_2026-08-03_22-00-00.mp4", 299.5)
        self.assertEqual(legacy["cam_id"], 1)
        self.assertEqual(legacy["format"], "segment")
        self.assertAlmostEqual((legacy["end"] - legacy["start"]).total_seconds(), 299.5)
        self.assertIsNone(APP.parse_video_metadata("cam1_10-00-00_to_10-00-00_(03-08-2026).mp4"))

    def test_config_rejects_non_d_data_and_invalid_disk_limit(self):
        candidate = dict(APP.CONFIG)
        candidate["video_dir"] = r"C:\\cctv"
        self.assertIn("ổ D", APP.validate_config(candidate))
        candidate = dict(APP.CONFIG)
        candidate["disk_limit_gb"] = -1
        self.assertIn("lớn hơn 0", APP.validate_config(candidate))

    def test_index_exposes_legacy_video(self):
        filename = "cam1_segment_2026-08-03_22-00-00.mp4"
        path = os.path.join(self.temp.name, filename)
        with open(path, "wb") as handle:
            handle.write(b"test")
        indexed = APP.upsert_video_index(path, validate=False, known_duration=300)
        self.assertEqual(indexed["format"], "segment")
        with APP.app.test_client() as client:
            response = client.get("/list/cam1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()[0]["format"], "segment")

    def test_locked_video_is_protected_from_cleanup(self):
        filename = "cam1_10-00-00_to_10-05-00_(03-08-2026).mp4"
        path = os.path.join(self.temp.name, filename)
        with open(path, "wb") as handle:
            handle.write(b"test")
        APP.upsert_video_index(path, validate=False, known_duration=300)
        with APP.db_connection(APP.DB_PATH) as conn:
            conn.execute("UPDATE video_segments SET locked=1 WHERE filename=?", (filename,))
            conn.commit()
        self.assertTrue(APP.is_media_protected(path))

    def test_timeline_api_rejects_reversed_time(self):
        with APP.app.test_client() as client:
            response = client.get(
                "/api/timeline?start=2026-08-03T12:00&end=2026-08-03T11:00"
            )
        self.assertEqual(response.status_code, 400)

    def test_site_theme_restores_configured_segment_boundary_color(self):
        configured = {
            "site": {
                "name": "Cambida",
                "theme": {"segment_boundary": "#123456"},
            }
        }
        with patch.object(APP, "CONFIG", configured):
            site = APP.get_site_config()
        self.assertEqual(site["theme"]["segment_boundary"], "#123456")


if __name__ == "__main__":
    unittest.main()
