"""Core interfaces, protocols, data models, and registry for modular camera architecture.

This module provides:
- Strongly typed data models for devices, physical channels, stream endpoints, and recordings.
- Distinct stable ID helpers (device IDs, physical channel IDs, table binding IDs).
- Explicit capability definitions and unsupported feature error handling.
- Base adapter protocol and abstract base class for vendor implementations (Hikvision, Dahua, ONVIF, RTSP).
- Thread-safe adapter registry and camera manager.
- Safe credential masking to prevent secrets leaking into logs.
- Lazy dependency loading helpers.
"""

from __future__ import annotations

import abc
import copy
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import hashlib
import importlib
import logging
import re
import threading
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    Iterable,
    List,
    Optional,
    Protocol,
    Sequence,
    Set,
    Tuple,
    Type,
    Union,
    runtime_checkable,
)

logger = logging.getLogger("cambida.camera_modules.core")


# ============================================================================
# Security & Secret Sanitization
# ============================================================================

_SECRET_FIELD_NAMES: FrozenSet[str] = frozenset(
    {"password", "pass", "secret", "token", "auth_code", "authorization", "key"}
)


def mask_credential(value: Optional[str], mask_char: str = "*", show_chars: int = 0) -> str:
    """Mask a sensitive credential string.

    Never outputs raw passwords or secrets in plain text.
    """
    if not value:
        return ""
    str_val = str(value)
    if show_chars <= 0 or len(str_val) <= show_chars:
        return mask_char * 6
    return str_val[:show_chars] + (mask_char * 4)


