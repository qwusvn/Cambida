import importlib.util
import os
import tempfile
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location("cctv_timeline_app", os.path.join(ROOT, "1.py"))
APP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(APP)


class TimelineApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.old_video_dir = APP.VIDEO_DIR
        self.old_db_path = APP.DB_PATH
        self.old_camera_list = APP.CAMERA_LIST
        APP.VIDEO_DIR = self.temp.name
        APP.DB_PATH = os.path.join(self.temp.name, "analytics.db")
        APP.CAMERA_LIST = [{"name": "Bàn test 1"}, {"name": "Bàn test 2"}]
        APP.init_db()
        self.client = APP.app.test_client()

    def tearDown(self):
        APP.VIDEO_DIR = self.old_video_dir
        APP.DB_PATH = self.old_db_path
        APP.CAMERA_LIST = self.old_camera_list
        self.temp.cleanup()

    def touch(self, filename):
        with open(os.path.join(self.temp.name, filename), "wb") as handle:
            handle.write(b"not a real encoded video, but a real MP4 path")

    def test_timeline_page_renders_configured_cameras(self):
        response = self.client.get("/timeline")

        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("timelineContent", body)
        self.assertIn(r"B\u00e0n test 1", body)

    def test_valid_window_returns_only_intersecting_completed_clips(self):
        self.touch("cam1_09-50-00_to_10-05-00_(03-08-2026).mp4")
        self.touch("cam2_10-30-00_to_10-35-00_(03-08-2026).mp4")
        self.touch("cam1_11-00-00_to_11-05-00_(03-08-2026).mp4")
        self.touch("cam1_10-10-00_to_10-15-00_(03-08-2026).mp4.part")
        self.touch("cam1_invalid-name.mp4")
        os.makedirs(os.path.join(self.temp.name, "nested"))
        with open(
            os.path.join(self.temp.name, "nested", "cam1_10-20-00_to_10-25-00_(03-08-2026).mp4"),
            "wb",
        ) as handle:
            handle.write(b"nested file")

        response = self.client.get(
            "/api/timeline?start=2026-08-03T10:00:00&end=2026-08-03T11:00:00"
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["events"], [])
        self.assertEqual(
            [segment["filename"] for segment in payload["segments"]],
            [
                "cam1_09-50-00_to_10-05-00_(03-08-2026).mp4",
                "cam2_10-30-00_to_10-35-00_(03-08-2026).mp4",
            ],
        )
        first = payload["segments"][0]
        self.assertEqual(first["cam_id"], 1)
        self.assertEqual(first["started_at"], "2026-08-03T09:50:00")
        self.assertEqual(first["end_at"], "2026-08-03T10:05:00")
        self.assertEqual(first["duration_sec"], 900.0)
        self.assertEqual(first["status"], "complete")
        self.assertFalse(first["locked"])
        self.assertEqual(first["note"], "")

    def test_filename_end_crossing_midnight_is_used(self):
        filename = "cam1_23-58-00_to_00-03-00_(03-08-2026).mp4"
        self.touch(filename)

        response = self.client.get(
            "/api/timeline?start=2026-08-03T23:59:00&end=2026-08-04T00:04:00"
        )

        self.assertEqual(response.status_code, 200)
        segment = response.get_json()["segments"][0]
        self.assertEqual(segment["end_at"], "2026-08-04T00:03:00")
        self.assertEqual(segment["duration_sec"], 300.0)

    def test_timeline_uses_indexed_status_lock_note_and_events(self):
        filename = "cam1_10-00-00_to_10-05-00_(03-08-2026).mp4"
        self.touch(filename)
        APP.upsert_video_index(
            os.path.join(self.temp.name, filename), validate=False, known_duration=300
        )
        with APP.db_connection(APP.DB_PATH) as connection:
            connection.execute(
                "UPDATE video_segments SET status=?, locked=1, note=? WHERE filename=?",
                ("incomplete", "Thiếu đoạn cuối", filename),
            )
            connection.execute(
                "INSERT INTO video_events (filename, cam_id, event_time, label, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    filename,
                    1,
                    "2026-08-03T10:02:00",
                    "Phát hiện chuyển động",
                    "Có người tại bàn",
                    "2026-08-03T10:02:00",
                ),
            )
            connection.commit()

        response = self.client.get(
            "/api/timeline?start=2026-08-03T10:00&end=2026-08-03T10:10"
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["segments"][0]["status"], "incomplete")
        self.assertTrue(payload["segments"][0]["locked"])
        self.assertEqual(payload["segments"][0]["note"], "Thiếu đoạn cuối")
        self.assertEqual(payload["events"], [{
            "event_time": "2026-08-03T10:02:00",
            "label": "Phát hiện chuyển động",
            "note": "Có người tại bàn",
            "filename": filename,
            "cam_id": 1,
        }])

    def test_invalid_reversed_and_over_24_hour_windows_return_400(self):
        cases = (
            ("start=not-a-date&end=2026-08-03T11:00", "ISO"),
            ("start=2026-08-03T11:00&end=2026-08-03T10:00", "end"),
            ("start=2026-08-03T00:00&end=2026-08-04T00:01", "24"),
        )
        for query, expected_text in cases:
            with self.subTest(query=query):
                response = self.client.get(f"/api/timeline?{query}")
                self.assertEqual(response.status_code, 400)
                payload = response.get_json()
                self.assertFalse(payload["ok"])
                self.assertIn(expected_text, payload["error"])

    def test_invalid_indexed_paths_are_ignored(self):
        self.assertFalse(APP.safe_video_filename("..\\analytics.db"))
        with APP.db_connection(APP.DB_PATH) as connection:
            connection.execute(
                "INSERT INTO video_segments "
                "(filename, cam_id, started_at, ended_at, duration_sec, status, indexed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "../analytics.db",
                    1,
                    "2026-08-03T10:00:00",
                    "2026-08-03T10:05:00",
                    300,
                    "complete",
                    "2026-08-03T10:00:00",
                ),
            )
            connection.commit()
        response = self.client.get(
            "/api/timeline?start=2026-08-03T10:00&end=2026-08-03T10:10"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["segments"], [])


if __name__ == "__main__":
    unittest.main()
