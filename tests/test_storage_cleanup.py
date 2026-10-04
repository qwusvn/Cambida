import importlib.util
import os
import tempfile
import time
import unittest


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
SPEC = importlib.util.spec_from_file_location("cctv_storage_app", os.path.join(ROOT, "1.py"))
APP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(APP)


class StorageCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT)
        self.old_video_dir = APP.VIDEO_DIR
        self.old_db_path = APP.DB_PATH
        self.old_config = dict(APP.CONFIG)
        APP.VIDEO_DIR = self.temp.name
        APP.DB_PATH = os.path.join(self.temp.name, "analytics.db")
        APP.init_db()

    def tearDown(self):
        APP.VIDEO_DIR = self.old_video_dir
        APP.DB_PATH = self.old_db_path
        APP.CONFIG = self.old_config
        self.temp.cleanup()

    def _create_file(self, filename, size_bytes=1024, age_seconds=0):
        path = os.path.join(self.temp.name, filename)
        with open(path, "wb") as f:
            f.write(b"0" * size_bytes)
        mtime = time.time() - age_seconds
        os.utime(path, (mtime, mtime))
        return path

    def test_partition_usage_and_effective_limit(self):
        total, used, free = APP.get_disk_partition_usage(self.temp.name)
        self.assertGreater(total, 0)
        self.assertGreater(free, 0)

        # Test configured limit
        APP.CONFIG["disk_limit_gb"] = 25.5
        APP.CONFIG["minimum_free_disk_gb"] = 5.0
        limit = APP.get_effective_disk_limit_gb()
        self.assertEqual(limit, 25.5)

        # Test auto limit
        APP.CONFIG["disk_limit_gb"] = "auto"
        auto_limit = APP.get_effective_disk_limit_gb()
        self.assertGreater(auto_limit, 0)

    def test_cleanup_orphan_temp_files(self):
        # Old orphan files (> 30 min)
        old_part = self._create_file("cam1_old.mp4.part", 2048, age_seconds=3600)
        old_dav = self._create_file("cam1_old.mp4.part.netsdk.dav", 2048, age_seconds=3600)
        old_ps = self._create_file("cam1_old.mp4.part.hik.ps", 2048, age_seconds=3600)
        old_tmp = self._create_file(".tmp_junk.tmp", 1024, age_seconds=3600)

        # Active recording files (< 5 min)
        active_part = self._create_file("cam1_active.mp4.part", 2048, age_seconds=60)
        active_dav = self._create_file("cam1_active.mp4.part.netsdk.dav", 2048, age_seconds=60)

        freed = APP.cleanup_orphan_temp_files(min_age_seconds=1800)
        self.assertEqual(freed, 2048 + 2048 + 2048 + 1024)

        # Old files must be deleted
        self.assertFalse(os.path.exists(old_part))
        self.assertFalse(os.path.exists(old_dav))
        self.assertFalse(os.path.exists(old_ps))
        self.assertFalse(os.path.exists(old_tmp))

        # Active recording files must be preserved!
        self.assertTrue(os.path.exists(active_part))
        self.assertTrue(os.path.exists(active_dav))

    def test_delete_expired_merged_videos(self):
        old_merge = self._create_file("merge_cam1_abc.mp4", 4096, age_seconds=4 * 3600)
        recent_merge = self._create_file("merge_cam1_recent.mp4", 4096, age_seconds=3600)
        regular_video = self._create_file("cam1_00-00-00_to_00-05-00_(01-01-2026).mp4", 4096, age_seconds=4 * 3600)

        APP.delete_expired_merged_videos(max_age_seconds=3 * 3600)

        self.assertFalse(os.path.exists(old_merge))
        self.assertTrue(os.path.exists(recent_merge))
        self.assertTrue(os.path.exists(regular_video))

    def test_delete_expired_retention_videos(self):
        APP.CONFIG["retention_days"] = 7
        expired_file = "cam1_10-00-00_to_10-05-00_(01-01-2026).mp4"
        valid_file = "cam1_10-00-00_to_10-05-00_(01-10-2026).mp4"

        p_exp = self._create_file(expired_file, 1024, age_seconds=8 * 86400)
        p_val = self._create_file(valid_file, 1024, age_seconds=2 * 86400)

        APP.delete_expired_videos()

        self.assertFalse(os.path.exists(p_exp))
        self.assertTrue(os.path.exists(p_val))

    def test_locked_video_is_protected_from_deletion(self):
        APP.CONFIG["retention_days"] = 7
        locked_name = "cam1_10-00-00_to_10-05-00_(01-01-2026).mp4"
        p_locked = self._create_file(locked_name, 2048, age_seconds=10 * 86400)

        # Mark as locked in DB
        with APP.db_connection() as conn:
            APP._ensure_db_schema(conn)
            conn.execute(
                "INSERT INTO video_segments (filename, locked, indexed_at) VALUES (?, 1, datetime('now'))",
                (locked_name,)
            )
            conn.commit()

        APP.delete_expired_videos()

        # The locked video must NOT be deleted even though it is 10 days old!
        self.assertTrue(os.path.exists(p_locked))

    def test_adaptive_disk_cleanup_fifo_order(self):
        # Create 3 regular videos of different ages (10 MB each = 30 MB total)
        f1 = "cam1_01-00-00_to_01-05-00_(01-10-2026).mp4"
        f2 = "cam1_02-00-00_to_02-05-00_(01-10-2026).mp4"
        f3 = "cam1_03-00-00_to_03-05-00_(01-10-2026).mp4"

        p1 = self._create_file(f1, 10 * 1024 * 1024, age_seconds=7200) # oldest
        p2 = self._create_file(f2, 10 * 1024 * 1024, age_seconds=3600)
        p3 = self._create_file(f3, 10 * 1024 * 1024, age_seconds=1800) # newest

        # Set limit to 20 MB (0.02 GB) so it only needs to delete enough to reach 20 MB
        APP.CONFIG["disk_limit_gb"] = 0.020
        APP.CONFIG["minimum_free_disk_gb"] = 0.0

        APP.run_adaptive_disk_cleanup()

        # Oldest file (p1) must be deleted first
        self.assertFalse(os.path.exists(p1))
        # Newest file (p3) should remain
        self.assertTrue(os.path.exists(p3))


if __name__ == "__main__":
    unittest.main()