def sanitize_dict_for_logging(
    data: Dict[str, Any],
    secret_keys: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Return a shallow/recursive copy of dict with sensitive credentials masked."""
    target_keys = set(_SECRET_FIELD_NAMES)
    if secret_keys:
        target_keys.update(k.lower() for k in secret_keys)

    sanitized: Dict[str, Any] = {}
    for k, v in data.items():
        k_lower = str(k).lower()
        if k_lower in target_keys or any(s in k_lower for s in ("password", "secret", "passwd")):
            sanitized[k] = mask_credential(str(v)) if v not in (None, "") else ""
        elif isinstance(v, dict):
            sanitized[k] = sanitize_dict_for_logging(v, secret_keys=target_keys)
        elif isinstance(v, list):
            sanitized[k] = [
                sanitize_dict_for_logging(item, secret_keys=target_keys)
                if isinstance(item, dict)
                else item
                for item in v
            ]
        else:
            sanitized[k] = v
    return sanitized


# ============================================================================
# Exception Hierarchy
# ============================================================================


class CameraModuleError(Exception):
    """Base exception for all camera module errors."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        if self.details:
            safe_details = sanitize_dict_for_logging(self.details)
            return f"{self.message} (details: {safe_details})"
        return self.message


class UnsupportedCapabilityError(CameraModuleError):
    """Raised when an adapter or device does not support an operation.

    Adapters must NOT claim unsupported SDK or protocol operations.
    """

    def __init__(self, vendor: str, capability: str, reason: str = ""):
        msg = f"Vendor '{vendor}' does not support capability '{capability}'."
        if reason:
            msg += f" Reason: {reason}"
        super().__init__(msg, {"vendor": vendor, "capability": capability, "reason": reason})
        self.vendor = vendor
        self.capability = capability
        self.reason = reason


class AdapterNotFoundError(CameraModuleError):
    """Raised when no adapter is registered for the specified vendor."""

    def __init__(self, vendor: str):
        super().__init__(f"No camera adapter registered for vendor: '{vendor}'.", {"vendor": vendor})
        self.vendor = vendor


class CameraConnectionError(CameraModuleError):
    """Raised when connecting or probing a camera device fails."""

    def __init__(self, message: str, host: str, port: int, vendor: str = "", code: int = 0):
        super().__init__(
            message,
            {"host": host, "port": port, "vendor": vendor, "code": code},
        )
        self.host = host
        self.port = port
        self.vendor = vendor
        self.code = code


class AuthenticationError(CameraConnectionError):
    """Raised when device authentication / login fails."""

    pass


class DeviceNotFoundError(CameraModuleError):
    """Raised when a device ID is not found in the registry or manager."""

    def __init__(self, device_id: str):
        super().__init__(f"Device ID '{device_id}' was not found.", {"device_id": device_id})
        self.device_id = device_id


class ChannelNotFoundError(CameraModuleError):
    """Raised when a channel is not found on a device."""

    def __init__(self, device_id: str, channel_id: int):
        super().__init__(
            f"Channel '{channel_id}' not found on device '{device_id}'.",
            {"device_id": device_id, "channel_id": channel_id},
        )
        self.device_id = device_id
        self.channel_id = channel_id


class ConfigurationValidationError(CameraModuleError):
    """Raised when configuration validation encounters an error."""

    pass


class LazyDependencyMissingError(CameraModuleError):
    """Raised when an optional third-party library or SDK DLL is required but missing."""

    def __init__(self, library_name: str, purpose: str, resolution_hint: str = ""):
        msg = f"Optional dependency '{library_name}' is missing ({purpose})."
        if resolution_hint:
            msg += f" Hint: {resolution_hint}"
        super().__init__(
            msg,
            {
                "library_name": library_name,
                "purpose": purpose,
                "resolution_hint": resolution_hint,
            },
        )
        self.library_name = library_name
        self.purpose = purpose
        self.resolution_hint = resolution_hint


# ============================================================================
# Enumerations
# ============================================================================


class VendorType(str, Enum):
    """Supported vendor / driver types."""

    HIKVISION = "hikvision"  # Hikvision & Ezviz (ISAPI / NetSDK / RTSP)
    DAHUA = "dahua"          # Dahua & Imou (NetSDK 37777 / RTSP)
    KBVISION = "kbvision"    # KBVision (Port 8888 / RTSP / NetSDK compat)
    ONVIF = "onvif"          # Standard ONVIF profile S/G/T
    RTSP = "rtsp"            # Generic RTSP stream presets
    LOCAL = "local"          # Direct RTSP / NetSDK
    NVR = "nvr"              # NVR / DVR backend
    CUSTOM = "custom"        # Custom RTSP URL / pipeline

    @classmethod
    def normalize(cls, vendor_str: Optional[str]) -> str:
        """Normalize vendor string to standard lowercase key."""
        if not vendor_str:
            return cls.HIKVISION.value
        v = str(vendor_str).strip().lower()
        aliases = {
            "ezviz": cls.HIKVISION.value,
            "imou": cls.DAHUA.value,
            "dvr": cls.NVR.value,
            "generic": cls.RTSP.value,
        }
        return aliases.get(v, v)


class CameraCapability(str, Enum):
    """Explicit functional capabilities that a camera adapter may support.

    Used for feature gating so callers do not assume unsupported SDK operations.
    """

    DISCOVER = "discover"                      # LAN device discovery (SSDP, NetSDK broadcast, ONVIF WS-Discovery)
    CONNECT = "connect"                        # Connection verification / health probe
    ENUMERATE_CHANNELS = "enumerate_channels"  # Querying channel list & names from NVR / Multi-sensor
    LIVE_MAIN = "live_main"                    # Main stream live playback / capture
    LIVE_SUB = "live_sub"                      # Sub stream live playback / capture
    RECORDING_SEARCH = "recording_search"      # Query recorded video segments by time range
    RECORDING_DOWNLOAD = "recording_download"  # Download or stream recorded segment
    PTZ = "ptz"                                # Pan-tilt-zoom control
    AUDIO = "audio"                            # Two-way or incoming audio stream
    PRIVACY_MASK = "privacy_mask"              # Device-level privacy mask configuration


class StreamType(str, Enum):
    """Stream profile type."""

    MAIN = "main"
    SUB = "sub"
    AUTO = "auto"

    @classmethod
    def normalize(cls, val: Optional[str]) -> StreamType:
        if not val:
            return cls.AUTO
        s = str(val).strip().lower()
        if s == "main":
            return cls.MAIN
        if s == "sub":
            return cls.SUB
        return cls.AUTO


class TransportProtocol(str, Enum):
    """Underlying media and management transport protocols."""

    RTSP = "rtsp"
    NETSDK = "netsdk"
    ISAPI = "isapi"
    ONVIF = "onvif"
    HTTP = "http"
    HTTPS = "https"


class PlaybackSourceType(str, Enum):
    """Camera playback source routing."""

    LOCAL = "local"    # Local RTSP / NetSDK direct recording
    NVR = "nvr"        # Remote NVR / DVR retrieval
    HYBRID = "hybrid"  # Legacy hybrid mode


class TableColorState(str, Enum):
    """Table visual state for admin UI."""

    GREY = "grey"      # Guest access disabled / Privacy Off
    RED = "red"        # Guest access enabled / Active
    GREEN = "green"    # Optional custom status
    BLUE = "blue"      # Optional custom status


# ============================================================================
# Stable ID Generator & Validator Helpers
# ============================================================================

_SLUG_RE = re.compile(r"[^a-zA-Z0-9_-]")


def sanitize_slug(value: str) -> str:
    """Sanitize string into an alphanumeric identifier slug."""
    s = _SLUG_RE.sub("_", str(value).strip().lower())
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "default"


def generate_device_id(
    vendor: str,
    host: str,
    port: int,
    custom_id: Optional[str] = None,
) -> str:
    """Generate a distinct, stable device identifier.

    Identifies a physical device / NVR / DVR / camera host uniquely.
    If custom_id is provided, sanitizes and returns it.
    """
    if custom_id and str(custom_id).strip():
        return sanitize_slug(str(custom_id).strip())

    clean_vendor = sanitize_slug(vendor)
    clean_host = sanitize_slug(host.replace(".", "_"))
    return f"dev_{clean_vendor}_{clean_host}_{int(port)}"


def generate_camera_id(
    device_id: str,
    channel_id: int,
    custom_id: Optional[Union[str, int]] = None,
) -> str:
    """Generate a distinct, stable logical camera identifier.

    Preserves custom/legacy numeric or string camera_id when provided.
    """
    if custom_id is not None and str(custom_id).strip():
        # Preserve numeric or string camera_id without rewriting
        return str(custom_id).strip()
    clean_dev = sanitize_slug(device_id)
    return f"cam_{clean_dev}_ch{int(channel_id)}"


def generate_table_id(raw_id: Union[str, int]) -> str:
    """Generate a distinct, stable table binding identifier (e.g., 'ban-01').

    Guarantees table bindings remain stable when reordered.
    """
    cleaned = str(raw_id).strip().lower()
    return cleaned if cleaned else "table-unknown"


# ============================================================================
# Core Strongly Typed Dataclasses
# ============================================================================


@dataclass(frozen=True)
class DeviceInfo:
    """Represents a physical camera device or NVR/DVR host.

    Holds connection parameters and hardware metadata.
    Credentials are kept secure; password is never displayed in repr.
    """

    device_id: str
    vendor: str
    host: str
    port: int
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
    connect_timeout_sec: float = 5.0
    read_timeout_sec: float = 30.0
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self, include_secrets: bool = False) -> Dict[str, Any]:
        """Serialize to dictionary. Secrets are masked unless explicitly requested."""
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

    def to_config(self) -> Any:
        """Convert DeviceInfo to config DeviceConfig dataclass."""
        from .config import DeviceConfig
        return DeviceConfig.from_dict(self.to_dict(include_secrets=True))


