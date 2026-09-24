"""
Standalone RTSP Camera Module for Cambida.

Provides RTSP URL generation, presets for Hikvision/Ezviz, Dahua/Imou, KBVision,
and custom streams, credential sanitization, RTSP socket-level probing, FFmpeg
media verification, live JPEG streaming, and segment recording.

Follows .project/CAMERA_MODULE_CONTRACT_20260924.md:
- Separate IDs for devices, physical channels, and table bindings.
- All ports editable (default RTSP 554).
- Presets: Hikvision, Dahua, KBVision, custom, ONVIF.
- Never exposes credentials in logs, repr, or error messages.
- Legacy camera configuration compatibility.
- Capabilities strictly reported; unsupported operations not claimed.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Generator, List, Optional, Tuple, Union
from urllib.parse import quote, unquote, urlparse, urlunparse

try:
    from camera_modules.core import (
        BaseCameraAdapter,
        CameraCapability,
        ChannelInfo,
        DeviceConfig,
        DeviceInfo,
        ProbeResult as CoreProbeResult,
        StreamEndpoint,
        StreamType,
        TransportProtocol,
        VendorType,
        get_adapter_registry,
    )
except ImportError:
    BaseCameraAdapter = object  # type: ignore[misc,assignment]
    CameraCapability = None  # type: ignore[misc,assignment]
    ChannelInfo = None  # type: ignore[misc,assignment]
    DeviceConfig = None  # type: ignore[misc,assignment]
    DeviceInfo = None  # type: ignore[misc,assignment]
    CoreProbeResult = None  # type: ignore[misc,assignment]
    StreamEndpoint = None  # type: ignore[misc,assignment]
    StreamType = None  # type: ignore[misc,assignment]
    TransportProtocol = None  # type: ignore[misc,assignment]
    VendorType = None  # type: ignore[misc,assignment]
    get_adapter_registry = None  # type: ignore[misc,assignment]


DEFAULT_RTSP_PORT = 554

PRESET_HIKVISION = "hikvision"
PRESET_EZVIZ = "ezviz"
PRESET_DAHUA = "dahua"
PRESET_IMOU = "imou"
PRESET_KBVISION = "kbvision"
PRESET_CUSTOM = "custom"
PRESET_ONVIF = "onvif"

SUPPORTED_PRESETS = (
    PRESET_HIKVISION,
    PRESET_EZVIZ,
    PRESET_DAHUA,
    PRESET_IMOU,
    PRESET_KBVISION,
    PRESET_CUSTOM,
    PRESET_ONVIF,
)


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of probing a camera or stream."""

    ok: bool
    media_ok: bool
    channels: int = 1
    sdk_error: Optional[Union[int, str]] = None
    message: str = ""
    profile: Optional[str] = None
    transport: str = "rtsp"
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RTSPCandidate:
    """Represents an RTSP URL candidate for connection or testing."""

    profile: str
    url: str
    safe_url: str
    stream: str
    vendor: str = ""


# ---------------------------------------------------------------------------
# Credential Protection & Sanitization
# ---------------------------------------------------------------------------


def sanitize_rtsp_url(url: Optional[str], replacement: str = "***:***") -> str:
    """
    Remove username and password from an RTSP URL.

    Example:
        rtsp://admin:secret@192.168.1.10:554/live -> rtsp://***:***@192.168.1.10:554/live
    """
    if not url:
        return ""
    text = str(url)
    return re.sub(r"(?i)\b(rtsps?://)([^/\s@]+)@", rf"\1{replacement}@", text)


def sanitize_error_text(
    error_text: Any,
    secrets: Optional[List[str]] = None,
) -> str:
    """
    Sanitize error message to ensure no passwords or credentials leak.
    """
    if isinstance(error_text, bytes):
        text = error_text.decode("utf-8", errors="replace")
    else:
        text = str(error_text or "")

    # Sanitize standard rtsp / http URLs
    text = re.sub(r"(?i)\b(rtsps?://)([^/\s@]+)@", r"\1***:***@", text)
    text = re.sub(r"(?i)\b(https?://)([^/\s@]+)@", r"\1***:***@", text)

    # Sanitize explicit secrets passed in
    if secrets:
        for secret in secrets:
            if not secret or len(str(secret).strip()) == 0:
                continue
            sec_str = str(secret)
            text = text.replace(sec_str, "***")
            encoded = quote(sec_str, safe="")
            if encoded != sec_str:
                text = text.replace(encoded, "***")

    return text


