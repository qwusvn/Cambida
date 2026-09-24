"""Modular camera architecture package for Cambida.

Provides decoupled vendor camera adapters (Hikvision, Dahua, ONVIF, RTSP),
strongly typed data models, distinct stable ID management, and non-destructive
in-memory configuration migration helpers.
"""

from __future__ import annotations

from .core import (
    # Security & Logging
    mask_credential,
    sanitize_dict_for_logging,
    sanitize_slug,
    # Exception Hierarchy
    CameraModuleError,
    UnsupportedCapabilityError,
    AdapterNotFoundError,
    CameraConnectionError,
    AuthenticationError,
    DeviceNotFoundError,
    ChannelNotFoundError,
    ConfigurationValidationError,
    LazyDependencyMissingError,
    # Enums
    VendorType,
    CameraCapability,
    StreamType,
    TransportProtocol,
    PlaybackSourceType,
    TableColorState,
    # ID Generators
    generate_device_id,
    generate_camera_id,
    generate_table_id,
    # Core Data Models
    DeviceInfo,
    ChannelInfo,
    StreamEndpoint,
    RecordingSegment,
    ProbeResult,
    DownloadResult,
    # Adapter Protocol & Base Class
    CameraAdapterProtocol,
    BaseCameraAdapter,
    # Registry & Manager
    CameraAdapterRegistry,
    get_adapter_registry,
    CameraManager,
    get_camera_manager,
    # Lazy Import Helper
    lazy_import,
)

from .config import (
    # Presets & Port Mappings
    VENDOR_DEFAULT_PORTS,
    RTSP_PRESETS,
    get_vendor_default_port,
    build_rtsp_stream_path,
    build_rtsp_url,
    # Configuration Dataclasses
    DeviceConfig,
    CameraConfig,
    TableBinding,
    ModularCCTVConfig,
    # Migration & Validation Helpers
    migrate_legacy_config_to_modular,
    validate_modular_config,
)

from .dahua import (
    DahuaCameraAdapter,
    ImouCameraAdapter,
    KBVisionCameraAdapter,
    DahuaAdapterError,
    DahuaNetSDKNotFoundError,
    DahuaAuthenticationError,
    DahuaConnectionError,
    DahuaChannelError,
    DEFAULT_DAHUA_NETSDK_PORT,
    DEFAULT_DAHUA_HTTP_PORT,
    DEFAULT_DAHUA_RTSP_PORT,
    DEFAULT_KBVISION_PORT,
    DEFAULT_KBVISION_HTTP_PORT,
    DEFAULT_KBVISION_RTSP_PORT,
    build_dahua_rtsp_paths,
    build_dahua_rtsp_url,
    build_kbvision_rtsp_url,
    probe_dahua_device,
    probe_kbvision_device,
    is_dahua_netsdk_available,
    find_dahua_netsdk_dir,
    probe_dvrip_auth_safe,
)

__version__ = "1.0.0"

__all__ = [
    # Metadata
    "__version__",
    # Security & Sanitization
    "mask_credential",
    "sanitize_dict_for_logging",
    "sanitize_slug",
    # Exceptions
    "CameraModuleError",
    "UnsupportedCapabilityError",
    "AdapterNotFoundError",
    "CameraConnectionError",
    "AuthenticationError",
    "DeviceNotFoundError",
    "ChannelNotFoundError",
    "ConfigurationValidationError",
    "LazyDependencyMissingError",
    # Enums
    "VendorType",
    "CameraCapability",
    "StreamType",
    "TransportProtocol",
    "PlaybackSourceType",
    "TableColorState",
    # Stable ID Generators
    "generate_device_id",
    "generate_camera_id",
    "generate_table_id",
    # Data Models
    "DeviceInfo",
    "ChannelInfo",
    "StreamEndpoint",
    "RecordingSegment",
    "ProbeResult",
    "DownloadResult",
    # Protocols & Adapters
    "CameraAdapterProtocol",
    "BaseCameraAdapter",
    # Registry & Management
    "CameraAdapterRegistry",
    "get_adapter_registry",
    "CameraManager",
    "get_camera_manager",
    "lazy_import",
    # Port Mappings & Presets
    "VENDOR_DEFAULT_PORTS",
    "RTSP_PRESETS",
    "get_vendor_default_port",
    "build_rtsp_stream_path",
    "build_rtsp_url",
    # Config Dataclasses
    "DeviceConfig",
    "CameraConfig",
    "TableBinding",
    "ModularCCTVConfig",
    # Migration & Validation
    "migrate_legacy_config_to_modular",
    "validate_modular_config",
    # Dahua, Imou & KBVision Adapters
    "DahuaCameraAdapter",
    "ImouCameraAdapter",
    "KBVisionCameraAdapter",
    "DahuaAdapterError",
    "DahuaNetSDKNotFoundError",
    "DahuaAuthenticationError",
    "DahuaConnectionError",
    "DahuaChannelError",
    "DEFAULT_DAHUA_NETSDK_PORT",
    "DEFAULT_DAHUA_HTTP_PORT",
    "DEFAULT_DAHUA_RTSP_PORT",
    "DEFAULT_KBVISION_PORT",
    "DEFAULT_KBVISION_HTTP_PORT",
    "DEFAULT_KBVISION_RTSP_PORT",
    "build_dahua_rtsp_paths",
    "build_dahua_rtsp_url",
    "build_kbvision_rtsp_url",
    "probe_dahua_device",
    "probe_kbvision_device",
    "is_dahua_netsdk_available",
    "find_dahua_netsdk_dir",
    "probe_dvrip_auth_safe",
]