@dataclass(frozen=True)
class ChannelInfo:
    """Represents a physical channel on a multi-channel device or standalone camera."""

    channel_id: int               # 1-based physical channel number
    device_id: str                # Associated DeviceInfo.device_id
    name: str = ""
    enabled: bool = True
    main_stream_path: Optional[str] = None
    sub_stream_path: Optional[str] = None
    main_stream_url: Optional[str] = None
    sub_stream_url: Optional[str] = None
    is_online: Optional[bool] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "device_id": self.device_id,
            "name": self.name,
            "enabled": self.enabled,
            "main_stream_path": self.main_stream_path,
            "sub_stream_path": self.sub_stream_path,
            "main_stream_url": self.main_stream_url,
            "sub_stream_url": self.sub_stream_url,
            "is_online": self.is_online,
            "extra": copy.deepcopy(self.extra),
        }


@dataclass(frozen=True)
class StreamEndpoint:
    """Information required by consumer (FFmpeg, NetSDK, WebRTC, HLS) to acquire a stream."""

    stream_type: StreamType
    url: Optional[str] = None
    protocol: TransportProtocol = TransportProtocol.RTSP
    codec: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[float] = None
    direct_transport: Optional[str] = None  # e.g., 'netsdk', 'ffmpeg', 'isapi'
    extra_params: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self, include_secrets: bool = False) -> Dict[str, Any]:
        url_display = self.url
        if url_display and not include_secrets and "@" in url_display:
            # Mask user:pass in RTSP/HTTP URL
            url_display = re.sub(r"://([^:@]+):([^@]+)@", r"://\1:******@", url_display)
        return {
            "stream_type": self.stream_type.value,
            "url": url_display,
            "protocol": self.protocol.value,
            "codec": self.codec,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "direct_transport": self.direct_transport,
            "extra_params": copy.deepcopy(self.extra_params),
        }