def classify_rtsp_error(error_text: str) -> str:
    """Categorize RTSP failure based on error strings or status codes."""
    raw = sanitize_error_text(error_text).lower()
    if any(
        kw in raw
        for kw in (
            "401",
            "unauthorized",
            "authentication failed",
            "invalid credentials",
            "access denied",
            "forbidden",
        )
    ):
        return "auth"
    if any(
        kw in raw
        for kw in (
            "connection reset",
            "forcibly closed by the remote host",
            "error number -10054",
            "winerror 10054",
            "wsaeconnreset",
            "broken pipe",
        )
    ):
        return "reset"
    if any(
        kw in raw
        for kw in (
            "connection refused",
            "failed to connect",
            "timed out",
            "timeout",
            "network is unreachable",
            "no route",
            "could not resolve host",
            "name or service not known",
            "unreachable",
        )
    ):
        return "network"
    if any(
        kw in raw
        for kw in (
            "404",
            "not found",
            "invalid data found",
            "method not allowed",
            "no such file",
            "server returned 400",
            "unsupported protocol",
        )
    ):
        return "path"
    return "stream"


def rtsp_failure_message(kind: str, vendor: Optional[str] = None) -> str:
    """User-friendly Vietnamese failure message matching project guidelines."""
    vendor_lower = str(vendor or "").strip().lower()
    if kind == "auth":
        if vendor_lower in {"imou", "ezviz"}:
            return (
                "Kết nối thất bại: sai tài khoản hoặc mật khẩu. "
                "Với Imou/Ezviz, thường dùng admin + Safety Code (Mã an toàn / Verification Code) "
                "in trên nhãn thiết bị, không phải mật khẩu tài khoản app di động."
            )
        return "Kết nối thất bại: sai tài khoản hoặc mật khẩu camera."
    if kind == "reset":
        return (
            "Kết nối thất bại: camera hoặc router đã ngắt/reset phiên RTSP. "
            "Kiểm tra port RTSP (mặc định 554), giới hạn số lượng kết nối và firewall/NAT."
        )
    if kind == "network":
        return "Kết nối thất bại: không truy cập được IP/port camera trên mạng LAN."
    if kind == "path":
        return "Kết nối thất bại: sai đường dẫn luồng RTSP hoặc camera không hỗ trợ kênh này."
    return "Kết nối thất bại: camera không phản hồi hoặc không trả về luồng video hợp lệ."


# ---------------------------------------------------------------------------
# RTSP URL and Preset Builders
# ---------------------------------------------------------------------------


def normalize_channel(channel: Union[int, str, None]) -> int:
    """Normalize channel to a positive integer (>= 1)."""
    if channel is None or channel == "":
        return 1
    try:
        val = int(channel)
        return max(1, val)
    except (ValueError, TypeError):
        return 1


def build_rtsp_path(
    preset: str,
    channel: Union[int, str] = 1,
    stream: str = "main",
    custom_path: Optional[str] = None,
) -> str:
    """
    Generate the RTSP path portion for a given preset, channel, and stream.

    Presets:
    - hikvision / ezviz:
        main: Streaming/Channels/{ch}01
        sub:  Streaming/Channels/{ch}02
    - dahua / imou:
        main: cam/realmonitor?channel={ch}&subtype=0
        sub:  cam/realmonitor?channel={ch}&subtype=1
    - kbvision:
        main: cam/realmonitor?channel={ch}&subtype=0
        sub:  cam/realmonitor?channel={ch}&subtype=1
    - onvif:
        main: cam/realmonitor?channel={ch}&subtype=0&unicast=true&proto=Onvif
        sub:  cam/realmonitor?channel={ch}&subtype=1&unicast=true&proto=Onvif
    - custom:
        uses custom_path or legacy default
    """
    ch = normalize_channel(channel)
    is_main = str(stream).strip().lower() in {"main", "record", "0"}
    preset_lower = str(preset or PRESET_CUSTOM).strip().lower()

    if preset_lower in {PRESET_HIKVISION, PRESET_EZVIZ}:
        track = ch * 100 + (1 if is_main else 2)
        return f"Streaming/Channels/{track}"

    if preset_lower in {PRESET_DAHUA, PRESET_IMOU}:
        subtype = 0 if is_main else 1
        return f"cam/realmonitor?channel={ch}&subtype={subtype}"

    if preset_lower == PRESET_KBVISION:
        subtype = 0 if is_main else 1
        return f"cam/realmonitor?channel={ch}&subtype={subtype}"

    if preset_lower == PRESET_ONVIF:
        subtype = 0 if is_main else 1
        return f"cam/realmonitor?channel={ch}&subtype={subtype}&unicast=true&proto=Onvif"

    # Custom or fallback
    if custom_path:
        path = str(custom_path).strip().lstrip("/")
        # If template contains {channel} or {stream}
        path = path.replace("{channel}", str(ch))
        path = path.replace("{stream}", "main" if is_main else "sub")
        return path

    # Legacy default fallback
    stream_name = "main" if is_main else "sub"
    return f"h264/ch{ch}/{stream_name}/av_stream"


