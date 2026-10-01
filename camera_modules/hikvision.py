"""
Hikvision and Ezviz camera module adapter for Cambida.

Provides an optional, lazy-loaded ctypes wrapper for Hikvision HCNetSDK (Windows),
supporting device login, automated real channel inventory distinguishing IP cameras
from DVR/NVR units without requiring manual user toggles, device and channel
capability inspection, and guaranteed safe disconnection.

Also provides explicit capability declarations and paths for ISAPI and RTSP fallbacks
when native HCNetSDK binaries are not present, ensuring genuine protocol boundaries
without faking SDK connectivity. Credentials are never persisted, logged, or exposed.
"""

from __future__ import annotations

import atexit
import ctypes as C
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import quote

# Try importing core abstractions if available concurrently
try:
    from camera_modules.core import ChannelInfo, DeviceConfig
except ImportError:
    ChannelInfo = None  # type: ignore[assignment, misc]
    DeviceConfig = None  # type: ignore[assignment, misc]


# =============================================================================
# Constants and Error Codes
# =============================================================================

DEFAULT_HIKVISION_SDK_PORT = 8000
DEFAULT_HIKVISION_HTTP_PORT = 80
DEFAULT_HIKVISION_HTTPS_PORT = 443
DEFAULT_HIKVISION_RTSP_PORT = 554

SUPPORTED_VENDORS = {"hikvision", "ezviz", "hik", "ezviz_camera"}

MAX_CHANNUM_V30 = 64
MAX_DIGITAL_CHANNELS = MAX_CHANNUM_V30 * 4
NET_DVR_GET_DIGITAL_CHANNEL_STATE = 6126
STREAM_PS = 0x1

HIKVISION_DIGITAL_CHANNEL_STATUS: Dict[int, str] = {
    0: "invalid",
    1: "connected",
    2: "connecting",
    3: "bandwidth_exceeded",
    4: "domain_error",
    5: "channel_error",
    6: "account_error",
    7: "stream_type_unsupported",
    8: "dvr_ip_conflict",
    9: "ipc_ip_conflict",
    10: "network_unreachable",
    11: "ipc_not_present",
    12: "ipc_exception",
    13: "other_error",
    14: "resolution_unsupported",
    15: "language_mismatch",
    16: "user_locked",
    17: "not_activated",
    18: "user_not_exist",
    19: "ipc_unregistered",
    20: "poe_detecting",
    24: "token_auth_failed",
}

# HCNetSDK Error Codes Mapping
HIKVISION_SDK_ERRORS: Dict[int, str] = {
    0: "NET_DVR_NOERROR: Thao tác thành công",
    1: "NET_DVR_PASSWORD_ERROR: Tên đăng nhập hoặc mật khẩu không chính xác",
    2: "NET_DVR_NOENOUGHPRI: Tài khoản không đủ quyền hạn thực hiện thao tác",
    3: "NET_DVR_NOINIT: SDK chưa được khởi tạo",
    4: "NET_DVR_CHANNEL_ERROR: Số thứ tự kênh không hợp lệ",
    5: "NET_DVR_OVER_MAXLINK: Vượt quá giới hạn kết nối đồng thời của thiết bị",
    7: "NET_DVR_NETWORK_FAIL_CONNECT: Không thể kết nối tới thiết bị (IP hoặc cổng không đúng)",
    8: "NET_DVR_NETWORK_SEND_ERROR: Lỗi gửi gói tin qua mạng tới thiết bị",
    9: "NET_DVR_NETWORK_RECV_ERROR: Lỗi nhận dữ liệu từ thiết bị qua mạng",
    10: "NET_DVR_NETWORK_RECV_TIMEOUT: Hết thời gian chờ nhận phản hồi từ thiết bị (Timeout)",
    11: "NET_DVR_NETWORK_ERRORDATA: Dữ liệu mạng truyền nhận bị lỗi hoặc sai định dạng",
    12: "NET_DVR_ORDER_ERROR: Sai thứ tự gọi hàm SDK",
    13: "NET_DVR_OPERNOPERMIT: Thao tác không được phép trên thiết bị này",
    14: "NET_DVR_COMMANDTIMEOUT: Lệnh gửi đi bị timeout",
    17: "NET_DVR_PARAMETER_ERROR: Tham số truyền vào hàm không hợp lệ",
    18: "NET_DVR_CHAN_EXCEPTION: Kênh đang trong trạng thái ngoại lệ",
    19: "NET_DVR_NODISK: Thiết bị không có ổ cứng lưu trữ",
    23: "NET_DVR_DVROUT_RESOURCE: Tài nguyên thiết bị (CPU/RAM/Băng thông) đã cạn kiệt",
    24: "NET_DVR_DVROPNOTSUPPORT: Thiết bị không hỗ trợ thao tác này",
    29: "NET_DVR_DVRUSER_LOCKED: Tài khoản bị khóa tạm thời do nhập sai thông tin nhiều lần",
    46: "NET_DVR_NOSUPPORT: Thiết bị không hỗ trợ chức năng được yêu cầu",
    52: "NET_DVR_DIR_ERROR: Lỗi đường dẫn thư mục",
}


# =============================================================================
# Custom Exceptions
# =============================================================================

class HikvisionError(RuntimeError):
    """Base exception for all Hikvision adapter errors."""


class HikvisionSDKNotFoundError(HikvisionError):
    """Raised when HCNetSDK.dll or Windows dependencies are absent or unreadable."""


class HikvisionAuthError(HikvisionError):
    """Raised when authentication to the device fails."""


class HikvisionConnectionError(HikvisionError):
    """Raised when network connectivity or socket connection fails."""


class HikvisionDeviceError(HikvisionError):
    """Raised when the device returns a functional or hardware error."""


class HikvisionChannelError(HikvisionDeviceError):
    """Raised when an invalid channel or stream configuration is requested."""


# =============================================================================
# Data Structures
# =============================================================================

@dataclass
class HikvisionDeviceInfo:
    """Device metadata and hardware specifications returned by HCNetSDK or ISAPI."""
    device_id: str
    vendor: str
    model: str
    serial_number: str
    device_kind: str  # 'ipc', 'nvr', 'dvr', or 'unknown'
    analog_channels: int
    digital_channels: int
    total_channels: int
    start_analog_channel: int
    start_digital_channel: int
    dvr_type: int
    raw_details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class HikvisionChannel:
    """Detailed channel descriptor including capabilities, streams, and fallback routes."""
    channel_id: Union[int, str]
    channel_number: int  # 1-based sequential index
    name: str
    is_ip_camera: bool
    is_online: Optional[bool]
    supports_main_stream: bool
    supports_sub_stream: bool
    rtsp_main_path: str
    rtsp_sub_path: str
    isapi_channel_id: int
    sdk_channel_number: int
    status_code: Optional[int] = None
    status_text: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_core_channel_info(self) -> Any:
        """Convert to camera_modules.core.ChannelInfo if available."""
        if ChannelInfo is not None:
            return ChannelInfo(channel_id=self.channel_id, name=self.name)
        return {"channel_id": self.channel_id, "name": self.name}


@dataclass
class HikvisionCapabilities:
    """Explicit capability matrix for the adapter and target vendor environment."""
    vendor: str
    sdk_available: bool
    sdk_binary_path: Optional[str]
    supports_sdk_login: bool
    supports_isapi: bool
    supports_rtsp: bool
    default_sdk_port: int = DEFAULT_HIKVISION_SDK_PORT
    default_http_port: int = DEFAULT_HIKVISION_HTTP_PORT
    default_rtsp_port: int = DEFAULT_HIKVISION_RTSP_PORT
    rtsp_presets: Dict[str, str] = field(default_factory=dict)
    isapi_endpoints: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class ProbeResult:
    """Summary result of probing a Hikvision/Ezviz endpoint."""
    ok: bool
    transport: str  # 'hcnetsdk', 'isapi', 'rtsp', or 'none'
    device_kind: str
    channels: List[HikvisionChannel] = field(default_factory=list)
    message: str = ""
    error_code: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# =============================================================================
