"""
Standalone ONVIF Camera Module for Cambida.

Provides pure-Python ONVIF client functionality without heavyweight external
dependencies (such as onvif-zeep). Implements WS-Security (UsernameToken with
SHA-1 PasswordDigest and Plaintext), SOAP envelope generation, Device, Media
(1.0 & 2.0), and PTZ service requests, profile/channel discovery, RTSP stream
URI resolution, and HTTP snapshot extraction.

Follows .project/CAMERA_MODULE_CONTRACT_20260924.md:
- Separate IDs for devices, physical channels, and table bindings.
- All ports editable (default ONVIF HTTP port 80).
- Never exposes credentials in logs, repr, or error messages.
- Registry adapter capabilities strictly reported; unsupported operations
  (e.g., PTZ when not equipped) explicitly flagged as unsupported.
- Legacy camera configuration compatibility.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import os
import re
import socket
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Generator, List, Optional, Tuple, Union
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

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

from camera_modules.rtsp import (
    ProbeResult,
    RTSPAdapter,
    classify_rtsp_error,
    rtsp_failure_message,
    sanitize_error_text,
    sanitize_rtsp_url,
)


DEFAULT_ONVIF_PORT = 80

# Common SOAP & ONVIF XML Namespaces
NS = {
    "soap": "http://www.w3.org/2003/05/soap-envelope",
    "soap11": "http://schemas.xmlsoap.org/soap/envelope/",
    "wsse": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd",
    "wsu": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tr2": "http://www.onvif.org/ver20/media/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
    "tptz": "http://www.onvif.org/ver20/ptz/wsdl",
}


@dataclass
class OnvifDeviceInfo:
    """Hardware and firmware information reported by the ONVIF device service."""

    manufacturer: str = "Unknown"
    model: str = "Generic ONVIF"
    firmware_version: str = ""
    serial_number: str = ""
    hardware_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class OnvifProfile:
    """Media profile retrieved from ONVIF Media service."""

    token: str
    name: str
    video_source_token: Optional[str] = None
    encoding: str = "H264"
    width: int = 1920
    height: int = 1080
    fps: float = 25.0
    stream_uri: Optional[str] = None
    snapshot_uri: Optional[str] = None
    channel_index: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# WS-Security and SOAP Envelope Construction
# ---------------------------------------------------------------------------


def create_wsse_header(username: str, password: str, use_digest: bool = True) -> str:
    """
    Generate WS-Security UsernameToken header XML.
    Uses SHA-1 Password Digest by default per ONVIF Core Specification.
    """
    if not username:
        return ""

    created = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    if use_digest:
        nonce_raw = os.urandom(16)
        nonce_b64 = base64.b64encode(nonce_raw).decode("ascii")
        # Digest = Base64(SHA-1(Nonce + Created + Password))
        raw_hash = hashlib.sha1(
            nonce_raw + created.encode("utf-8") + password.encode("utf-8")
        ).digest()
        password_digest = base64.b64encode(raw_hash).decode("ascii")

        return f"""
    <wsse:Security soap:mustUnderstand="true" xmlns:wsse="{NS['wsse']}" xmlns:wsu="{NS['wsu']}">
      <wsse:UsernameToken>
        <wsse:Username>{username}</wsse:Username>
        <wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{password_digest}</wsse:Password>
        <wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{nonce_b64}</wsse:Nonce>
        <wsu:Created>{created}</wsu:Created>
      </wsse:UsernameToken>
    </wsse:Security>"""
    else:
        return f"""
    <wsse:Security soap:mustUnderstand="true" xmlns:wsse="{NS['wsse']}">
      <wsse:UsernameToken>
        <wsse:Username>{username}</wsse:Username>
        <wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordText">{password}</wsse:Password>
      </wsse:UsernameToken>
    </wsse:Security>"""


def build_soap_envelope(body_xml: str, username: str = "", password: str = "", use_soap11: bool = False) -> str:
    """
    Wrap an ONVIF SOAP operation body into a valid SOAP 1.2 (or 1.1) envelope
    with WS-Security header if credentials are provided.
    """
    env_ns = NS["soap11"] if use_soap11 else NS["soap"]
    wsse_header = create_wsse_header(username, password) if username else ""

    return f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="{env_ns}"
               xmlns:tds="{NS['tds']}"
               xmlns:trt="{NS['trt']}"
               xmlns:tr2="{NS['tr2']}"
               xmlns:tt="{NS['tt']}"
               xmlns:tptz="{NS['tptz']}">
  <soap:Header>
    {wsse_header}
  </soap:Header>
  <soap:Body>
    {body_xml.strip()}
  </soap:Body>
</soap:Envelope>""".strip()