def build_rtsp_url(
    host: str,
    port: int = DEFAULT_RTSP_PORT,
    username: str = "",
    password: str = "",
    path: str = "",
    add_timeout: bool = False,
) -> str:
    """
    Construct a complete RTSP URL with URL-encoded credentials.
    All ports are editable.
    """
    clean_host = str(host or "").strip()
    clean_port = int(port or DEFAULT_RTSP_PORT)
    clean_path = str(path or "").lstrip("/")

    if add_timeout:
        path_without_frag, frag_sep, frag = clean_path.partition("#")
        if not re.search(r"(?:[?&])timeout=", path_without_frag, re.IGNORECASE):
            sep = "&" if "?" in path_without_frag else "?"
            path_without_frag += f"{sep}timeout=20000000"
        clean_path = path_without_frag + (f"#{frag}" if frag_sep else "")

    user_enc = quote(str(username or ""), safe="")
    pass_enc = quote(str(password or ""), safe="")

    if user_enc or pass_enc:
        auth_part = f"{user_enc}:{pass_enc}@"
    else:
        auth_part = ""

    return f"rtsp://{auth_part}{clean_host}:{clean_port}/{clean_path}"


def get_rtsp_candidates(
    camera_or_config: Union[Dict[str, Any], Any],
    stream: str = "main",
    channel: Union[int, str] = 1,
    vendor: Optional[str] = None,
) -> List[RTSPCandidate]:
    """
    Generate ordered RTSP URL candidates for connection/testing without modifying config.
    Supports legacy camera dictionary or modern config objects.
    """
    if isinstance(camera_or_config, dict):
        cam = camera_or_config
        host = cam.get("ip") or cam.get("host") or ""
        port = int(cam.get("port") or cam.get("rtsp_port") or DEFAULT_RTSP_PORT)
        user = cam.get("user") or cam.get("username") or ""
        passwd = cam.get("pass") if cam.get("pass") is not None else cam.get("password", "")
        chan = cam.get("rtsp_channel") or cam.get("channel") or channel
        cand_vendor = (vendor or cam.get("vendor") or "").strip().lower()
        rec_path = cam.get("record_path")
        prev_path = cam.get("preview_path")
        direct_url = (
            cam.get(f"{stream}_rtsp_url")
            or (cam.get("record_rtsp_url") if stream == "main" else cam.get("preview_rtsp_url"))
        )
    else:
        host = getattr(camera_or_config, "host", "")
        port = getattr(camera_or_config, "port", DEFAULT_RTSP_PORT)
        user = getattr(camera_or_config, "username", "")
        passwd = getattr(camera_or_config, "password", "")
        chan = channel
        cand_vendor = (vendor or "").strip().lower()
        rec_path = None
        prev_path = None
        direct_url = None

    is_main = str(stream).strip().lower() in {"main", "record", "0"}
    custom_path = rec_path if is_main else prev_path

    candidates: List[RTSPCandidate] = []
    seen_urls = set()

    def _add(profile_name: str, preset_name: str, path_override: Optional[str] = None, add_timeout: bool = False):
        p = path_override if path_override is not None else build_rtsp_path(preset_name, chan, stream, custom_path)
        u = build_rtsp_url(host, port, user, passwd, p, add_timeout=add_timeout)
        if u not in seen_urls:
            seen_urls.add(u)
            candidates.append(
                RTSPCandidate(
                    profile=profile_name,
                    url=u,
                    safe_url=sanitize_rtsp_url(u),
                    stream="main" if is_main else "sub",
                    vendor=preset_name,
                )
            )

    # 1. Direct configured URL
    if direct_url:
        candidates.append(
            RTSPCandidate(
                profile="direct",
                url=str(direct_url),
                safe_url=sanitize_rtsp_url(str(direct_url)),
                stream="main" if is_main else "sub",
                vendor="custom",
            )
        )
        return candidates

    # 2. Configured custom path if provided
    if custom_path:
        _add("configured", PRESET_CUSTOM, path_override=str(custom_path).lstrip("/"))

    # 3. Vendor presets in order of priority based on vendor selection
    if cand_vendor in {PRESET_HIKVISION, PRESET_EZVIZ}:
        _add(PRESET_HIKVISION, PRESET_HIKVISION)
        _add(f"{PRESET_HIKVISION}_alt", PRESET_CUSTOM, path_override=f"h264/ch{normalize_channel(chan)}/{'main' if is_main else 'sub'}/av_stream")
        _add(PRESET_ONVIF, PRESET_ONVIF)
        _add(PRESET_DAHUA, PRESET_DAHUA)
    elif cand_vendor in {PRESET_DAHUA, PRESET_IMOU}:
        _add(cand_vendor or PRESET_DAHUA, PRESET_DAHUA)
        _add(f"{cand_vendor or PRESET_DAHUA}_onvif", PRESET_ONVIF)
        _add(PRESET_HIKVISION, PRESET_HIKVISION)
    elif cand_vendor == PRESET_KBVISION:
        _add(PRESET_KBVISION, PRESET_KBVISION)
        _add(f"{PRESET_KBVISION}_alt", PRESET_CUSTOM, path_override=f"live/ch{normalize_channel(chan)}")
        _add(PRESET_ONVIF, PRESET_ONVIF)
        _add(PRESET_DAHUA, PRESET_DAHUA)
    else:
        # Generic / auto
        _add(PRESET_DAHUA, PRESET_DAHUA)
        _add(PRESET_HIKVISION, PRESET_HIKVISION)
        _add(PRESET_ONVIF, PRESET_ONVIF)
        _add(PRESET_KBVISION, PRESET_KBVISION)

    # Legacy default fallback
    _add("legacy", PRESET_CUSTOM, path_override=f"h264/ch{normalize_channel(chan)}/{'main' if is_main else 'sub'}/av_stream", add_timeout=True)

    return candidates