# Ctypes Structs for Hikvision HCNetSDK (Windows x86 / x64)
# =============================================================================

# HCNetSDK uses 32-bit LONG/BOOL on Windows even in the x64 SDK.
LONG = C.c_int32
DWORD = C.c_uint32
WORD = C.c_uint16
BYTE = C.c_ubyte
BOOL = C.c_int32
HWND = C.c_void_p


class NET_DVR_DEVICEINFO_V30(C.Structure):
    """Official HCNetSDK NET_DVR_DEVICEINFO_V30 ABI."""
    _fields_ = [
        ("sSerialNumber", BYTE * 48),
        ("byAlarmInPortNum", BYTE),
        ("byAlarmOutPortNum", BYTE),
        ("byDiskNum", BYTE),
        ("byDVRType", BYTE),
        ("byChanNum", BYTE),
        ("byStartChan", BYTE),
        ("byAudioChanNum", BYTE),
        ("byIPChanNum", BYTE),
        ("byZeroChanNum", BYTE),
        ("byMainProto", BYTE),
        ("bySubProto", BYTE),
        ("bySupport", BYTE),
        ("bySupport1", BYTE),
        ("bySupport2", BYTE),
        ("wDevType", WORD),
        ("bySupport3", BYTE),
        ("byMultiStreamProto", BYTE),
        ("byStartDChan", BYTE),
        ("byStartDTalkChan", BYTE),
        ("byHighDChanNum", BYTE),
        ("bySupport4", BYTE),
        ("byLanguageType", BYTE),
        ("byVoiceInChanNum", BYTE),
        ("byStartVoiceInChanNo", BYTE),
        ("byRes3", BYTE * 2),
        ("byMirrorChanNum", BYTE),
        ("wStartMirrorChanNo", WORD),
        ("byRes2", BYTE * 2),
    ]


class NET_DVR_USER_LOGIN_INFO(C.Structure):
    """Official current NET_DVR_USER_LOGIN_INFO ABI for NET_DVR_Login_V40."""
    _fields_ = [
        ("sDeviceAddress", C.c_char * 129),
        ("byUseTransport", BYTE),
        ("wPort", WORD),
        ("szUserName", C.c_char * 64),
        ("szPassword", C.c_char * 64),
        ("cbLoginResult", C.c_void_p),
        ("pUser", C.c_void_p),
        ("bUseAsynLogin", BOOL),
        ("byProxyType", BYTE),
        ("byUseUTCTime", BYTE),
        ("byLoginMode", BYTE),
        ("byHttps", BYTE),
        ("iProxyID", LONG),
        ("byVerifyMode", LONG),
        ("byRes3", BYTE * 119),
    ]


class NET_DVR_DEVICEINFO_V40(C.Structure):
    """Official current NET_DVR_DEVICEINFO_V40 output ABI."""
    _fields_ = [
        ("struDeviceV30", NET_DVR_DEVICEINFO_V30),
        ("bySupportLock", BYTE),
        ("byRetryLoginTime", BYTE),
        ("byPasswordLevel", BYTE),
        ("byProxyType", BYTE),
        ("dwSurplusLockTime", DWORD),
        ("byCharEncodeType", BYTE),
        ("bySupportDev5", BYTE),
        ("byLoginMode", BYTE),
        ("byRes3", DWORD),
        ("iResidualValidity", C.c_int32),
        ("byResidualValidity", BYTE),
        ("bySingleStartDTalkChan", BYTE),
        ("bySingleDTalkChanNums", BYTE),
        ("byPassWordResetLevel", BYTE),
        ("bySupportStreamEncrypt", BYTE),
        ("byMarketType", BYTE),
        ("byRes2", BYTE * 238),
    ]


class NET_DVR_DIGITAL_CHANNEL_STATE(C.Structure):
    """Digital channel connection states returned by command 6126."""
    _fields_ = [
        ("dwSize", DWORD),
        ("byDigitalAudioChanTalkState", BYTE * MAX_CHANNUM_V30),
        ("byDigitalChanState", BYTE * MAX_CHANNUM_V30),
        ("byDigitalAudioChanTalkStateEx", BYTE * (MAX_CHANNUM_V30 * 3)),
        ("byDigitalChanStateEx", BYTE * (MAX_CHANNUM_V30 * 3)),
        ("byRes", BYTE * 64),
    ]


class NET_DVR_PREVIEWINFO(C.Structure):
    """Preview parameters used by NET_DVR_RealPlay_V40."""
    _fields_ = [
        ("lChannel", LONG),
        ("dwStreamType", DWORD),
        ("dwLinkMode", DWORD),
        ("hPlayWnd", HWND),
        ("bBlocked", BOOL),
        ("bPassbackRecord", BOOL),
        ("byPreviewMode", BYTE),
        ("byStreamID", BYTE * 32),
        ("byProtoType", BYTE),
        ("byRes1", BYTE),
        ("byVideoCodingType", BYTE),
        ("dwDisplayBufNum", DWORD),
        ("byNPQMode", BYTE),
        ("byRecvMetaData", BYTE),
        ("byDataType", BYTE),
        ("byRes", BYTE * 213),
    ]


class NET_DVR_JPEGPARA(C.Structure):
    _fields_ = [
        ("wPicSize", WORD),
        ("wPicQuality", WORD),
    ]


# =============================================================================
# Runtime Loader for HCNetSDK
# =============================================================================

_RUNTIME_LOCK = threading.RLock()
_RUNTIME_DLL: Any = None
_RUNTIME_DLL_DIR_HANDLES: List[Any] = []
_RUNTIME_INITIALIZED: bool = False


def _candidate_sdk_dirs(base_dir: Optional[str] = None) -> List[str]:
    """Return an ordered, deduplicated list of candidate directories for HCNetSDK."""
    seen = set()
    candidates = []

    # 1. Environment variables
    for env_var in ("HIKVISION_NETSDK_DIR", "HCNETSDK_DIR", "HIKVISION_SDK_DIR"):
        val = os.environ.get(env_var)
        if val:
            path = os.path.abspath(val)
            if path not in seen:
                seen.add(path)
                candidates.append(path)

    # 2. Caller-specified base_dir (or directory containing HCNetSDK.dll directly)
    if base_dir:
        abs_base = os.path.abspath(base_dir)
        if os.path.isfile(os.path.join(abs_base, "HCNetSDK.dll")):
            if abs_base not in seen:
                seen.add(abs_base)
                candidates.append(abs_base)
        else:
            for sub in ("vendor/hikvision_netsdk", "vendor/hcnetsdk", "vendor/hikvision", "hcnetsdk"):
                sub_path = os.path.abspath(os.path.join(abs_base, sub))
                if sub_path not in seen:
                    seen.add(sub_path)
                    candidates.append(sub_path)

    # 3. Module directory relative paths
    module_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(module_dir, ".."))
    for root_candidate in (project_root, module_dir):
        for sub in ("vendor/hikvision_netsdk", "vendor/hcnetsdk", "vendor/hikvision", "hcnetsdk"):
            p = os.path.abspath(os.path.join(root_candidate, sub))
            if p not in seen:
                seen.add(p)
                candidates.append(p)

    # 4. Standard Windows iVMS-4200 and SADP installation locations
    if os.name == "nt":
        for std_dir in (
            r"C:\Program Files (x86)\iVMS-4200 Site\iVMS-4200 Client\Client",
            r"C:\Program Files (x86)\iVMS-4200 Site\iVMS-4200 Client\Server",
            r"C:\Program Files\iVMS-4200 Lite",
            r"C:\Program Files (x86)\SADP\SADP",
            r"C:\Program Files (x86)\Ezviz Studio\sadp",
        ):
            if os.path.isdir(std_dir):
                std_abs = os.path.abspath(std_dir)
                if std_abs not in seen:
                    seen.add(std_abs)
                    candidates.append(std_abs)

    return candidates