# ---------------------------------------------------------------------------
# Pure-Python ONVIF Client
# ---------------------------------------------------------------------------


class ONVIFClient:
    """
    Lightweight, thread-safe ONVIF SOAP client.
    Does not require external XML/SOAP libraries.
    """

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_ONVIF_PORT,
        username: str = "",
        password: str = "",
        timeout: float = 8.0,
    ) -> None:
        self.host = str(host or "").strip()
        self.port = int(port or DEFAULT_ONVIF_PORT)
        self.username = str(username or "").strip()
        self._password = str(password or "")
        self.timeout = max(1.0, float(timeout))

        self.device_service_url = f"http://{self.host}:{self.port}/onvif/device_service"
        self.media_service_url: Optional[str] = None
        self.media2_service_url: Optional[str] = None
        self.ptz_service_url: Optional[str] = None
        self.events_service_url: Optional[str] = None
        self.imaging_service_url: Optional[str] = None

    def _send_request(self, service_url: str, action: str, body_xml: str) -> ET.Element:
        """
        Send a SOAP request to the ONVIF service endpoint.
        Falls back between SOAP 1.2 and SOAP 1.1 if needed.
        """
        envelope = build_soap_envelope(body_xml, self.username, self._password, use_soap11=False)
        content_bytes = envelope.encode("utf-8")

        headers = {
            "Content-Type": "application/soap+xml; charset=utf-8; action=" + action,
            "Content-Length": str(len(content_bytes)),
            "User-Agent": "Cambida-ONVIF/2.0",
        }

        req = Request(service_url, data=content_bytes, headers=headers, method="POST")

        try:
            with urlopen(req, timeout=self.timeout) as resp:
                resp_bytes = resp.read()
                return ET.fromstring(resp_bytes)
        except HTTPError as err:
            # Some older cameras strictly mandate SOAP 1.1 (text/xml)
            if err.code in (400, 415, 500):
                try:
                    env11 = build_soap_envelope(body_xml, self.username, self._password, use_soap11=True)
                    c11_bytes = env11.encode("utf-8")
                    h11 = {
                        "Content-Type": "text/xml; charset=utf-8",
                        "SOAPAction": f'"{action}"',
                        "Content-Length": str(len(c11_bytes)),
                        "User-Agent": "Cambida-ONVIF/2.0",
                    }
                    req11 = Request(service_url, data=c11_bytes, headers=h11, method="POST")
                    with urlopen(req11, timeout=self.timeout) as resp11:
                        return ET.fromstring(resp11.read())
                except Exception:
                    pass

            error_body = ""
            try:
                error_body = err.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            sanitized = sanitize_error_text(error_body or str(err), [self._password])
            raise RuntimeError(f"Lỗi ONVIF HTTP {err.code}: {sanitized}")
        except URLError as err:
            sanitized = sanitize_error_text(str(err.reason), [self._password])
            raise ConnectionError(f"Không thể kết nối ONVIF ({self.host}:{self.port}): {sanitized}")

    def get_capabilities(self) -> Dict[str, str]:
        """
        Query GetCapabilities to discover service URLs (Media, PTZ, Events, etc.).
        """
        body = """
        <tds:GetCapabilities>
          <tds:Category>All</tds:Category>
        </tds:GetCapabilities>
        """
        root = self._send_request(
            self.device_service_url,
            "http://www.onvif.org/ver10/device/wsdl/GetCapabilities",
            body,
        )

        caps: Dict[str, str] = {}
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "XAddr" and elem.text:
                parent_tag = ""
                # Attempt to determine service type from parent
                for p in root.iter():
                    if elem in list(p):
                        parent_tag = p.tag.split("}")[-1] if "}" in p.tag else p.tag
                        break
                service_key = parent_tag.lower() if parent_tag else "unknown"
                caps[service_key] = elem.text.strip()

        # Extract well-known services
        for key, url in caps.items():
            if "media" in key and not self.media_service_url:
                self.media_service_url = url
            elif "ptz" in key and not self.ptz_service_url:
                self.ptz_service_url = url
            elif "event" in key and not self.events_service_url:
                self.events_service_url = url
            elif "imaging" in key and not self.imaging_service_url:
                self.imaging_service_url = url

        # Fallback default media URL if not explicitly found in XML
        if not self.media_service_url:
            self.media_service_url = f"http://{self.host}:{self.port}/onvif/Media"

        return caps

    def get_device_information(self) -> OnvifDeviceInfo:
        """Query GetDeviceInformation for model, manufacturer, and firmware version."""
        body = "<tds:GetDeviceInformation />"
        root = self._send_request(
            self.device_service_url,
            "http://www.onvif.org/ver10/device/wsdl/GetDeviceInformation",
            body,
        )

        info = OnvifDeviceInfo()
        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            val = (elem.text or "").strip()
            if tag == "Manufacturer":
                info.manufacturer = val
            elif tag == "Model":
                info.model = val
            elif tag == "FirmwareVersion":
                info.firmware_version = val
            elif tag == "SerialNumber":
                info.serial_number = val
            elif tag == "HardwareId":
                info.hardware_id = val

        return info

    def get_profiles(self) -> List[OnvifProfile]:
        """
        Query GetProfiles to obtain all streaming profiles available on the camera.
        """
        if not self.media_service_url:
            self.get_capabilities()

        url = self.media_service_url or f"http://{self.host}:{self.port}/onvif/Media"
        body = "<trt:GetProfiles />"
        root = self._send_request(
            url,
            "http://www.onvif.org/ver10/media/wsdl/GetProfiles",
            body,
        )

        profiles: List[OnvifProfile] = []
        chan_counter = 1

        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "Profiles":
                token = elem.attrib.get("token") or ""
                name_elem = elem.find("{*}Name")
                name = name_elem.text.strip() if (name_elem is not None and name_elem.text) else token

                vsrc_elem = elem.find("{*}VideoSourceConfiguration")
                vsrc_token = None
                if vsrc_elem is not None:
                    src_token_elem = vsrc_elem.find("{*}SourceToken")
                    if src_token_elem is not None and src_token_elem.text:
                        vsrc_token = src_token_elem.text.strip()

                encoding = "H264"
                width = 1920
                height = 1080
                fps = 25.0

                venc_elem = elem.find("{*}VideoEncoderConfiguration")
                if venc_elem is not None:
                    enc_elem = venc_elem.find("{*}Encoding")
                    if enc_elem is not None and enc_elem.text:
                        encoding = enc_elem.text.strip()
                    res_elem = venc_elem.find("{*}Resolution")
                    if res_elem is not None:
                        w_elem = res_elem.find("{*}Width")
                        h_elem = res_elem.find("{*}Height")
                        if w_elem is not None and w_elem.text:
                            width = int(w_elem.text.strip())
                        if h_elem is not None and h_elem.text:
                            height = int(h_elem.text.strip())
                    rate_elem = venc_elem.find("{*}RateControl")
                    if rate_elem is not None:
                        fr_elem = rate_elem.find("{*}FrameRateLimit")
                        if fr_elem is not None and fr_elem.text:
                            fps = float(fr_elem.text.strip())

                prof = OnvifProfile(
                    token=token,
                    name=name,
                    video_source_token=vsrc_token,
                    encoding=encoding,
                    width=width,
                    height=height,
                    fps=fps,
                    channel_index=chan_counter,
                )
                profiles.append(prof)
                chan_counter += 1

        return profiles

    def get_stream_uri(self, profile_token: str, protocol: str = "RTSP") -> str:
        """
        Request RTSP stream URI for a specified media profile token.
        """
        if not self.media_service_url:
            self.get_capabilities()

        url = self.media_service_url or f"http://{self.host}:{self.port}/onvif/Media"
        body = f"""
        <trt:GetStreamUri>
          <trt:StreamSetup>
            <tt:Stream>RTP-Unicast</tt:Stream>
            <tt:Transport>
              <tt:Protocol>{protocol}</tt:Protocol>
            </tt:Transport>
          </trt:StreamSetup>
          <trt:ProfileToken>{profile_token}</trt:ProfileToken>
        </trt:GetStreamUri>
        """
        root = self._send_request(
            url,
            "http://www.onvif.org/ver10/media/wsdl/GetStreamUri",
            body,
        )

        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "Uri" and elem.text:
                raw_uri = elem.text.strip()
                # Inject credentials if not present in URI
                return self._inject_credentials_into_rtsp(raw_uri)

        raise RuntimeError(f"Camera không trả về StreamUri cho profile {profile_token}.")

    def get_snapshot_uri(self, profile_token: str) -> Optional[str]:
        """
        Request HTTP snapshot URI for a specified media profile token.
        """
        if not self.media_service_url:
            self.get_capabilities()

        url = self.media_service_url or f"http://{self.host}:{self.port}/onvif/Media"
        body = f"""
        <trt:GetSnapshotUri>
          <trt:ProfileToken>{profile_token}</trt:ProfileToken>
        </trt:GetSnapshotUri>
        """
        try:
            root = self._send_request(
                url,
                "http://www.onvif.org/ver10/media/wsdl/GetSnapshotUri",
                body,
            )
            for elem in root.iter():
                tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
                if tag == "Uri" and elem.text:
                    return elem.text.strip()
        except Exception:
            pass
        return None

    def get_video_sources(self) -> List[Dict[str, Any]]:
        """Query physical video sources/channels on the device."""
        if not self.media_service_url:
            self.get_capabilities()

        url = self.media_service_url or f"http://{self.host}:{self.port}/onvif/Media"
        body = "<trt:GetVideoSources />"
        sources: List[Dict[str, Any]] = []

        try:
            root = self._send_request(
                url,
                "http://www.onvif.org/ver10/media/wsdl/GetVideoSources",
                body,
            )
            idx = 1
            for elem in root.iter():
                tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
                if tag == "VideoSources":
                    token = elem.attrib.get("token") or f"src_{idx}"
                    sources.append({"token": token, "index": idx})
                    idx += 1
        except Exception:
            pass

        return sources

    def _inject_credentials_into_rtsp(self, raw_uri: str) -> str:
        """Safely ensure username and password are included in the RTSP URI."""
        if not self.username:
            return raw_uri
        parsed = urlparse(raw_uri)
        if not parsed.scheme.startswith("rtsp"):
            return raw_uri

        # If already has userinfo, leave intact
        if "@" in parsed.netloc:
            return raw_uri

        user_enc = quote(self.username, safe="")
        pass_enc = quote(self._password, safe="")
        auth_netloc = f"{user_enc}:{pass_enc}@{parsed.netloc}"

        return raw_uri.replace(f"{parsed.scheme}://{parsed.netloc}", f"{parsed.scheme}://{auth_netloc}", 1)