# ---------------------------------------------------------------------------
# RTSP Socket Probe (Pure Python Handshake)
# ---------------------------------------------------------------------------


def probe_rtsp_socket(
    host: str,
    port: int = DEFAULT_RTSP_PORT,
    timeout: float = 5.0,
) -> Tuple[bool, int, str]:
    """
    Perform a low-level RTSP socket probe (OPTIONS request).
    Returns (reachable, status_code, message/banner).
    """
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(max(1.0, float(timeout)))
        sock.connect((str(host), int(port)))

        req = (
            f"OPTIONS rtsp://{host}:{port}/ RTSP/1.0\r\n"
            f"CSeq: 1\r\n"
            f"User-Agent: Cambida-RTSP/2.0\r\n"
            f"\r\n"
        )
        sock.sendall(req.encode("ascii", errors="replace"))

        resp = sock.recv(1024).decode("latin-1", errors="replace")
        if not resp:
            return True, 0, "Connected, but no response header."

        match = re.search(r"RTSP/1\.\d\s+(\d{3})\s*(.*)", resp)
        if match:
            code = int(match.group(1))
            status_text = match.group(2).strip()
            return True, code, status_text

        return True, 200, "RTSP service active."
    except socket.timeout:
        return False, 0, "Connection timed out."
    except ConnectionRefusedError:
        return False, 0, "Connection refused (port closed)."
    except Exception as exc:
        return False, 0, str(exc)
    finally:
        if sock:
            try:
                sock.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# RTSPAdapter Implementation