def find_hcnetsdk_dir(base_dir: Optional[str] = None) -> Optional[str]:
    """Scan candidate directories and return the directory containing HCNetSDK.dll, or None."""
    for path in _candidate_sdk_dirs(base_dir):
        if os.path.isfile(os.path.join(path, "HCNetSDK.dll")):
            return path
    return None


def hcnetsdk_available(base_dir: Optional[str] = None) -> bool:
    """Return True if running on Windows and HCNetSDK.dll is found."""
    if os.name != "nt":
        return False
    return find_hcnetsdk_dir(base_dir) is not None


def _configure_api(dll: Any) -> None:
    """Set up ctypes prototypes for required HCNetSDK functions."""
    dll.NET_DVR_Init.restype = BOOL
    dll.NET_DVR_Init.argtypes = []

    dll.NET_DVR_Cleanup.restype = BOOL
    dll.NET_DVR_Cleanup.argtypes = []

    dll.NET_DVR_GetLastError.restype = DWORD
    dll.NET_DVR_GetLastError.argtypes = []

    dll.NET_DVR_SetConnectTime.restype = BOOL
    dll.NET_DVR_SetConnectTime.argtypes = [DWORD, DWORD]

    dll.NET_DVR_SetReconnect.restype = BOOL
    dll.NET_DVR_SetReconnect.argtypes = [DWORD, BOOL]

    if hasattr(dll, "NET_DVR_Login_V40"):
        dll.NET_DVR_Login_V40.restype = LONG
        dll.NET_DVR_Login_V40.argtypes = [C.c_void_p, C.c_void_p]

    if hasattr(dll, "NET_DVR_Login_V30"):
        dll.NET_DVR_Login_V30.restype = LONG
        dll.NET_DVR_Login_V30.argtypes = [
            C.c_char_p,
            WORD,
            C.c_char_p,
            C.c_char_p,
            C.c_void_p,
        ]

    if hasattr(dll, "NET_DVR_Logout"):
        dll.NET_DVR_Logout.restype = BOOL
        dll.NET_DVR_Logout.argtypes = [LONG]

    if hasattr(dll, "NET_DVR_Logout_V30"):
        dll.NET_DVR_Logout_V30.restype = BOOL
        dll.NET_DVR_Logout_V30.argtypes = [LONG]

    if hasattr(dll, "NET_DVR_GetDVRConfig"):
        dll.NET_DVR_GetDVRConfig.restype = BOOL
        dll.NET_DVR_GetDVRConfig.argtypes = [
            LONG,
            DWORD,
            LONG,
            C.c_void_p,
            DWORD,
            C.POINTER(DWORD),
        ]

    if hasattr(dll, "NET_DVR_RealPlay_V40"):
        dll.NET_DVR_RealPlay_V40.restype = LONG
        dll.NET_DVR_RealPlay_V40.argtypes = [
            LONG,
            C.POINTER(NET_DVR_PREVIEWINFO),
            C.c_void_p,
            C.c_void_p,
        ]

    if hasattr(dll, "NET_DVR_StopRealPlay"):
        dll.NET_DVR_StopRealPlay.restype = BOOL
        dll.NET_DVR_StopRealPlay.argtypes = [LONG]

    if hasattr(dll, "NET_DVR_SaveRealData_V30"):
        dll.NET_DVR_SaveRealData_V30.restype = BOOL
        dll.NET_DVR_SaveRealData_V30.argtypes = [LONG, DWORD, C.c_char_p]

    if hasattr(dll, "NET_DVR_StopSaveRealData"):
        dll.NET_DVR_StopSaveRealData.restype = BOOL
        dll.NET_DVR_StopSaveRealData.argtypes = [LONG]

    if hasattr(dll, "NET_DVR_CaptureJPEGPicture"):
        dll.NET_DVR_CaptureJPEGPicture.restype = BOOL
        dll.NET_DVR_CaptureJPEGPicture.argtypes = [
            LONG,
            LONG,
            C.POINTER(NET_DVR_JPEGPARA),
            C.c_char_p,
        ]


def _load_runtime(base_dir: Optional[str] = None) -> Any:
    """
    Lazy load and initialize HCNetSDK.dll.
    Raises HikvisionSDKNotFoundError with an actionable Vietnamese message if unavailable.
    """
    global _RUNTIME_DLL, _RUNTIME_DLL_DIR_HANDLES, _RUNTIME_INITIALIZED
    with _RUNTIME_LOCK:
        if _RUNTIME_DLL is not None and _RUNTIME_INITIALIZED:
            return _RUNTIME_DLL

        if os.name != "nt":
            raise HikvisionSDKNotFoundError(
                "Adapter Hikvision HCNetSDK hiện chỉ hỗ trợ môi trường Windows (yêu cầu WinDLL)."
            )

        sdk_dir = find_hcnetsdk_dir(base_dir)
        if not sdk_dir:
            raise HikvisionSDKNotFoundError(
                "Không tìm thấy thư viện Hikvision HCNetSDK (HCNetSDK.dll). "
                "Vui lòng cung cấp thư mục vendor/hikvision_netsdk hoặc thiết lập biến môi trường HIKVISION_NETSDK_DIR."
            )

        # Keep DLL-directory handles alive for the full HCNetSDK runtime.
        if hasattr(os, "add_dll_directory"):
            try:
                _RUNTIME_DLL_DIR_HANDLES.append(os.add_dll_directory(sdk_dir))
                com_dir = os.path.join(sdk_dir, "HCNetSDKCom")
                if os.path.isdir(com_dir):
                    _RUNTIME_DLL_DIR_HANDLES.append(os.add_dll_directory(com_dir))
            except OSError as exc:
                raise HikvisionSDKNotFoundError(
                    f"Không thể đăng ký thư mục DLL HCNetSDK ({sdk_dir}): {exc}"
                ) from exc

        dll_path = os.path.join(sdk_dir, "HCNetSDK.dll")
        try:
            # Pre-load dependent libraries if present in the same directory
            for dep in ("HCCore.dll", "hpr.dll", "PlayCtrl.dll"):
                dep_path = os.path.join(sdk_dir, dep)
                if os.path.isfile(dep_path):
                    try:
                        C.WinDLL(dep_path)
                    except OSError:
                        pass
            dll = C.WinDLL(dll_path)
        except OSError as exc:
            raise HikvisionSDKNotFoundError(
                f"Không nạp được thư viện Hikvision HCNetSDK ({dll_path}): {exc}"
            ) from exc

        _configure_api(dll)

        if not dll.NET_DVR_Init():
            err_code = int(dll.NET_DVR_GetLastError())
            err_desc = HIKVISION_SDK_ERRORS.get(err_code, f"Lỗi 0x{err_code:08X}")
            raise HikvisionSDKNotFoundError(
                f"Khởi tạo NET_DVR_Init thất bại: {err_desc} (code {err_code})."
            )

        # Default connect timeout: 5000ms, 2 attempts
        dll.NET_DVR_SetConnectTime(5000, 2)
        dll.NET_DVR_SetReconnect(10000, True)

        _RUNTIME_DLL = dll
        _RUNTIME_INITIALIZED = True
        return dll


