"""
Unit and integration tests for Hikvision and Ezviz SDK adapter (camera_modules/hikvision.py).
Tests native HCNetSDK and Sadp.dll loading, discovery, channel inventory, and fallbacks.
"""

import ctypes as C
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from camera_modules.hikvision import (
    DEFAULT_HIKVISION_HTTP_PORT,
    DEFAULT_HIKVISION_RTSP_PORT,
    DEFAULT_HIKVISION_SDK_PORT,
    HIKVISION_SDK_ERRORS,
    NET_DVR_DEVICEINFO_V30,
    NET_DVR_DEVICEINFO_V40,
    NET_DVR_DIGITAL_CHANNEL_STATE,
    NET_DVR_PREVIEWINFO,
    NET_DVR_USER_LOGIN_INFO,
    HikvisionAdapter,
    HikvisionAuthError,
    HikvisionCapabilities,
    HikvisionChannel,
    HikvisionConnectionError,
    HikvisionDeviceInfo,
    HikvisionError,
    HikvisionSDKNotFoundError,
    ProbeResult,
    SADP_DEVICE_INFO,
    build_hikvision_isapi_endpoints,
    build_hikvision_rtsp_paths,
    build_hikvision_rtsp_url,
    detect_device_kind,
    discover_hikvision_sadp,
    find_hcnetsdk_dir,
    find_sadp_dir,
    get_hikvision_capabilities,
    hcnetsdk_available,
    is_hikvision_compatible,
    probe_hikvision_device,
    sadp_available,
)


class TestHikvisionSDKAvailability(unittest.TestCase):
    """Test SDK presence and loading on Windows."""

    def test_hcnetsdk_is_available(self):
        self.assertTrue(
            hcnetsdk_available(),
            "HCNetSDK.dll should be available in vendor/hikvision_netsdk or iVMS path.",
        )

    def test_find_hcnetsdk_dir(self):
        sdk_dir = find_hcnetsdk_dir()
        self.assertIsNotNone(sdk_dir)
        self.assertTrue(os.path.isdir(sdk_dir))
        self.assertTrue(os.path.isfile(os.path.join(sdk_dir, "HCNetSDK.dll")))

    def test_sadp_is_available(self):
        self.assertTrue(
            sadp_available(),
            "Sadp.dll should be available in vendor/hikvision_netsdk or SADP/iVMS path.",
        )

    def test_find_sadp_dir(self):
        sadp_dir = find_sadp_dir()
        self.assertIsNotNone(sadp_dir)
        self.assertTrue(os.path.isdir(sadp_dir))
        self.assertTrue(os.path.isfile(os.path.join(sadp_dir, "Sadp.dll")))


class TestHikvisionRTSPAndISAPI(unittest.TestCase):
    """Test URL builders and path generators for Hikvision / Ezviz."""

    def test_rtsp_paths_channel_1(self):
        res = build_hikvision_rtsp_paths(channel=1, stream="main")
        self.assertEqual(res["primary_path"], "Streaming/Channels/101")
        self.assertEqual(res["legacy_path"], "h264/ch1/main/av_stream")
        self.assertEqual(res["track_chan"], 101)

    def test_rtsp_paths_channel_2_sub(self):
        res = build_hikvision_rtsp_paths(channel=2, stream="sub")
        self.assertEqual(res["primary_path"], "Streaming/Channels/202")
        self.assertEqual(res["legacy_path"], "h264/ch2/sub/av_stream")
        self.assertEqual(res["track_chan"], 202)

    def test_rtsp_url_construction(self):
        url = build_hikvision_rtsp_url(
            host="192.168.1.202",
            port=554,
            username="admin",
            password="pwd@123",
            channel=1,
            stream="main",
        )
        self.assertIn("192.168.1.202:554/Streaming/Channels/101", url)
        self.assertTrue(url.startswith("rtsp://admin:pwd%40123@"))

    def test_isapi_endpoints(self):
        endpoints = build_hikvision_isapi_endpoints(host="192.168.1.202", port=80, use_https=False)
        self.assertEqual(endpoints["device_info"], "http://192.168.1.202:80/ISAPI/System/deviceInfo")
        self.assertEqual(endpoints["channels"], "http://192.168.1.202:80/ISAPI/System/Video/inputs/channels")


class TestHikvisionDeviceKindDetection(unittest.TestCase):
    """Test autonomous classification of device kind (IPC, NVR, DVR)."""

    def test_single_channel_ipc(self):
        self.assertEqual(detect_device_kind(analog_count=0, digital_count=1), "ipc")
        self.assertEqual(detect_device_kind(analog_count=1, digital_count=0), "ipc")

    def test_nvr_classification(self):
        self.assertEqual(detect_device_kind(analog_count=0, digital_count=8), "nvr")
        self.assertEqual(detect_device_kind(analog_count=0, digital_count=16, model="DS-7616NI-K2"), "nvr")

    def test_dvr_classification(self):
        self.assertEqual(detect_device_kind(analog_count=4, digital_count=0), "dvr")
        self.assertEqual(detect_device_kind(analog_count=4, digital_count=2), "dvr")


