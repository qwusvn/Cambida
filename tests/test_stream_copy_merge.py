import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location("cctv_app", os.path.join(ROOT, "1.py"))
APP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(APP)


class TestStreamCopyMerge(unittest.TestCase):
    def setUp(self):
        self.client = APP.app.test_client()
        self.test_dir = tempfile.mkdtemp(prefix="test_cambida_stream_copy_")
        self.orig_video_dir = APP.VIDEO_DIR
        APP.VIDEO_DIR = self.test_dir

    def tearDown(self):
        APP.VIDEO_DIR = self.orig_video_dir
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_materialize_nvr_merge_part_uses_stream_copy_and_faststart(self):
        part = {
            "vendor": "hikvision",
            "token": "tok123",
            "reference": {"segment_start": "2026-09-21T10:00:00"},
            "segment_start": datetime.fromisoformat("2026-09-21T10:00:00"),
            "start": datetime.fromisoformat("2026-09-21T10:01:00"),
            "end": datetime.fromisoformat("2026-09-21T10:03:00"),
        }
        nvr = {"vendor": "hikvision", "read_timeout_sec": 30}
        target_path = os.path.join(self.test_dir, "part_test.mp4")

        with patch.object(APP, "_download_hikvision_reference", return_value="fake_hik.mp4"), \
             patch("subprocess.run") as mock_run, \
             patch("os.path.isfile", return_value=True), \
             patch("os.path.getsize", return_value=1024):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            dur = APP._materialize_nvr_merge_part(part, nvr, target_path)
            self.assertEqual(dur, 120.0)
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]

            # Verify stream-copy flags
            self.assertIn("-c", cmd)
            self.assertEqual(cmd[cmd.index("-c") + 1], "copy")
            self.assertIn("+faststart", cmd)
            # Ensure no re-encoding codecs
            self.assertNotIn("libx264", cmd)
            self.assertNotIn("aac", cmd)
            # Verify offset and duration
            self.assertIn("-ss", cmd)
            self.assertEqual(cmd[cmd.index("-ss") + 1], "60.000")
            self.assertIn("-t", cmd)
            self.assertEqual(cmd[cmd.index("-t") + 1], "120.000")
            # Verify audio/no-audio map
            self.assertIn("-map", cmd)
            self.assertIn("0:v:0", cmd)
            self.assertIn("0:a?", cmd)

    def test_materialize_nvr_merge_part_audio_fallback(self):
        part = {
            "vendor": "dahua",
            "token": "tok456",
            "reference": {},
            "segment_start": datetime.fromisoformat("2026-09-21T10:00:00"),
            "start": datetime.fromisoformat("2026-09-21T10:00:00"),
            "end": datetime.fromisoformat("2026-09-21T10:01:00"),
        }
        nvr = {"vendor": "dahua", "read_timeout_sec": 30}
        target_path = os.path.join(self.test_dir, "part_fallback.mp4")

        with patch.object(APP, "_download_dahua_reference", return_value="fake_dahua.mp4"), \
             patch("subprocess.run") as mock_run, \
             patch("os.path.isfile", return_value=True), \
             patch("os.path.getsize", return_value=1024):
            # First call fails (e.g. incompatible audio in stream copy)
            fail_res = MagicMock(returncode=1, stdout="Audio codec error", stderr="Error muxing audio")
            # Second call (fallback with -an) succeeds
            succ_res = MagicMock(returncode=0, stdout="", stderr="")
            mock_run.side_effect = [fail_res, succ_res]

            dur = APP._materialize_nvr_merge_part(part, nvr, target_path)
            self.assertEqual(dur, 60.0)
            self.assertEqual(mock_run.call_count, 2)

            fallback_cmd = mock_run.call_args_list[1][0][0]
            self.assertIn("-an", fallback_cmd)
            self.assertIn("-c:v", fallback_cmd)
            self.assertEqual(fallback_cmd[fallback_cmd.index("-c:v") + 1], "copy")
            self.assertNotIn("libx264", fallback_cmd)

    def test_nvr_merge_response_sse_gap_notice_and_concat(self):
        nvr = {"vendor": "hikvision"}
        parts = [
            {
                "vendor": "hikvision",
                "token": "tok1",
                "reference": {},
                "segment_start": datetime.fromisoformat("2026-09-21T10:00:00"),
                "start": datetime.fromisoformat("2026-09-21T10:00:00"),
                "end": datetime.fromisoformat("2026-09-21T10:01:00"),
            },
            {
                "vendor": "hikvision",
                "token": "tok2",
                "reference": {},
                "segment_start": datetime.fromisoformat("2026-09-21T10:02:00"),
                "start": datetime.fromisoformat("2026-09-21T10:02:00"),
                "end": datetime.fromisoformat("2026-09-21T10:03:00"),
            },
        ]
        # missing_duration = 60s gap
        missing_duration = 60.0

        def fake_materialize(part, nvr_cfg, target_path):
            with open(target_path, "wb") as f:
                f.write(b"dummy_mp4_content")
            return 60.0

        def fake_run(cmd, **kwargs):
            out_file = cmd[-1]
            with open(out_file, "wb") as f:
                f.write(b"merged_output")
            return MagicMock(returncode=0, stdout="")

        with patch.object(APP, "_prepare_nvr_merge_parts", return_value=(nvr, parts, missing_duration)), \
             patch.object(APP, "_materialize_nvr_merge_part", side_effect=fake_materialize), \
             patch("subprocess.run", side_effect=fake_run) as mock_run, \
             self.client.application.test_request_context():

            res = APP._merge_nvr_response(
                1,
                datetime.fromisoformat("2026-09-21T10:00:00"),
                datetime.fromisoformat("2026-09-21T10:03:00"),
            )
            events = "".join(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk for chunk in res.response)

            # Check gap notice preserved
            self.assertIn("data: notice:Khoảng đã chọn có 60 giây không có bản ghi NVR;", events)
            # Check SSE progress steps
            self.assertIn("data: 5\n\n", events)
            self.assertIn("data: 95\n\n", events)
            self.assertIn("data: 100\n\n", events)
            self.assertIn("data: done:merge_cam1_", events)

            # Check concat command used -c copy
            concat_cmd = mock_run.call_args[0][0]
            self.assertIn("concat", concat_cmd)
            self.assertIn("-c", concat_cmd)
            self.assertEqual(concat_cmd[concat_cmd.index("-c") + 1], "copy")
            self.assertIn("+faststart", concat_cmd)
            self.assertNotIn("libx264", concat_cmd)

    def test_local_merge_route_stream_copy_and_cleanup(self):
        f1 = os.path.join(self.test_dir, "cam1_10-00-00_to_10-01-00_(21-09-2026).mp4")
        f2 = os.path.join(self.test_dir, "cam1_10-02-00_to_10-03-00_(21-09-2026).mp4")
        with open(f1, "wb") as f:
            f.write(b"seg1")
        with open(f2, "wb") as f:
            f.write(b"seg2")

        created_subprocesses = []

        class FakePopen:
            def __init__(self, cmd, *args, **kwargs):
                self.cmd = cmd
                created_subprocesses.append(cmd)
                out_path = cmd[-1]
                with open(out_path, "wb") as out_f:
                    out_f.write(b"cut_part")
                self.stdout = ["out_time_ms=30000000\n", "progress=end\n"]
                self.returncode = 0

            def poll(self):
                return self.returncode

            def wait(self):
                return self.returncode

            def kill(self):
                pass

        def fake_run(cmd, **kwargs):
            out_file = cmd[-1]
            with open(out_file, "wb") as f:
                f.write(b"merged_concat")
            return MagicMock(returncode=0, stdout="")

        with patch.object(APP, "_timeline_playback_mode", return_value="local"), \
             patch("subprocess.Popen", side_effect=FakePopen), \
             patch("subprocess.run", side_effect=fake_run) as mock_run:

            resp = self.client.get(
                "/merge?cam_id=1&start=2026-09-21T10:00:00&end=2026-09-21T10:03:00"
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.get_data(as_text=True)

            # Verify gap notice for missing 60s
            self.assertIn("data: notice:Khoảng đã chọn có 60 giây không có video;", data)
            self.assertIn("data: done:merge_cam1_", data)

            # Check all cut piece commands used -c copy and NO libx264
            self.assertEqual(len(created_subprocesses), 2)
            for cmd in created_subprocesses:
                self.assertIn("-c", cmd)
                self.assertEqual(cmd[cmd.index("-c") + 1], "copy")
                self.assertIn("+faststart", cmd)
                self.assertNotIn("libx264", cmd)
                self.assertIn("-map", cmd)
                self.assertIn("0:v:0", cmd)
                self.assertIn("0:a?", cmd)

            # Check concat command used -c copy
            concat_cmd = mock_run.call_args[0][0]
            self.assertIn("concat", concat_cmd)
            self.assertIn("-c", concat_cmd)
            self.assertEqual(concat_cmd[concat_cmd.index("-c") + 1], "copy")
            self.assertNotIn("libx264", concat_cmd)

    def test_cut_and_cut_progress_use_fast_stream_copy(self):
        f = os.path.join(self.test_dir, "cam1_10-00-00_to_10-01-00_(21-09-2026).mp4")
        with open(f, "wb") as fh:
            fh.write(b"video_data")

        # Test /cut endpoint
        with patch("subprocess.run") as mock_run, \
             patch("os.path.isfile", return_value=True), \
             patch("os.path.getsize", return_value=1024):
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            res = self.client.get(f"/cut?filename={os.path.basename(f)}&start=10.5&duration=20.0")
            self.assertEqual(res.status_code, 200)
            json_data = res.get_json()
            self.assertIn("output", json_data)

            cmd = mock_run.call_args[0][0]
            self.assertIn("-c", cmd)
            self.assertEqual(cmd[cmd.index("-c") + 1], "copy")
            self.assertIn("+faststart", cmd)
            self.assertNotIn("libx264", cmd)
            self.assertIn("-ss", cmd)
            self.assertEqual(cmd[cmd.index("-ss") + 1], "10.500")
            self.assertIn("-t", cmd)
            self.assertEqual(cmd[cmd.index("-t") + 1], "20.000")

        # Test /cut_progress endpoint
        class FakeCutPopen:
            def __init__(self, cmd, *args, **kwargs):
                self.cmd = cmd
                out_path = cmd[-1]
                with open(out_path, "wb") as out_f:
                    out_f.write(b"cut_progress_part")
                self.stdout = ["out_time_ms=10000000\n", "progress=end\n"]
                self.returncode = 0

            def poll(self):
                return self.returncode

            def wait(self):
                return self.returncode

            def kill(self):
                pass

        with patch("subprocess.Popen", side_effect=FakeCutPopen):
            res = self.client.get(f"/cut_progress?filename={os.path.basename(f)}&start=5.0&duration=15.0")
            self.assertEqual(res.status_code, 200)
            sse_text = res.get_data(as_text=True)
            self.assertIn("data: 0\n\n", sse_text)
            self.assertIn("data: 100\n\n", sse_text)

    def test_real_ffmpeg_stream_copy_cut_and_merge_e2e(self):
        """End-to-end integration test running actual ffmpeg.exe with synthetic files."""
        if not os.path.isfile(APP.FFMPEG_PATH):
            self.skipTest("ffmpeg.exe not found for E2E test")

        # Generate 2 synthetic 2-second clips with different timestamps
        f1 = os.path.join(self.test_dir, "cam1_10-00-00_to_10-00-02_(21-09-2026).mp4")
        f2 = os.path.join(self.test_dir, "cam1_10-00-03_to_10-00-05_(21-09-2026).mp4")

        cmd1 = [
            APP.FFMPEG_PATH, "-y", "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=1000:duration=2",
            "-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart", f1,
        ]
        cmd2 = [
            APP.FFMPEG_PATH, "-y", "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=10",
            "-f", "lavfi", "-i", "sine=frequency=1200:duration=2",
            "-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart", f2,
        ]
        subprocess.run(cmd1, check=True, capture_output=True)
        subprocess.run(cmd2, check=True, capture_output=True)

        with patch.object(APP, "_timeline_playback_mode", return_value="local"):
            # Request merge from 10:00:00 to 10:00:05 (spans both files with 1s gap)
            resp = self.client.get(
                "/merge?cam_id=1&start=2026-09-21T10:00:00&end=2026-09-21T10:00:05"
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.get_data(as_text=True)

            self.assertIn("notice:Khoảng đã chọn có 1 giây không có video;", data)
            self.assertIn("data: done:", data)

            done_line = [l for l in data.splitlines() if l.startswith("data: done:")][0]
            output_filename = done_line.replace("data: done:", "").strip()
            output_path = os.path.join(self.test_dir, output_filename)

            self.assertTrue(os.path.isfile(output_path))
            self.assertGreater(os.path.getsize(output_path), 0)

            # Validate that output container is valid and playable
            probe = subprocess.run(
                [APP.FFMPEG_PATH, "-v", "error", "-i", output_path, "-f", "null", "-"],
                capture_output=True,
                text=True,
            )
            self.assertEqual(probe.returncode, 0, f"FFmpeg validation failed: {probe.stderr}")

            # Check faststart: moov atom is before mdat
            with open(output_path, "rb") as out_fh:
                header_bytes = out_fh.read(1024 * 64)
                self.assertIn(b"moov", header_bytes)

    def test_local_merge_single_part_direct_copy(self):
        f = os.path.join(self.test_dir, "cam1_10-00-00_to_10-01-00_(21-09-2026).mp4")
        with open(f, "wb") as fh:
            fh.write(b"single_part_data")

        class FakeSinglePopen:
            def __init__(self, cmd, *args, **kwargs):
                out_path = cmd[-1]
                with open(out_path, "wb") as out_f:
                    out_f.write(b"cut_piece")
                self.stdout = ["out_time_ms=10000000\n", "progress=end\n"]
                self.returncode = 0

            def poll(self):
                return self.returncode

            def wait(self):
                return self.returncode

            def kill(self):
                pass

        with patch.object(APP, "_timeline_playback_mode", return_value="local"), \
             patch("subprocess.Popen", side_effect=FakeSinglePopen), \
             patch("subprocess.run") as mock_run:
            resp = self.client.get(
                "/merge?cam_id=1&start=2026-09-21T10:00:10&end=2026-09-21T10:00:30"
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.get_data(as_text=True)
            self.assertIn("data: done:", data)
            # When len(part_paths) == 1, concat_cmd is not invoked
            mock_run.assert_not_called()

    def test_local_merge_concat_audio_fallback(self):
        f1 = os.path.join(self.test_dir, "cam1_10-00-00_to_10-01-00_(21-09-2026).mp4")
        f2 = os.path.join(self.test_dir, "cam1_10-01-00_to_10-02-00_(21-09-2026).mp4")
        with open(f1, "wb") as fh:
            fh.write(b"part1")
        with open(f2, "wb") as fh:
            fh.write(b"part2")

        class FakePopen:
            def __init__(self, cmd, *args, **kwargs):
                out_path = cmd[-1]
                with open(out_path, "wb") as out_f:
                    out_f.write(b"cut_piece")
                self.stdout = ["out_time_ms=10000000\n", "progress=end\n"]
                self.returncode = 0

            def poll(self):
                return self.returncode

            def wait(self):
                return self.returncode

            def kill(self):
                pass

        def fake_run(cmd, **kwargs):
            out_file = cmd[-1]
            if "-an" not in cmd:
                # First concat attempt with audio fails
                return MagicMock(returncode=1, stdout="Incompatible audio streams")
            # Second concat attempt with -an succeeds
            with open(out_file, "wb") as out_f:
                out_f.write(b"merged_no_audio")
            return MagicMock(returncode=0, stdout="")

        with patch.object(APP, "_timeline_playback_mode", return_value="local"), \
             patch("subprocess.Popen", side_effect=FakePopen), \
             patch("subprocess.run", side_effect=fake_run) as mock_run:
            resp = self.client.get(
                "/merge?cam_id=1&start=2026-09-21T10:00:00&end=2026-09-21T10:02:00"
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.get_data(as_text=True)
            self.assertIn("data: done:", data)

            self.assertEqual(mock_run.call_count, 2)
            first_cmd = mock_run.call_args_list[0][0][0]
            second_cmd = mock_run.call_args_list[1][0][0]
            self.assertIn("-c", first_cmd)
            self.assertIn("-an", second_cmd)
            self.assertIn("-c:v", second_cmd)
            self.assertEqual(second_cmd[second_cmd.index("-c:v") + 1], "copy")

    def test_safe_cleanup_on_generator_exit(self):
        f = os.path.join(self.test_dir, "cam1_10-00-00_to_10-01-00_(21-09-2026).mp4")
        with open(f, "wb") as fh:
            fh.write(b"part")

        killed = []

        class HangingPopen:
            def __init__(self, cmd, *args, **kwargs):
                self.stdout = ["out_time_ms=1000\n"]
                self.returncode = None

            def poll(self):
                return self.returncode

            def wait(self):
                return 0

            def kill(self):
                killed.append(True)

        with patch.object(APP, "_timeline_playback_mode", return_value="local"), \
             patch("subprocess.Popen", side_effect=HangingPopen), \
             self.client.application.test_request_context("/merge?cam_id=1&start=2026-09-21T10:00:00&end=2026-09-21T10:01:00"):
            resp = APP.merge_video()
            gen = resp.response
            # Read initial event and step into first piece processing
            first_event = next(gen)
            self.assertIn("data: 5", str(first_event))
            second_event = next(gen)
            # Simulate client disconnect by closing generator while process is active
            gen.close()
            # Verify process was killed
            self.assertTrue(len(killed) > 0)
            # Verify no partial output files remain in test_dir
            merged_files = [fn for fn in os.listdir(self.test_dir) if fn.startswith("merge_cam1_")]
            self.assertEqual(merged_files, [])


if __name__ == "__main__":
    unittest.main()
