import importlib.util
import json
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import dahua_37777 as D

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location(
    "cambida_preview_stream_select", os.path.join(ROOT, "1.py")
)
APP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(APP)


class FakeNetSDKForPreview:
    def __init__(self, login_ok=True):
        self.login_ok = login_ok
        self.channel = None
        self.real_type = None
        self.saved_path = None
        self.stopped_save = 0
        self.stopped_play = 0
        self.logged_out = 0

    def CLIENT_GetLastError(self):
        return 0 if self.login_ok else 0x80000064

    def CLIENT_LoginWithHighLevelSecurity(self, pin_ptr, pout_ptr):
        import ctypes as C

        pout = C.cast(
            pout_ptr, C.POINTER(D.NET_OUT_LOGIN_WITH_HIGHLEVEL_SECURITY)
        ).contents
        pout.stuDeviceInfo.nChanNum = 2
        return 101

    def CLIENT_RealPlayEx(self, login, channel, hwnd, real_type):
        self.channel = channel
        self.real_type = real_type
        return 202

    def CLIENT_SaveRealData(self, real, path):
        self.saved_path = os.fsdecode(path)
        with open(self.saved_path, "wb") as handle:
            handle.write(b"DHAV" + b"\x00" * 2048)
        return 1

    def CLIENT_StopSaveRealData(self, real):
        self.stopped_save += 1
        return 1

    def CLIENT_StopRealPlayEx(self, real):
        self.stopped_play += 1
        return 1

    def CLIENT_Logout(self, login):
        self.logged_out += 1
        return 1


