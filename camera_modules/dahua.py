"""Dahua, Imou, and KBVision camera module adapter for Cambida.

Provides native NetSDK 37777 integration (reusing dahua_37777.py and vendor/dahua_netsdk),
DVRIP challenge-response authentication, channel enumeration (stuDeviceInfo.nChanNum),
live stream endpoint resolution (NetSDK RealPlay and RTSP fallback), JPEG snapshot/frame
extraction via FFmpeg pipe, and segment recording.

Follows .project/CAMERA_MODULE_CONTRACT_20260924.md:
- Separate IDs for devices, physical channels, and stable table bindings.
- Vendor choice: Dahua / Imou (default port 37777), KBVision (default port 8888).
  All ports are editable.
- CAUTION ON KBVISION & EZVIZ COMPATIBILITY:
  Does NOT assume or claim KBVision NetSDK compatibility on port 8888 without
  prior on-device verification. While KBVision often shares Dahua OEM roots,
  firmware variations and non-standard management ports mean NetSDK operation
  is treated as UNVERIFIED. Primary reliable stream transport for KBVision
  is RTSP (cam/realmonitor) on port 554.
- Capabilities strictly reported; unsupported operations (e.g. remote recording search
  or PTZ via NetSDK) are NOT claimed and raise UnsupportedCapabilityError.
- Never exposes or logs passwords, tokens, or plaintext secrets.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import os
import queue
import re
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import (
    Any,
    Callable,
    Dict,
    Generator,
    Iterable,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

logger = logging.getLogger("cambida.camera_modules.dahua")

# =============================================================================
# Core Abstractions & Safe Fallbacks
# =============================================================================

try:
    from camera_modules.core import (
        BaseCameraAdapter,
        CameraCapability,
        ChannelInfo,
        DeviceInfo,
        DownloadResult,
        ProbeResult as CoreProbeResult,
        RecordingSegment,
        StreamEndpoint,
        StreamType,
        TableColorState,
        TransportProtocol,
        VendorType,
        CameraModuleError,
        UnsupportedCapabilityError,
        CameraConnectionError,
        AuthenticationError,
        DeviceNotFoundError,
        ChannelNotFoundError,
        generate_camera_id,
        generate_device_id,
        generate_table_id,
        get_adapter_registry,
        mask_credential,
        sanitize_dict_for_logging,
        sanitize_slug,
    )
except ImportError:
    BaseCameraAdapter = object  # type: ignore[misc,assignment]
    CameraCapability = None  # type: ignore[misc,assignment]
    ChannelInfo = None  # type: ignore[misc,assignment]
    DeviceInfo = None  # type: ignore[misc,assignment]
    DownloadResult = None  # type: ignore[misc,assignment]
    CoreProbeResult = None  # type: ignore[misc,assignment]
    RecordingSegment = None  # type: ignore[misc,assignment]
    StreamEndpoint = None  # type: ignore[misc,assignment]
    StreamType = None  # type: ignore[misc,assignment]
    TableColorState = None  # type: ignore[misc,assignment]
    TransportProtocol = None  # type: ignore[misc,assignment]
    VendorType = None  # type: ignore[misc,assignment]
    CameraModuleError = Exception  # type: ignore[misc,assignment]
    UnsupportedCapabilityError = Exception  # type: ignore[misc,assignment]
    CameraConnectionError = Exception  # type: ignore[misc,assignment]
    AuthenticationError = Exception  # type: ignore[misc,assignment]
    DeviceNotFoundError = Exception  # type: ignore[misc,assignment]
    ChannelNotFoundError = Exception  # type: ignore[misc,assignment]
    get_adapter_registry = None  # type: ignore[misc,assignment]

    def mask_credential(val: Optional[str]) -> str:
        return "******" if val else ""

    def sanitize_dict_for_logging(data: Dict[str, Any]) -> Dict[str, Any]:
        return dict(data)

    def generate_device_id(vendor: str, host: str, port: int, custom_id: Optional[str] = None) -> str:
        return str(custom_id) if custom_id else f"dev_{vendor}_{host.replace('.', '_')}_{port}"

    def generate_camera_id(device_id: str, channel_id: int, custom_id: Optional[Union[str, int]] = None) -> str:
        return str(custom_id) if custom_id is not None else f"cam_{device_id}_ch{channel_id}"

    def generate_table_id(raw_id: Union[str, int]) -> str:
        return str(raw_id).strip().lower()

try:
    from camera_modules.config import (
        DeviceConfig,
        VENDOR_DEFAULT_PORTS,
        RTSP_PRESETS,
        get_vendor_default_port,
        build_rtsp_stream_path,
        build_rtsp_url,
    )
except ImportError:
    DeviceConfig = None  # type: ignore[misc,assignment]
    VENDOR_DEFAULT_PORTS = {
        "dahua": {"primary": 37777, "http": 81, "rtsp": 554},
        "imou": {"primary": 37777, "http": 81, "rtsp": 554},
        "kbvision": {"primary": 8888, "http": 80, "rtsp": 554},
    }
    RTSP_PRESETS = {
        "dahua": {
            "record_path": "cam/realmonitor?channel={channel}&subtype=0",
            "preview_path": "cam/realmonitor?channel={channel}&subtype=1",
        },
        "kbvision": {
            "record_path": "cam/realmonitor?channel={channel}&subtype=0",
            "preview_path": "cam/realmonitor?channel={channel}&subtype=1",
        },
    }

    def build_rtsp_url(host: str, port: int = 554, path: str = "", user: Optional[str] = None, password: Optional[str] = None) -> str:
        clean_path = str(path or "").lstrip("/")
        port_str = f":{port}" if port and port != 554 else ""
        if user:
            return f"rtsp://{user}:{password or ''}@{host}{port_str}/{clean_path}"
        return f"rtsp://{host}{port_str}/{clean_path}"

    def build_rtsp_stream_path(vendor: str, channel: int = 1, stream: Any = "main") -> str:
        subtype = 0 if getattr(stream, "value", str(stream)).lower() in {"main", "record", "0"} else 1
        return f"cam/realmonitor?channel={channel}&subtype={subtype}"


# =============================================================================
# NetSDK / dahua_37777 Backend Dynamic Loader
# =============================================================================

_dahua_backend = None
try:
    import dahua_37777 as _dahua_backend
except ImportError:
    parent_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    try:
        import dahua_37777 as _dahua_backend
    except ImportError:
        _dahua_backend = None


# =============================================================================
# Constants & Defaults
# =============================================================================

DEFAULT_DAHUA_NETSDK_PORT = 37777
DEFAULT_DAHUA_HTTP_PORT = 81
DEFAULT_DAHUA_RTSP_PORT = 554

DEFAULT_KBVISION_PORT = 8888
DEFAULT_KBVISION_HTTP_PORT = 80
DEFAULT_KBVISION_RTSP_PORT = 554

SUPPORTED_VENDORS = {"dahua", "imou", "kbvision"}


# =============================================================================
# Exceptions
# =============================================================================

class DahuaAdapterError(CameraModuleError):
    """Base exception for Dahua adapter errors."""


class DahuaNetSDKNotFoundError(DahuaAdapterError):
    """Raised when Dahua NetSDK binaries (dhnetsdk.dll) are not found."""


class DahuaAuthenticationError(DahuaAdapterError, AuthenticationError):
    """Raised when Dahua / Imou / KBVision authentication fails."""


class DahuaConnectionError(DahuaAdapterError, CameraConnectionError):
    """Raised when network connection to Dahua device fails."""


class DahuaChannelError(DahuaAdapterError, ChannelNotFoundError):
    """Raised when a specified Dahua channel is invalid or unavailable."""


# =============================================================================
# NetSDK Environment & Discovery Helpers
# =============================================================================

def find_dahua_netsdk_dir(base_dir: Optional[str] = None) -> Optional[str]:
    """Locate Dahua NetSDK directory containing dhnetsdk.dll."""
    if _dahua_backend is not None and hasattr(_dahua_backend, "find_netsdk_dir"):
        return _dahua_backend.find_netsdk_dir(base_dir)

    for value in (
        os.environ.get("DAHUA_NETSDK_DIR"),
        base_dir if base_dir and os.path.isfile(os.path.join(base_dir, "dhnetsdk.dll")) else None,
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor", "dahua_netsdk"),
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "netsdk"),
    ):
        if value and os.path.isfile(os.path.join(value, "dhnetsdk.dll")):
            return os.path.abspath(value)
    return None


def is_dahua_netsdk_available(base_dir: Optional[str] = None) -> bool:
    """Return True if Dahua NetSDK DLL is present on the filesystem."""
    return find_dahua_netsdk_dir(base_dir) is not None


# =============================================================================
# Standalone DVRIP Challenge-Response Helper (Fallback if backend unloaded)
# =============================================================================

def _fallback_dahua_gen1_hash(password: str) -> str:
    digest = hashlib.md5(str(password).encode("latin-1")).digest()
    output = []
    for index in range(0, len(digest), 2):
        value = (digest[index] + digest[index + 1]) % 62
        if value < 10:
            value += 48
        elif value < 36:
            value += 55
        else:
            value += 61
        output.append(chr(value))
    return "".join(output)


def _fallback_dahua_dvrip_md5(random_value: str, username: str, password: str) -> str:
    value = f"{username}:{random_value}:{_fallback_dahua_gen1_hash(password)}"
    return hashlib.md5(value.encode("latin-1")).hexdigest().upper()


def _fallback_dahua_gen2_md5(random_value: str, realm: str, username: str, password: str) -> str:
    password_db = hashlib.md5(f"{username}:{realm}:{password}".encode("latin-1")).hexdigest().upper()
    value = f"{username}:{random_value}:{password_db}"
    return hashlib.md5(value.encode("latin-1")).hexdigest().upper()


def _fallback_recv_exact(sock: socket.socket, length: int) -> bytes:
    chunks = []
    remaining = int(length)
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise DahuaConnectionError("Kết nối DVRIP đóng trước khi nhận đủ dữ liệu.", "", 0)
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _fallback_recv_dvrip_packet(sock: socket.socket) -> Tuple[bytes, bytes]:
    header = _fallback_recv_exact(sock, 32)
    if header[:2] not in {b"\xb0\x00", b"\xb0\x01", b"\xf6\x00", b"\xa0\x01", b"\xa0\x05"}:
        raise DahuaConnectionError(f"Phản hồi DVRIP không hợp lệ ({header[:2].hex()}).", "", 0)
    payload_len = struct.unpack("<I", header[4:8])[0]
    if payload_len > 4 * 1024 * 1024:
        raise DahuaConnectionError("Phản hồi DVRIP vượt giới hạn an toàn.", "", 0)
    payload = _fallback_recv_exact(sock, payload_len) if payload_len else b""
    return header, payload


def probe_dvrip_auth_safe(
    host: str,
    username: str,
    password: str,
    port: int = 37777,
    timeout: float = 5.0,
) -> Tuple[bool, str, str]:
    """Perform Dahua DVRIP challenge/login check on port 37777 without leaking credentials.

    Returns: (ok, message, error_code)
    """
    if _dahua_backend is not None and hasattr(_dahua_backend, "probe_dvrip_auth"):
        try:
            res = _dahua_backend.probe_dvrip_auth(host, username, password, port=port, timeout=timeout)
            return res.ok, res.message, res.error_code
        except Exception as exc:
            return False, f"Lỗi xác thực DVRIP: {exc}", "DVRIP_EXC"

    # Pure Python socket fallback
    try:
        resolved = socket.gethostbyname(host)
    except OSError as exc:
        return False, f"Không phân giải được địa chỉ camera: {exc}", "DNS_FAIL"

    realm_header = struct.pack(">I", 0xA0010000) + (b"\x00" * 20) + struct.pack(">Q", 0x050201010000A1AA)
    try:
        with socket.create_connection((resolved, port), timeout=float(timeout)) as sock:
            sock.settimeout(float(timeout))
            sock.sendall(realm_header)
            _, realm_payload = _fallback_recv_dvrip_packet(sock)
            text = realm_payload.decode("latin-1", errors="replace")
            fields: Dict[str, str] = {}
            for line in text.splitlines():
                key, sep, value = line.partition(":")
                if sep:
                    fields[key.strip().lower()] = value.strip()
            realm = fields.get("realm")
            random_value = fields.get("random")
            if not realm or not random_value:
                return False, "DVRIP không trả Realm/Random hợp lệ.", "NO_REALM"

            challenge = (
                username
                + "&&"
                + _fallback_dahua_gen2_md5(random_value, realm, username, password)
                + _fallback_dahua_dvrip_md5(random_value, username, password)
            ).encode("latin-1")
            login_header = (
                struct.pack(">I", 0xA0050000)
                + struct.pack("<I", len(challenge))
                + (b"\x00" * 16)
                + struct.pack(">Q", 0x050200080000A1AA)
            )
            sock.sendall(login_header + challenge)
            response_header, _ = _fallback_recv_dvrip_packet(sock)
            error_code = response_header[8:12].hex()
            status = error_code[:4]
            status_messages = {
                "0008": "DVRIP 37777 đăng nhập thành công.",
                "0100": "DVRIP 37777 từ chối mật khẩu.",
                "0101": "DVRIP 37777 từ chối tài khoản.",
                "0104": "Tài khoản DVRIP 37777 đang bị khóa.",
                "0105": "DVRIP 37777 trả mã xác thực không xác định.",
                "0113": "DVRIP 37777 không hỗ trợ kiểu đăng nhập này.",
                "0303": "Tài khoản DVRIP 37777 đã có phiên kết nối.",
            }
            ok = status == "0008"
            return ok, status_messages.get(status, f"DVRIP 37777 trả mã xác thực {status}."), error_code
    except Exception as exc:
        return False, f"Không thể kết nối DVRIP 37777: {exc}", "CONN_FAIL"


# =============================================================================
# RTSP URL Builders for Dahua & KBVision
# =============================================================================

def build_dahua_rtsp_paths(channel: int = 1, stream: str = "main") -> Dict[str, str]:
    """Build canonical Dahua / Imou RTSP paths for a channel.

    Channel is 1-based.
    Subtype: 0 for Main, 1 for Sub.
    """
    ch = max(1, int(channel))
    sub = 1 if str(stream).strip().lower() in {"sub", "preview", "1"} else 0
    return {
        "primary_path": f"cam/realmonitor?channel={ch}&subtype={sub}",
        "main_path": f"cam/realmonitor?channel={ch}&subtype=0",
        "sub_path": f"cam/realmonitor?channel={ch}&subtype=1",
    }


def build_dahua_rtsp_url(
    host: str,
    port: int = DEFAULT_DAHUA_RTSP_PORT,
    channel: int = 1,
    stream: str = "main",
    user: Optional[str] = None,
    password: Optional[str] = None,
) -> str:
    """Build fully qualified RTSP URL for Dahua / Imou device."""
    paths = build_dahua_rtsp_paths(channel=channel, stream=stream)
    return build_rtsp_url(
        host=host,
        port=port,
        path=paths["primary_path"],
        user=user,
        password=password,
    )


def build_kbvision_rtsp_url(
    host: str,
    port: int = DEFAULT_KBVISION_RTSP_PORT,
    channel: int = 1,
    stream: str = "main",
    user: Optional[str] = None,
    password: Optional[str] = None,
) -> str:
    """Build fully qualified RTSP URL for KBVision device."""
    # KBVision uses standard Dahua-compatible realmonitor query paths on port 554
    paths = build_dahua_rtsp_paths(channel=channel, stream=stream)
    return build_rtsp_url(
        host=host,
        port=port,
        path=paths["primary_path"],
        user=user,
        password=password,
    )


# =============================================================================
# Dahua / Imou Camera Adapter
# =============================================================================

class DahuaCameraAdapter(BaseCameraAdapter):
    """Camera module adapter for Dahua and Imou devices.

    Implements BaseCameraAdapter and CameraAdapterProtocol:
    - NetSDK 37777 high-level security login, channel count detection,
      RealPlay, frame streaming, and DAV->MP4 segment recording.
    - DVRIP challenge-response login diagnostic when NetSDK DLL is not loaded.
    - RTSP URL generation fallback (cam/realmonitor?channel={ch}&subtype={sub}).
    - Enforces separate stable identifiers (device_id, physical channel_id).
    - Unimplemented operations (remote recording search / PTZ via NetSDK)
      strictly raise UnsupportedCapabilityError and are not claimed.
    - Credentials are never logged in plaintext or leaked.
    """

    vendor: str = "dahua"
    default_port: int = DEFAULT_DAHUA_NETSDK_PORT
    default_http_port: int = DEFAULT_DAHUA_HTTP_PORT
    default_rtsp_port: int = DEFAULT_DAHUA_RTSP_PORT

    def __init__(
        self,
        base_dir: Optional[str] = None,
        supported_capabilities: Optional[Iterable[CameraCapability]] = None,
    ) -> None:
        self.base_dir = base_dir
        super().__init__(supported_capabilities=supported_capabilities)

    def _default_capabilities(self) -> Set[CameraCapability]:
        caps = set()
        if CameraCapability is not None:
            caps.update(
                {
                    CameraCapability.CONNECT,
                    CameraCapability.LIVE_MAIN,
                    CameraCapability.LIVE_SUB,
                    CameraCapability.ENUMERATE_CHANNELS,
                    CameraCapability.DISCOVER,
                }
            )
        return caps

    def discover(self, timeout_sec: float = 3.0, **kwargs: Any) -> List[DeviceInfo]:
        """Discover Dahua / Imou / KBVision devices on the local subnet via UDP broadcast."""
        self.require_capability(CameraCapability.DISCOVER)
        devices: List[DeviceInfo] = []
        try:
            from camera_modules.discovery import discover_dahua_devices
            discovered = discover_dahua_devices(timeout_sec=timeout_sec)
            for d in discovered:
                dev_id = getattr(d, "device_id", "") or generate_device_id(self.vendor, getattr(d, "ip", ""), getattr(d, "port", self.default_port))
                if DeviceInfo is not None:
                    devices.append(
                        DeviceInfo(
                            device_id=dev_id,
                            vendor=self.vendor,
                            host=getattr(d, "ip", ""),
                            port=getattr(d, "port", self.default_port),
                            http_port=getattr(d, "http_port", self.default_http_port),
                            rtsp_port=getattr(d, "rtsp_port", self.default_rtsp_port),
                            name=getattr(d, "name", ""),
                            model=getattr(d, "model", None),
                            channel_count=max(1, int(getattr(d, "channel_count", 1) or 1)),
                            extra=getattr(d, "extra", {}),
                        )
                    )
        except Exception as exc:
            logger.debug("Dahua discovery scan: %s", exc)
        return devices

    def connect(self, device: DeviceInfo, timeout_sec: float = 5.0) -> bool:
        """Verify network connectivity and credentials with the Dahua device."""
        self.require_capability(CameraCapability.CONNECT)
        device = self._ensure_device_info(device)
        probe_res = self.probe(device, timeout_sec=timeout_sec)
        return bool(probe_res.ok)

    def probe(self, device: DeviceInfo, timeout_sec: float = 5.0) -> CoreProbeResult:
        """Perform full capability probe on Dahua device.

        1. If port is 37777 and NetSDK is available: attempts NetSDK login,
           retrieves actual hardware channel count from stuDeviceInfo.nChanNum.
        2. If NetSDK is not available or port is 37777: attempts DVRIP auth probe.
        3. If port is RTSP/custom: tests TCP connection on port.
        """
        self.require_capability(CameraCapability.CONNECT)
        device = self._ensure_device_info(device)
        t0 = time.monotonic()
        port = device.port or self.default_port
        dev_id = getattr(device, "device_id", "") or generate_device_id(self.vendor, device.host, port)
        sdk_dir = getattr(device, "extra", {}).get("sdk_dir") or self.base_dir

        # Attempt 1: NetSDK Native Probe (Windows dhnetsdk.dll)
        if port == DEFAULT_DAHUA_NETSDK_PORT and is_dahua_netsdk_available(sdk_dir) and _dahua_backend is not None:
            try:
                adapter = _dahua_backend.Dahua37777Adapter(
                    host=device.host,
                    username=device.user,
                    password=device.password,
                    port=port,
                    channel=0,
                    stream="main",
                    sdk_dir=sdk_dir,
                )
                sdk_res = adapter.probe(require_media=False, media_seconds=1.0)
                latency_ms = (time.monotonic() - t0) * 1000.0

                if sdk_res.ok:
                    detected_channels = max(1, int(sdk_res.channels or device.channel_count or 1))
                    channels = self._generate_channels(device, detected_channels)
                    return CoreProbeResult(
                        ok=True,
                        device_id=dev_id,
                        vendor=self.vendor,
                        media_ok=sdk_res.media_ok,
                        channel_count=detected_channels,
                        message=f"Dahua NetSDK 37777 kết nối thành công ({detected_channels} kênh).",
                        discovered_channels=channels,
                        latency_ms=latency_ms,
                    )
                else:
                    return CoreProbeResult(
                        ok=False,
                        device_id=dev_id,
                        vendor=self.vendor,
                        media_ok=False,
                        channel_count=0,
                        message=sdk_res.message or "Đăng nhập NetSDK thất bại.",
                        error_code=f"0x{sdk_res.sdk_error:08X}" if sdk_res.sdk_error else None,
                        latency_ms=latency_ms,
                    )
            except Exception as exc:
                logger.debug("NetSDK probe failed, trying DVRIP fallback: %s", exc)

        # Attempt 2: DVRIP Socket Challenge-Response Probe (Port 37777)
        if port == DEFAULT_DAHUA_NETSDK_PORT:
            ok, msg, err_code = probe_dvrip_auth_safe(
                host=device.host,
                username=device.user,
                password=device.password,
                port=port,
                timeout=timeout_sec,
            )
            latency_ms = (time.monotonic() - t0) * 1000.0
            if ok:
                channel_count = max(1, int(getattr(device, "channel_count", 1) or 1))
                channels = self._generate_channels(device, channel_count)
                status_msg = (
                    "Dahua DVRIP 37777 xác thực thành công. "
                    "Ghi chú: NetSDK DLL chưa tải; luồng trực tiếp sẽ dùng RTSP (cam/realmonitor)."
                )
                return CoreProbeResult(
                    ok=True,
                    device_id=dev_id,
                    vendor=self.vendor,
                    media_ok=False,
                    channel_count=channel_count,
                    message=status_msg,
                    discovered_channels=channels,
                    latency_ms=latency_ms,
                )
            else:
                return CoreProbeResult(
                    ok=False,
                    device_id=dev_id,
                    vendor=self.vendor,
                    media_ok=False,
                    channel_count=0,
                    message=msg,
                    error_code=err_code,
                    latency_ms=latency_ms,
                )

        # Attempt 3: General Socket Reachability (Custom port or RTSP port)
        socket_ok = False
        try:
            with socket.create_connection((device.host, port), timeout=float(timeout_sec)):
                socket_ok = True
        except OSError as exc:
            socket_ok = False
            msg = f"Không thể kết nối cổng {port}: {exc}"

        latency_ms = (time.monotonic() - t0) * 1000.0
        channel_count = max(1, int(getattr(device, "channel_count", 1) or 1))
        channels = self._generate_channels(device, channel_count)

        if socket_ok:
            return CoreProbeResult(
                ok=True,
                device_id=dev_id,
                vendor=self.vendor,
                media_ok=False,
                channel_count=channel_count,
                message=f"Đã mở kết nối cổng {port}. Luồng media dùng RTSP cổng {device.rtsp_port or 554}.",
                discovered_channels=channels,
                latency_ms=latency_ms,
            )
        else:
            return CoreProbeResult(
                ok=False,
                device_id=dev_id,
                vendor=self.vendor,
                media_ok=False,
                channel_count=0,
                message=f"Không thể kết nối {device.host}:{port}.",
                latency_ms=latency_ms,
            )

    def _generate_channels(self, device: DeviceInfo, count: int) -> List[ChannelInfo]:
        """Generate structured ChannelInfo descriptors for a device."""
        channels: List[ChannelInfo] = []
        dev_id = getattr(device, "device_id", "") or generate_device_id(self.vendor, device.host, device.port)
        rtsp_port = getattr(device, "rtsp_port", None) or DEFAULT_DAHUA_RTSP_PORT

        for i in range(1, count + 1):
            paths = build_dahua_rtsp_paths(channel=i)
            main_url = build_rtsp_url(
                host=device.host,
                port=rtsp_port,
                path=paths["main_path"],
                user=device.user,
                password=device.password,
            )
            sub_url = build_rtsp_url(
                host=device.host,
                port=rtsp_port,
                path=paths["sub_path"],
                user=device.user,
                password=device.password,
            )

            if ChannelInfo is not None:
                channels.append(
                    ChannelInfo(
                        channel_id=i,
                        device_id=dev_id,
                        name=f"Kênh {i}",
                        enabled=True,
                        main_stream_path=paths["main_path"],
                        sub_stream_path=paths["sub_path"],
                        main_stream_url=main_url,
                        sub_stream_url=sub_url,
                        is_online=True,
                    )
                )
            else:
                channels.append(  # type: ignore[arg-type]
                    {
                        "channel_id": i,
                        "device_id": dev_id,
                        "name": f"Kênh {i}",
                        "enabled": True,
                        "main_stream_path": paths["main_path"],
                        "sub_stream_path": paths["sub_path"],
                        "main_stream_url": main_url,
                        "sub_stream_url": sub_url,
                    }
                )
        return channels

    def enumerate_channels(self, device: DeviceInfo, timeout_sec: float = 5.0) -> List[ChannelInfo]:
        """Enumerate physical channels available on Dahua device."""
        self.require_capability(CameraCapability.ENUMERATE_CHANNELS)
        device = self._ensure_device_info(device)
        probe_res = self.probe(device, timeout_sec=timeout_sec)
        if probe_res.discovered_channels:
            return probe_res.discovered_channels
        count = max(1, int(getattr(device, "channel_count", 1) or 1))
        return self._generate_channels(device, count)

    def get_live_stream(
        self,
        device: DeviceInfo,
        channel_id: int,
        stream: StreamType = StreamType.MAIN,
    ) -> StreamEndpoint:
        """Resolve live stream endpoint for Dahua / Imou camera channel.

        Returns StreamEndpoint configured with NetSDK or RTSP transport based
        on device configuration and availability.
        """
        device = self._ensure_device_info(device)
        ch = max(1, int(channel_id))
        is_sub = getattr(stream, "value", str(stream)).lower() in {"sub", "preview", "1"}
        sub_type = 1 if is_sub else 0
        st = StreamType.SUB if is_sub else StreamType.MAIN

        paths = build_dahua_rtsp_paths(channel=ch, stream="sub" if is_sub else "main")
        rtsp_port = getattr(device, "rtsp_port", None) or DEFAULT_DAHUA_RTSP_PORT
        rtsp_url = build_rtsp_url(
            host=device.host,
            port=rtsp_port,
            path=paths["primary_path"],
            user=device.user,
            password=device.password,
        )

        # Check if direct NetSDK is requested and operational
        wants_netsdk = (
            getattr(device, "extra", {}).get("local_transport") == "netsdk"
            or device.port == DEFAULT_DAHUA_NETSDK_PORT
        )
        sdk_dir = getattr(device, "extra", {}).get("sdk_dir") or self.base_dir
        can_netsdk = wants_netsdk and is_dahua_netsdk_available(sdk_dir) and _dahua_backend is not None

        if can_netsdk:
            netsdk_url = f"netsdk://{device.host}:{device.port}/channel={ch}/stream={'sub' if is_sub else 'main'}"
            if StreamEndpoint is not None:
                return StreamEndpoint(
                    stream_type=st,
                    url=netsdk_url,
                    protocol=TransportProtocol.NETSDK,
                    codec="h264",
                    direct_transport="netsdk",
                    extra_params={
                        "channel": ch,
                        "sdk_channel": max(0, ch - 1),
                        "stream": "sub" if is_sub else "main",
                        "netsdk_port": device.port,
                        "rtsp_fallback_url": rtsp_url,
                    },
                )
            return netsdk_url  # type: ignore[return-value]

        # Standard RTSP transport
        if StreamEndpoint is not None:
            return StreamEndpoint(
                stream_type=st,
                url=rtsp_url,
                protocol=TransportProtocol.RTSP,
                codec="h264",
                direct_transport="ffmpeg",
                extra_params={
                    "channel": ch,
                    "stream": "sub" if is_sub else "main",
                    "rtsp_path": paths["primary_path"],
                },
            )
        return rtsp_url  # type: ignore[return-value]

    def search_recordings(
        self,
        device: DeviceInfo,
        channel_id: int,
        start_time: datetime,
        end_time: datetime,
        **kwargs: Any,
    ) -> List[RecordingSegment]:
        """Remote recording search via NetSDK is not implemented in this driver.

        Contract requires explicit UnsupportedCapabilityError rather than claiming
        unsupported SDK operations.
        """
        self.require_capability(
            CameraCapability.RECORDING_SEARCH,
            reason=(
                "Dahua NetSDK tìm kiếm bản ghi từ xa chưa được triển khai trong trình điều khiển này; "
                "Cambida lưu trữ và quản lý các phân đoạn video liên tục cục bộ."
            ),
        )
        return []

    def download_recording(
        self,
        device: DeviceInfo,
        segment: RecordingSegment,
        destination: str,
        **kwargs: Any,
    ) -> DownloadResult:
        """Remote recording download via NetSDK is not implemented in this driver."""
        self.require_capability(
            CameraCapability.RECORDING_DOWNLOAD,
            reason="Dahua NetSDK tải bản ghi từ xa chưa được triển khai trong trình điều khiển này.",
        )
        return DownloadResult(
            ok=False,
            output_path=destination,
            message="Chức năng tải bản ghi từ xa không được hỗ trợ qua Dahua NetSDK hiện tại.",
        )

    # -------------------------------------------------------------------------
    # Optional Media Pipeline Helpers (reusing dahua_37777)
    # -------------------------------------------------------------------------

    def capture_jpeg(
        self,
        device: DeviceInfo,
        channel_id: int,
        ffmpeg_path: str,
        timeout: float = 12.0,
    ) -> bytes:
        """Capture single snapshot via Dahua NetSDK (or raise DahuaAdapterError)."""
        sdk_dir = getattr(device, "extra", {}).get("sdk_dir") or self.base_dir
        if not is_dahua_netsdk_available(sdk_dir) or _dahua_backend is None:
            raise DahuaNetSDKNotFoundError("Không tìm thấy thư viện Dahua NetSDK để trích xuất JPEG.")
        adapter = _dahua_backend.Dahua37777Adapter(
            host=device.host,
            username=device.user,
            password=device.password,
            port=device.port or self.default_port,
            channel=max(0, int(channel_id) - 1),
            stream="sub",
            sdk_dir=sdk_dir,
        )
        return adapter.capture_jpeg(ffmpeg_path, timeout=timeout)

    def iter_jpeg_frames(
        self,
        device: DeviceInfo,
        channel_id: int,
        ffmpeg_path: str,
        fps: float = 10.0,
        frame_timeout: float = 12.0,
    ) -> Generator[bytes, None, None]:
        """Yield live JPEG frames directly from Dahua NetSDK RealPlay."""
        sdk_dir = getattr(device, "extra", {}).get("sdk_dir") or self.base_dir
        if not is_dahua_netsdk_available(sdk_dir) or _dahua_backend is None:
            raise DahuaNetSDKNotFoundError("Không tìm thấy thư viện Dahua NetSDK để stream JPEG.")
        adapter = _dahua_backend.Dahua37777Adapter(
            host=device.host,
            username=device.user,
            password=device.password,
            port=device.port or self.default_port,
            channel=max(0, int(channel_id) - 1),
            stream="sub",
            sdk_dir=sdk_dir,
        )
        yield from adapter.iter_jpeg_frames(ffmpeg_path, fps=fps, frame_timeout=frame_timeout)

    def record_segment(
        self,
        device: DeviceInfo,
        channel_id: int,
        output_path: str,
        duration: float,
        ffmpeg_path: str,
        stop_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        """Record video segment using Dahua NetSDK RealPlay SaveRealData -> MP4."""
        sdk_dir = getattr(device, "extra", {}).get("sdk_dir") or self.base_dir
        if not is_dahua_netsdk_available(sdk_dir) or _dahua_backend is None:
            raise DahuaNetSDKNotFoundError("Không tìm thấy thư viện Dahua NetSDK để ghi hình.")
        adapter = _dahua_backend.Dahua37777Adapter(
            host=device.host,
            username=device.user,
            password=device.password,
            port=device.port or self.default_port,
            channel=max(0, int(channel_id) - 1),
            stream="main",
            sdk_dir=sdk_dir,
        )
        return adapter.record_segment(output_path, duration, ffmpeg_path, stop_event=stop_event)


# =============================================================================
# Imou Camera Adapter
# =============================================================================

class ImouCameraAdapter(DahuaCameraAdapter):
    """Adapter for Imou consumer camera devices (Dahua protocol on port 37777 / RTSP 554)."""

    vendor: str = "imou"
    default_port: int = DEFAULT_DAHUA_NETSDK_PORT
    default_http_port: int = DEFAULT_DAHUA_HTTP_PORT
    default_rtsp_port: int = DEFAULT_DAHUA_RTSP_PORT


# =============================================================================
# KBVision Camera Adapter (Explicit SDK Unverified Policy)
# =============================================================================

class KBVisionCameraAdapter(DahuaCameraAdapter):
    """Camera module adapter for KBVision devices.

    IMPORTANT SAFETY NOTICE ON KBVISION SDK COMPATIBILITY:
    - KBVision devices commonly use TCP port 8888 for management and 554 for RTSP.
    - While some KBVision hardware is OEM-related to Dahua, NetSDK compatibility
      on port 8888 or with KBVision firmware revisions is UNVERIFIED and must NOT
      be assumed without on-device verification.
    - This adapter provides explicit capability gating:
      * By default, stream transport uses verified RTSP (cam/realmonitor) on port 554.
      * Probing verifies TCP socket connectivity on port 8888 and RTSP port 554.
      * If NetSDK is explicitly configured, it is treated as experimental and
        does not claim verified status.
    """

    vendor: str = "kbvision"
    default_port: int = DEFAULT_KBVISION_PORT
    default_http_port: int = DEFAULT_KBVISION_HTTP_PORT
    default_rtsp_port: int = DEFAULT_KBVISION_RTSP_PORT

    def probe(self, device: DeviceInfo, timeout_sec: float = 5.0) -> CoreProbeResult:
        """Probe KBVision device with explicit notice of unverified NetSDK compatibility."""
        self.require_capability(CameraCapability.CONNECT)
        device = self._ensure_device_info(device)
        t0 = time.monotonic()
        port = device.port or self.default_port
        dev_id = getattr(device, "device_id", "") or generate_device_id(self.vendor, device.host, port)
        rtsp_port = getattr(device, "rtsp_port", None) or DEFAULT_KBVISION_RTSP_PORT

        # Step 1: Probe management port reachability (port 8888)
        mgmt_ok = False
        try:
            with socket.create_connection((device.host, port), timeout=float(timeout_sec)):
                mgmt_ok = True
        except OSError:
            mgmt_ok = False

        # Step 2: Probe RTSP port reachability (port 554)
        rtsp_ok = False
        try:
            with socket.create_connection((device.host, rtsp_port), timeout=float(timeout_sec)):
                rtsp_ok = True
        except OSError:
            rtsp_ok = False

        latency_ms = (time.monotonic() - t0) * 1000.0
        channel_count = max(1, int(getattr(device, "channel_count", 1) or 1))
        channels = self._generate_channels(device, channel_count)

        if mgmt_ok or rtsp_ok:
            status_text = (
                f"KBVision thiết bị phản hồi (Cổng quản lý {port}: {'MỞ' if mgmt_ok else 'ĐÓNG'}, "
                f"Cổng RTSP {rtsp_port}: {'MỞ' if rtsp_ok else 'ĐÓNG'}). "
                "CẢNH BÁO TƯƠNG THÍCH: NetSDK trên cổng 8888 của KBVision CHƯA ĐƯỢC KIỂM CHỨNG "
                "trên thiết bị thật. Tuyến truyền dẫn khuyến nghị: RTSP (cam/realmonitor)."
            )
            return CoreProbeResult(
                ok=True,
                device_id=dev_id,
                vendor=self.vendor,
                media_ok=rtsp_ok,
                channel_count=channel_count,
                message=status_text,
                discovered_channels=channels,
                latency_ms=latency_ms,
            )
        else:
            return CoreProbeResult(
                ok=False,
                device_id=dev_id,
                vendor=self.vendor,
                media_ok=False,
                channel_count=0,
                message=(
                    f"Không thể kết nối thiết bị KBVision tại {device.host} "
                    f"(cổng quản lý {port} và RTSP {rtsp_port} đều không phản hồi)."
                ),
                latency_ms=latency_ms,
            )

    def get_live_stream(
        self,
        device: DeviceInfo,
        channel_id: int,
        stream: StreamType = StreamType.MAIN,
    ) -> StreamEndpoint:
        """Resolve live stream endpoint for KBVision.

        Defaults to RTSP to avoid unverified NetSDK issues on port 8888.
        If user explicitly opted into local_transport == "netsdk", calls base NetSDK resolver.
        """
        device = self._ensure_device_info(device)
        wants_netsdk = getattr(device, "extra", {}).get("local_transport") == "netsdk"
        if wants_netsdk:
            logger.warning(
                "KBVision: Đang sử dụng NetSDK thử nghiệm theo cấu hình; tương thích chưa được kiểm chứng trên thiết bị thật."
            )
            return super().get_live_stream(device, channel_id, stream=stream)

        # Standard safe RTSP path
        ch = max(1, int(channel_id))
        is_sub = getattr(stream, "value", str(stream)).lower() in {"sub", "preview", "1"}
        sub_type = 1 if is_sub else 0
        st = StreamType.SUB if is_sub else StreamType.MAIN

        paths = build_dahua_rtsp_paths(channel=ch, stream="sub" if is_sub else "main")
        rtsp_port = getattr(device, "rtsp_port", None) or DEFAULT_KBVISION_RTSP_PORT
        rtsp_url = build_rtsp_url(
            host=device.host,
            port=rtsp_port,
            path=paths["primary_path"],
            user=device.user,
            password=device.password,
        )

        if StreamEndpoint is not None:
            return StreamEndpoint(
                stream_type=st,
                url=rtsp_url,
                protocol=TransportProtocol.RTSP,
                codec="h264",
                direct_transport="ffmpeg",
                extra_params={
                    "channel": ch,
                    "stream": "sub" if is_sub else "main",
                    "rtsp_path": paths["primary_path"],
                    "unverified_sdk_notice": "KBVision NetSDK unverified on port 8888; RTSP transport utilized.",
                },
            )
        return rtsp_url  # type: ignore[return-value]


# =============================================================================
# High-Level Probing Functions
# =============================================================================

def probe_dahua_device(
    host: str,
    username: str = "admin",
    password: str = "",
    port: int = DEFAULT_DAHUA_NETSDK_PORT,
    base_dir: Optional[str] = None,
    timeout_sec: float = 5.0,
) -> CoreProbeResult:
    """Diagnostic probe for Dahua / Imou device."""
    adapter = DahuaCameraAdapter(base_dir=base_dir)
    dev_id = generate_device_id("dahua", host, port)
    dev = DeviceInfo(
        device_id=dev_id,
        vendor="dahua",
        host=host,
        port=port,
        user=username,
        password=password,
    )
    return adapter.probe(dev, timeout_sec=timeout_sec)


def probe_kbvision_device(
    host: str,
    username: str = "admin",
    password: str = "",
    port: int = DEFAULT_KBVISION_PORT,
    base_dir: Optional[str] = None,
    timeout_sec: float = 5.0,
) -> CoreProbeResult:
    """Diagnostic probe for KBVision device with unverified SDK compatibility notice."""
    adapter = KBVisionCameraAdapter(base_dir=base_dir)
    dev_id = generate_device_id("kbvision", host, port)
    dev = DeviceInfo(
        device_id=dev_id,
        vendor="kbvision",
        host=host,
        port=port,
        user=username,
        password=password,
    )
    return adapter.probe(dev, timeout_sec=timeout_sec)


# =============================================================================
# Registry Registration
# =============================================================================

if get_adapter_registry is not None:
    try:
        _reg = get_adapter_registry()
        _reg.register("imou", ImouCameraAdapter)
        _reg.register("dahua", DahuaCameraAdapter)
        _reg.register("kbvision", KBVisionCameraAdapter)
    except Exception as exc:
        logger.debug("Failed to register Dahua adapters with registry: %s", exc)


__all__ = [
    # Classes
    "DahuaCameraAdapter",
    "ImouCameraAdapter",
    "KBVisionCameraAdapter",
    # Exceptions
    "DahuaAdapterError",
    "DahuaNetSDKNotFoundError",
    "DahuaAuthenticationError",
    "DahuaConnectionError",
    "DahuaChannelError",
    # Functions
    "find_dahua_netsdk_dir",
    "is_dahua_netsdk_available",
    "probe_dvrip_auth_safe",
    "build_dahua_rtsp_paths",
    "build_dahua_rtsp_url",
    "build_kbvision_rtsp_url",
    "probe_dahua_device",
    "probe_kbvision_device",
    # Constants
    "DEFAULT_DAHUA_NETSDK_PORT",
    "DEFAULT_DAHUA_HTTP_PORT",
    "DEFAULT_DAHUA_RTSP_PORT",
    "DEFAULT_KBVISION_PORT",
    "DEFAULT_KBVISION_HTTP_PORT",
    "DEFAULT_KBVISION_RTSP_PORT",
    "SUPPORTED_VENDORS",
]
