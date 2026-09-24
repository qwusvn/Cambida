"""Unit and integration mock tests for Dahua, Imou, and KBVision camera modules.

Validates:
1. DahuaCameraAdapter, ImouCameraAdapter, and KBVisionCameraAdapter architecture.
2. Default and custom port configuration (NetSDK 37777, KBVision 8888, RTSP 554).
3. Capability matrix gating and explicit UnsupportedCapabilityError enforcement.
4. RTSP path and URL generation (cam/realmonitor format, channel 1-based vs SDK 0-based).
5. KBVision NetSDK unverified status policy and safe RTSP default routing.
6. NetSDK / DVRIP probe mechanisms and error handling.
7. Multi-channel enumeration and distinct stable ID integrity.
8. Polymorphic input compatibility (DeviceInfo, DeviceConfig, dict).
9. Integration with CameraAdapterRegistry and CameraManager.
10. Media pipeline delegation to dahua_37777 without mutating base files.
"""

from __future__ import annotations

import datetime
import os
import socket
import unittest
from unittest.mock import MagicMock, patch

from camera_modules.core import (
    CameraCapability,
    CameraManager,
    ChannelInfo,
    DeviceInfo,
    ProbeResult,
    StreamEndpoint,
    StreamType,
    TransportProtocol,
    UnsupportedCapabilityError,
    VendorType,
    generate_camera_id,
    generate_device_id,
    generate_table_id,
    get_adapter_registry,
    get_camera_manager,
    mask_credential,
    sanitize_dict_for_logging,
)
from camera_modules.config import (
    DeviceConfig,
    CameraConfig,
    TableBinding,
    build_rtsp_url,
)
from camera_modules.dahua import (
    DEFAULT_DAHUA_HTTP_PORT,
    DEFAULT_DAHUA_NETSDK_PORT,
    DEFAULT_DAHUA_RTSP_PORT,
    DEFAULT_KBVISION_HTTP_PORT,
    DEFAULT_KBVISION_PORT,
    DEFAULT_KBVISION_RTSP_PORT,
    DahuaCameraAdapter,
    ImouCameraAdapter,
    KBVisionCameraAdapter,
    build_dahua_rtsp_paths,
    build_dahua_rtsp_url,
    build_kbvision_rtsp_url,
    is_dahua_netsdk_available,
    probe_dahua_device,
    probe_kbvision_device,
)


class FakeNetSDKBackend:
    """Mock for dahua_37777 backend adapter."""

    def __init__(self, login_ok: bool = True, channel_count: int = 4):
        self.login_ok = login_ok
        self.channel_count = channel_count
        self.record_called = False
        self.capture_called = False

    def probe(self, require_media: bool = False, media_seconds: float = 0.5):
        from dahua_37777 import ProbeResult
        if self.login_ok:
            return ProbeResult(ok=True, media_ok=True, channels=self.channel_count, sdk_error=0, message="OK")
        return ProbeResult(ok=False, media_ok=False, channels=0, sdk_error=0x80000064, message="NET_LOGIN_ERROR_PASSWORD")

    def capture_jpeg(self, ffmpeg_path: str, timeout: float = 12.0) -> bytes:
        self.capture_called = True
        return b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"fake_jpeg_payload"

    def record_segment(self, output_path: str, duration: float, ffmpeg_path: str, stop_event=None):
        self.record_called = True
        return {
            "ok": True,
            "output_path": output_path,
            "duration": duration,
            "bytes": 2048,
        }

    def iter_jpeg_frames(self, ffmpeg_path: str, fps: float = 10.0, frame_timeout: float = 12.0):
        yield b"\xff\xd8\xff\xe0_frame_1"
        yield b"\xff\xd8\xff\xe0_frame_2"