class PreviewStreamSelectTests(unittest.TestCase):
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
            "admin_auth": {"username": "admin", "password": "unit-test-password"},
            "server_port": 8000,
            "video_dir": "cctv_videos",
            "db_path": "analytics.db",
            "log_dir": "logs",
            "record_duration_sec": 300,
            "record_timeout_sec": 330,
            "cameras": [] if cameras is None else cameras,
            "tables": [] if tables is None else tables,
        }

    # -------------------------------------------------------------------------
    # 1. Recording ALWAYS uses Main stream across transports
    # -------------------------------------------------------------------------
    def test_rtsp_recording_always_uses_main_stream_regardless_of_view_stream(self):
        """Even if view_stream is 'sub', recording URL must use Main stream."""
        for view_stream in ("sub", "auto", "main"):
            cam = {
                "name": "Local Dahua RTSP",
                "playback_source": "local",
                "ip": "192.168.1.50",
                "user": "admin",
                "pass": "secret",
                "local_transport": "rtsp",
                "port": 554,
                "record_path": "cam/realmonitor?channel=1&subtype=0",
                "preview_path": "cam/realmonitor?channel=1&subtype=1",
                "view_stream": view_stream,
            }
            url = APP.build_rtsp_url(cam, "record")
            self.assertIn("subtype=0", url, f"Failed for view_stream={view_stream}")
            self.assertNotIn("subtype=1", url, f"Failed for view_stream={view_stream}")

    def test_netsdk_recording_always_uses_main_stream_real_type_0(self):
        """NetSDK record_segment must force real_type=0 even if adapter stream is 'sub'."""
        fake = FakeNetSDKForPreview()
        camera = {
            "name": "Local NetSDK",
            "playback_source": "local",
            "ip": "192.168.1.50",
            "user": "admin",
            "pass": "secret",
            "local_transport": "netsdk",
            "netsdk_port": 37777,
            "netsdk_channel": 1,
            "netsdk_stream": "sub",
            "view_stream": "sub",
        }
        with patch.object(D, "_load_runtime", return_value=fake):
            adapter = D.Dahua37777Adapter.from_camera(camera)
            self.assertEqual(adapter.stream, "sub")
            with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
                out_path = tmp.name
            try:
                def fake_dav_to_mp4(dav_path, output_path, ffmpeg_path):
                    with open(output_path, "wb") as f:
                        f.write(b"mp4data")
                    return "copy"

                with patch.object(D.Dahua37777Adapter, "_dav_to_mp4", side_effect=fake_dav_to_mp4):
                    result = adapter.record_segment(out_path, duration=0.1, ffmpeg_path="ffmpeg.exe")
                    self.assertTrue(result["ok"])
                    # Verify that _open_realplay was called with real_type=0 (DH_RType_Realplay = main stream)
                    self.assertEqual(fake.real_type, 0, "NetSDK record_segment must force real_type=0")
            finally:
                if os.path.exists(out_path):
                    os.remove(out_path)

    def test_nvr_recording_always_uses_main_stream(self):
        """NVR recording stream must use record stream regardless of view_stream."""
        nvr_cam = {
            "name": "NVR CH1",
            "playback_source": "nvr",
            "vendor": "dahua",
            "host": "192.168.1.100",
            "http_port": 81,
            "rtsp_port": 554,
            "user": "admin",
            "pass": "secret",
            "nvr_channel": 1,
            "stream": "main",
            "view_stream": "sub",
        }
        url = APP.build_rtsp_url(nvr_cam, "record")
        self.assertIn("subtype=0", url)
        self.assertNotIn("subtype=1", url)

    # -------------------------------------------------------------------------
    # 2. Preview Main vs Sub switching reaches correct underlying stream
    # -------------------------------------------------------------------------
    def test_rtsp_preview_main_vs_sub_switching(self):
        """RTSP URL construction must switch between subtype 0 (main) and subtype 1 (sub)."""
        cam = {
            "name": "Local Dahua RTSP",
            "playback_source": "local",
            "ip": "192.168.1.50",
            "user": "admin",
            "pass": "secret",
            "local_transport": "rtsp",
            "port": 554,
            "record_path": "cam/realmonitor?channel=1&subtype=0",
            "preview_path": "cam/realmonitor?channel=1&subtype=1",
            "view_stream": "auto",
        }
        main_url = APP.build_rtsp_url(cam, "main")
        sub_url = APP.build_rtsp_url(cam, "sub")
        self.assertIn("subtype=0", main_url)
        self.assertIn("subtype=1", sub_url)

        # Also test Hikvision style path with explicit vendor="hikvision"
        hik_cam = {
            "name": "Local Hik RTSP",
            "playback_source": "local",
            "vendor": "hikvision",
            "ip": "192.168.1.51",
            "user": "admin",
            "pass": "secret",
            "local_transport": "rtsp",
            "port": 554,
            "record_path": "h264/ch1/main/av_stream",
            "preview_path": "h264/ch1/sub/av_stream",
            "view_stream": "auto",
        }
        self.assertIn("main/av_stream", APP.build_rtsp_url(hik_cam, "main"))
        self.assertIn("sub/av_stream", APP.build_rtsp_url(hik_cam, "sub"))

    def test_netsdk_preview_main_vs_sub_realplay(self):
        """NetSDK preview must use real_type=0 for main and real_type=3 for sub."""
        fake = FakeNetSDKForPreview()
        with patch.object(D, "_load_runtime", return_value=fake):
            # Adapter with main stream
            adapter_main = D.Dahua37777Adapter("192.168.1.50", "admin", "secret", stream="main")
            login_handle, _ = adapter_main._login()
            handle = adapter_main._open_realplay(login_handle)
            self.assertEqual(fake.real_type, 0, "Main stream must use real_type=0")
            adapter_main.dll.CLIENT_StopRealPlayEx(handle)
            adapter_main.dll.CLIENT_Logout(login_handle)

            # Adapter with sub stream
            adapter_sub = D.Dahua37777Adapter("192.168.1.50", "admin", "secret", stream="sub")
            login_handle, _ = adapter_sub._login()
            handle = adapter_sub._open_realplay(login_handle)
            self.assertEqual(fake.real_type, 3, "Sub stream must use real_type=3")
            adapter_sub.dll.CLIENT_StopRealPlayEx(handle)
            adapter_sub.dll.CLIENT_Logout(login_handle)

    # -------------------------------------------------------------------------
    # 3. Auto stream resolution is deterministic and documented
    # -------------------------------------------------------------------------
    def test_auto_stream_resolution(self):
        """resolve_view_stream rules:
        - Explicit requested stream 'main' or 'sub' always wins
        - Configured 'main' or 'sub' wins when requested is 'auto' or None
        - Configured 'auto' resolves to:
            * 'sub' for grid/multi/live_all context
            * 'main' for single/replay/fullscreen context
            * 'sub' for unspecified or unknown context
        """
        # Explicit request
        self.assertEqual(APP.resolve_view_stream({}, "main"), "main")
        self.assertEqual(APP.resolve_view_stream({}, "sub"), "sub")

        # Configured view_stream wins over auto request
        cam_sub = {"view_stream": "sub"}
        self.assertEqual(APP.resolve_view_stream(cam_sub, "auto"), "sub")
        self.assertEqual(APP.resolve_view_stream(cam_sub, None), "sub")
        self.assertEqual(APP.resolve_view_stream(cam_sub, "auto", context="single"), "sub")

        cam_main = {"view_stream": "main"}
        self.assertEqual(APP.resolve_view_stream(cam_main, "auto"), "main")
        self.assertEqual(APP.resolve_view_stream(cam_main, None), "main")
        self.assertEqual(APP.resolve_view_stream(cam_main, "auto", context="grid"), "main")

        # Configured auto: context-sensitive
        cam_auto = {"view_stream": "auto"}
        self.assertEqual(APP.resolve_view_stream(cam_auto, "auto", context="grid"), "sub")
        self.assertEqual(APP.resolve_view_stream(cam_auto, "auto", context="multi"), "sub")
        self.assertEqual(APP.resolve_view_stream(cam_auto, "auto", context="live_all"), "sub")
        self.assertEqual(APP.resolve_view_stream(cam_auto, "auto", context="single"), "main")
        self.assertEqual(APP.resolve_view_stream(cam_auto, "auto", context="replay"), "main")
        self.assertEqual(APP.resolve_view_stream(cam_auto, "auto", context="fullscreen"), "main")

        # Fallback when context unknown
        self.assertEqual(APP.resolve_view_stream(cam_auto, "auto", context="unknown"), "sub")
        self.assertEqual(APP.resolve_view_stream(cam_auto, "auto", context=None), "sub")
        self.assertEqual(APP.resolve_view_stream({}, None, context=None), "sub")

    # -------------------------------------------------------------------------
    # 4. Config save/load and normalization preserves view_stream
    # -------------------------------------------------------------------------
    def test_config_normalise_and_validation(self):
        """view_stream must normalize to 'auto', 'main', or 'sub' and be validated."""
        # Normalization defaults
        n1 = APP._normalise_camera_entry({"ip": "192.168.1.10"}, 0)
        self.assertEqual(n1["view_stream"], "auto")

        # Preserves explicit values
        n2 = APP._normalise_camera_entry({"ip": "192.168.1.10", "view_stream": "sub"}, 0)
        self.assertEqual(n2["view_stream"], "sub")

        n3 = APP._normalise_camera_entry({"ip": "192.168.1.10", "view_stream": "main"}, 0)
        self.assertEqual(n3["view_stream"], "main")

        # Legacy netsdk_stream mapped if view_stream absent; netsdk_stream is not retained
        n4 = APP._normalise_camera_entry(
            {"ip": "192.168.1.10", "local_transport": "netsdk", "netsdk_stream": "sub"},
            0,
        )
        self.assertEqual(n4["view_stream"], "sub")
        self.assertNotIn("netsdk_stream", n4)

        n4_main = APP._normalise_camera_entry(
            {"ip": "192.168.1.10", "local_transport": "netsdk", "netsdk_stream": "main"},
            0,
        )
        self.assertEqual(n4_main["view_stream"], "main")
        self.assertNotIn("netsdk_stream", n4_main)

        # Stale legacy netsdk_stream cannot override valid view_stream
        n_stale1 = APP._normalise_camera_entry(
            {"ip": "192.168.1.10", "local_transport": "netsdk", "view_stream": "sub", "netsdk_stream": "main"},
            0,
        )
        self.assertEqual(n_stale1["view_stream"], "sub")
        self.assertNotIn("netsdk_stream", n_stale1)

        n_stale2 = APP._normalise_camera_entry(
            {"ip": "192.168.1.10", "local_transport": "netsdk", "view_stream": "auto", "netsdk_stream": "sub"},
            0,
        )
        self.assertEqual(n_stale2["view_stream"], "auto")
        self.assertNotIn("netsdk_stream", n_stale2)

        n_stale3 = APP._normalise_camera_entry(
            {"ip": "192.168.1.10", "local_transport": "netsdk", "view_stream": "main", "netsdk_stream": "sub"},
            0,
        )
        self.assertEqual(n_stale3["view_stream"], "main")
        self.assertNotIn("netsdk_stream", n_stale3)

        # NVR camera preserves view_stream
        n5 = APP._normalise_camera_entry(
            {"playback_source": "nvr", "host": "192.168.1.100", "view_stream": "sub"},
            0,
        )
        self.assertEqual(n5["view_stream"], "sub")

        # Validation rejects invalid view_stream
        cfg_bad = self._root([{"ip": "192.168.1.10", "user": "u", "pass": "p", "view_stream": "ultra_hd"}])
        err = APP.validate_config(cfg_bad)
        self.assertIsNotNone(err)
        self.assertIn("view_stream", err)

        cfg_good = self._root([{"ip": "192.168.1.10", "user": "u", "pass": "p", "view_stream": "sub"}])
        self.assertIsNone(APP.validate_config(cfg_good))

    def test_clean_config_for_saving_preserves_view_stream(self):
        """clean_config_for_saving must keep view_stream across all camera types and drop netsdk_stream."""
        cfg = {
            "cameras": [
                {
                    "name": "Local RTSP",
                    "playback_source": "local",
                    "ip": "192.168.1.10",
                    "local_transport": "rtsp",
                    "view_stream": "sub",
                },
                {
                    "name": "Local NetSDK",
                    "playback_source": "local",
                    "ip": "192.168.1.11",
                    "local_transport": "netsdk",
                    "view_stream": "main",
                    "netsdk_port": 37777,
                    "netsdk_channel": 1,
                    "netsdk_stream": "main",
                },
                {
                    "name": "NVR",
                    "playback_source": "nvr",
                    "host": "192.168.1.12",
                    "view_stream": "auto",
                    "vendor": "dahua",
                    "http_port": 81,
                    "rtsp_port": 554,
                    "nvr_channel": 1,
                    "stream": "main",
                },
            ]
        }
        cleaned = APP.clean_config_for_saving(cfg)
        self.assertEqual(cleaned["cameras"][0]["view_stream"], "sub")
        self.assertEqual(cleaned["cameras"][1]["view_stream"], "main")
        self.assertEqual(cleaned["cameras"][2]["view_stream"], "auto")
        self.assertNotIn("netsdk_stream", cleaned["cameras"][1])

    def test_admin_api_save_and_reload_view_stream(self):
        """PUT /api/admin/config must preserve view_stream."""
        self._auth()
        payload = self._root(
            [
                {
                    "name": "Sub Cam",
                    "playback_source": "local",
                    "ip": "192.168.1.20",
                    "user": "admin",
                    "pass": "secret",
                    "local_transport": "rtsp",
                    "port": 554,
                    "record_path": "cam/realmonitor?channel=1&subtype=0",
                    "preview_path": "cam/realmonitor?channel=1&subtype=1",
                    "view_stream": "sub",
                }
            ]
        )
        with patch.object(APP, "save_config") as mock_save:
            res = self.client.put("/api/admin/config", json=payload)
            self.assertEqual(res.status_code, 200)
            mock_save.assert_called_once()
            saved_config = mock_save.call_args[0][0]
            self.assertEqual(saved_config["cameras"][0]["view_stream"], "sub")

    # -------------------------------------------------------------------------
    # 5. Transport isolation invariants hold
    # -------------------------------------------------------------------------
    def test_transport_isolation_disjoint_keys(self):
        """view_stream does not violate disjoint transport validation rules."""
        # RTSP camera with NetSDK keys must still be rejected
        cfg_mixed_rtsp = self._root(
            [
                {
                    "name": "Bad RTSP",
                    "playback_source": "local",
                    "ip": "192.168.1.30",
                    "user": "admin",
                    "pass": "secret",
                    "local_transport": "rtsp",
                    "netsdk_port": 37777,
                    "view_stream": "sub",
                }
            ]
        )
        err = APP.validate_config(cfg_mixed_rtsp)
        self.assertIsNotNone(err)
        self.assertIn("netsdk_port", err)

        # NetSDK camera with RTSP keys must still be rejected
        cfg_mixed_netsdk = self._root(
            [
                {
                    "name": "Bad NetSDK",
                    "playback_source": "local",
                    "ip": "192.168.1.31",
                    "user": "admin",
                    "pass": "secret",
                    "local_transport": "netsdk",
                    "record_path": "h264/ch1/main/av_stream",
                    "view_stream": "sub",
                }
            ]
        )
        err = APP.validate_config(cfg_mixed_netsdk)
        self.assertIsNotNone(err)
        self.assertIn("record_path", err)

        # Valid configs for RTSP, NetSDK, and NVR all allow view_stream
        cfg_valid = self._root(
            [
                {
                    "name": "Valid RTSP",
                    "playback_source": "local",
                    "ip": "192.168.1.30",
                    "user": "admin",
                    "pass": "secret",
                    "local_transport": "rtsp",
                    "view_stream": "sub",
                },
                {
                    "name": "Valid NetSDK",
                    "playback_source": "local",
                    "ip": "192.168.1.31",
                    "user": "admin",
                    "pass": "secret",
                    "local_transport": "netsdk",
                    "netsdk_port": 37777,
                    "netsdk_channel": 1,
                    "netsdk_stream": "sub",
                    "view_stream": "sub",
                },
                {
                    "name": "Valid NVR",
                    "playback_source": "nvr",
                    "vendor": "hikvision",
                    "host": "192.168.1.32",
                    "user": "admin",
                    "pass": "secret",
                    "http_port": 80,
                    "rtsp_port": 554,
                    "nvr_channel": 1,
                    "stream": "main",
                    "view_stream": "sub",
                },
            ]
        )
        self.assertIsNone(APP.validate_config(cfg_valid))

    def test_netsdk_port_never_used_in_rtsp_url(self):
        """Port 37777 must never be passed to RTSP candidate or URL generation."""
        netsdk_cam = {
            "name": "NetSDK Cam",
            "playback_source": "local",
            "ip": "192.168.1.40",
            "user": "admin",
            "pass": "secret",
            "local_transport": "netsdk",
            "netsdk_port": 37777,
            "netsdk_channel": 1,
            "netsdk_stream": "main",
            "view_stream": "auto",
        }
        APP.CAMERA_LIST = [netsdk_cam]
        self.assertIsNone(APP.get_rtsp_url(1, "preview"))
        self.assertIsNone(APP.get_rtsp_url(1, "main"))
        self.assertIsNone(APP.get_rtsp_url(1, "sub"))

    # -------------------------------------------------------------------------
    # 6. Issue #2: Legacy netsdk_stream migration and conflict resolution
    # -------------------------------------------------------------------------
    def test_stale_legacy_netsdk_stream_cannot_override_valid_view_stream(self):
        """Stale legacy netsdk_stream must never override valid view_stream."""
        # 1. resolve_view_stream: view_stream wins
        cam_sub_stale_main = {"view_stream": "sub", "netsdk_stream": "main"}
        self.assertEqual(APP.resolve_view_stream(cam_sub_stale_main), "sub")
        self.assertEqual(APP.resolve_view_stream(cam_sub_stale_main, context="single"), "sub")

        cam_main_stale_sub = {"view_stream": "main", "netsdk_stream": "sub"}
        self.assertEqual(APP.resolve_view_stream(cam_main_stale_sub), "main")
        self.assertEqual(APP.resolve_view_stream(cam_main_stale_sub, context="grid"), "main")

        cam_auto_stale_sub = {"view_stream": "auto", "netsdk_stream": "sub"}
        self.assertEqual(APP.resolve_view_stream(cam_auto_stale_sub, context="single"), "main")
        self.assertEqual(APP.resolve_view_stream(cam_auto_stale_sub, context="grid"), "sub")

        cam_auto_stale_main = {"view_stream": "auto", "netsdk_stream": "main"}
        self.assertEqual(APP.resolve_view_stream(cam_auto_stale_main, context="grid"), "sub")
        self.assertEqual(APP.resolve_view_stream(cam_auto_stale_main, context="single"), "main")

        # 2. _normalise_camera_entry: view_stream wins and netsdk_stream is purged
        norm1 = APP._normalise_camera_entry(
            {"local_transport": "netsdk", "ip": "192.168.1.10", "view_stream": "sub", "netsdk_stream": "main"},
            0,
        )
        self.assertEqual(norm1["view_stream"], "sub")
        self.assertNotIn("netsdk_stream", norm1)

        norm2 = APP._normalise_camera_entry(
            {"local_transport": "netsdk", "ip": "192.168.1.10", "view_stream": "main", "netsdk_stream": "sub"},
            0,
        )
        self.assertEqual(norm2["view_stream"], "main")
        self.assertNotIn("netsdk_stream", norm2)

        norm3 = APP._normalise_camera_entry(
            {"local_transport": "netsdk", "ip": "192.168.1.10", "view_stream": "auto", "netsdk_stream": "sub"},
            0,
        )
        self.assertEqual(norm3["view_stream"], "auto")
        self.assertNotIn("netsdk_stream", norm3)

        # 3. clean_config_for_saving: drops netsdk_stream so settings cannot disagree
        saved = APP.clean_config_for_saving(
            self._root([
                {"local_transport": "netsdk", "ip": "192.168.1.10", "view_stream": "sub", "netsdk_stream": "main"},
                {"local_transport": "netsdk", "ip": "192.168.1.11", "view_stream": "auto", "netsdk_stream": "sub"},
            ])
        )
        for cam in saved["cameras"]:
            self.assertNotIn("netsdk_stream", cam)
        self.assertEqual(saved["cameras"][0]["view_stream"], "sub")
        self.assertEqual(saved["cameras"][1]["view_stream"], "auto")

    def test_legacy_only_netsdk_stream_migration(self):
        """Legacy configs with only netsdk_stream migrate correctly to view_stream."""
        # 1. resolve_view_stream fallback
        self.assertEqual(APP.resolve_view_stream({"netsdk_stream": "sub"}), "sub")
        self.assertEqual(APP.resolve_view_stream({"netsdk_stream": "main"}), "main")
        self.assertEqual(APP.resolve_view_stream({"view_stream": "", "netsdk_stream": "sub"}), "sub")
        self.assertEqual(APP.resolve_view_stream({"view_stream": None, "netsdk_stream": "main"}), "main")
        self.assertEqual(APP.resolve_view_stream({"view_stream": "invalid", "netsdk_stream": "sub"}), "sub")

        # 2. _normalise_camera_entry maps legacy netsdk_stream to view_stream and prunes netsdk_stream
        n_sub = APP._normalise_camera_entry(
            {"local_transport": "netsdk", "ip": "192.168.1.10", "netsdk_stream": "sub"},
            0,
        )
        self.assertEqual(n_sub["view_stream"], "sub")
        self.assertNotIn("netsdk_stream", n_sub)

        n_main = APP._normalise_camera_entry(
            {"local_transport": "netsdk", "ip": "192.168.1.10", "netsdk_stream": "main"},
            0,
        )
        self.assertEqual(n_main["view_stream"], "main")
        self.assertNotIn("netsdk_stream", n_main)

        # 3. clean_config_for_saving persists migrated view_stream without netsdk_stream
        saved = APP.clean_config_for_saving(
            self._root([
                {"local_transport": "netsdk", "ip": "192.168.1.10", "netsdk_stream": "sub"},
                {"local_transport": "netsdk", "ip": "192.168.1.11", "netsdk_stream": "main"},
            ])
        )
        self.assertEqual(saved["cameras"][0]["view_stream"], "sub")
        self.assertNotIn("netsdk_stream", saved["cameras"][0])
        self.assertEqual(saved["cameras"][1]["view_stream"], "main")
        self.assertNotIn("netsdk_stream", saved["cameras"][1])

    def test_netsdk_adapter_from_camera_stream_resolution(self):
        """Dahua37777Adapter.from_camera prioritizes view_stream over stale netsdk_stream."""
        base = {"ip": "192.168.1.50", "user": "admin", "pass": "secret"}
        # Valid view_stream takes precedence over stale netsdk_stream
        ad1 = D.Dahua37777Adapter.from_camera(
            {**base, "view_stream": "sub", "netsdk_stream": "main"}
        )
        self.assertEqual(ad1.stream, "sub")

        ad2 = D.Dahua37777Adapter.from_camera(
            {**base, "view_stream": "main", "netsdk_stream": "sub"}
        )
        self.assertEqual(ad2.stream, "main")

        # view_stream='auto' with stale netsdk_stream does not use stale 'sub'
        ad3 = D.Dahua37777Adapter.from_camera(
            {**base, "view_stream": "auto", "netsdk_stream": "sub"}
        )
        self.assertEqual(ad3.stream, "main")

        # Legacy-only fallback
        ad4 = D.Dahua37777Adapter.from_camera(
            {**base, "netsdk_stream": "sub"}
        )
        self.assertEqual(ad4.stream, "sub")

        ad5 = D.Dahua37777Adapter.from_camera(
            {**base, "netsdk_stream": "main"}
        )
        self.assertEqual(ad5.stream, "main")

        ad6 = D.Dahua37777Adapter.from_camera(
            {**base, "view_stream": "bad_value", "netsdk_stream": "sub"}
        )
        self.assertEqual(ad6.stream, "sub")

    def test_runtime_netsdk_preview_uses_transient_clone_without_persisting(self):
        """Runtime preview sets transient netsdk_stream on cloned camera; original is not mutated."""
        camera = {
            "name": "Live NetSDK",
            "playback_source": "local",
            "ip": "192.168.1.50",
            "user": "admin",
            "pass": "secret",
            "local_transport": "netsdk",
            "netsdk_port": 37777,
            "netsdk_channel": 1,
            "view_stream": "auto",
        }
        # Original camera must not have netsdk_stream
        self.assertNotIn("netsdk_stream", camera)

        passed_cameras = []

        class DummyAdapter:
            def __init__(self, cam):
                passed_cameras.append(dict(cam))
            def iter_jpeg_frames(self, *args, **kwargs):
                return iter([b"fakeframe"])
            def capture_jpeg(self, *args, **kwargs):
                return b"fakejpeg"

        with patch.object(APP.Dahua37777Adapter, "from_camera", side_effect=lambda cam, **kw: DummyAdapter(cam)):
            # 1. gen_netsdk_frames in grid context -> target_stream is 'sub'
            gen = APP.gen_netsdk_frames(camera, context="grid")
            frame = next(gen)
            self.assertIn(b"fakeframe", frame)
            gen.close()

            self.assertEqual(len(passed_cameras), 1)
            cloned = passed_cameras[0]
            # Transient clone has target stream
            self.assertEqual(cloned["netsdk_stream"], "sub")
            self.assertEqual(cloned["view_stream"], "sub")
            # Original camera is untouched
            self.assertNotIn("netsdk_stream", camera)
            self.assertEqual(camera["view_stream"], "auto")

            # 2. Snapshot in single context -> target_stream is 'main'
            passed_cameras.clear()
            APP.CAMERA_LIST = [camera]
            res = self.client.get("/snapshot/cam1?context=single")
            self.assertEqual(res.status_code, 200)

            self.assertEqual(len(passed_cameras), 1)
            cloned2 = passed_cameras[0]
            self.assertEqual(cloned2["netsdk_stream"], "main")
            self.assertEqual(cloned2["view_stream"], "main")
            # Original camera in CAMERA_LIST remains untouched
            self.assertNotIn("netsdk_stream", camera)
            self.assertEqual(camera["view_stream"], "auto")


if __name__ == "__main__":
    unittest.main()