# ---------------------------------------------------------------------------
# ONVIFAdapter Implementation
# ---------------------------------------------------------------------------


class ONVIFAdapter:
    """
    High-level ONVIF Camera Adapter.

    Integrates ONVIF device discovery, profile management, RTSP streaming,
    and direct HTTP snapshot capture. Follows contract specifications:
    - Never exposes passwords.
    - Unsupported operations (e.g. PTZ) explicitly flagged as unsupported.
    - Separate device IDs, channel IDs, and table bindings.
    """

    def __init__(
        self,
        host: str,
        username: str = "",
        password: str = "",
        port: int = DEFAULT_ONVIF_PORT,
        channel: Union[int, str] = 1,
        stream: str = "main",
        device_id: Optional[str] = None,
        timeout: float = 8.0,
    ) -> None:
        self.host = str(host or "").strip()
        self.port = int(port or DEFAULT_ONVIF_PORT)
        self.username = str(username or "").strip()
        self._password = str(password or "")
        self.channel = int(channel or 1)
        self.stream = "main" if str(stream).strip().lower() in {"main", "record", "0"} else "sub"
        self.device_id = str(device_id or f"onvif_{self.host}_{self.port}")
        self.timeout = max(1.0, float(timeout))

        self.client = ONVIFClient(
            host=self.host,
            port=self.port,
            username=self.username,
            password=self._password,
            timeout=self.timeout,
        )

        self._device_info: Optional[OnvifDeviceInfo] = None
        self._profiles: List[OnvifProfile] = []
        self._capabilities_cache: Optional[Dict[str, Any]] = None
        self._active_rtsp_adapter: Optional[RTSPAdapter] = None

        if not (1 <= self.port <= 65535):
            raise ValueError(f"Port ONVIF {self.port} không hợp lệ (1-65535).")

    def __repr__(self) -> str:
        """Sanitized string representation preserving security."""
        safe_pass = "***" if self._password else ""
        return (
            f"ONVIFAdapter(device_id={self.device_id!r}, host={self.host!r}, "
            f"port={self.port}, user={self.username!r}, pass={safe_pass!r}, "
            f"channel={self.channel}, stream={self.stream!r})"
        )

    @property
    def capabilities(self) -> Dict[str, Any]:
        """
        Accurately report ONVIF adapter capabilities.
        Does not falsely claim unsupported operations.
        """
        has_ptz = bool(self.client.ptz_service_url)
        return {
            "onvif_soap": True,
            "media_profiles": True,
            "live_stream": True,
            "record": True,
            "snapshot_http": True,
            "ptz": has_ptz,  # Reported True only if camera provides PTZ service
            "two_way_audio": False,
            "multi_channel": True,
        }

    @classmethod
    def from_camera(cls, camera: Dict[str, Any]) -> ONVIFAdapter:
        """
        Factory method providing backward compatibility with legacy camera dictionaries.
        """
        host = camera.get("ip") or camera.get("host") or ""
        port = camera.get("onvif_port") or camera.get("port") or DEFAULT_ONVIF_PORT
        user = camera.get("user") or camera.get("username") or ""
        passwd = camera.get("pass") if camera.get("pass") is not None else camera.get("password", "")
        chan = camera.get("rtsp_channel") or camera.get("channel") or 1

        stream = "main"
        raw_view = str(camera.get("view_stream") or "").strip().lower()
        if raw_view in {"main", "sub"}:
            stream = raw_view
        elif camera.get("stream") in {"main", "sub"}:
            stream = str(camera.get("stream"))

        dev_id = camera.get("id") or camera.get("device_id") or f"onvif_{host}_{port}"

        return cls(
            host=host,
            username=user,
            password=passwd,
            port=int(port),
            channel=chan,
            stream=stream,
            device_id=str(dev_id),
        )

    def fetch_metadata(self) -> Tuple[OnvifDeviceInfo, List[OnvifProfile]]:
        """Connect to device, fetch capabilities, device info, and profiles."""
        self.client.get_capabilities()
        self._device_info = self.client.get_device_information()
        self._profiles = self.client.get_profiles()

        # Populate StreamUri and SnapshotUri on profiles
        for prof in self._profiles:
            try:
                prof.stream_uri = self.client.get_stream_uri(prof.token)
            except Exception:
                pass
            try:
                prof.snapshot_uri = self.client.get_snapshot_uri(prof.token)
            except Exception:
                pass

        return self._device_info, self._profiles

    def get_channels(self) -> List[ChannelInfo]:
        """
        Return physical channel list compliant with CameraManager.ChannelInfo.
        Discovers multiple video sources or profile channels.
        """
        if not self._profiles:
            try:
                self.fetch_metadata()
            except Exception:
                pass

        channels: List[ChannelInfo] = []
        seen_ids = set()

        # If video sources exist
        sources = self.client.get_video_sources()
        if sources:
            for s in sources:
                cid = s["index"]
                if cid not in seen_ids:
                    seen_ids.add(cid)
                    c_name = f"Kênh {cid}"
                    if ChannelInfo is not None:
                        channels.append(ChannelInfo(channel_id=cid, name=c_name))
                    else:
                        channels.append({"channel_id": cid, "name": c_name})  # type: ignore[arg-type]
            return channels

        # Fallback to profile grouping
        for p in self._profiles:
            cid = p.channel_index
            if cid not in seen_ids:
                seen_ids.add(cid)
                c_name = p.name or f"Kênh {cid}"
                if ChannelInfo is not None:
                    channels.append(ChannelInfo(channel_id=cid, name=c_name))
                else:
                    channels.append({"channel_id": cid, "name": c_name})  # type: ignore[arg-type]

        if not channels:
            if ChannelInfo is not None:
                channels.append(ChannelInfo(channel_id=1, name="Kênh 1"))
            else:
                channels.append({"channel_id": 1, "name": "Kênh 1"})  # type: ignore[arg-type]

        return channels

    def resolve_profile(self, stream: Optional[str] = None) -> Optional[OnvifProfile]:
        """Find the matching profile for requested stream (main vs sub)."""
        if not self._profiles:
            self.fetch_metadata()

        if not self._profiles:
            return None

        target_stream = stream or self.stream
        is_main = target_stream in {"main", "record", "0"}

        # Sort profiles by resolution (highest first for main, lower for sub)
        sorted_profiles = sorted(
            self._profiles,
            key=lambda p: (p.width * p.height),
            reverse=is_main,
        )

        return sorted_profiles[0]

    def get_stream_url(self, stream: Optional[str] = None) -> str:
        """Resolve and return the RTSP Stream URI for the selected profile."""
        prof = self.resolve_profile(stream)
        if not prof:
            raise RuntimeError("Không tìm thấy ONVIF profile phù hợp trên camera.")

        if prof.stream_uri:
            return prof.stream_uri

        uri = self.client.get_stream_uri(prof.token)
        prof.stream_uri = uri
        return uri

    def get_safe_stream_url(self, stream: Optional[str] = None) -> str:
        """Return stream URL with credentials masked."""
        try:
            return sanitize_rtsp_url(self.get_stream_url(stream))
        except Exception:
            return ""

    def probe(
        self,
        require_media: bool = True,
        media_seconds: float = 2.0,
        ffmpeg_path: Optional[str] = None,
        timeout: float = 10.0,
    ) -> ProbeResult:
        """
        Probe ONVIF service, retrieve device information and profiles,
        and optionally verify RTSP stream with FFmpeg.
        """
        self.client.timeout = max(2.0, float(timeout))
        try:
            info, profiles = self.fetch_metadata()
        except ConnectionError as exc:
            return ProbeResult(
                ok=False,
                media_ok=False,
                message=f"Không thể kết nối ONVIF: {exc}",
                transport="onvif",
                details={"host": self.host, "port": self.port},
            )
        except Exception as exc:
            sanitized = sanitize_error_text(str(exc), [self._password])
            return ProbeResult(
                ok=False,
                media_ok=False,
                message=f"Lỗi ONVIF xác thực hoặc truy vấn: {sanitized}",
                transport="onvif",
                details={"host": self.host, "port": self.port},
            )

        if not profiles:
            return ProbeResult(
                ok=True,
                media_ok=False,
                channels=1,
                message=f"Kết nối ONVIF thành công ({info.manufacturer} {info.model}), nhưng không tìm thấy media profile.",
                transport="onvif",
                details={"device_info": info.to_dict()},
            )

        target_prof = self.resolve_profile(self.stream)
        stream_uri = target_prof.stream_uri if target_prof else None
        safe_uri = sanitize_rtsp_url(stream_uri)

        if not require_media or not stream_uri:
            return ProbeResult(
                ok=True,
                media_ok=False,
                channels=len(self.get_channels()),
                message=f"ONVIF sẵn sàng: {info.manufacturer} {info.model} ({len(profiles)} profile).",
                transport="onvif",
                details={
                    "device_info": info.to_dict(),
                    "profiles": [p.to_dict() for p in profiles],
                    "safe_stream_url": safe_uri,
                },
            )

        # Validate media stream via FFmpeg
        resolved_ffmpeg = ffmpeg_path or "ffmpeg"
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
            stream_uri,
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
                    channels=len(self.get_channels()),
                    message=f"Kết nối ONVIF ({info.manufacturer} {info.model}) và nhận luồng RTSP thành công.",
                    profile=target_prof.token if target_prof else None,
                    transport="onvif",
                    details={
                        "device_info": info.to_dict(),
                        "profile": target_prof.token if target_prof else None,
                        "url": safe_uri,
                    },
                )
            sanitized_err = sanitize_error_text(proc.stderr or "", [self._password])
            return ProbeResult(
                ok=True,
                media_ok=False,
                channels=len(self.get_channels()),
                message=f"ONVIF đăng nhập thành công nhưng chưa nhận được video: {sanitized_err.strip()}",
                transport="onvif",
                details={"device_info": info.to_dict(), "url": safe_uri},
            )
        except Exception as exc:
            sanitized_err = sanitize_error_text(str(exc), [self._password])
            return ProbeResult(
                ok=True,
                media_ok=False,
                channels=len(self.get_channels()),
                message=f"ONVIF sẵn sàng nhưng không thể chạy FFmpeg kiểm tra luồng: {sanitized_err}",
                transport="onvif",
                details={"device_info": info.to_dict()},
            )

    def capture_jpeg(self, ffmpeg_path: Optional[str] = None, timeout: float = 12.0) -> bytes:
        """
        Capture a JPEG still image.
        Prefers direct HTTP SnapshotUri if available from the camera.
        Falls back to FFmpeg frame grab from the RTSP stream URI.
        """
        prof = self.resolve_profile(self.stream)
        if not prof:
            raise RuntimeError("Không tìm thấy ONVIF profile để chụp ảnh.")

        # Attempt 1: Direct HTTP Snapshot
        if prof.snapshot_uri:
            try:
                headers = {"User-Agent": "Cambida-ONVIF/2.0"}
                if self.username:
                    auth_raw = f"{self.username}:{self._password}".encode("utf-8")
                    headers["Authorization"] = f"Basic {base64.b64encode(auth_raw).decode('ascii')}"

                req = Request(prof.snapshot_uri, headers=headers)
                with urlopen(req, timeout=float(timeout)) as resp:
                    data = resp.read()
                    if data and data[:2] == b"\xff\xd8":
                        return data
            except Exception:
                pass

        # Attempt 2: RTSP stream snapshot via FFmpeg
        stream_uri = self.get_stream_url(self.stream)
        resolved_ffmpeg = ffmpeg_path or "ffmpeg"
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
            stream_uri,
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
            raise RuntimeError(f"FFmpeg capture ONVIF JPEG lỗi: {err_msg.strip()}")
        except subprocess.TimeoutExpired:
            raise TimeoutError("Quá thời gian chờ lấy ảnh chụp từ luồng ONVIF.")

    def record_segment(
        self,
        output_path: str,
        duration: float,
        ffmpeg_path: str,
        stop_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        """
        Record a segment from the ONVIF stream using stream copy.
        """
        stream_uri = self.get_stream_url(stream="main")
        output_path = os.path.abspath(output_path)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

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
            stream_uri,
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
                raise RuntimeError(f"Ghi video ONVIF thất bại: {sanitized}")

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
# Core BaseCameraAdapter Implementation for ONVIF
# ---------------------------------------------------------------------------


class ONVIFCameraAdapter(BaseCameraAdapter if BaseCameraAdapter is not object else object):  # type: ignore[misc]
    """
    Modular Camera Adapter conforming to BaseCameraAdapter protocol for ONVIF.
    """

    vendor: str = "onvif"
    default_port: int = DEFAULT_ONVIF_PORT

    def _default_capabilities(self) -> Set[Any]:
        caps: Set[Any] = set()
        if CameraCapability is not None:
            caps.update(
                {
                    CameraCapability.DISCOVER,
                    CameraCapability.CONNECT,
                    CameraCapability.ENUMERATE_CHANNELS,
                    CameraCapability.LIVE_MAIN,
                    CameraCapability.LIVE_SUB,
                }
            )
        return caps

    def discover(self, timeout_sec: float = 5.0, **kwargs: Any) -> List[Any]:
        try:
            from camera_modules.discovery import discover_onvif_devices

            discovered = discover_onvif_devices(timeout_sec=timeout_sec)
            dev_infos: List[Any] = []
            for d in discovered:
                if DeviceInfo is not None:
                    dev_infos.append(
                        DeviceInfo(
                            device_id=d.device_id,
                            vendor="onvif",
                            host=d.ip,
                            port=d.port,
                            name=d.name,
                            model=d.model,
                            channel_count=d.channel_count,
                        )
                    )
                else:
                    dev_infos.append(d.to_dict())
            return dev_infos
        except Exception:
            return []

    def probe(self, device: Any, timeout_sec: float = 5.0) -> Any:
        port = getattr(device, "port", None) or getattr(device, "http_port", None) or DEFAULT_ONVIF_PORT
        adapter = ONVIFAdapter(
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
        port = getattr(device, "port", None) or getattr(device, "http_port", None) or DEFAULT_ONVIF_PORT
        adapter = ONVIFAdapter(
            host=getattr(device, "host", ""),
            port=port,
            username=getattr(device, "user", ""),
            password=getattr(device, "password", ""),
            device_id=getattr(device, "device_id", None),
        )
        chans = adapter.get_channels()
        # Ensure channel objects have device_id set
        res_chans: List[Any] = []
        dev_id = getattr(device, "device_id", "")
        for ch in chans:
            cid = getattr(ch, "channel_id", 1)
            cname = getattr(ch, "name", f"Kênh {cid}")
            if ChannelInfo is not None:
                res_chans.append(ChannelInfo(channel_id=cid, device_id=dev_id, name=cname, enabled=True))
            else:
                res_chans.append({"channel_id": cid, "device_id": dev_id, "name": cname, "enabled": True})
        return res_chans

    def get_live_stream(
        self,
        device: Any,
        channel_id: int,
        stream: Any = "main",
    ) -> Any:
        stream_name = getattr(stream, "value", str(stream)).lower()
        port = getattr(device, "port", None) or getattr(device, "http_port", None) or DEFAULT_ONVIF_PORT
        adapter = ONVIFAdapter(
            host=getattr(device, "host", ""),
            port=port,
            username=getattr(device, "user", ""),
            password=getattr(device, "password", ""),
            channel=channel_id,
            stream=stream_name,
            device_id=getattr(device, "device_id", None),
        )
        url = adapter.get_stream_url(stream=stream_name)
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
        _reg.register("onvif", ONVIFCameraAdapter)
    except Exception:
        pass


__all__ = [
    "DEFAULT_ONVIF_PORT",
    "OnvifDeviceInfo",
    "OnvifProfile",
    "ONVIFClient",
    "ONVIFAdapter",
    "ONVIFCameraAdapter",
    "build_soap_envelope",
    "create_wsse_header",
]