class DahuaModuleAdapterTests(unittest.TestCase):
    """Unit tests for Dahua, Imou, and KBVision camera adapters."""

    def setUp(self):
        self.dahua_adapter = DahuaCameraAdapter()
        self.imou_adapter = ImouCameraAdapter()
        self.kb_adapter = KBVisionCameraAdapter()

    # -------------------------------------------------------------------------
    # 1. Adapter Vendor Identity & Default Ports
    # -------------------------------------------------------------------------

    def test_vendor_and_default_ports(self):
        self.assertEqual(self.dahua_adapter.vendor, "dahua")
        self.assertEqual(self.dahua_adapter.default_port, DEFAULT_DAHUA_NETSDK_PORT)
        self.assertEqual(self.dahua_adapter.default_port, 37777)
        self.assertEqual(self.dahua_adapter.default_http_port, 81)
        self.assertEqual(self.dahua_adapter.default_rtsp_port, 554)

        self.assertEqual(self.imou_adapter.vendor, "imou")
        self.assertEqual(self.imou_adapter.default_port, 37777)
        self.assertEqual(self.imou_adapter.default_http_port, 81)
        self.assertEqual(self.imou_adapter.default_rtsp_port, 554)

        self.assertEqual(self.kb_adapter.vendor, "kbvision")
        self.assertEqual(self.kb_adapter.default_port, DEFAULT_KBVISION_PORT)
        self.assertEqual(self.kb_adapter.default_port, 8888)
        self.assertEqual(self.kb_adapter.default_http_port, 80)
        self.assertEqual(self.kb_adapter.default_rtsp_port, 554)

    # -------------------------------------------------------------------------
    # 2. Capability Matrix & Strict Contract Enforcements
    # -------------------------------------------------------------------------

    def test_supported_capabilities(self):
        supported = self.dahua_adapter.supported_capabilities
        self.assertIn(CameraCapability.CONNECT, supported)
        self.assertIn(CameraCapability.ENUMERATE_CHANNELS, supported)
        self.assertIn(CameraCapability.LIVE_MAIN, supported)
        self.assertIn(CameraCapability.LIVE_SUB, supported)
        self.assertIn(CameraCapability.DISCOVER, supported)

    def test_unsupported_capabilities_raise_contract_error(self):
        # The contract specifically forbids falsely claiming unsupported SDK features
        with self.assertRaises(UnsupportedCapabilityError) as ctx:
            self.dahua_adapter.require_capability(CameraCapability.PTZ)
        self.assertEqual(ctx.exception.vendor, "dahua")
        self.assertEqual(ctx.exception.capability, CameraCapability.PTZ.value)

        # Search recordings explicitly raises UnsupportedCapabilityError
        dev = DeviceInfo(device_id="test_dev", vendor="dahua", host="192.168.1.10", port=37777)
        with self.assertRaises(UnsupportedCapabilityError):
            self.dahua_adapter.search_recordings(
                dev, 1, datetime.datetime.now(), datetime.datetime.now()
            )

        # Download recording explicitly raises UnsupportedCapabilityError
        with self.assertRaises(UnsupportedCapabilityError):
            self.dahua_adapter.download_recording(
                dev, MagicMock(), "/tmp/out.mp4"
            )

    # -------------------------------------------------------------------------
    # 3. RTSP URL & Path Generation
    # -------------------------------------------------------------------------

    def test_build_dahua_rtsp_paths(self):
        paths_ch1 = build_dahua_rtsp_paths(channel=1, stream="main")
        self.assertEqual(paths_ch1["primary_path"], "cam/realmonitor?channel=1&subtype=0")
        self.assertEqual(paths_ch1["main_path"], "cam/realmonitor?channel=1&subtype=0")
        self.assertEqual(paths_ch1["sub_path"], "cam/realmonitor?channel=1&subtype=1")

        paths_ch4_sub = build_dahua_rtsp_paths(channel=4, stream="sub")
        self.assertEqual(paths_ch4_sub["primary_path"], "cam/realmonitor?channel=4&subtype=1")

    def test_build_dahua_rtsp_url(self):
        url = build_dahua_rtsp_url(
            host="192.168.1.200",
            channel=2,
            stream="sub",
            user="admin",
            password="mypassword",
            port=554,
        )
        self.assertEqual(
            url,
            "rtsp://admin:mypassword@192.168.1.200/cam/realmonitor?channel=2&subtype=1",
        )

    def test_build_kbvision_rtsp_url(self):
        url = build_kbvision_rtsp_url(
            host="192.168.1.250",
            channel=1,
            stream="main",
            user="admin",
            password="kbpassword",
            port=554,
        )
        self.assertEqual(
            url,
            "rtsp://admin:kbpassword@192.168.1.250/cam/realmonitor?channel=1&subtype=0",
        )

    # -------------------------------------------------------------------------
    # 4. KBVision Specific Contract Compliance
    # -------------------------------------------------------------------------

    def test_kbvision_defaults_to_rtsp_with_unverified_sdk_notice(self):
        dev = DeviceInfo(
            device_id="dev_kb_1",
            vendor="kbvision",
            host="192.168.1.60",
            port=8888,
            rtsp_port=554,
            user="admin",
            password="secret_pass",
        )
        endpoint = self.kb_adapter.get_live_stream(dev, channel_id=1, stream=StreamType.MAIN)
        self.assertEqual(endpoint.protocol, TransportProtocol.RTSP)
        self.assertIn("cam/realmonitor?channel=1&subtype=0", endpoint.url)
        self.assertIn("unverified_sdk_notice", endpoint.extra_params)
        self.assertIn("KBVision NetSDK unverified", endpoint.extra_params["unverified_sdk_notice"])

    def test_kbvision_probe_socket_check_and_warning(self):
        dev = DeviceInfo(
            device_id="dev_kb_probe",
            vendor="kbvision",
            host="127.0.0.1",
            port=8888,
            rtsp_port=554,
            channel_count=2,
        )
        # Mock socket create_connection to simulate open RTSP port
        def fake_connect(addr, timeout=5.0):
            host, port = addr
            if port == 554:
                return MagicMock()
            raise OSError("Port closed")

        with patch("socket.create_connection", side_effect=fake_connect):
            res = self.kb_adapter.probe(dev)

        self.assertTrue(res.ok)
        self.assertIn("CẢNH BÁO TƯƠNG THÍCH", res.message)
        self.assertIn("NetSDK trên cổng 8888 của KBVision CHƯA ĐƯỢC KIỂM CHỨNG", res.message)
        self.assertEqual(len(res.discovered_channels), 2)

    # -------------------------------------------------------------------------
    # 5. Dahua NetSDK Probing with Mock Backend
    # -------------------------------------------------------------------------

    def test_dahua_probe_via_netsdk_mock_success(self):
        fake_backend = FakeNetSDKBackend(login_ok=True, channel_count=4)
        dev = DeviceInfo(
            device_id="dev_dahua_probe",
            vendor="dahua",
            host="192.168.1.100",
            port=37777,
            user="admin",
            password="password123",
        )
        with patch("camera_modules.dahua._dahua_backend") as mock_mod, \
             patch("camera_modules.dahua.is_dahua_netsdk_available", return_value=True):
            mock_mod.Dahua37777Adapter.return_value = fake_backend
            res = self.dahua_adapter.probe(dev)

        self.assertTrue(res.ok)
        self.assertTrue(res.media_ok)
        self.assertEqual(res.channel_count, 4)
        self.assertEqual(len(res.discovered_channels), 4)
        self.assertEqual(res.discovered_channels[0].channel_id, 1)
        self.assertEqual(res.discovered_channels[3].channel_id, 4)

    def test_dahua_probe_via_dvrip_fallback_when_netsdk_fails(self):
        fake_backend = FakeNetSDKBackend(login_ok=False)
        dev = DeviceInfo(
            device_id="dev_dahua_dvrip",
            vendor="dahua",
            host="192.168.1.101",
            port=37777,
            user="admin",
            password="wrong_password",
        )
        # Simulate NetSDK auth fail, DVRIP also reports auth fail
        with patch("camera_modules.dahua._dahua_backend") as mock_mod, \
             patch("camera_modules.dahua.is_dahua_netsdk_available", return_value=True), \
             patch("camera_modules.dahua.probe_dvrip_auth_safe", return_value=(False, "Dvrip auth failed")):
            mock_mod.Dahua37777Adapter.return_value = fake_backend
            res = self.dahua_adapter.probe(dev)

        self.assertFalse(res.ok)
        self.assertTrue(
            "error" in res.message.lower()
            or "thất bại" in res.message.lower()
            or "password" in res.message.lower()
        )

    # -------------------------------------------------------------------------
    # 6. Multi-Channel Enumeration & Stable ID Integrity
    # -------------------------------------------------------------------------

    def test_enumerate_channels_zero_based_sdk_to_one_based_channel_info(self):
        dev = DeviceInfo(
            device_id="dev_nvr_1",
            vendor="dahua",
            host="192.168.1.20",
            port=37777,
            channel_count=8,
        )
        with patch.object(self.dahua_adapter, "probe") as mock_probe:
            mock_probe.return_value = ProbeResult(
                ok=True,
                device_id="dev_nvr_1",
                vendor="dahua",
                channel_count=8,
                discovered_channels=[],
            )
            channels = self.dahua_adapter.enumerate_channels(dev)

        self.assertEqual(len(channels), 8)
        for idx, ch in enumerate(channels, start=1):
            self.assertEqual(ch.channel_id, idx)
            self.assertEqual(ch.device_id, "dev_nvr_1")
            self.assertIn(f"channel={idx}", ch.main_stream_path)
            self.assertIn("subtype=0", ch.main_stream_path)
            self.assertIn(f"channel={idx}", ch.sub_stream_path)
            self.assertIn("subtype=1", ch.sub_stream_path)

    def test_stable_id_generators_distinct(self):
        # Contract rule: device_id, channel_id, camera_id, and table_id must remain distinct
        dev_id = generate_device_id("dahua", "192.168.1.10", 37777)
        cam_id = generate_camera_id(dev_id, channel_id=2)
        tbl_id = generate_table_id("Bàn 02")

        self.assertEqual(dev_id, "dev_dahua_192_168_1_10_37777")
        self.assertEqual(cam_id, f"cam_{dev_id}_ch2")
        self.assertEqual(tbl_id, "bàn 02")
        self.assertNotEqual(dev_id, cam_id)
        self.assertNotEqual(cam_id, tbl_id)

    # -------------------------------------------------------------------------
    # 7. Credential Protection & Safe Sanitization
    # -------------------------------------------------------------------------

    def test_credentials_never_exposed_in_repr_or_sanitized_dicts(self):
        dev = DeviceInfo(
            device_id="dev_sec",
            vendor="dahua",
            host="192.168.1.1",
            port=37777,
            user="admin",
            password="SuperSecretPassword!",
        )
        repr_str = repr(dev)
        # DeviceInfo uses repr=False on password field, completely preventing password leakage
        self.assertNotIn("SuperSecretPassword!", repr_str)
        self.assertNotIn("password=", repr_str)

        masked_dict = dev.to_dict(include_secrets=False)
        self.assertEqual(masked_dict["password"], "******")

        sanitized = sanitize_dict_for_logging({"user": "admin", "password": "SecretPassword"})
        self.assertEqual(sanitized["password"], "******")

    # -------------------------------------------------------------------------
    # 8. Polymorphic Input Support (DeviceInfo, DeviceConfig, dict)
    # -------------------------------------------------------------------------

    def test_adapter_accepts_device_info_device_config_and_dict(self):
        # 1. DeviceInfo
        d_info = DeviceInfo(device_id="d1", vendor="dahua", host="192.168.1.1", port=37777)
        ep1 = self.dahua_adapter.get_live_stream(d_info, 1, StreamType.MAIN)
        self.assertTrue(ep1.url.startswith("netsdk://") or ep1.url.startswith("rtsp://"))

        # 2. DeviceConfig
        d_cfg = DeviceConfig(device_id="d2", vendor="dahua", host="192.168.1.2", port=37777)
        ep2 = self.dahua_adapter.get_live_stream(d_cfg, 1, StreamType.SUB)
        self.assertTrue(ep2.url.startswith("netsdk://") or ep2.url.startswith("rtsp://"))

        # 3. Legacy dict
        d_dict = {"device_id": "d3", "vendor": "kbvision", "host": "192.168.1.3", "port": 8888}
        ep3 = self.kb_adapter.get_live_stream(d_dict, 1, StreamType.MAIN)
        self.assertTrue(ep3.url.startswith("rtsp://"))

    # -------------------------------------------------------------------------
    # 9. CameraAdapterRegistry & CameraManager Integration
    # -------------------------------------------------------------------------

    def test_registry_and_camera_manager_integration(self):
        registry = get_adapter_registry()
        self.assertTrue(registry.is_supported("dahua"))
        self.assertTrue(registry.is_supported("imou"))
        self.assertTrue(registry.is_supported("kbvision"))

        manager = CameraManager(registry=registry)

        # Register Dahua device
        dev = DeviceInfo(
            device_id="dev_mgr_dahua",
            vendor="dahua",
            host="192.168.1.55",
            port=37777,
            user="admin",
            password="pwd",
            channel_count=2,
        )
        manager.register_device(dev)
        self.assertIsNotNone(manager.get_device("dev_mgr_dahua"))

        # Channel resolution
        stream_main = manager.get_live_stream_endpoint("dev_mgr_dahua", 1, StreamType.MAIN)
        self.assertEqual(stream_main.stream_type, StreamType.MAIN)

        stream_sub = manager.get_live_stream_endpoint("dev_mgr_dahua", 2, StreamType.SUB)
        self.assertEqual(stream_sub.stream_type, StreamType.SUB)

        # Unregister
        manager.unregister_device("dev_mgr_dahua")
        self.assertIsNone(manager.get_device("dev_mgr_dahua"))

    # -------------------------------------------------------------------------
    # 10. Media Delegation (capture_jpeg, record_segment)
    # -------------------------------------------------------------------------

    def test_media_pipeline_delegation_to_dahua_37777(self):
        fake_backend = FakeNetSDKBackend()
        dev = DeviceInfo(
            device_id="dev_media",
            vendor="dahua",
            host="192.168.1.99",
            port=37777,
            user="admin",
            password="pwd",
        )
        with patch("camera_modules.dahua._dahua_backend") as mock_mod, \
             patch("camera_modules.dahua.is_dahua_netsdk_available", return_value=True):
            mock_mod.Dahua37777Adapter.return_value = fake_backend

            # 1. capture_jpeg
            jpeg_bytes = self.dahua_adapter.capture_jpeg(dev, channel_id=1, ffmpeg_path="ffmpeg")
            self.assertTrue(jpeg_bytes.startswith(b"\xff\xd8"))
            self.assertTrue(fake_backend.capture_called)

            # 2. record_segment
            rec_result = self.dahua_adapter.record_segment(
                dev, channel_id=1, output_path="out.mp4", duration=5.0, ffmpeg_path="ffmpeg"
            )
            self.assertTrue(rec_result["ok"])
            self.assertTrue(fake_backend.record_called)

            # 3. iter_jpeg_frames
            frames = list(self.dahua_adapter.iter_jpeg_frames(dev, channel_id=1, ffmpeg_path="ffmpeg"))
            self.assertEqual(len(frames), 2)


if __name__ == "__main__":
    unittest.main()