def _cleanup_runtime() -> None:
    """Clean up HCNetSDK runtime on process termination."""
    global _RUNTIME_DLL, _RUNTIME_INITIALIZED, _RUNTIME_DLL_DIR_HANDLES
    with _RUNTIME_LOCK:
        if _RUNTIME_DLL is not None and _RUNTIME_INITIALIZED:
            try:
                _RUNTIME_DLL.NET_DVR_Cleanup()
            except Exception:
                pass
        _RUNTIME_DLL = None
        _RUNTIME_INITIALIZED = False
        for handle in reversed(_RUNTIME_DLL_DIR_HANDLES):
            try:
                handle.close()
            except Exception:
                pass
        _RUNTIME_DLL_DIR_HANDLES = []


atexit.register(_cleanup_runtime)


# =============================================================================
# SADP Auto-Discovery (Windows x64 via Sadp.dll)
# =============================================================================

class SADP_DEVICE_INFO(C.Structure):
    """Structure matching Sadp.dll callback output (reverse-engineered from x64 DLL)."""
    _fields_ = [
        ("dwDevType", DWORD),
        ("byRes1", BYTE * 8),
        ("szSerialNO", C.c_char * 48),
        ("szMAC", C.c_char * 20),
        ("szIPv4Address", C.c_char * 16),
        ("szIPv4SubnetMask", C.c_char * 16),
        ("byRes2", BYTE * 4),
        ("dwPort", DWORD),
        ("byRes3", BYTE * 8),
        ("szSoftwareVersion", C.c_char * 48),
    ]

_SADP_LOCK = threading.RLock()
_SADP_CALLBACK_TYPE = C.WINFUNCTYPE(None, C.POINTER(SADP_DEVICE_INFO), C.c_void_p) if os.name == "nt" else None


def find_sadp_dir(base_dir: Optional[str] = None) -> Optional[str]:
    """Scan candidate directories and return the directory containing Sadp.dll, or None."""
    for path in _candidate_sdk_dirs(base_dir):
        if os.path.isfile(os.path.join(path, "Sadp.dll")):
            return path
    return None


def sadp_available(base_dir: Optional[str] = None) -> bool:
    """Return True if running on Windows and Sadp.dll is available."""
    if os.name != "nt":
        return False
    return find_sadp_dir(base_dir) is not None