@dataclass(frozen=True)
class RecordingSegment:
    """Represents a recorded video segment found on an NVR or local storage."""

    channel_id: int
    start_time: datetime
    end_time: datetime
    file_path: Optional[str] = None
    file_size: Optional[int] = None
    duration_sec: Optional[float] = None
    record_type: str = "normal"
    stream_type: StreamType = StreamType.MAIN
    device_id: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat(),
            "file_path": self.file_path,
            "file_size": self.file_size,
            "duration_sec": self.duration_sec,
            "record_type": self.record_type,
            "stream_type": self.stream_type.value,
            "device_id": self.device_id,
            "extra": copy.deepcopy(self.extra),
        }


@dataclass(frozen=True)
class ProbeResult:
    """Result of testing connection to a camera device."""

    ok: bool
    device_id: str
    vendor: str
    media_ok: bool = False
    channel_count: int = 0
    message: str = ""
    error_code: Optional[str] = None
    discovered_channels: List[ChannelInfo] = field(default_factory=list)
    latency_ms: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "device_id": self.device_id,
            "vendor": self.vendor,
            "media_ok": self.media_ok,
            "channel_count": self.channel_count,
            "message": self.message,
            "error_code": self.error_code,
            "discovered_channels": [ch.to_dict() for ch in self.discovered_channels],
            "latency_ms": round(self.latency_ms, 2),
        }


@dataclass(frozen=True)
class DownloadResult:
    """Result of downloading or extracting a recording segment."""

    ok: bool
    output_path: str
    bytes_downloaded: int = 0
    duration_sec: float = 0.0
    mode: str = "direct"
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "output_path": self.output_path,
            "bytes_downloaded": self.bytes_downloaded,
            "duration_sec": round(self.duration_sec, 2),
            "mode": self.mode,
            "message": self.message,
        }


# ============================================================================
# Vendor Adapter Protocol & Abstract Base Class
# ============================================================================


