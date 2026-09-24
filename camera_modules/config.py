"""Configuration data structures, vendor presets, and migration helpers for modular camera architecture.

This module provides:
- Strongly typed dataclasses for DeviceConfig, CameraConfig, TableBinding, and ModularCCTVConfig.
- Distinct stable ID helpers (device IDs, physical channel IDs, table binding IDs).
- Vendor default port mappings (Hikvision 8000, Dahua 37777, KBVision 8888, ONVIF 80, RTSP 554).
- Preset RTSP stream paths and URL builders (Hikvision, Dahua, KBVision, Custom).
- Pure in-memory migration helpers from legacy config dictionaries to modular structures
  preserving all existing settings and source identities without destructive writes.
- Conversion back to canonical legacy format for seamless backwards compatibility.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
import ipaddress
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

from .core import (
    CameraCapability,
    PlaybackSourceType,
    StreamType,
    TableColorState,
    TransportProtocol,
    VendorType,
    generate_camera_id,
    generate_device_id,
    generate_table_id,
    mask_credential,
    sanitize_dict_for_logging,
    sanitize_slug,
)

logger = logging.getLogger("cambida.camera_modules.config")


# ============================================================================
# Vendor Default Ports & Stream Presets
# ============================================================================

# All ports are editable in the UI / config. These are sensible defaults per vendor.
VENDOR_DEFAULT_PORTS: Dict[str, Dict[str, int]] = {
    "hikvision": {"primary": 8000, "http": 80, "rtsp": 554, "https": 443},
    "ezviz": {"primary": 8000, "http": 80, "rtsp": 554, "https": 443},
    "dahua": {"primary": 37777, "http": 81, "rtsp": 554, "https": 443},
    "imou": {"primary": 37777, "http": 81, "rtsp": 554, "https": 443},
    "kbvision": {"primary": 8888, "http": 80, "rtsp": 554, "https": 443},
    "onvif": {"primary": 80, "http": 80, "rtsp": 554, "https": 443},
    "rtsp": {"primary": 554, "http": 80, "rtsp": 554, "https": 443},
}

RTSP_PRESETS: Dict[str, Dict[str, str]] = {
    "hikvision": {
        "record_path": "h264/ch{channel}/main/av_stream",
        "preview_path": "h264/ch{channel}/sub/av_stream",
        "isapi_main": "Streaming/Channels/{channel}01",
        "isapi_sub": "Streaming/Channels/{channel}02",
    },
    "dahua": {
        "record_path": "cam/realmonitor?channel={channel}&subtype=0",
        "preview_path": "cam/realmonitor?channel={channel}&subtype=1",
    },
    "kbvision": {
        "record_path": "cam/realmonitor?channel={channel}&subtype=0",
        "preview_path": "cam/realmonitor?channel={channel}&subtype=1",
    },
    "generic": {
        "record_path": "live/ch{channel}/main",
        "preview_path": "live/ch{channel}/sub",
    },
}


def get_vendor_default_port(vendor: str, port_type: str = "primary") -> int:
    """Return default port for vendor and port_type ('primary', 'http', 'rtsp')."""
    v = VendorType.normalize(vendor)
    ports = VENDOR_DEFAULT_PORTS.get(v, VENDOR_DEFAULT_PORTS["rtsp"])
    return ports.get(port_type, ports.get("primary", 554))


def build_rtsp_stream_path(
    vendor: str,
    channel: int = 1,
    stream: StreamType = StreamType.MAIN,
) -> str:
    """Build canonical RTSP path for vendor and channel."""
    v = VendorType.normalize(vendor)
    presets = RTSP_PRESETS.get(v, RTSP_PRESETS["generic"])
    template = presets["record_path"] if stream == StreamType.MAIN else presets["preview_path"]
    return template.format(channel=max(1, int(channel)))


def build_rtsp_url(
    host: str,
    port: int = 554,
    path: str = "",
    user: Optional[str] = None,
    password: Optional[str] = None,
) -> str:
    """Construct full RTSP URL safely."""
    clean_host = str(host).strip()
    clean_path = str(path or "").lstrip("/")
    port_str = f":{int(port)}" if port and port != 554 else ""

    if user:
        clean_user = str(user).strip()
        clean_pass = str(password or "")
        return f"rtsp://{clean_user}:{clean_pass}@{clean_host}{port_str}/{clean_path}"
    return f"rtsp://{clean_host}{port_str}/{clean_path}"


# ============================================================================
# Modular Configuration Dataclasses
# ============================================================================


@dataclass
class DeviceConfig:
    """Configuration for a physical camera device or NVR/DVR host."""

    device_id: str
    vendor: str
    host: str
    port: int = 554
    http_port: Optional[int] = None
    rtsp_port: Optional[int] = None
    user: str = "admin"
    password: str = field(default="", repr=False)
    name: str = ""
    serial_number: Optional[str] = None
    model: Optional[str] = None
    firmware_version: Optional[str] = None
    channel_count: int = 1
    use_https: bool = False
    verify_tls: bool = False
    connect_timeout_sec: int = 5
    read_timeout_sec: int = 30
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self, include_secrets: bool = False) -> Dict[str, Any]:
        """Convert to dictionary. Secrets are masked unless explicitly requested."""
        d: Dict[str, Any] = {
            "device_id": self.device_id,
            "vendor": self.vendor,
            "host": self.host,
            "port": self.port,
            "http_port": self.http_port,
            "rtsp_port": self.rtsp_port,
            "user": self.user,
            "name": self.name,
            "serial_number": self.serial_number,
            "model": self.model,
            "firmware_version": self.firmware_version,
            "channel_count": self.channel_count,
            "use_https": self.use_https,
            "verify_tls": self.verify_tls,
            "connect_timeout_sec": self.connect_timeout_sec,
            "read_timeout_sec": self.read_timeout_sec,
            "extra": copy.deepcopy(self.extra),
        }
        if include_secrets:
            d["password"] = self.password
        else:
            d["password"] = mask_credential(self.password)
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> DeviceConfig:
        d = dict(data)
        device_id = str(d.pop("device_id", "")).strip()
        vendor = VendorType.normalize(d.pop("vendor", "hikvision"))
        host = str(d.pop("host", "") or d.pop("ip", "")).strip()
        port = int(d.pop("port", 0) or get_vendor_default_port(vendor, "primary"))
        if not device_id:
            device_id = generate_device_id(vendor, host, port)

        user = str(d.pop("user", "") or d.pop("username", "admin")).strip()
        password = str(d.pop("password", "") or d.pop("pass", ""))
        name = str(d.pop("name", "")).strip()

        http_port = d.pop("http_port", None)
        http_port = int(http_port) if http_port not in (None, "") else None

        rtsp_port = d.pop("rtsp_port", None)
        rtsp_port = int(rtsp_port) if rtsp_port not in (None, "") else None

        return cls(
            device_id=device_id,
            vendor=vendor,
            host=host,
            port=port,
            http_port=http_port,
            rtsp_port=rtsp_port,
            user=user,
            password=password,
            name=name,
            serial_number=d.pop("serial_number", None),
            model=d.pop("model", None),
            firmware_version=d.pop("firmware_version", None),
            channel_count=max(1, int(d.pop("channel_count", 1) or 1)),
            use_https=bool(d.pop("use_https", False)),
            verify_tls=bool(d.pop("verify_tls", False)),
            connect_timeout_sec=max(1, int(d.pop("connect_timeout_sec", 5) or 5)),
            read_timeout_sec=max(5, int(d.pop("read_timeout_sec", 30) or 30)),
            extra=d,
        )

    def to_device_info(self) -> Any:
        """Convert DeviceConfig to core DeviceInfo dataclass."""
        from .core import DeviceInfo
        return DeviceInfo(
            device_id=self.device_id,
            vendor=self.vendor,
            host=self.host,
            port=self.port,
            http_port=self.http_port,
            rtsp_port=self.rtsp_port,
            user=self.user,
            password=self.password,
            name=self.name,
            serial_number=self.serial_number,
            model=self.model,
            firmware_version=self.firmware_version,
            channel_count=self.channel_count,
            use_https=self.use_https,
            verify_tls=self.verify_tls,
            connect_timeout_sec=float(self.connect_timeout_sec),
            read_timeout_sec=float(self.read_timeout_sec),
            extra=copy.deepcopy(self.extra),
        )


@dataclass
class CameraConfig:
    """Strongly typed camera channel configuration.

    Fully backwards-compatible with existing Cambida camera dicts while supporting
    new distinct stable IDs (device_id, channel_id, camera_id).
    """

    # Identity fields (preserved exactly from legacy entries)
    name: str = "Camera"
    id: Optional[str] = None
    camera_id: Optional[Union[int, str]] = None
    uuid: Optional[str] = None

    # Modular distinct stable IDs
    device_id: Optional[str] = None
    channel_id: Optional[int] = None  # 1-based physical channel number

    # Mode & Stream selection
    playback_source: str = "local"  # "local" or "nvr"
    view_stream: str = "auto"       # "auto", "main", or "sub"
    local_transport: str = "rtsp"   # "rtsp" or "netsdk"

    # Local RTSP / Direct connection settings
    ip: str = ""
    port: int = 554
    user: str = "admin"
    password: str = field(default="", repr=False)
    record_path: str = "h264/ch1/main/av_stream"
    preview_path: str = "h264/ch1/sub/av_stream"
    record_rtsp_url: Optional[str] = None
    preview_rtsp_url: Optional[str] = None
    vendor: str = "hikvision"
    rtsp_channel: Optional[int] = None

    # Local NetSDK settings (Dahua / Imou port 37777, KBVision port 8888)
    netsdk_port: Optional[int] = None
    netsdk_channel: Optional[int] = None

    # NVR settings
    host: str = ""
    http_port: Optional[int] = None
    rtsp_port: Optional[int] = None
    nvr_channel: Optional[int] = None
    stream: str = "main"            # "main" or "sub"
    backup_local: bool = False
    timezone_offset_minutes: int = 420
    connect_timeout_sec: int = 5
    read_timeout_sec: int = 30
    playback_chunk_sec: int = 300
    max_search_pages: Optional[int] = None
    use_https: bool = False
    verify_tls: bool = False

    # Extra keys preserved during round-trip
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_legacy_dict(self) -> Dict[str, Any]:
        """Convert to canonical legacy dictionary format matching 1.py _normalise_camera_entry.

        Enforces strict transport disjointness so local settings don't contaminate NVR and vice-versa.
        """
        result: Dict[str, Any] = {}

        # Identity keys
        if self.name:
            result["name"] = self.name
        if self.id is not None:
            result["id"] = self.id
        if self.camera_id is not None:
            result["camera_id"] = self.camera_id
        if self.uuid is not None:
            result["uuid"] = self.uuid

        result["playback_source"] = self.playback_source
        result["view_stream"] = self.view_stream

        if self.playback_source == "nvr":
            vendor = VendorType.normalize(self.vendor)
            if vendor not in {"hikvision", "dahua"}:
                vendor = "hikvision"
            result["vendor"] = vendor
            result["host"] = self.host
            result["http_port"] = self.http_port if self.http_port else (81 if vendor == "dahua" else 80)
            result["rtsp_port"] = self.rtsp_port or 554
            result["user"] = self.user
            result["pass"] = self.password
            result["nvr_channel"] = max(1, self.nvr_channel or 1)
            result["stream"] = "sub" if str(self.stream).lower() == "sub" else "main"
            result["backup_local"] = bool(self.backup_local)
            result["timezone_offset_minutes"] = self.timezone_offset_minutes
            result["connect_timeout_sec"] = max(1, self.connect_timeout_sec)
            result["read_timeout_sec"] = max(5, self.read_timeout_sec)
            result["playback_chunk_sec"] = max(30, min(3600, self.playback_chunk_sec))
            result["use_https"] = bool(self.use_https)
            result["verify_tls"] = bool(self.verify_tls)
            if self.max_search_pages is not None:
                result["max_search_pages"] = max(1, min(50, self.max_search_pages))
            return result

        # Local playback source
        result["ip"] = self.ip or self.host
        result["user"] = self.user
        result["pass"] = self.password
        transport = "netsdk" if self.local_transport == "netsdk" else "rtsp"
        result["local_transport"] = transport

        if transport == "netsdk":
            result["netsdk_port"] = self.netsdk_port or 37777
            result["netsdk_channel"] = max(1, self.netsdk_channel or 1)
            return result

        # Local RTSP transport
        result["port"] = self.port or 554
        result["record_path"] = self.record_path or "h264/ch1/main/av_stream"
        result["preview_path"] = self.preview_path or "h264/ch1/sub/av_stream"
        if self.record_rtsp_url:
            result["record_rtsp_url"] = self.record_rtsp_url
        if self.preview_rtsp_url:
            result["preview_rtsp_url"] = self.preview_rtsp_url
        if self.vendor:
            result["vendor"] = VendorType.normalize(self.vendor)
        if self.rtsp_channel is not None:
            result["rtsp_channel"] = int(self.rtsp_channel)
        return result

    def to_modular_dict(self, include_secrets: bool = False) -> Dict[str, Any]:
        """Convert to fully explicit modular dictionary."""
        d = copy.deepcopy(self.__dict__)
        d["password"] = self.password if include_secrets else mask_credential(self.password)
        return d

    @classmethod
    def from_dict(cls, camera: Dict[str, Any], index: int = 1) -> CameraConfig:
        """Parse dictionary (legacy or modular) into strongly typed CameraConfig."""
        cam = dict(camera)
        source = str(cam.get("playback_source", "local")).strip().lower()
        if source not in {"local", "nvr"}:
            source = "local"

        name = str(cam.get("name") or f"Camera {index}").strip()
        cam_id = cam.get("camera_id", index)
        custom_id = cam.get("id")
        uuid_val = cam.get("uuid")

        # View stream
        view_stream = str(cam.get("view_stream") or cam.get("preview_stream") or "").strip().lower()
        if view_stream not in {"auto", "main", "sub"}:
            legacy_netsdk = str(cam.get("netsdk_stream") or "").strip().lower()
            view_stream = legacy_netsdk if legacy_netsdk in {"main", "sub"} else "auto"

        nested_nvr = cam.get("nvr") if isinstance(cam.get("nvr"), dict) else {}

        if source == "nvr":
            vendor = str(
                cam.get("vendor")
                or cam.get("nvr_vendor")
                or nested_nvr.get("vendor")
                or "hikvision"
            ).strip().lower()
            host = str(cam.get("host") or cam.get("ip") or nested_nvr.get("host") or "").strip()
            user = str(cam.get("user") or cam.get("username") or nested_nvr.get("username") or "admin").strip()
            password = str(cam.get("pass") if cam.get("pass") is not None else (cam.get("password") or nested_nvr.get("password") or ""))
            channel = int(cam.get("nvr_channel") or cam.get("channel") or nested_nvr.get("channel") or index)
            http_port = int(cam.get("http_port") or nested_nvr.get("http_port") or (81 if vendor == "dahua" else 80))
            rtsp_port = int(cam.get("rtsp_port") or cam.get("port") or nested_nvr.get("rtsp_port") or 554)
            stream_mode = "sub" if str(cam.get("stream") or nested_nvr.get("stream") or "main").strip().lower() == "sub" else "main"

            dev_id = cam.get("device_id") or generate_device_id(vendor, host, http_port)

            return cls(
                name=name,
                id=str(custom_id) if custom_id is not None else None,
                camera_id=cam_id,
                uuid=str(uuid_val) if uuid_val is not None else None,
                device_id=dev_id,
                channel_id=max(1, channel),
                playback_source="nvr",
                view_stream=view_stream,
                host=host,
                http_port=http_port,
                rtsp_port=rtsp_port,
                user=user,
                password=password,
                vendor=vendor,
                nvr_channel=max(1, channel),
                stream=stream_mode,
                backup_local=bool(cam.get("backup_local", False)),
                timezone_offset_minutes=int(cam.get("timezone_offset_minutes") or nested_nvr.get("timezone_offset_minutes") or 420),
                connect_timeout_sec=max(1, int(cam.get("connect_timeout_sec") or nested_nvr.get("connect_timeout_sec") or 5)),
                read_timeout_sec=max(5, int(cam.get("read_timeout_sec") or nested_nvr.get("read_timeout_sec") or 30)),
                playback_chunk_sec=max(30, min(3600, int(cam.get("playback_chunk_sec") or nested_nvr.get("playback_chunk_sec") or 300))),
                max_search_pages=int(cam["max_search_pages"]) if "max_search_pages" in cam else None,
                use_https=bool(cam.get("use_https", nested_nvr.get("use_https", False))),
                verify_tls=bool(cam.get("verify_tls", nested_nvr.get("verify_tls", False))),
            )

        # Local source
        ip = str(cam.get("ip") or cam.get("host") or "").strip()
        user = str(cam.get("user") or cam.get("username") or "admin").strip()
        password = str(cam.get("pass") if cam.get("pass") is not None else (cam.get("password") or ""))
        transport = str(cam.get("local_transport", "rtsp") or "rtsp").strip().lower()
        transport = transport if transport in {"rtsp", "netsdk"} else "rtsp"
        vendor = str(cam.get("vendor") or "hikvision").strip().lower()

        if transport == "netsdk":
            netsdk_port = int(cam.get("netsdk_port") or (37777 if vendor == "dahua" else 8888))
            netsdk_channel = max(1, int(cam.get("netsdk_channel") or 1))
            dev_id = cam.get("device_id") or generate_device_id(vendor, ip, netsdk_port)
            return cls(
                name=name,
                id=str(custom_id) if custom_id is not None else None,
                camera_id=cam_id,
                uuid=str(uuid_val) if uuid_val is not None else None,
                device_id=dev_id,
                channel_id=netsdk_channel,
                playback_source="local",
                view_stream=view_stream,
                local_transport="netsdk",
                ip=ip,
                user=user,
                password=password,
                vendor=vendor,
                netsdk_port=netsdk_port,
                netsdk_channel=netsdk_channel,
            )

        # Local RTSP
        port = int(cam.get("port") or cam.get("rtsp_port") or 554)
        record_path = str(cam.get("record_path") or "h264/ch1/main/av_stream").strip()
        preview_path = str(cam.get("preview_path") or "h264/ch1/sub/av_stream").strip()
        dev_id = cam.get("device_id") or generate_device_id(vendor, ip, port)
        rtsp_channel = cam.get("rtsp_channel") or cam.get("channel")
        ch_idx = int(rtsp_channel) if rtsp_channel not in (None, "") else index

        return cls(
            name=name,
            id=str(custom_id) if custom_id is not None else None,
            camera_id=cam_id,
            uuid=str(uuid_val) if uuid_val is not None else None,
            device_id=dev_id,
            channel_id=max(1, ch_idx),
            playback_source="local",
            view_stream=view_stream,
            local_transport="rtsp",
            ip=ip,
            port=port,
            user=user,
            password=password,
            record_path=record_path,
            preview_path=preview_path,
            record_rtsp_url=str(cam["record_rtsp_url"]).strip() if "record_rtsp_url" in cam else None,
            preview_rtsp_url=str(cam["preview_rtsp_url"]).strip() if "preview_rtsp_url" in cam else None,
            vendor=vendor,
            rtsp_channel=int(rtsp_channel) if rtsp_channel not in (None, "") else None,
        )


@dataclass
class TableBinding:
    """Stable table binding binding a physical table to a camera/channel.

    Preserves stable table ID and QR code identity when tables are reordered.
    Includes privacy_off switch and color state.
    """

    table_id: str                      # Distinct stable ID (e.g. 'ban-01')
    name: str                          # Display name (e.g. 'Bàn 1')
    camera_id: Optional[Union[int, str]] = None  # Reference to CameraConfig.camera_id
    device_id: Optional[str] = None    # Associated device
    channel_id: Optional[int] = None   # Associated physical channel
    enabled: bool = True               # Whether table is active
    privacy_off: bool = False          # Server-side privacy off: blocks guest live/replay/download while recording continues
    color_state: str = "grey"          # Visual color: 'grey' (idle) or 'red' (active/privacy off)
    sort_order: int = 0                # Position index for admin reorder
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_legacy_dict(self) -> Dict[str, Any]:
        """Convert to legacy format expected by 1.py ({'id': ..., 'name': ..., 'camera_id': ...})."""
        d: Dict[str, Any] = {
            "id": self.table_id,
            "name": self.name,
        }
        if self.camera_id is not None:
            d["camera_id"] = self.camera_id
        return d

    def to_modular_dict(self) -> Dict[str, Any]:
        """Convert to full modular schema dictionary."""
        return {
            "id": self.table_id,
            "name": self.name,
            "camera_id": self.camera_id,
            "device_id": self.device_id,
            "channel_id": self.channel_id,
            "enabled": self.enabled,
            "privacy_off": self.privacy_off,
            "color_state": self.color_state,
            "sort_order": self.sort_order,
            "extra": copy.deepcopy(self.extra),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], index: int = 1) -> TableBinding:
        d = dict(data)
        raw_id = d.pop("id", None) or f"ban-{index:02d}"
        table_id = generate_table_id(raw_id)
        name = str(d.pop("name", f"Bàn {index}")).strip()
        camera_id = d.pop("camera_id", None)

        return cls(
            table_id=table_id,
            name=name,
            camera_id=camera_id,
            device_id=d.pop("device_id", None),
            channel_id=int(d["channel_id"]) if d.get("channel_id") is not None else None,
            enabled=bool(d.pop("enabled", True)),
            privacy_off=bool(d.pop("privacy_off", False)),
            color_state=str(d.pop("color_state", "grey")).strip().lower() or "grey",
            sort_order=int(d.pop("sort_order", index)),
            extra=d,
        )


@dataclass
class ModularCCTVConfig:
    """Unified container for modular CCTV configuration."""

    devices: List[DeviceConfig] = field(default_factory=list)
    cameras: List[CameraConfig] = field(default_factory=list)
    tables: List[TableBinding] = field(default_factory=list)
    root_metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self, include_secrets: bool = False) -> Dict[str, Any]:
        return {
            "devices": [dev.to_dict(include_secrets=include_secrets) for dev in self.devices],
            "cameras": [cam.to_modular_dict(include_secrets=include_secrets) for cam in self.cameras],
            "tables": [tbl.to_modular_dict() for tbl in self.tables],
            "metadata": copy.deepcopy(self.root_metadata),
        }

    def to_legacy_root_config(self) -> Dict[str, Any]:
        """Convert to legacy config dictionary compatible with 1.py and tests."""
        cfg = copy.deepcopy(self.root_metadata)
        cfg.pop("playback_source", None)
        cfg.pop("retention_days", None)
        cfg["cameras"] = [cam.to_legacy_dict() for cam in self.cameras]
        cfg["tables"] = [tbl.to_legacy_dict() for tbl in self.tables]
        return cfg


# ============================================================================
# Migration Helpers (In-Memory, Non-Destructive)
# ============================================================================


def migrate_legacy_config_to_modular(raw_config: Dict[str, Any]) -> ModularCCTVConfig:
    """Migrate a raw/legacy configuration dictionary into a ModularCCTVConfig.

    Pure in-memory transformation. Does NOT perform auto migrations of live config
    or destructive writes to disk. Preserves all existing settings and source identity.
    """
    if not isinstance(raw_config, dict):
        return ModularCCTVConfig()

    cfg = copy.deepcopy(raw_config)
    global_ps = cfg.get("playback_source")
    global_nvr = global_ps.get("nvr", {}) if isinstance(global_ps, dict) else {}
    global_channel_map = global_nvr.get("channel_map", {}) if isinstance(global_nvr, dict) else {}
    global_mode = str(global_ps.get("mode", "")).strip().lower() if isinstance(global_ps, dict) else ""

    raw_cameras = cfg.get("cameras", [])
    raw_tables = cfg.get("tables", [])

    modular_cameras: List[CameraConfig] = []
    device_map: Dict[str, DeviceConfig] = {}

    if isinstance(raw_cameras, list):
        for index, raw_cam in enumerate(raw_cameras, start=1):
            if not isinstance(raw_cam, dict):
                continue
            cam_entry = dict(raw_cam)
            explicit_mode = str(cam_entry.get("playback_source", "")).strip().lower()

            # Reproduce legacy mode resolution
            if explicit_mode == "hybrid":
                cam_entry["playback_source"] = "nvr"
                cam_entry["backup_local"] = True
            elif explicit_mode in {"local", "nvr"}:
                cam_entry["playback_source"] = explicit_mode
            elif global_mode == "nvr":
                cam_entry["playback_source"] = "nvr"
            elif global_mode == "hybrid":
                if str(index) in global_channel_map or cam_entry.get("nvr_channel"):
                    cam_entry["playback_source"] = "nvr"
                    cam_entry["backup_local"] = True
                else:
                    cam_entry["playback_source"] = "local"
            else:
                cam_entry["playback_source"] = "local"

            # Apply global NVR fallbacks if NVR mode
            if cam_entry["playback_source"] == "nvr":
                if not cam_entry.get("nvr_channel"):
                    cam_entry["nvr_channel"] = global_channel_map.get(str(index), index)

                legacy_shared_nvr = bool(global_nvr.get("host")) and not str(cam_entry.get("host") or "").strip()
                if not cam_entry.get("host") and global_nvr.get("host"):
                    cam_entry["host"] = global_nvr.get("host")
                if legacy_shared_nvr and global_nvr.get("username") is not None:
                    cam_entry["user"] = global_nvr.get("username")
                elif not cam_entry.get("user") and not cam_entry.get("username") and global_nvr.get("username"):
                    cam_entry["user"] = global_nvr.get("username")
                elif global_nvr.get("username") and cam_entry.get("user") == "admin":
                    cam_entry["user"] = global_nvr.get("username")
                if legacy_shared_nvr and global_nvr.get("password") is not None:
                    cam_entry["pass"] = global_nvr.get("password")
                elif not cam_entry.get("pass") and not cam_entry.get("password") and global_nvr.get("password"):
                    cam_entry["pass"] = global_nvr.get("password")
                if not cam_entry.get("vendor") and global_nvr.get("vendor"):
                    cam_entry["vendor"] = global_nvr.get("vendor")
                if not cam_entry.get("http_port") and global_nvr.get("http_port"):
                    cam_entry["http_port"] = global_nvr.get("http_port")
                if not cam_entry.get("rtsp_port") and global_nvr.get("rtsp_port"):
                    cam_entry["rtsp_port"] = global_nvr.get("rtsp_port")
                elif not cam_entry.get("rtsp_port") and cam_entry.get("port"):
                    cam_entry["rtsp_port"] = cam_entry.get("port")
                if not cam_entry.get("stream") and global_nvr.get("stream"):
                    cam_entry["stream"] = global_nvr.get("stream")
                if "backup_local" not in cam_entry:
                    cam_entry["backup_local"] = False

            cam_obj = CameraConfig.from_dict(cam_entry, index=index)
            modular_cameras.append(cam_obj)

            # Synthesize or extract device entry
            host = cam_obj.host or cam_obj.ip
            if host and cam_obj.device_id:
                if cam_obj.device_id not in device_map:
                    port = cam_obj.http_port or cam_obj.netsdk_port or cam_obj.port
                    device_map[cam_obj.device_id] = DeviceConfig(
                        device_id=cam_obj.device_id,
                        vendor=cam_obj.vendor,
                        host=host,
                        port=port,
                        http_port=cam_obj.http_port,
                        rtsp_port=cam_obj.rtsp_port or cam_obj.port,
                        user=cam_obj.user,
                        password=cam_obj.password,
                        name=f"Device {cam_obj.vendor.title()} ({host})",
                    )

    # Process tables
    modular_tables: List[TableBinding] = []
    if isinstance(raw_tables, list):
        for index, raw_table in enumerate(raw_tables, start=1):
            if not isinstance(raw_table, dict):
                continue
            table_obj = TableBinding.from_dict(raw_table, index=index)

            # Correlate camera_id to device_id and channel_id if possible
            if table_obj.camera_id is not None:
                for cam in modular_cameras:
                    if str(cam.camera_id) == str(table_obj.camera_id):
                        table_obj.device_id = cam.device_id
                        table_obj.channel_id = cam.channel_id
                        break
            modular_tables.append(table_obj)

    # Strip legacy camera & table entries from root metadata
    root_metadata = copy.deepcopy(cfg)
    root_metadata.pop("cameras", None)
    root_metadata.pop("tables", None)
    root_metadata.pop("playback_source", None)

    return ModularCCTVConfig(
        devices=list(device_map.values()),
        cameras=modular_cameras,
        tables=modular_tables,
        root_metadata=root_metadata,
    )


def validate_modular_config(config: ModularCCTVConfig) -> List[str]:
    """Validate a ModularCCTVConfig for internal consistency and syntax errors.

    Returns a list of validation error messages. An empty list signifies success.
    """
    errors: List[str] = []

    # Check devices
    seen_device_ids: Set[str] = set()
    for dev in config.devices:
        if not dev.device_id:
            errors.append("Device missing device_id.")
        elif dev.device_id in seen_device_ids:
            errors.append(f"Duplicate device_id: '{dev.device_id}'.")
        seen_device_ids.add(dev.device_id)

        if not dev.host:
            errors.append(f"Device '{dev.device_id}' has empty host.")
        if dev.port <= 0 or dev.port > 65535:
            errors.append(f"Device '{dev.device_id}' port {dev.port} out of range (1-65535).")

    # Check cameras
    for idx, cam in enumerate(config.cameras, start=1):
        if not cam.name:
            errors.append(f"Camera index {idx} has empty name.")
        if cam.playback_source == "nvr":
            if not cam.host:
                errors.append(f"NVR Camera '{cam.name}' has empty host.")
            if cam.http_port is not None and (cam.http_port <= 0 or cam.http_port > 65535):
                errors.append(f"NVR Camera '{cam.name}' invalid http_port: {cam.http_port}.")
            if cam.rtsp_port is not None and (cam.rtsp_port <= 0 or cam.rtsp_port > 65535):
                errors.append(f"NVR Camera '{cam.name}' invalid rtsp_port: {cam.rtsp_port}.")
        else:
            if not cam.ip:
                errors.append(f"Local Camera '{cam.name}' has empty IP address.")
            if cam.local_transport == "netsdk":
                if cam.netsdk_port is not None and (cam.netsdk_port <= 0 or cam.netsdk_port > 65535):
                    errors.append(f"NetSDK Camera '{cam.name}' invalid netsdk_port: {cam.netsdk_port}.")
            else:
                if cam.port <= 0 or cam.port > 65535:
                    errors.append(f"RTSP Camera '{cam.name}' invalid port: {cam.port}.")

    # Check tables
    seen_table_ids: Set[str] = set()
    cam_ids = {str(c.camera_id) for c in config.cameras if c.camera_id is not None}
    cam_count = len(config.cameras)

    for tbl in config.tables:
        if not tbl.table_id:
            errors.append("Table missing table_id.")
        elif tbl.table_id in seen_table_ids:
            errors.append(f"Duplicate table_id: '{tbl.table_id}'.")
        seen_table_ids.add(tbl.table_id)

        if not tbl.name:
            errors.append(f"Table '{tbl.table_id}' missing name.")

        if tbl.camera_id is not None and tbl.camera_id != "":
            # Validate camera reference: can be numeric 1-based index or string camera_id
            is_valid_ref = False
            try:
                num_id = int(tbl.camera_id)
                if 1 <= num_id <= cam_count or str(num_id) in cam_ids:
                    is_valid_ref = True
            except (ValueError, TypeError):
                if str(tbl.camera_id) in cam_ids:
                    is_valid_ref = True

            if not is_valid_ref and cam_count > 0:
                errors.append(f"Bàn '{tbl.table_id}' tham chiếu camera không tồn tại: {tbl.camera_id}.")

    return errors