# ---------------------------------------------------------------------------


class RTSPAdapter:
    """
    Standalone RTSP Camera Adapter.

    Handles RTSP URL composition, connection testing, stream recording,
    and JPEG live frame extraction. Never logs credentials.
    """

    def __init__(
        self,
        host: str,
        username: str = "",
        password: str = "",
        port: int = DEFAULT_RTSP_PORT,
        channel: Union[int, str] = 1,
        stream: str = "main",
        preset: str = PRESET_CUSTOM,
        custom_path: Optional[str] = None,
        device_id: Optional[str] = None,
    ) -> None:
        self.host = str(host or "").strip()
        self.port = int(port or DEFAULT_RTSP_PORT)
        self.username = str(username or "").strip()
        self._password = str(password or "")
        self.channel = normalize_channel(channel)
        self.stream = "main" if str(stream).strip().lower() in {"main", "record", "0"} else "sub"
        self.preset = str(preset or PRESET_CUSTOM).strip().lower()
        self.custom_path = str(custom_path).strip().lstrip("/") if custom_path else None
        self.device_id = str(device_id or f"rtsp_{self.host}_{self.port}")

        if not (1 <= self.port <= 65535):
            raise ValueError(f"Port RTSP {self.port} không hợp lệ (1-65535).")

    def __repr__(self) -> str:
        """Never expose password in repr."""
        safe_pass = "***" if self._password else ""
        return (
            f"RTSPAdapter(device_id={self.device_id!r}, host={self.host!r}, "
            f"port={self.port}, user={self.username!r}, pass={safe_pass!r}, "
            f"channel={self.channel}, stream={self.stream!r}, preset={self.preset!r})"
        )

    @property
    def capabilities(self) -> Dict[str, Any]:
        """
        Report supported features.
        PTZ and two-way audio are unsupported over standard RTSP adapter.
        """
        return {
            "live_stream": True,
            "record": True,
            "snapshot": True,
            "ptz": False,
            "two_way_audio": False,
            "multi_channel": True,
        }

    @classmethod
    def from_camera(cls, camera: Dict[str, Any]) -> RTSPAdapter:
        """
        Factory method providing backward compatibility with legacy camera dictionaries.
        """
        host = camera.get("ip") or camera.get("host") or ""
        port = camera.get("port") or camera.get("rtsp_port") or DEFAULT_RTSP_PORT
        user = camera.get("user") or camera.get("username") or ""
        passwd = camera.get("pass") if camera.get("pass") is not None else camera.get("password", "")
        chan = camera.get("rtsp_channel") or camera.get("channel") or 1

        stream = "main"
        raw_view = str(camera.get("view_stream") or "").strip().lower()
        if raw_view in {"main", "sub"}:
            stream = raw_view
        elif camera.get("stream") in {"main", "sub"}:
            stream = str(camera.get("stream"))

        vendor = str(camera.get("vendor") or "").strip().lower()
        preset = PRESET_CUSTOM
        if vendor in {PRESET_HIKVISION, PRESET_EZVIZ}:
            preset = PRESET_HIKVISION
        elif vendor in {PRESET_DAHUA, PRESET_IMOU}:
            preset = PRESET_DAHUA
        elif vendor == PRESET_KBVISION:
            preset = PRESET_KBVISION

        custom_path = (
            camera.get("record_path") if stream == "main" else camera.get("preview_path")
        )

        dev_id = camera.get("id") or camera.get("device_id") or f"cam_{host}_{port}"

        return cls(
            host=host,
            username=user,
            password=passwd,
            port=int(port),
            channel=chan,
            stream=stream,
            preset=preset,
            custom_path=custom_path,
            device_id=str(dev_id),
        )

    def get_url(self, stream: Optional[str] = None, include_auth: bool = True) -> str:
        """Get the RTSP URL for the current or specified stream."""
        target_stream = stream or self.stream
        path = build_rtsp_path(self.preset, self.channel, target_stream, self.custom_path)
        u = self.username if include_auth else ""
        p = self._password if include_auth else ""
        return build_rtsp_url(self.host, self.port, u, p, path)

    def get_safe_url(self, stream: Optional[str] = None) -> str:
        """Get sanitized RTSP URL without credentials."""
        return sanitize_rtsp_url(self.get_url(stream=stream, include_auth=False))

    def get_candidates(self, stream: Optional[str] = None) -> List[RTSPCandidate]:
        """Generate candidates for probing."""
        st = stream or self.stream
        return get_rtsp_candidates(
            {
                "ip": self.host,
                "port": self.port,
                "user": self.username,
                "pass": self._password,
                "rtsp_channel": self.channel,
                "vendor": self.preset,
                "record_path": self.custom_path if st == "main" else None,
                "preview_path": self.custom_path if st == "sub" else None,
            },
            stream=st,
            channel=self.channel,
            vendor=self.preset,
        )

    def probe(
        self,
        require_media: bool = True,
        media_seconds: float = 2.0,
        ffmpeg_path: Optional[str] = None,
        timeout: float = 10.0,
    ) -> ProbeResult:
        """
        Probe camera connection.

        1. Performs RTSP socket check to verify IP/port and server responsiveness.
        2. If require_media is True and FFmpeg is available, tests stream decode.
        """
        # Step 1: Low-level socket check
        reachable, code, banner = probe_rtsp_socket(self.host, self.port, timeout=timeout)
        if not reachable:
            msg = rtsp_failure_message("network", self.preset)
            return ProbeResult(
                ok=False,
                media_ok=False,
                message=f"{msg} ({banner})",
                transport="rtsp",
                details={"host": self.host, "port": self.port, "stage": "socket"},
            )

        if not require_media:
            return ProbeResult(
                ok=True,
                media_ok=False,
                channels=1,
                message=f"RTSP port {self.port} mở và phản hồi thành công.",
                transport="rtsp",
                details={"status_code": code, "banner": banner},
            )

        # Step 2: Media probe via FFmpeg
        resolved_ffmpeg = ffmpeg_path or "ffmpeg"
        candidates = self.get_candidates(self.stream)
        last_error = ""
        attempted_profiles = []

        for cand in candidates:
            attempted_profiles.append(cand.profile)
            cmd = [
                resolved_ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-rtsp_transport",
                "tcp",
                "-rtsp_flags",
                "prefer_tcp",
                "-i",
                cand.url,
                "-map",
                "0:v:0",
                "-t",
                str(max(1.0, float(media_seconds))),
                "-f",
                "null",
                "-",
            ]
            try:
                proc = subprocess.run(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=max(3.0, float(timeout)),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    encoding="utf-8",
                    errors="replace",
                )
                if proc.returncode == 0:
                    return ProbeResult(
                        ok=True,
                        media_ok=True,
                        channels=1,
                        message="Kết nối RTSP và nhận luồng video thành công.",
                        profile=cand.profile,
                        transport="rtsp",
                        details={"profile": cand.profile, "url": cand.safe_url},
                    )
                last_error = proc.stderr or ""
            except subprocess.TimeoutExpired:
                last_error = "FFmpeg timed out waiting for video packets."
            except FileNotFoundError:
                return ProbeResult(
                    ok=True,
                    media_ok=False,
                    channels=1,
                    message=f"RTSP service hoạt động nhưng không tìm thấy FFmpeg để kiểm tra luồng video.",
                    transport="rtsp",
                    details={"status_code": code},
                )
            except Exception as exc:
                last_error = str(exc)

        sanitized_err = sanitize_error_text(last_error, [self._password])
        kind = classify_rtsp_error(sanitized_err)
        friendly_msg = rtsp_failure_message(kind, self.preset)

        return ProbeResult(
            ok=False,
            media_ok=False,
            channels=1,
            message=friendly_msg,
            profile=attempted_profiles[-1] if attempted_profiles else None,
            transport="rtsp",
            details={
                "error_kind": kind,
                "attempted_profiles": attempted_profiles,
                "raw_error": sanitized_err.strip().splitlines()[-1:] if sanitized_err else [],
            },
        )

    def capture_jpeg(self, ffmpeg_path: str, timeout: float = 12.0) -> bytes:
        """Capture a single JPEG snapshot from the RTSP stream using FFmpeg."""
        if not os.path.isfile(ffmpeg_path):
            raise RuntimeError("Không tìm thấy ffmpeg.exe cho RTSP snapshot.")

        url = self.get_url(self.stream, include_auth=True)
        cmd = [
            ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-rtsp_transport",
            "tcp",
            "-rtsp_flags",
            "prefer_tcp",
            "-i",
            url,
            "-vframes",
            "1",
            "-f",
            "image2",
            "-vcodec",
            "mjpeg",
            "pipe:1",
        ]

        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=float(timeout),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode == 0 and proc.stdout:
                return proc.stdout
            err_msg = sanitize_error_text(proc.stderr, [self._password])
            raise RuntimeError(f"FFmpeg capture JPEG lỗi: {err_msg.strip()}")
        except subprocess.TimeoutExpired:
            raise TimeoutError("Quá thời gian chờ lấy ảnh chụp JPEG từ RTSP.")

    def iter_jpeg_frames(
        self,
        ffmpeg_path: str,
        fps: float = 10.0,
        frame_timeout: float = 12.0,
    ) -> Generator[bytes, None, None]:
        """
        Yield JPEG frames continuously for low-latency live preview.
        Uses FFmpeg mjpeg stream piped through stdout.
        """
        if not os.path.isfile(ffmpeg_path):
            raise RuntimeError("Không tìm thấy ffmpeg.exe cho RTSP live preview.")

        url = self.get_url(stream="sub", include_auth=True)
        cmd = [
            ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-rtsp_transport",
            "tcp",
            "-rtsp_flags",
            "prefer_tcp",
            "-i",
            url,
            "-an",
            "-r",
            str(max(1.0, float(fps))),
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "-q:v",
            "5",
            "pipe:1",
        ]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1024 * 1024,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        buffer = bytearray()
        last_frame_time = time.monotonic()

        try:
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    if proc.poll() is not None:
                        break
                    time.sleep(0.01)
                    if time.monotonic() - last_frame_time > frame_timeout:
                        break
                    continue

                buffer.extend(chunk)

                # Find JPEG SOI (0xFF, 0xD8) and EOI (0xFF, 0xD9)
                while True:
                    start = buffer.find(b"\xff\xd8")
                    if start == -1:
                        buffer.clear()
                        break
                    end = buffer.find(b"\xff\xd9", start + 2)
                    if end == -1:
                        # Retain buffer from start marker onwards
                        if start > 0:
                            del buffer[:start]
                        break

                    jpeg_frame = bytes(buffer[start : end + 2])
                    del buffer[: end + 2]
                    last_frame_time = time.monotonic()
                    yield jpeg_frame

                if time.monotonic() - last_frame_time > frame_timeout:
                    break
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def record_segment(
        self,
        output_path: str,
        duration: float,
        ffmpeg_path: str,
        stop_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        """
        Record an RTSP segment directly to an MP4 file.
        Uses stream copy (-c copy) for maximum efficiency and fast start.
        """
        output_path = os.path.abspath(output_path)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        url = self.get_url(stream="main", include_auth=True)
        started = time.monotonic()

        cmd = [
            ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-rtsp_transport",
            "tcp",
            "-rtsp_flags",
            "prefer_tcp",
            "-i",
            url,
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-t",
            str(max(1.0, float(duration))),
            "-f",
            "mp4",
            output_path,
        ]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        try:
            while proc.poll() is None:
                if stop_event is not None and stop_event.is_set():
                    proc.terminate()
                    try:
                        proc.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    break
                time.sleep(0.2)

            out_size = os.path.getsize(output_path) if os.path.isfile(output_path) else 0
            if proc.returncode != 0 and out_size <= 0:
                err_text = ""
                if proc.stderr:
                    err_text = proc.stderr.read().decode("utf-8", errors="replace")
                sanitized = sanitize_error_text(err_text, [self._password])
                raise RuntimeError(f"Ghi video RTSP thất bại: {sanitized}")

            return {
                "ok": True,
                "mode": "copy",
                "bytes": out_size,
                "duration": time.monotonic() - started,
                "output_path": output_path,
            }
        finally:
            if proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Core BaseCameraAdapter Implementation for RTSP
# ---------------------------------------------------------------------------


class RTSPCameraAdapter(BaseCameraAdapter if BaseCameraAdapter is not object else object):  # type: ignore[misc]
    """
    Modular Camera Adapter conforming to BaseCameraAdapter protocol for RTSP.
    """

    vendor: str = "rtsp"
    default_port: int = DEFAULT_RTSP_PORT

    def _default_capabilities(self) -> Set[Any]:
        caps: Set[Any] = set()
        if CameraCapability is not None:
            caps.update(
                {
                    CameraCapability.CONNECT,
                    CameraCapability.LIVE_MAIN,
                    CameraCapability.LIVE_SUB,
                    CameraCapability.ENUMERATE_CHANNELS,
                }
            )
        return caps

    def probe(self, device: Any, timeout_sec: float = 5.0) -> Any:
        port = getattr(device, "port", None) or getattr(device, "rtsp_port", None) or DEFAULT_RTSP_PORT
        adapter = RTSPAdapter(
            host=getattr(device, "host", ""),
            port=port,
            username=getattr(device, "user", ""),
            password=getattr(device, "password", ""),
            device_id=getattr(device, "device_id", None),
        )
        res = adapter.probe(timeout=timeout_sec)
        if CoreProbeResult is not None:
            chans = self.enumerate_channels(device, timeout_sec=timeout_sec)
            return CoreProbeResult(
                ok=res.ok,
                device_id=getattr(device, "device_id", ""),
                vendor=self.vendor,
                media_ok=res.media_ok,
                channel_count=len(chans),
                message=res.message,
                discovered_channels=chans if isinstance(chans, list) else [],
                latency_ms=res.latency_ms,
            )
        return res

    def enumerate_channels(self, device: Any, timeout_sec: float = 5.0) -> List[Any]:
        channels: List[Any] = []
        count = max(1, int(getattr(device, "channel_count", 1) or 1))
        dev_id = getattr(device, "device_id", "")
        for i in range(1, count + 1):
            c_name = f"Kênh {i}"
            if ChannelInfo is not None:
                channels.append(ChannelInfo(channel_id=i, device_id=dev_id, name=c_name, enabled=True))
            else:
                channels.append({"channel_id": i, "device_id": dev_id, "name": c_name, "enabled": True})
        return channels

    def get_live_stream(
        self,
        device: Any,
        channel_id: int,
        stream: Any = "main",
    ) -> Any:
        stream_name = getattr(stream, "value", str(stream)).lower()
        preset = getattr(device, "vendor", "generic")
        port = getattr(device, "port", None) or getattr(device, "rtsp_port", None) or DEFAULT_RTSP_PORT
        adapter = RTSPAdapter(
            host=getattr(device, "host", ""),
            port=port,
            username=getattr(device, "user", ""),
            password=getattr(device, "password", ""),
            channel=channel_id,
            stream=stream_name,
            preset=preset,
        )
        url = adapter.get_stream_url()
        if StreamEndpoint is not None and TransportProtocol is not None and StreamType is not None:
            st = StreamType.MAIN if stream_name in {"main", "record", "0"} else StreamType.SUB
            return StreamEndpoint(
                stream_type=st,
                url=url,
                protocol=TransportProtocol.RTSP,
                direct_transport="ffmpeg",
            )
        return url


# Register adapter with global registry if available
if get_adapter_registry is not None:
    try:
        _reg = get_adapter_registry()
        _reg.register("rtsp", RTSPCameraAdapter)
        _reg.register("generic", RTSPCameraAdapter)
    except Exception:
        pass


__all__ = [
    "DEFAULT_RTSP_PORT",
    "PRESET_HIKVISION",
    "PRESET_EZVIZ",
    "PRESET_DAHUA",
    "PRESET_IMOU",
    "PRESET_KBVISION",
    "PRESET_CUSTOM",
    "PRESET_ONVIF",
    "SUPPORTED_PRESETS",
    "ProbeResult",
    "RTSPCandidate",
    "RTSPAdapter",
    "RTSPCameraAdapter",
    "sanitize_rtsp_url",
    "sanitize_error_text",
    "classify_rtsp_error",
    "rtsp_failure_message",
    "build_rtsp_path",
    "build_rtsp_url",
    "get_rtsp_candidates",
    "probe_rtsp_socket",
]