def discover_hikvision_sadp(timeout_sec: float = 2.0, base_dir: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Discover Hikvision and Ezviz devices on LAN using native Sadp.dll.
    Returns a list of device dictionaries: ip, port, serial, mac, subnet_mask, firmware, vendor.
    """
    if not sadp_available(base_dir):
        return []

    sdk_dir = find_sadp_dir(base_dir)
    dll_path = os.path.join(sdk_dir, "Sadp.dll")

    with _SADP_LOCK:
        if hasattr(os, "add_dll_directory"):
            try:
                os.add_dll_directory(sdk_dir)
            except Exception:
                pass

        try:
            sadp = C.WinDLL(dll_path)
            sadp.SADP_Start_V30.argtypes = [_SADP_CALLBACK_TYPE, C.c_int, C.c_void_p]
            sadp.SADP_Start_V30.restype = BOOL
            sadp.SADP_SendInquiry.argtypes = []
            sadp.SADP_SendInquiry.restype = BOOL
            sadp.SADP_Stop.argtypes = []
            sadp.SADP_Stop.restype = BOOL
        except Exception:
            return []

        devices: List[Dict[str, Any]] = []
        seen_keys = set()

        def _cb(p_info, p_user):
            if not p_info:
                return
            try:
                info = p_info.contents
                ip = info.szIPv4Address.decode("ascii", errors="ignore").strip("\x00")
                if not ip or ip == "0.0.0.0":
                    return
                port = int(info.dwPort) or DEFAULT_HIKVISION_SDK_PORT
                serial = info.szSerialNO.decode("ascii", errors="ignore").strip("\x00")
                mac = info.szMAC.decode("ascii", errors="ignore").strip("\x00")
                mask = info.szIPv4SubnetMask.decode("ascii", errors="ignore").strip("\x00")
                fw = info.szSoftwareVersion.decode("ascii", errors="ignore").strip("\x00")

                key = f"{ip}:{port}"
                if key not in seen_keys:
                    seen_keys.add(key)
                    vendor = "ezviz" if "CS-" in serial or "EZVIZ" in serial.upper() else "hikvision"
                    devices.append({
                        "ip": ip,
                        "port": port,
                        "serial": serial,
                        "mac": mac,
                        "subnet_mask": mask,
                        "firmware": fw,
                        "vendor": vendor,
                    })
            except Exception:
                pass

        cb_holder = _SADP_CALLBACK_TYPE(_cb)
        started = sadp.SADP_Start_V30(cb_holder, 1, None)
        if started:
            try:
                sadp.SADP_SendInquiry()
                sleep_duration = min(1.2, max(0.5, float(timeout_sec) - 0.5))
                time.sleep(sleep_duration)
            finally:
                sadp.SADP_Stop()

        return devices


# =============================================================================
# Helper Utilities & Validation
# =============================================================================

def _sdk_error(dll: Any) -> Tuple[int, str]:
    """Retrieve last error code and human-readable Vietnamese description."""
    if dll is None:
        return 0, "No DLL"
    code = int(dll.NET_DVR_GetLastError())
    desc = HIKVISION_SDK_ERRORS.get(code, f"Lỗi không xác định (0x{code:08X})")
    return code, desc


def _resolve_host(host: str) -> str:
    """Validate and resolve host without throwing raw unhandled socket exceptions."""
    stripped = str(host or "").strip()
    if not stripped:
        raise HikvisionConnectionError("Địa chỉ IP/host của thiết bị Hikvision không được để trống.")
    try:
        return socket.gethostbyname(stripped)
    except OSError as exc:
        raise HikvisionConnectionError(
            f"Không phân giải được địa chỉ thiết bị '{stripped}': {exc}"
        ) from exc


def _bounded_ascii(value: Any, field_name: str, maximum: int = 63) -> bytes:
    """Encode an ASCII/UTF-8 string and ensure it fits within ctypes byte buffers."""
    raw = str(value or "").encode("utf-8")
    if len(raw) > maximum:
        raise HikvisionError(f"{field_name} vượt quá độ dài tối đa cho phép ({maximum} bytes).")
    return raw


def is_hikvision_compatible(vendor: str) -> bool:
    """
    Check if a given vendor string is compatible with the Hikvision/Ezviz protocol family.
    Ezviz is Hikvision's consumer line and shares the port 8000 SDK / ISAPI / RTSP conventions.
    """
    v = str(vendor or "").strip().lower()
    return v in SUPPORTED_VENDORS


def detect_device_kind(
    analog_count: int,
    digital_count: int,
    dvr_type: int = 0,
    model: str = "",
) -> str:
    """
    Autonomous classification of device kind ('ipc', 'nvr', 'dvr') without manual user toggle.

    Rules:
    - Pure IP Camera (IPC): analog == 0 and digital == 1, or analog == 1 and digital == 0
      with DVR type indicating IPC / dome or model string containing 'IPC'/'DS-2CD'.
    - NVR (Network Video Recorder): digital > 1 and analog == 0.
    - DVR (Digital Video Recorder): analog > 1, or hybrid analog > 0 and digital > 0.
    """
    model_upper = str(model or "").upper()

    # Model keyword overrides
    if "NVR" in model_upper or "DS-7" in model_upper or "DS-9" in model_upper:
        if digital_count > 1 or analog_count == 0:
            return "nvr"
    if "IPC" in model_upper or "DS-2CD" in model_upper or "EZVIZ" in model_upper:
        if analog_count <= 1 and digital_count <= 1:
            return "ipc"

    # Channel count heuristics
    if analog_count == 0 and digital_count == 1:
        return "ipc"
    if analog_count == 1 and digital_count == 0:
        return "ipc"
    if digital_count > 1 and analog_count == 0:
        return "nvr"
    if analog_count > 1 and digital_count == 0:
        return "dvr"
    if analog_count > 0 and digital_count > 0:
        return "dvr"  # Hybrid DVR / XVR

    # Default fallback
    return "ipc" if (analog_count + digital_count) <= 1 else "nvr"


# =============================================================================
# Capability and Fallback Path Builders (Explicit, No Fake SDK Connections)
# =============================================================================

def build_hikvision_rtsp_paths(
    channel: int = 1,
    stream: str = "main",
    vendor: str = "hikvision",
) -> Dict[str, Any]:
    """
    Generate explicit RTSP URL paths for Hikvision and Ezviz cameras.

    Hikvision convention:
    - Primary standard: Streaming/Channels/{track_chan}
      where track_chan = channel * 100 + (1 for main, 2 for sub). E.g. 101, 102, 201, 202.
    - Alternative legacy: h264/ch{channel}/{stream}/av_stream.
    """
    chan = max(1, int(channel or 1))
    canonical_stream = "main" if str(stream).strip().lower() in {"main", "record"} else "sub"
    stream_idx = 1 if canonical_stream == "main" else 2
    track_chan = chan * 100 + stream_idx

    primary = f"Streaming/Channels/{track_chan}"
    legacy = f"h264/ch{chan}/{canonical_stream}/av_stream"

    return {
        "channel": chan,
        "stream": canonical_stream,
        "track_chan": track_chan,
        "primary_path": primary,
        "legacy_path": legacy,
        "candidate_paths": [primary, legacy],
    }


def build_hikvision_rtsp_url(
    host: str,
    port: int = DEFAULT_HIKVISION_RTSP_PORT,
    username: str = "",
    password: str = "",
    channel: int = 1,
    stream: str = "main",
    vendor: str = "hikvision",
) -> str:
    """
    Construct a complete RTSP streaming URL safely.
    Credentials are URL-encoded if present; never exposed or logged here.
    """
    resolved_host = str(host or "").strip()
    resolved_port = int(port or DEFAULT_HIKVISION_RTSP_PORT)
    path_info = build_hikvision_rtsp_paths(channel=channel, stream=stream, vendor=vendor)
    primary_path = path_info["primary_path"]

    if username or password:
        user_enc = quote(str(username or ""), safe="")
        pass_enc = quote(str(password or ""), safe="")
        return f"rtsp://{user_enc}:{pass_enc}@{resolved_host}:{resolved_port}/{primary_path}"
    return f"rtsp://{resolved_host}:{resolved_port}/{primary_path}"


def build_hikvision_isapi_endpoints(
    host: str,
    port: int = DEFAULT_HIKVISION_HTTP_PORT,
    use_https: bool = False,
) -> Dict[str, str]:
    """
    Construct standard ISAPI REST endpoint URLs for HTTP/HTTPS access.
    """
    proto = "https" if use_https else "http"
    base = f"{proto}://{str(host).strip()}:{int(port)}"
    return {
        "device_info": f"{base}/ISAPI/System/deviceInfo",
        "channels": f"{base}/ISAPI/System/Video/inputs/channels",
        "streaming_channels": f"{base}/ISAPI/Streaming/channels",
        "search": f"{base}/ISAPI/ContentMgmt/search",
        "download": f"{base}/ISAPI/ContentMgmt/download",
    }


def get_hikvision_capabilities(
    vendor: str = "hikvision",
    base_dir: Optional[str] = None,
) -> HikvisionCapabilities:
    """
    Inspect environment and return explicit capabilities.
    Does NOT claim SDK operation if HCNetSDK.dll is absent.
    """
    sdk_found = hcnetsdk_available(base_dir)
    sdk_path = find_hcnetsdk_dir(base_dir) if sdk_found else None

    norm_vendor = "ezviz" if str(vendor).strip().lower() in {"ezviz", "ezviz_camera"} else "hikvision"

    return HikvisionCapabilities(
        vendor=norm_vendor,
        sdk_available=sdk_found,
        sdk_binary_path=sdk_path,
        supports_sdk_login=sdk_found,
        supports_isapi=True,  # HTTP ISAPI standard across Hikvision family
        supports_rtsp=True,   # RTSP standard across Hikvision and Ezviz
        default_sdk_port=DEFAULT_HIKVISION_SDK_PORT,
        default_http_port=DEFAULT_HIKVISION_HTTP_PORT,
        default_rtsp_port=DEFAULT_HIKVISION_RTSP_PORT,
        rtsp_presets={
            "main_track": "Streaming/Channels/101",
            "sub_track": "Streaming/Channels/102",
            "main_legacy": "h264/ch1/main/av_stream",
            "sub_legacy": "h264/ch1/sub/av_stream",
        },
        isapi_endpoints={
            "device_info": "/ISAPI/System/deviceInfo",
            "channels": "/ISAPI/System/Video/inputs/channels",
            "search": "/ISAPI/ContentMgmt/search",
            "download": "/ISAPI/ContentMgmt/download",
        },
    )


# =============================================================================
# Hikvision HCNetSDK Adapter Class
# =============================================================================

class HikvisionAdapter:
    """
    Thread-safe per-device adapter wrapping Hikvision HCNetSDK.

    Supports:
    - Lazy DLL loading on Windows.
    - Login and safe session termination.
    - Autonomous channel inventory (IPC vs NVR/DVR without user switch).
    - Device channel capability querying.
    - Explicit ISAPI / RTSP fallbacks when native SDK is unavailable.
    - Zero credential leakage in repr or logs.
    """

    def __init__(
        self,
        host: str,
        username: str = "admin",
        password: str = "",
        port: int = DEFAULT_HIKVISION_SDK_PORT,
        vendor: str = "hikvision",
        base_dir: Optional[str] = None,
        sdk_dir: Optional[str] = None,
    ) -> None:
        self._lock = threading.RLock()
        self.host = str(host or "").strip()
        self.username = str(username or "")
        self._password = str(password if password is not None else "")
        self.port = int(port or DEFAULT_HIKVISION_SDK_PORT)
        self.vendor = "ezviz" if str(vendor).strip().lower() in {"ezviz", "ezviz_camera"} else "hikvision"
        self.base_dir = base_dir
        self.sdk_dir = sdk_dir

        if not 1 <= self.port <= 65535:
            raise HikvisionError(f"Cổng cổng kết nối không hợp lệ: {self.port} (phạm vi 1-65535).")
        if not self.host:
            raise HikvisionConnectionError("Địa chỉ IP/host của thiết bị không được để trống.")

        self._login_handle: int = -1
        self._device_info: Optional[HikvisionDeviceInfo] = None

    def __repr__(self) -> str:
        # Crucial security guarantee: never format or expose password
        return (
            f"HikvisionAdapter(host={self.host!r}, port={self.port}, "
            f"user={self.username!r}, vendor={self.vendor!r}, connected={self.is_connected})"
        )

    def __enter__(self) -> HikvisionAdapter:
        self.connect()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.disconnect()

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._login_handle >= 0

    @classmethod
    def from_camera(cls, camera: Dict[str, Any], base_dir: Optional[str] = None) -> HikvisionAdapter:
        """Create adapter from standard camera configuration dictionary."""
        nested = camera.get("nvr") if isinstance(camera.get("nvr"), dict) else {}
        host = camera.get("host") or camera.get("ip") or nested.get("host") or ""
        username = camera.get("user") or camera.get("username") or nested.get("username") or "admin"
        password = camera.get("pass") if camera.get("pass") is not None else camera.get("password", nested.get("password", ""))
        port = (
            camera.get("netsdk_port")
            or camera.get("sdk_port")
            or camera.get("port")
            or nested.get("sdk_port")
            or DEFAULT_HIKVISION_SDK_PORT
        )
        vendor = camera.get("vendor") or camera.get("nvr_vendor") or nested.get("vendor") or "hikvision"

        return cls(
            host=str(host),
            username=str(username),
            password=str(password),
            port=int(port),
            vendor=str(vendor),
            base_dir=base_dir,
        )

    def connect(self) -> HikvisionDeviceInfo:
        """
        Establish authenticated session with the device using HCNetSDK.
        Raises HikvisionSDKNotFoundError if DLL is absent.
        Raises HikvisionAuthError on authentication rejection.
        Raises HikvisionConnectionError on network timeout or connection failure.
        """
        with self._lock:
            if self.is_connected and self._device_info is not None:
                return self._device_info

            # Load runtime DLL; raises HikvisionSDKNotFoundError if missing
            dll = _load_runtime(self.sdk_dir or self.base_dir)

            resolved_ip = _resolve_host(self.host)
            ip_bytes = _bounded_ascii(resolved_ip, "Địa chỉ IP", 128)
            user_bytes = _bounded_ascii(self.username, "Tên đăng nhập", 63)
            pass_bytes = _bounded_ascii(self._password, "Mật khẩu", 63)

            handle = -1
            v30_info = NET_DVR_DEVICEINFO_V30()

            # Attempt modern login NET_DVR_Login_V40 if supported
            if hasattr(dll, "NET_DVR_Login_V40"):
                login_info = NET_DVR_USER_LOGIN_INFO()
                C.memset(C.byref(login_info), 0, C.sizeof(NET_DVR_USER_LOGIN_INFO))
                login_info.sDeviceAddress = ip_bytes
                login_info.wPort = WORD(self.port)
                login_info.szUserName = user_bytes
                login_info.szPassword = pass_bytes
                login_info.bUseAsynLogin = BOOL(0)  # Synchronous login
                login_info.byLoginMode = BYTE(0)    # Private protocol port 8000

                v40_out = NET_DVR_DEVICEINFO_V40()
                C.memset(C.byref(v40_out), 0, C.sizeof(NET_DVR_DEVICEINFO_V40))

                raw_handle = dll.NET_DVR_Login_V40(C.byref(login_info), C.byref(v40_out))
                if raw_handle >= 0:
                    handle = int(raw_handle)
                    v30_info = v40_out.struDeviceV30

            # Fallback to universally supported NET_DVR_Login_V30
            if handle < 0 and hasattr(dll, "NET_DVR_Login_V30"):
                raw_handle = dll.NET_DVR_Login_V30(
                    ip_bytes,
                    WORD(self.port),
                    user_bytes,
                    pass_bytes,
                    C.byref(v30_info),
                )
                if raw_handle >= 0:
                    handle = int(raw_handle)

            if handle < 0:
                err_code, err_desc = _sdk_error(dll)
                if err_code in {1, 29}:
                    raise HikvisionAuthError(f"Đăng nhập Hikvision thất bại: {err_desc} (code {err_code}).")
                if err_code in {7, 8, 9, 10, 14}:
                    raise HikvisionConnectionError(
                        f"Không kết nối được tới thiết bị Hikvision tại {self.host}:{self.port} - {err_desc} (code {err_code})."
                    )
                raise HikvisionDeviceError(f"Lỗi kết nối SDK Hikvision: {err_desc} (code {err_code}).")

            self._login_handle = handle

            # Extract hardware details
            serial_raw = bytes(v30_info.sSerialNumber).split(b"\x00", 1)[0]
            serial_str = serial_raw.decode("ascii", errors="replace").strip()

            analog_count = int(v30_info.byChanNum)
            start_analog = int(v30_info.byStartChan)
            ip_low = int(v30_info.byIPChanNum)
            ip_high = int(v30_info.byHighDChanNum)
            digital_count = ip_low + (ip_high << 8)
            start_digital = int(v30_info.byStartDChan)
            dvr_type = int(v30_info.byDVRType)

            total_channels = analog_count + digital_count
            if total_channels == 0:
                total_channels = 1  # Standard single-lens IP camera default

            # Autonomous device kind detection without user switch
            kind = detect_device_kind(
                analog_count=analog_count,
                digital_count=digital_count,
                dvr_type=dvr_type,
                model=serial_str,
            )

            device_id = serial_str if serial_str else f"hik_{self.host}_{self.port}"

            self._device_info = HikvisionDeviceInfo(
                device_id=device_id,
                vendor=self.vendor,
                model=serial_str or "Hikvision Device",
                serial_number=serial_str,
                device_kind=kind,
                analog_channels=analog_count,
                digital_channels=digital_count,
                total_channels=total_channels,
                start_analog_channel=start_analog if start_analog else 1,
                start_digital_channel=start_digital if start_digital else 33,
                dvr_type=dvr_type,
                raw_details={
                    "alarm_in": int(v30_info.byAlarmInPortNum),
                    "alarm_out": int(v30_info.byAlarmOutPortNum),
                    "disk_num": int(v30_info.byDiskNum),
                    "audio_chan": int(v30_info.byAudioChanNum),
                },
            )
            return self._device_info

    def disconnect(self) -> bool:
        """
        Safely log out and release session handle.
        Guaranteed idempotent; safe to call repeatedly.
        """
        with self._lock:
            if self._login_handle < 0:
                return True

            handle = self._login_handle
            self._login_handle = -1
            self._device_info = None

            if _RUNTIME_DLL is not None and _RUNTIME_INITIALIZED:
                try:
                    if hasattr(_RUNTIME_DLL, "NET_DVR_Logout"):
                        _RUNTIME_DLL.NET_DVR_Logout(LONG(handle))
                    elif hasattr(_RUNTIME_DLL, "NET_DVR_Logout_V30"):
                        _RUNTIME_DLL.NET_DVR_Logout_V30(LONG(handle))
                except Exception:
                    pass
            return True

    def get_device_info(self) -> HikvisionDeviceInfo:
        """Return cached device info or connect to retrieve it."""
        with self._lock:
            if self._device_info is not None:
                return self._device_info
            return self.connect()

    def get_digital_channel_states(self) -> Dict[int, int]:
        """Return digital-channel status codes keyed by 1-based digital slot.

        Command 6126 is optional on older devices. Failure is treated as
        "status unknown" rather than inventing online/offline state.
        """
        with self._lock:
            self.get_device_info()
            dll = _load_runtime(self.sdk_dir or self.base_dir)
            if not hasattr(dll, "NET_DVR_GetDVRConfig"):
                return {}

            state = NET_DVR_DIGITAL_CHANNEL_STATE()
            C.memset(C.byref(state), 0, C.sizeof(state))
            state.dwSize = C.sizeof(state)
            returned = DWORD(0)
            ok = dll.NET_DVR_GetDVRConfig(
                LONG(self._login_handle),
                DWORD(NET_DVR_GET_DIGITAL_CHANNEL_STATE),
                LONG(-1),
                C.byref(state),
                DWORD(C.sizeof(state)),
                C.byref(returned),
            )
            if not ok:
                return {}

            values = list(state.byDigitalChanState) + list(state.byDigitalChanStateEx)
            return {idx + 1: int(code) for idx, code in enumerate(values)}

    @staticmethod
    def _status_fields(status_code: Optional[int]) -> Tuple[Optional[bool], str]:
        if status_code is None:
            return None, "unknown"
        return status_code == 1, HIKVISION_DIGITAL_CHANNEL_STATUS.get(
            int(status_code), f"status_{int(status_code)}"
        )

    def get_channel_inventory(self) -> List[HikvisionChannel]:
        """Enumerate physical channels using HCNetSDK channel numbers.

        Digital slots use command 6126 when supported, so unconfigured slots
        are not exposed as cameras and configured-but-offline channels remain
        visible with an explicit offline/error status.
        """
        with self._lock:
            info = self.get_device_info()
            channels: List[HikvisionChannel] = []
            digital_states = self.get_digital_channel_states() if info.digital_channels else {}

            if info.device_kind == "ipc":
                sdk_chan = info.start_analog_channel or info.start_digital_channel or 1
                rtsp_paths = build_hikvision_rtsp_paths(channel=1, vendor=self.vendor)
                channels.append(
                    HikvisionChannel(
                        channel_id=1,
                        channel_number=1,
                        name="Camera 1",
                        is_ip_camera=True,
                        is_online=True,
                        supports_main_stream=True,
                        supports_sub_stream=True,
                        rtsp_main_path=rtsp_paths["primary_path"],
                        rtsp_sub_path=build_hikvision_rtsp_paths(
                            channel=1, stream="sub", vendor=self.vendor
                        )["primary_path"],
                        isapi_channel_id=101,
                        sdk_channel_number=sdk_chan,
                        status_code=1,
                        status_text="connected",
                    )
                )
                return channels

            seq = 1
            analog_start = info.start_analog_channel or 1
            for a_idx in range(info.analog_channels):
                sdk_chan = analog_start + a_idx
                track_main = seq * 100 + 1
                channels.append(
                    HikvisionChannel(
                        channel_id=seq,
                        channel_number=seq,
                        name=f"Camera Analog {seq:02d}",
                        is_ip_camera=False,
                        is_online=None,
                        supports_main_stream=True,
                        supports_sub_stream=True,
                        rtsp_main_path=f"Streaming/Channels/{track_main}",
                        rtsp_sub_path=f"Streaming/Channels/{seq * 100 + 2}",
                        isapi_channel_id=track_main,
                        sdk_channel_number=sdk_chan,
                        status_code=None,
                        status_text="unknown",
                    )
                )
                seq += 1

            digital_count = min(max(0, info.digital_channels), MAX_DIGITAL_CHANNELS)
            digital_start = info.start_digital_channel or 33
            for d_idx in range(digital_count):
                status_code = digital_states.get(d_idx + 1)
                # 0 = invalid slot, 11 = no IPC attached. When state query is
                # unsupported, retain the slot but mark status unknown.
                if digital_states and status_code in {0, 11}:
                    continue
                online, status_text = self._status_fields(status_code)
                sdk_chan = digital_start + d_idx
                track_main = seq * 100 + 1
                channels.append(
                    HikvisionChannel(
                        channel_id=seq,
                        channel_number=seq,
                        name=f"Camera IP {d_idx + 1:02d}",
                        is_ip_camera=True,
                        is_online=online,
                        supports_main_stream=True,
                        supports_sub_stream=True,
                        rtsp_main_path=f"Streaming/Channels/{track_main}",
                        rtsp_sub_path=f"Streaming/Channels/{seq * 100 + 2}",
                        isapi_channel_id=track_main,
                        sdk_channel_number=sdk_chan,
                        status_code=status_code,
                        status_text=status_text,
                    )
                )
                seq += 1

            # Some NVRs report channel counts but omit state support. Preserve
            # honest "unknown" channels instead of claiming they are online.
            if not channels and info.total_channels:
                count = min(max(1, info.total_channels), MAX_DIGITAL_CHANNELS)
                for idx in range(1, count + 1):
                    channels.append(
                        HikvisionChannel(
                            channel_id=idx,
                            channel_number=idx,
                            name=f"Camera {idx:02d}",
                            is_ip_camera=True,
                            is_online=None,
                            supports_main_stream=True,
                            supports_sub_stream=True,
                            rtsp_main_path=f"Streaming/Channels/{idx * 100 + 1}",
                            rtsp_sub_path=f"Streaming/Channels/{idx * 100 + 2}",
                            isapi_channel_id=idx * 100 + 1,
                            sdk_channel_number=(info.start_digital_channel or 1) + idx - 1,
                            status_code=None,
                            status_text="unknown",
                        )
                    )
            return channels

    def get_core_channels(self) -> List[Any]:
        """Return list of ChannelInfo instances directly compatible with CameraManager."""
        return [ch.to_core_channel_info() for ch in self.get_channel_inventory()]

    def get_device_config(self, protocol: str = "hikvision_sdk") -> Any:
        """Create DeviceConfig instance for CameraManager registration."""
        dev_info = self.get_device_info()
        if DeviceConfig is not None:
            return DeviceConfig(
                device_id=dev_info.device_id,
                vendor=self.vendor,
                host=self.host,
                port=self.port,
            )
        return {
            "device_id": dev_info.device_id,
            "protocol": protocol,
            "host": self.host,
            "port": self.port,
        }

    def get_rtsp_candidates(self, channel: int = 1, stream: str = "main") -> List[Dict[str, str]]:
        """
        Return ordered RTSP fallback paths for a specific channel.
        Safe for use without an active SDK connection.
        """
        info = build_hikvision_rtsp_paths(channel=channel, stream=stream, vendor=self.vendor)
        return [
            {"profile": "hikvision_streaming", "path": info["primary_path"]},
            {"profile": "hikvision_legacy", "path": info["legacy_path"]},
        ]

    def get_capabilities(self) -> HikvisionCapabilities:
        """Inspect and return capability matrix for this adapter and host."""
        return get_hikvision_capabilities(self.vendor, self.base_dir)


    def _start_preview(self, sdk_channel_number: int, stream: str = "main") -> Tuple[Any, int]:
        """Open one HCNetSDK RealPlay session without a display window."""
        self.connect()
        dll = _load_runtime(self.sdk_dir or self.base_dir)
        if not hasattr(dll, "NET_DVR_RealPlay_V40"):
            raise HikvisionDeviceError("HCNetSDK hiện tại không có NET_DVR_RealPlay_V40.")

        channel = int(sdk_channel_number)
        if channel < 1:
            raise HikvisionChannelError("Số kênh SDK Hikvision phải từ 1 trở lên.")

        preview = NET_DVR_PREVIEWINFO()
        C.memset(C.byref(preview), 0, C.sizeof(preview))
        preview.lChannel = LONG(channel)
        preview.dwStreamType = DWORD(
            1 if str(stream or "main").strip().lower() == "sub" else 0
        )
        preview.dwLinkMode = DWORD(0)  # TCP
        preview.hPlayWnd = None
        preview.bBlocked = BOOL(1)

        handle = int(
            dll.NET_DVR_RealPlay_V40(
                LONG(self._login_handle),
                C.byref(preview),
                None,
                None,
            )
        )
        if handle < 0:
            code, desc = _sdk_error(dll)
            raise HikvisionChannelError(
                f"Không mở được luồng HCNetSDK kênh {channel}: {desc} (code {code})."
            )
        return dll, handle

    def capture_jpeg(
        self,
        sdk_channel_number: int,
        timeout: float = 12.0,
    ) -> bytes:
        """Capture one JPEG directly from the device through HCNetSDK."""
        del timeout  # HCNetSDK owns the request timeout configured by the runtime.
        self.connect()
        dll = _load_runtime(self.sdk_dir or self.base_dir)
        if not hasattr(dll, "NET_DVR_CaptureJPEGPicture"):
            raise HikvisionDeviceError("HCNetSDK hiện tại không hỗ trợ chụp JPEG trực tiếp.")

        channel = int(sdk_channel_number)
        if channel < 1:
            raise HikvisionChannelError("Số kênh SDK Hikvision phải từ 1 trở lên.")

        jpeg = NET_DVR_JPEGPARA()
        jpeg.wPicSize = WORD(0xFF)   # Auto/current stream resolution
        jpeg.wPicQuality = WORD(1)   # Better quality

        fd, temp_path = tempfile.mkstemp(prefix="cambida_hik_", suffix=".jpg")
        os.close(fd)
        try:
            ok = dll.NET_DVR_CaptureJPEGPicture(
                LONG(self._login_handle),
                LONG(channel),
                C.byref(jpeg),
                os.fsencode(os.path.abspath(temp_path)),
            )
            if not ok:
                code, desc = _sdk_error(dll)
                raise HikvisionChannelError(
                    f"Không chụp được JPEG HCNetSDK kênh {channel}: {desc} (code {code})."
                )
            with open(temp_path, "rb") as handle:
                data = handle.read()
            if len(data) < 4 or not data.startswith(b"\xff\xd8"):
                raise HikvisionDeviceError("HCNetSDK trả về ảnh JPEG không hợp lệ.")
            return data
        finally:
            try:
                os.remove(temp_path)
            except OSError:
                pass

    def iter_jpeg_frames(
        self,
        sdk_channel_number: int,
        fps: float = 5.0,
        frame_timeout: float = 12.0,
    ):
        """Yield direct-SDK JPEG snapshots as an MJPEG-compatible preview source."""
        interval = 1.0 / max(0.5, min(float(fps or 5.0), 8.0))
        try:
            self.connect()
            while True:
                started = time.monotonic()
                yield self.capture_jpeg(sdk_channel_number, timeout=frame_timeout)
                remaining = interval - (time.monotonic() - started)
                if remaining > 0:
                    time.sleep(remaining)
        finally:
            self.disconnect()

    def record_segment(
        self,
        sdk_channel_number: int,
        output_path: str,
        duration: float,
        ffmpeg_path: str,
        stop_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        """Record through HCNetSDK RealPlay/SaveRealData and remux PS to MP4."""
        if not ffmpeg_path or not os.path.isfile(ffmpeg_path):
            raise HikvisionDeviceError("Không tìm thấy ffmpeg.exe để đóng gói video HCNetSDK thành MP4.")

        output_abs = os.path.abspath(output_path)
        os.makedirs(os.path.dirname(output_abs) or ".", exist_ok=True)
        ps_path = output_abs + ".hik.ps"
        try:
            os.remove(ps_path)
        except OSError:
            pass

        dll = None
        real_handle = -1
        saving = False
        try:
            dll, real_handle = self._start_preview(sdk_channel_number, stream="main")
            if not hasattr(dll, "NET_DVR_SaveRealData_V30") or not hasattr(
                dll, "NET_DVR_StopSaveRealData"
            ):
                raise HikvisionDeviceError(
                    "HCNetSDK hiện tại không hỗ trợ SaveRealData_V30."
                )

            if not dll.NET_DVR_SaveRealData_V30(
                LONG(real_handle),
                DWORD(STREAM_PS),
                os.fsencode(ps_path),
            ):
                code, desc = _sdk_error(dll)
                raise HikvisionDeviceError(
                    f"HCNetSDK không bắt đầu ghi kênh {sdk_channel_number}: {desc} (code {code})."
                )
            saving = True

            deadline = time.monotonic() + max(0.1, float(duration))
            while time.monotonic() < deadline:
                if stop_event is not None and stop_event.is_set():
                    break
                time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
        finally:
            if dll is not None and saving:
                try:
                    dll.NET_DVR_StopSaveRealData(LONG(real_handle))
                except Exception:
                    pass
            if dll is not None and real_handle >= 0 and hasattr(dll, "NET_DVR_StopRealPlay"):
                try:
                    dll.NET_DVR_StopRealPlay(LONG(real_handle))
                except Exception:
                    pass

        if stop_event is not None and stop_event.is_set():
            try:
                os.remove(ps_path)
            except OSError:
                pass
            return {"ok": False, "stopped": True, "output_path": output_abs}

        if not os.path.isfile(ps_path) or os.path.getsize(ps_path) <= 0:
            raise HikvisionDeviceError("HCNetSDK không tạo được dữ liệu video PS hợp lệ.")

        cmd = [
            ffmpeg_path,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            ps_path,
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            output_abs,
        ]
        completed = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=max(30.0, float(duration) + 30.0),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            os.remove(ps_path)
        except OSError:
            pass

        if completed.returncode != 0 or not os.path.isfile(output_abs) or os.path.getsize(output_abs) <= 0:
            err = completed.stderr.decode("utf-8", errors="replace")[-600:]
            raise HikvisionDeviceError(
                "FFmpeg không đóng gói được video HCNetSDK thành MP4"
                + (f": {err}" if err else ".")
            )
        return {
            "ok": True,
            "output_path": output_abs,
            "transport": "hcnetsdk",
            "sdk_channel_number": int(sdk_channel_number),
        }



# =============================================================================
# High-Level Factory & Probing (Explicit Capability Diagnostics)
# =============================================================================

def probe_hikvision_device(
    host: str,
    username: str = "admin",
    password: str = "",
    port: int = DEFAULT_HIKVISION_SDK_PORT,
    vendor: str = "hikvision",
    base_dir: Optional[str] = None,
) -> ProbeResult:
    """
    Attempt HCNetSDK connection diagnostic if binaries are present.
    If binaries are absent, reports explicit ISAPI/RTSP fallback capability
    WITHOUT fabricating a fake SDK connection.
    """
    if not hcnetsdk_available(base_dir):
        # Explicit fallback reporting: do not fake SDK connection
        return ProbeResult(
            ok=False,
            transport="none",
            device_kind="unknown",
            channels=[],
            message=(
                "HCNetSDK.dll không có sẵn trong hệ thống. "
                "Cần sử dụng tuyến truyền dẫn trực tiếp RTSP (cổng 554) hoặc ISAPI (cổng 80/443)."
            ),
            error_code=None,
        )

    adapter = HikvisionAdapter(
        host=host,
        username=username,
        password=password,
        port=port,
        vendor=vendor,
        base_dir=base_dir,
    )
    try:
        with adapter:
            dev_info = adapter.get_device_info()
            channels = adapter.get_channel_inventory()
            return ProbeResult(
                ok=True,
                transport="hcnetsdk",
                device_kind=dev_info.device_kind,
                channels=channels,
                message=f"Kết nối Hikvision HCNetSDK thành công: {dev_info.model} ({dev_info.device_kind.upper()}).",
            )
    except HikvisionError as exc:
        return ProbeResult(
            ok=False,
            transport="hcnetsdk",
            device_kind="unknown",
            channels=[],
            message=str(exc),
        )


__all__ = [
    # Classes & Dataclasses
    "HikvisionAdapter",
    "HikvisionDeviceInfo",
    "HikvisionChannel",
    "HikvisionCapabilities",
    "ProbeResult",
    "SADP_DEVICE_INFO",
    # Exceptions
    "HikvisionError",
    "HikvisionSDKNotFoundError",
    "HikvisionAuthError",
    "HikvisionConnectionError",
    "HikvisionDeviceError",
    "HikvisionChannelError",
    # Functions
    "find_hcnetsdk_dir",
    "hcnetsdk_available",
    "find_sadp_dir",
    "sadp_available",
    "discover_hikvision_sadp",
    "is_hikvision_compatible",
    "detect_device_kind",
    "build_hikvision_rtsp_paths",
    "build_hikvision_rtsp_url",
    "build_hikvision_isapi_endpoints",
    "get_hikvision_capabilities",
    "probe_hikvision_device",
    # Constants
    "DEFAULT_HIKVISION_SDK_PORT",
    "DEFAULT_HIKVISION_HTTP_PORT",
    "DEFAULT_HIKVISION_HTTPS_PORT",
    "DEFAULT_HIKVISION_RTSP_PORT",
    "SUPPORTED_VENDORS",
    "HIKVISION_SDK_ERRORS",
]