@runtime_checkable
class CameraAdapterProtocol(Protocol):
    """Protocol that every vendor camera adapter must satisfy."""

    @property
    def vendor(self) -> str:
        ...

    @property
    def supported_capabilities(self) -> Set[CameraCapability]:
        ...

    def supports(self, capability: CameraCapability) -> bool:
        ...

    def discover(self, timeout_sec: float = 5.0, **kwargs: Any) -> List[DeviceInfo]:
        ...

    def connect(self, device: DeviceInfo, timeout_sec: float = 5.0) -> bool:
        ...

    def probe(self, device: DeviceInfo, timeout_sec: float = 5.0) -> ProbeResult:
        ...

    def enumerate_channels(self, device: DeviceInfo, timeout_sec: float = 5.0) -> List[ChannelInfo]:
        ...

    def get_live_stream(
        self,
        device: DeviceInfo,
        channel_id: int,
        stream: StreamType = StreamType.MAIN,
    ) -> StreamEndpoint:
        ...

    def search_recordings(
        self,
        device: DeviceInfo,
        channel_id: int,
        start_time: datetime,
        end_time: datetime,
        **kwargs: Any,
    ) -> List[RecordingSegment]:
        ...

    def download_recording(
        self,
        device: DeviceInfo,
        segment: RecordingSegment,
        destination: str,
        **kwargs: Any,
    ) -> DownloadResult:
        ...


class BaseCameraAdapter(abc.ABC):
    """Abstract base class for vendor camera adapters.

    Provides capability checking and raises explicit UnsupportedCapabilityError
    when an optional capability is not implemented.
    """

    vendor: str = "base"
    default_port: int = 554
    default_http_port: int = 80
    default_rtsp_port: int = 554

    def __init__(self, supported_capabilities: Optional[Iterable[CameraCapability]] = None):
        self._capabilities: Set[CameraCapability] = (
            set(supported_capabilities) if supported_capabilities is not None else self._default_capabilities()
        )

    @abc.abstractmethod
    def _default_capabilities(self) -> Set[CameraCapability]:
        """Return the default set of capabilities supported by this adapter subclass."""
        raise NotImplementedError

    @property
    def supported_capabilities(self) -> Set[CameraCapability]:
        """Return set of explicitly supported capabilities."""
        return set(self._capabilities)

    def supports(self, capability: CameraCapability) -> bool:
        """Check if capability is explicitly supported."""
        return capability in self._capabilities

    def require_capability(self, capability: CameraCapability, reason: str = "") -> None:
        """Raise UnsupportedCapabilityError if the capability is not supported."""
        if not self.supports(capability):
            raise UnsupportedCapabilityError(self.vendor, capability.value, reason=reason)

    def _ensure_device_info(self, device: Any) -> DeviceInfo:
        """Coerce DeviceConfig or dictionary into strongly typed DeviceInfo."""
        if isinstance(device, DeviceInfo):
            return device
        if hasattr(device, "to_device_info") and callable(device.to_device_info):
            return device.to_device_info()
        if isinstance(device, dict):
            host = str(device.get("host") or device.get("ip") or "").strip()
            port = int(device.get("port") or self.default_port)
            dev_id = str(device.get("device_id") or generate_device_id(self.vendor, host, port))
            return DeviceInfo(
                device_id=dev_id,
                vendor=self.vendor,
                host=host,
                port=port,
                http_port=device.get("http_port"),
                rtsp_port=device.get("rtsp_port"),
                user=str(device.get("user") or device.get("username") or "admin"),
                password=str(device.get("password") or device.get("pass") or ""),
                name=str(device.get("name") or ""),
                channel_count=int(device.get("channel_count", 1) or 1),
                extra=device.get("extra", {}),
            )
        return device

    def discover(self, timeout_sec: float = 5.0, **kwargs: Any) -> List[DeviceInfo]:
        """Discover devices on local network."""
        self.require_capability(
            CameraCapability.DISCOVER,
            f"LAN discovery is not implemented for {self.vendor}.",
        )
        return []

    def connect(self, device: DeviceInfo, timeout_sec: float = 5.0) -> bool:
        """Verify network connectivity and credentials with device."""
        self.require_capability(CameraCapability.CONNECT)
        return True

    def probe(self, device: DeviceInfo, timeout_sec: float = 5.0) -> ProbeResult:
        """Perform full capability probe on device."""
        self.require_capability(CameraCapability.CONNECT)
        return ProbeResult(
            ok=True,
            device_id=device.device_id,
            vendor=self.vendor,
            media_ok=True,
            channel_count=device.channel_count,
            message="Probe successful (base implementation)",
        )

    def enumerate_channels(self, device: DeviceInfo, timeout_sec: float = 5.0) -> List[ChannelInfo]:
        """Enumerate physical channels available on device."""
        self.require_capability(
            CameraCapability.ENUMERATE_CHANNELS,
            f"Multi-channel enumeration is not supported for {self.vendor}.",
        )
        # Default single channel fallback if enumerated capability is declared
        return [
            ChannelInfo(
                channel_id=1,
                device_id=device.device_id,
                name=device.name or "Channel 1",
                enabled=True,
            )
        ]

    @abc.abstractmethod
    def get_live_stream(
        self,
        device: DeviceInfo,
        channel_id: int,
        stream: StreamType = StreamType.MAIN,
    ) -> StreamEndpoint:
        """Acquire endpoint configuration for live stream (Main or Sub)."""
        raise NotImplementedError

    def search_recordings(
        self,
        device: DeviceInfo,
        channel_id: int,
        start_time: datetime,
        end_time: datetime,
        **kwargs: Any,
    ) -> List[RecordingSegment]:
        """Search recordings stored on the device/NVR."""
        self.require_capability(
            CameraCapability.RECORDING_SEARCH,
            f"Remote recording search is not supported by {self.vendor}.",
        )
        return []

    def download_recording(
        self,
        device: DeviceInfo,
        segment: RecordingSegment,
        destination: str,
        **kwargs: Any,
    ) -> DownloadResult:
        """Download or stream a recording segment to local destination."""
        self.require_capability(
            CameraCapability.RECORDING_DOWNLOAD,
            f"Remote recording download is not supported by {self.vendor}.",
        )
        return DownloadResult(
            ok=False,
            output_path=destination,
            message=f"Recording download not supported for {self.vendor}.",
        )