class TestHikvisionVendorCompatibility(unittest.TestCase):
    """Test vendor string checks."""

    def test_vendor_strings(self):
        self.assertTrue(is_hikvision_compatible("hikvision"))
        self.assertTrue(is_hikvision_compatible("Hikvision"))
        self.assertTrue(is_hikvision_compatible("ezviz"))
        self.assertTrue(is_hikvision_compatible("EZVIZ_CAMERA"))
        self.assertFalse(is_hikvision_compatible("dahua"))
        self.assertFalse(is_hikvision_compatible("unknown"))


class TestHikvisionSADPDiscovery(unittest.TestCase):
    """Test native Sadp.dll LAN discovery."""

    def test_sadp_discovery_execution(self):
        if not sadp_available():
            self.skipTest("Sadp.dll is not available on this platform.")
        devices = discover_hikvision_sadp(timeout_sec=1.5)
        self.assertIsInstance(devices, list)
        for dev in devices:
            self.assertIn("ip", dev)
            self.assertIn("port", dev)
            self.assertIn("serial", dev)
            self.assertIn("mac", dev)
            self.assertIn("vendor", dev)
            self.assertIn(dev["vendor"], {"hikvision", "ezviz"})


class TestHikvisionABIAndInventory(unittest.TestCase):
    """Lock down the HCNetSDK ABI and physical SDK channel mapping."""

    def test_ctypes_abi_sizes_match_x64_hcnetsdk_headers(self):
        # These structures contain explicit fixed-width C types. Pointer fields
        # make USER_LOGIN/PREVIEWINFO x64-specific in this packaged Windows app.
        self.assertEqual(C.sizeof(NET_DVR_DEVICEINFO_V30), 80)
        self.assertEqual(C.sizeof(NET_DVR_DEVICEINFO_V40), 344)
        self.assertEqual(C.sizeof(NET_DVR_USER_LOGIN_INFO), 416)
        self.assertEqual(C.sizeof(NET_DVR_DIGITAL_CHANNEL_STATE), 580)
        self.assertEqual(C.sizeof(NET_DVR_PREVIEWINFO), 288)
        fields = {name for name, *_ in NET_DVR_DEVICEINFO_V30._fields_}
        self.assertIn("byHighDChanNum", fields)
        self.assertNotIn("byHighIPChanNum", fields)

    def test_digital_inventory_filters_empty_slots_and_keeps_sdk_channel_numbers(self):
        adapter = HikvisionAdapter("192.0.2.20", "admin", "test", port=8000)
        info = HikvisionDeviceInfo(
            device_id="unit-nvr",
            vendor="hikvision",
            model="DS-7604",
            serial_number="unit",
            device_kind="nvr",
            analog_channels=0,
            digital_channels=4,
            total_channels=4,
            start_analog_channel=1,
            start_digital_channel=33,
            dvr_type=0,
        )
        with patch.object(adapter, "get_device_info", return_value=info), patch.object(
            adapter,
            "get_digital_channel_states",
            return_value={1: 1, 2: 11, 3: 6, 4: 0},
        ):
            channels = adapter.get_channel_inventory()

        self.assertEqual([ch.sdk_channel_number for ch in channels], [33, 35])
        self.assertEqual([ch.is_online for ch in channels], [True, False])
        self.assertEqual([ch.status_text for ch in channels], ["connected", "account_error"])
        self.assertEqual([ch.channel_id for ch in channels], [1, 2])

    def test_inventory_does_not_claim_online_when_state_query_is_unavailable(self):
        adapter = HikvisionAdapter("192.0.2.20", "admin", "test", port=8000)
        info = HikvisionDeviceInfo(
            device_id="unit-nvr",
            vendor="hikvision",
            model="DS-7602",
            serial_number="unit",
            device_kind="nvr",
            analog_channels=0,
            digital_channels=2,
            total_channels=2,
            start_analog_channel=1,
            start_digital_channel=33,
            dvr_type=0,
        )
        with patch.object(adapter, "get_device_info", return_value=info), patch.object(
            adapter, "get_digital_channel_states", return_value={}
        ):
            channels = adapter.get_channel_inventory()

        self.assertEqual([ch.sdk_channel_number for ch in channels], [33, 34])
        self.assertTrue(all(ch.is_online is None for ch in channels))
        self.assertTrue(all(ch.status_text == "unknown" for ch in channels))


class TestHikvisionAdapterMocked(unittest.TestCase):
    """Test adapter behavior with simulated/mocked SDK handles."""

    def test_adapter_repr_never_leaks_password(self):
        adapter = HikvisionAdapter(
            host="192.168.1.202",
            username="admin",
            password="SuperSecretPassword123!",
            port=8000,
        )
        repr_str = repr(adapter)
        self.assertNotIn("SuperSecretPassword123!", repr_str)
        self.assertIn("192.168.1.202", repr_str)
        self.assertIn("admin", repr_str)

    def test_probe_offline_device(self):
        # Non-routable or unused IP
        res = probe_hikvision_device(
            host="192.0.2.1",
            username="admin",
            password="password",
            port=8000,
        )
        self.assertIsInstance(res, ProbeResult)
        self.assertFalse(res.ok)
        self.assertEqual(res.transport, "hcnetsdk")


if __name__ == "__main__":
    unittest.main()