# ============================================================================
# Adapter Registry
# ============================================================================


class CameraAdapterRegistry:
    """Thread-safe registry for vendor camera adapters."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._adapters: Dict[str, BaseCameraAdapter] = {}
        self._factories: Dict[str, Callable[[], BaseCameraAdapter]] = {}

    def register(
        self,
        vendor: str,
        adapter_or_factory: Union[BaseCameraAdapter, Callable[[], BaseCameraAdapter], Type[BaseCameraAdapter]],
    ) -> None:
        """Register an adapter instance, class, or factory callable for a vendor."""
        clean_vendor = VendorType.normalize(vendor)
        with self._lock:
            if isinstance(adapter_or_factory, BaseCameraAdapter):
                self._adapters[clean_vendor] = adapter_or_factory
                self._factories.pop(clean_vendor, None)
            elif isinstance(adapter_or_factory, type) and issubclass(adapter_or_factory, BaseCameraAdapter):
                self._factories[clean_vendor] = adapter_or_factory
                self._adapters.pop(clean_vendor, None)
            elif callable(adapter_or_factory):
                self._factories[clean_vendor] = adapter_or_factory
                self._adapters.pop(clean_vendor, None)
            else:
                raise TypeError(f"Invalid adapter or factory for vendor '{vendor}': {adapter_or_factory}")
            logger.debug("Registered camera adapter for vendor '%s'", clean_vendor)

    def unregister(self, vendor: str) -> None:
        """Unregister an adapter for a vendor."""
        clean_vendor = VendorType.normalize(vendor)
        with self._lock:
            self._adapters.pop(clean_vendor, None)
            self._factories.pop(clean_vendor, None)

    def get(self, vendor: str) -> BaseCameraAdapter:
        """Retrieve an initialized adapter instance for a vendor.

        Raises AdapterNotFoundError if no adapter is registered.
        """
        clean_vendor = VendorType.normalize(vendor)
        with self._lock:
            if clean_vendor in self._adapters:
                return self._adapters[clean_vendor]
            if clean_vendor in self._factories:
                instance = self._factories[clean_vendor]()
                self._adapters[clean_vendor] = instance
                return instance
            raise AdapterNotFoundError(vendor)

    def has_adapter(self, vendor: str) -> bool:
        """Check if an adapter is registered for a vendor."""
        clean_vendor = VendorType.normalize(vendor)
        with self._lock:
            return clean_vendor in self._adapters or clean_vendor in self._factories

    def is_supported(self, vendor: str) -> bool:
        """Alias for has_adapter."""
        return self.has_adapter(vendor)

    def list_supported_vendors(self) -> List[str]:
        """List all registered vendor names."""
        with self._lock:
            return sorted(set(self._adapters.keys()) | set(self._factories.keys()))

    def get_vendor_capabilities(self, vendor: str) -> Set[CameraCapability]:
        """Get capabilities for a registered vendor adapter without failing on optional deps."""
        try:
            adapter = self.get(vendor)
            return adapter.supported_capabilities
        except CameraModuleError:
            return set()

    def clear(self) -> None:
        """Clear all registered adapters."""
        with self._lock:
            self._adapters.clear()
            self._factories.clear()


# Global Singleton Registry
_GLOBAL_REGISTRY = CameraAdapterRegistry()


def get_adapter_registry() -> CameraAdapterRegistry:
    """Return the global camera adapter registry."""
    return _GLOBAL_REGISTRY


# ============================================================================
# Lazy Dependency Helper
# ============================================================================


def lazy_import(
    module_name: str,
    feature_name: str = "",
    resolution_hint: str = "",
) -> Any:
    """Lazily import a module or raise LazyDependencyMissingError when missing.

    Ensures optional vendor SDKs / packages do not crash module loading.
    """
    try:
        return importlib.import_module(module_name)
    except ImportError as e:
        raise LazyDependencyMissingError(
            library_name=module_name,
            purpose=feature_name or f"Feature requiring {module_name}",
            resolution_hint=resolution_hint or f"Install {module_name} using pip or configure SDK path.",
        ) from e


# ============================================================================
# Camera Manager (In-Memory Device & Channel Orchestration)
# ============================================================================


class CameraManager:
    """Thread-safe manager for device lifecycle, channel lookups, and stream acquisition."""

    def __init__(self, registry: Optional[CameraAdapterRegistry] = None) -> None:
        self._registry = registry or get_adapter_registry()
        self._lock = threading.RLock()
        self._devices: Dict[str, DeviceInfo] = {}
        self._channels: Dict[Tuple[str, int], ChannelInfo] = {}

    def register_device(self, device: Any) -> str:
        """Register a device in the manager. Accepts DeviceInfo, DeviceConfig, or dict. Returns device_id."""
        with self._lock:
            if hasattr(device, "to_device_info") and callable(device.to_device_info):
                device = device.to_device_info()
            elif isinstance(device, dict):
                vendor = VendorType.normalize(device.get("vendor", "dahua"))
                host = str(device.get("host") or device.get("ip") or "").strip()
                port = int(device.get("port") or 554)
                dev_id = str(device.get("device_id") or generate_device_id(vendor, host, port))
                device = DeviceInfo(
                    device_id=dev_id,
                    vendor=vendor,
                    host=host,
                    port=port,
                    http_port=device.get("http_port"),
                    rtsp_port=device.get("rtsp_port"),
                    user=str(device.get("user") or device.get("username") or "admin"),
                    password=str(device.get("password") or device.get("pass") or ""),
                    name=str(device.get("name") or ""),
                    channel_count=int(device.get("channel_count", 1) or 1),
                    extra=device.get("extra", {}),
                )
            elif not isinstance(device, DeviceInfo) and hasattr(device, "device_id"):
                device = DeviceInfo(
                    device_id=str(getattr(device, "device_id")),
                    vendor=VendorType.normalize(getattr(device, "vendor", "dahua")),
                    host=str(getattr(device, "host", getattr(device, "ip", ""))),
                    port=int(getattr(device, "port", 554)),
                    http_port=getattr(device, "http_port", None),
                    rtsp_port=getattr(device, "rtsp_port", None),
                    user=str(getattr(device, "user", getattr(device, "username", "admin"))),
                    password=str(getattr(device, "password", getattr(device, "pass", ""))),
                    name=str(getattr(device, "name", "")),
                    serial_number=getattr(device, "serial_number", None),
                    model=getattr(device, "model", None),
                    firmware_version=getattr(device, "firmware_version", None),
                    channel_count=int(getattr(device, "channel_count", 1) or 1),
                    extra=getattr(device, "extra", {}),
                )
            self._devices[device.device_id] = device
            logger.info("CameraManager: Registered device '%s' (%s)", device.device_id, device.vendor)
            return device.device_id

    def unregister_device(self, device_id: str) -> None:
        """Unregister a device and its associated channels."""
        with self._lock:
            self._devices.pop(device_id, None)
            to_remove = [k for k in self._channels if k[0] == device_id]
            for k in to_remove:
                self._channels.pop(k, None)

    def get_device(self, device_id: str) -> Optional[DeviceInfo]:
        """Look up device by device_id."""
        with self._lock:
            return self._devices.get(device_id)

    def list_devices(self) -> List[DeviceInfo]:
        """Return list of all registered devices."""
        with self._lock:
            return list(self._devices.values())

    def register_channel(self, channel: ChannelInfo) -> None:
        """Register a channel for a device."""
        with self._lock:
            self._channels[(channel.device_id, channel.channel_id)] = channel

    def get_channel(self, device_id: str, channel_id: int) -> Optional[ChannelInfo]:
        """Look up a channel by (device_id, channel_id)."""
        with self._lock:
            return self._channels.get((device_id, channel_id))

    def list_channels_for_device(self, device_id: str) -> List[ChannelInfo]:
        """Return list of channels registered for a device."""
        with self._lock:
            return [ch for (d_id, _), ch in self._channels.items() if d_id == device_id]

    def probe_device(self, device_id: str, timeout_sec: float = 5.0) -> ProbeResult:
        """Probe device connection and capabilities using appropriate vendor adapter."""
        dev = self.get_device(device_id)
        if not dev:
            raise DeviceNotFoundError(device_id)
        adapter = self._registry.get(dev.vendor)
        return adapter.probe(dev, timeout_sec=timeout_sec)

    def enumerate_channels(self, device_id: str, timeout_sec: float = 5.0) -> List[ChannelInfo]:
        """Enumerate channels from device via vendor adapter and update manager registry."""
        dev = self.get_device(device_id)
        if not dev:
            raise DeviceNotFoundError(device_id)
        adapter = self._registry.get(dev.vendor)
        channels = adapter.enumerate_channels(dev, timeout_sec=timeout_sec)
        with self._lock:
            for ch in channels:
                self._channels[(ch.device_id, ch.channel_id)] = ch
        return channels

    def get_live_stream_endpoint(
        self,
        device_id: str,
        channel_id: int,
        stream: StreamType = StreamType.MAIN,
    ) -> StreamEndpoint:
        """Resolve live stream endpoint for a device and channel."""
        dev = self.get_device(device_id)
        if not dev:
            raise DeviceNotFoundError(device_id)
        adapter = self._registry.get(dev.vendor)
        return adapter.get_live_stream(dev, channel_id, stream=stream)


# Global Singleton Manager
_GLOBAL_MANAGER = CameraManager()


def get_camera_manager() -> CameraManager:
    """Return the global camera manager instance."""
    return _GLOBAL_MANAGER


def __getattr__(name: str) -> Any:
    """PEP 562 lazy attribute resolver for configuration classes to prevent circular imports."""
    if name in ("DeviceConfig", "CameraConfig", "TableBinding", "ModularCCTVConfig"):
        from .config import (
            CameraConfig,
            DeviceConfig,
            ModularCCTVConfig,
            TableBinding,
        )
        mapping = {
            "DeviceConfig": DeviceConfig,
            "CameraConfig": CameraConfig,
            "TableBinding": TableBinding,
            "ModularCCTVConfig": ModularCCTVConfig,
        }
        return mapping[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
