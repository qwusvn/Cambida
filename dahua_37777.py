"""Dahua/Imou private TCP adapter for port 37777.

The media path uses Dahua NetSDK directly (CLIENT_RealPlayEx +
CLIENT_SaveRealData) and converts the resulting DHAV/DAV stream to MP4 with
FFmpeg.  Credentials are passed by the caller and are never persisted here.
"""

from __future__ import annotations

import atexit
import ctypes as C
import hashlib
import os
import queue
import socket
import struct
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Optional


LLONG = C.c_int64
DWORD = C.c_uint32
BYTE = C.c_ubyte
BOOL = C.c_int
fRealData = C.CFUNCTYPE(None, LLONG, DWORD, C.POINTER(BYTE), DWORD, C.c_void_p)


class NET_DEVICEINFO_Ex(C.Structure):
    _fields_ = [
        ("sSerialNumber", BYTE * 48),
        ("nAlarmInPortNum", C.c_int),
        ("nAlarmOutPortNum", C.c_int),
        ("nDiskNum", C.c_int),
        ("nDVRType", C.c_int),
        ("nChanNum", C.c_int),
        ("byLimitLoginTime", BYTE),
        ("byLeftLogTimes", BYTE),
        ("bReserved", BYTE * 2),
        ("nLockLeftTime", C.c_int),
        ("Reserved", C.c_char * 4),
        ("nNTlsPort", C.c_int),
        ("Reserved2", C.c_char * 16),
    ]


class NET_IN_LOGIN_WITH_HIGHLEVEL_SECURITY(C.Structure):
    _fields_ = [
        ("dwSize", DWORD),
        ("szIP", C.c_char * 64),
        ("nPort", C.c_int),
        ("szUserName", C.c_char * 64),
        ("szPassword", C.c_char * 64),
        ("emSpecCap", C.c_int),
        ("byReserved", BYTE * 4),
        ("pCapParam", C.c_void_p),
        ("emTLSCap", C.c_int),
    ]


class NET_OUT_LOGIN_WITH_HIGHLEVEL_SECURITY(C.Structure):
    _fields_ = [
        ("dwSize", DWORD),
        ("stuDeviceInfo", NET_DEVICEINFO_Ex),
        ("nError", C.c_int),
        ("byReserved", BYTE * 132),
    ]


ERROR_NAMES = {
    0x00000000: "NET_NOERROR",
    0x80000064: "NET_LOGIN_ERROR_PASSWORD",
    0x80000065: "NET_LOGIN_ERROR_USER",
    0x80000066: "NET_LOGIN_ERROR_TIMEOUT",
    0x80000067: "NET_LOGIN_ERROR_RELOGIN",
    0x80000068: "NET_LOGIN_ERROR_LOCKED",
    0x80000069: "NET_LOGIN_ERROR_BLACKLIST",
    0x8000006A: "NET_LOGIN_ERROR_BUSY",
    0x8000006B: "NET_LOGIN_ERROR_CONNECT",
    0x8000006C: "NET_LOGIN_ERROR_NETWORK",
    0x8000006D: "NET_LOGIN_ERROR_SUBCONNECT",
    0x8000006E: "NET_LOGIN_ERROR_MAXCONNECT",
}


class Dahua37777Error(RuntimeError):
    pass


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    media_ok: bool
    channels: int = 0
    sdk_error: int = 0
    message: str = ""


@dataclass(frozen=True)
class DvripAuthResult:
    ok: bool
    error_code: str = ""
    auth_code: str = ""
    session_id: int = 0
    message: str = ""


_RUNTIME_LOCK = threading.RLock()
_RUNTIME_DLL = None
_RUNTIME_DLL_DIR_HANDLE = None
_RUNTIME_INITIALIZED = False


def _candidate_sdk_dirs(base_dir: Optional[str] = None):
    seen = set()
    for value in (
        os.environ.get("DAHUA_NETSDK_DIR"),
        # ``from_camera`` passes the resolved SDK directory back into the
        # loader. Accept that form as well as the application-root form.
        base_dir if base_dir and os.path.isfile(os.path.join(base_dir, "dhnetsdk.dll")) else None,
        os.path.join(base_dir or os.path.dirname(__file__), "vendor", "dahua_netsdk"),
        os.path.join(base_dir or os.path.dirname(__file__), "netsdk"),
    ):
        if not value:
            continue
        path = os.path.abspath(value)
        if path not in seen:
            seen.add(path)
            yield path


def find_netsdk_dir(base_dir: Optional[str] = None) -> Optional[str]:
    for path in _candidate_sdk_dirs(base_dir):
        if os.path.isfile(os.path.join(path, "dhnetsdk.dll")):
            return path
    return None


def netsdk_available(base_dir: Optional[str] = None) -> bool:
    return find_netsdk_dir(base_dir) is not None


def _configure_api(dll):
    dll.CLIENT_Init.restype = BOOL
    dll.CLIENT_Init.argtypes = [C.c_void_p, C.c_void_p]
    dll.CLIENT_Cleanup.restype = None
    dll.CLIENT_Cleanup.argtypes = []
    dll.CLIENT_GetLastError.restype = DWORD
    dll.CLIENT_GetLastError.argtypes = []
    dll.CLIENT_SetConnectTime.restype = None
    dll.CLIENT_SetConnectTime.argtypes = [C.c_int, C.c_int]
    dll.CLIENT_LoginWithHighLevelSecurity.restype = LLONG
    dll.CLIENT_LoginWithHighLevelSecurity.argtypes = [C.c_void_p, C.c_void_p]
    dll.CLIENT_Logout.restype = BOOL
    dll.CLIENT_Logout.argtypes = [LLONG]
    dll.CLIENT_RealPlayEx.restype = LLONG
    dll.CLIENT_RealPlayEx.argtypes = [LLONG, C.c_int, C.c_void_p, C.c_int]
    dll.CLIENT_SetRealDataCallBackEx2.restype = BOOL
    dll.CLIENT_SetRealDataCallBackEx2.argtypes = [LLONG, fRealData, C.c_void_p, DWORD]
    dll.CLIENT_StopRealPlayEx.restype = BOOL
    dll.CLIENT_StopRealPlayEx.argtypes = [LLONG]
    dll.CLIENT_SaveRealData.restype = BOOL
    dll.CLIENT_SaveRealData.argtypes = [LLONG, C.c_char_p]
    dll.CLIENT_StopSaveRealData.restype = BOOL
    dll.CLIENT_StopSaveRealData.argtypes = [LLONG]
    if hasattr(dll, "CLIENT_MakeKeyFrame"):
        dll.CLIENT_MakeKeyFrame.restype = BOOL
        dll.CLIENT_MakeKeyFrame.argtypes = [LLONG, C.c_int, C.c_int]


def _load_runtime(base_dir: Optional[str] = None):
    global _RUNTIME_DLL, _RUNTIME_DLL_DIR_HANDLE, _RUNTIME_INITIALIZED
    with _RUNTIME_LOCK:
        if _RUNTIME_DLL is not None:
            return _RUNTIME_DLL
        sdk_dir = find_netsdk_dir(base_dir)
        if not sdk_dir:
            raise Dahua37777Error("Khong tim thay Dahua NetSDK (dhnetsdk.dll).")
        if os.name != "nt":
            raise Dahua37777Error("Adapter NetSDK 37777 hien chi duoc dong goi cho Windows.")
        if hasattr(os, "add_dll_directory"):
            _RUNTIME_DLL_DIR_HANDLE = os.add_dll_directory(sdk_dir)
        try:
            dll = C.WinDLL(os.path.join(sdk_dir, "dhnetsdk.dll"))
        except OSError as exc:
            raise Dahua37777Error(f"Khong nap duoc Dahua NetSDK: {exc}") from exc
        _configure_api(dll)
        if not dll.CLIENT_Init(None, None):
            code = int(dll.CLIENT_GetLastError())
            raise Dahua37777Error(f"CLIENT_Init that bai (0x{code:08X}).")
        dll.CLIENT_SetConnectTime(5000, 2)
        _RUNTIME_DLL = dll
        _RUNTIME_INITIALIZED = True
        return dll


def _cleanup_runtime():
    global _RUNTIME_DLL, _RUNTIME_INITIALIZED, _RUNTIME_DLL_DIR_HANDLE
    with _RUNTIME_LOCK:
        if _RUNTIME_DLL is not None and _RUNTIME_INITIALIZED:
            try:
                _RUNTIME_DLL.CLIENT_Cleanup()
            except Exception:
                pass
        _RUNTIME_DLL = None
        _RUNTIME_INITIALIZED = False
        if _RUNTIME_DLL_DIR_HANDLE is not None:
            try:
                _RUNTIME_DLL_DIR_HANDLE.close()
            except Exception:
                pass
        _RUNTIME_DLL_DIR_HANDLE = None


atexit.register(_cleanup_runtime)


def _sdk_error(dll) -> tuple[int, str]:
    code = int(dll.CLIENT_GetLastError())
    return code, ERROR_NAMES.get(code, "NETSDK_ERROR")


def _bounded_ascii(value, field_name: str, maximum: int = 63) -> bytes:
    raw = str(value or "").encode("utf-8")
    if not raw or len(raw) > maximum:
        raise Dahua37777Error(f"{field_name} khong hop le cho NetSDK.")
    return raw


def _resolve_host(host: str) -> str:
    host = str(host or "").strip()
    if not host:
        raise Dahua37777Error("Thieu dia chi camera.")
    try:
        return socket.gethostbyname(host)
    except OSError as exc:
        raise Dahua37777Error("Khong phan giai duoc dia chi camera.") from exc


def _dahua_gen1_hash(password: str) -> str:
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


def _dahua_dvrip_md5(random_value: str, username: str, password: str) -> str:
    value = f"{username}:{random_value}:{_dahua_gen1_hash(password)}"
    return hashlib.md5(value.encode("latin-1")).hexdigest().upper()


def _dahua_gen2_md5(random_value: str, realm: str, username: str, password: str) -> str:
    password_db = hashlib.md5(f"{username}:{realm}:{password}".encode("latin-1")).hexdigest().upper()
    value = f"{username}:{random_value}:{password_db}"
    return hashlib.md5(value.encode("latin-1")).hexdigest().upper()


def _recv_exact(sock: socket.socket, length: int) -> bytes:
    chunks = []
    remaining = int(length)
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise Dahua37777Error("Ket noi DVRIP dong truoc khi nhan du du lieu.")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _recv_dvrip_packet(sock: socket.socket) -> tuple[bytes, bytes]:
    header = _recv_exact(sock, 32)
    if header[:2] not in {b"\xb0\x00", b"\xb0\x01", b"\xf6\x00", b"\xa0\x01", b"\xa0\x05"}:
        raise Dahua37777Error(f"Phan hoi DVRIP khong hop le ({header[:2].hex()}).")
    payload_len = struct.unpack("<I", header[4:8])[0]
    if payload_len > 4 * 1024 * 1024:
        raise Dahua37777Error("Phan hoi DVRIP vuot gioi han an toan.")
    payload = _recv_exact(sock, payload_len) if payload_len else b""
    return header, payload


def probe_dvrip_auth(
    host: str,
    username: str,
    password: str,
    port: int = 37777,
    timeout: float = 5.0,
) -> DvripAuthResult:
    """Perform Dahua's normal DVRIP realm/challenge login on TCP 37777.

    This is an authentication diagnostic only. It never stores or returns the
    password or derived challenge hashes.
    """
    resolved = _resolve_host(host)
    username = str(username or "")
    password = str(password if password is not None else "")
    port = int(port or 37777)
    if not username:
        return DvripAuthResult(False, message="Thieu tai khoan DVRIP.")
    if not 1 <= port <= 65535:
        return DvripAuthResult(False, message="Port DVRIP khong hop le.")

    realm_header = struct.pack(">I", 0xA0010000) + (b"\x00" * 20) + struct.pack(">Q", 0x050201010000A1AA)
    try:
        with socket.create_connection((resolved, port), timeout=float(timeout)) as sock:
            sock.settimeout(float(timeout))
            sock.sendall(realm_header)
            _, realm_payload = _recv_dvrip_packet(sock)
            text = realm_payload.decode("latin-1", errors="replace")
            fields = {}
            for line in text.splitlines():
                key, sep, value = line.partition(":")
                if sep:
                    fields[key.strip().lower()] = value.strip()
            realm = fields.get("realm")
            random_value = fields.get("random")
            if not realm or not random_value:
                return DvripAuthResult(False, message="DVRIP khong tra Realm/Random hop le.")

            challenge = (
                username
                + "&&"
                + _dahua_gen2_md5(random_value, realm, username, password)
                + _dahua_dvrip_md5(random_value, username, password)
            ).encode("latin-1")
            login_header = (
                struct.pack(">I", 0xA0050000)
                + struct.pack("<I", len(challenge))
                + (b"\x00" * 16)
                + struct.pack(">Q", 0x050200080000A1AA)
            )
            sock.sendall(login_header + challenge)
            response_header, _ = _recv_dvrip_packet(sock)
            error_code = response_header[8:12].hex()
            auth_code = response_header[28:32].hex()
            session_id = struct.unpack("<I", response_header[16:20])[0]
            status = error_code[:4]
            status_messages = {
                "0008": "DVRIP 37777 dang nhap thanh cong.",
                "0100": "DVRIP 37777 tu choi mat khau.",
                "0101": "DVRIP 37777 tu choi tai khoan.",
                "0104": "Tai khoan DVRIP 37777 dang bi khoa.",
                "0105": "DVRIP 37777 tra ma xac thuc khong xac dinh.",
                "0113": "DVRIP 37777 khong ho tro kieu dang nhap nay.",
                "0303": "Tai khoan DVRIP 37777 da co phien ket noi.",
            }
            ok = status == "0008"
            return DvripAuthResult(
                ok=ok,
                error_code=error_code,
                auth_code=auth_code,
                session_id=session_id if ok else 0,
                message=status_messages.get(status, f"DVRIP 37777 tra ma xac thuc {status or 'unknown'}."),
            )
    except (OSError, Dahua37777Error) as exc:
        return DvripAuthResult(False, message=f"Khong the xac thuc DVRIP 37777: {exc}")


class Dahua37777Adapter:
    """Thin, thread-safe per-session facade around Dahua NetSDK."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: int = 37777,
        channel: int = 0,
        stream: str = "main",
        sdk_dir: Optional[str] = None,
    ):
        self.host = _resolve_host(host)
        self.username = str(username or "")
        self.password = str(password or "")
        self.port = int(port or 37777)
        self.channel = max(0, int(channel or 0))
        self.stream = str(stream or "main").strip().lower()
        self.sdk_dir = sdk_dir
        if not 1 <= self.port <= 65535:
            raise Dahua37777Error("Port NetSDK nam ngoai pham vi 1-65535.")
        if not self.username or self.password is None:
            raise Dahua37777Error("Thieu tai khoan camera.")
        self.dll = _load_runtime(sdk_dir)

    @classmethod
    def from_camera(cls, camera: dict, base_dir: Optional[str] = None):
        # Config/UI channel numbers are 1-based; NetSDK expects a 0-based index.
        try:
            configured_channel = int(
                camera.get("netsdk_channel")
                or camera.get("rtsp_channel")
                or camera.get("channel")
                or 1
            )
            channel = max(0, configured_channel - 1)
        except (TypeError, ValueError):
            channel = 0
        raw_view = str(camera.get("view_stream") or "").strip().lower()
        if raw_view in {"main", "sub"}:
            resolved_stream = raw_view
        elif raw_view == "auto":
            candidate = str(camera.get("stream") or "").strip().lower()
            resolved_stream = candidate if candidate in {"main", "sub"} else "main"
        else:
            raw_legacy = str(camera.get("netsdk_stream") or "").strip().lower()
            if raw_legacy in {"main", "sub"}:
                resolved_stream = raw_legacy
            else:
                candidate = str(camera.get("stream") or "").strip().lower()
                resolved_stream = candidate if candidate in {"main", "sub"} else "main"

        return cls(
            host=camera.get("ip") or camera.get("host"),
            username=camera.get("user") or camera.get("username"),
            password=camera.get("pass") if camera.get("pass") is not None else camera.get("password", ""),
            port=camera.get("netsdk_port") or camera.get("private_port") or 37777,
            channel=channel,
            stream=resolved_stream,
            sdk_dir=find_netsdk_dir(base_dir),
        )

    def _login(self):
        pin = NET_IN_LOGIN_WITH_HIGHLEVEL_SECURITY()
        pin.dwSize = C.sizeof(NET_IN_LOGIN_WITH_HIGHLEVEL_SECURITY)
        pin.szIP = _bounded_ascii(self.host, "Dia chi camera")
        pin.nPort = self.port
        pin.szUserName = _bounded_ascii(self.username, "Tai khoan")
        pin.szPassword = _bounded_ascii(self.password, "Mat khau")
        pin.emSpecCap = 0
        pin.pCapParam = None

        pout = NET_OUT_LOGIN_WITH_HIGHLEVEL_SECURITY()
        pout.dwSize = C.sizeof(NET_OUT_LOGIN_WITH_HIGHLEVEL_SECURITY)
        handle = int(self.dll.CLIENT_LoginWithHighLevelSecurity(C.byref(pin), C.byref(pout)))
        if not handle:
            code, name = _sdk_error(self.dll)
            raise Dahua37777Error(f"Dang nhap NetSDK that bai: {name} (0x{code:08X}, detail={pout.nError}).")
        return handle, pout

    def _realplay_type(self) -> int:
        # Dahua enum: 0 = main realplay, 3 = sub stream 1.
        return 3 if self.stream in {"sub", "extra", "extra1", "1"} else 0

    def _open_realplay(self, login_handle: int, real_type: Optional[int] = None) -> int:
        rtype = self._realplay_type() if real_type is None else int(real_type)
        real = int(self.dll.CLIENT_RealPlayEx(LLONG(login_handle), self.channel, None, rtype))
        if not real:
            code, name = _sdk_error(self.dll)
            raise Dahua37777Error(f"NetSDK RealPlay that bai: {name} (0x{code:08X}).")
        return real

    def probe(self, require_media: bool = True, media_seconds: float = 2.0) -> ProbeResult:
        login = 0
        real = 0
        temp_path = None
        try:
            login, out = self._login()
            channels = int(out.stuDeviceInfo.nChanNum)
            if not require_media:
                return ProbeResult(True, False, channels=channels, message="Dang nhap NetSDK 37777 thanh cong.")
            real = self._open_realplay(login)
            fd, temp_path = tempfile.mkstemp(prefix="cambida_netsdk_probe_", suffix=".dav")
            os.close(fd)
            try:
                os.remove(temp_path)
            except OSError:
                pass
            if not self.dll.CLIENT_SaveRealData(LLONG(real), os.fsencode(temp_path)):
                code, name = _sdk_error(self.dll)
                raise Dahua37777Error(f"NetSDK khong luu duoc du lieu RealPlay: {name} (0x{code:08X}).")
            time.sleep(max(0.5, float(media_seconds)))
            self.dll.CLIENT_StopSaveRealData(LLONG(real))
            media_ok = os.path.isfile(temp_path) and os.path.getsize(temp_path) > 1024
            if not media_ok:
                raise Dahua37777Error("Dang nhap 37777 thanh cong nhung chua nhan duoc media RealPlay.")
            return ProbeResult(True, True, channels=channels, message="NetSDK 37777 da nhan duoc luong video.")
        except Dahua37777Error as exc:
            code, _ = _sdk_error(self.dll)
            return ProbeResult(False, False, sdk_error=code, message=str(exc))
        finally:
            if real:
                try:
                    self.dll.CLIENT_StopSaveRealData(LLONG(real))
                except Exception:
                    pass
                try:
                    self.dll.CLIENT_StopRealPlayEx(LLONG(real))
                except Exception:
                    pass
            if login:
                try:
                    self.dll.CLIENT_Logout(LLONG(login))
                except Exception:
                    pass
            if temp_path:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    def iter_jpeg_frames(self, ffmpeg_path: str, fps: float = 10.0, frame_timeout: float = 12.0):
        """Yield JPEG frames from NetSDK RealPlay without routing through RTSP."""
        if not os.path.isfile(ffmpeg_path):
            raise Dahua37777Error("Khong tim thay ffmpeg.exe cho preview NetSDK.")

        login = 0
        real = 0
        proc = None
        stop = threading.Event()
        raw_queue = queue.Queue(maxsize=128)
        frame_queue = queue.Queue(maxsize=3)
        worker_threads = []

        def push_latest(target, value):
            try:
                target.put_nowait(value)
                return
            except queue.Full:
                pass
            try:
                target.get_nowait()
            except queue.Empty:
                pass
            try:
                target.put_nowait(value)
            except queue.Full:
                pass

        @fRealData
        def on_data(_real, _data_type, pbuffer, size, _user):
            if stop.is_set() or not pbuffer or int(size) <= 0:
                return
            try:
                push_latest(raw_queue, C.string_at(pbuffer, int(size)))
            except Exception:
                return

        def writer():
            while not stop.is_set():
                try:
                    chunk = raw_queue.get(timeout=0.5)
                except queue.Empty:
                    if proc is not None and proc.poll() is not None:
                        return
                    continue
                if chunk is None:
                    return
                try:
                    proc.stdin.write(chunk)
                    proc.stdin.flush()
                except (BrokenPipeError, OSError, ValueError):
                    return

        def reader():
            buffer = bytearray()
            while not stop.is_set():
                try:
                    chunk = proc.stdout.read(8192)
                except (OSError, ValueError):
                    return
                if not chunk:
                    return
                buffer.extend(chunk)
                while True:
                    start = buffer.find(b"\xff\xd8")
                    if start < 0:
                        if len(buffer) > 2_000_000:
                            del buffer[:-2]
                        break
                    end = buffer.find(b"\xff\xd9", start + 2)
                    if end < 0:
                        if start:
                            del buffer[:start]
                        break
                    frame = bytes(buffer[start:end + 2])
                    del buffer[:end + 2]
                    push_latest(frame_queue, frame)

        try:
            login, _ = self._login()
            command = [
                ffmpeg_path,
                "-hide_banner",
                "-loglevel", "error",
                "-probesize", "4096",
                "-analyzeduration", "0",
                "-flags", "low_delay",
                "-f", "dhav",
                "-i", "pipe:0",
                "-an",
                "-q:v", "5",
                "-vcodec", "mjpeg",
                "-f", "image2pipe",
                "pipe:1",
            ]
            proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )

            worker_threads = [
                threading.Thread(target=writer, name="netsdk-dhav-writer", daemon=True),
                threading.Thread(target=reader, name="netsdk-jpeg-reader", daemon=True),
            ]
            for thread in worker_threads:
                thread.start()

            real = self._open_realplay(login)
            if not self.dll.CLIENT_SetRealDataCallBackEx2(LLONG(real), on_data, None, 0x1F):
                code, name = _sdk_error(self.dll)
                raise Dahua37777Error(f"NetSDK callback RealPlay that bai: {name} (0x{code:08X}).")

            if hasattr(self.dll, "CLIENT_MakeKeyFrame"):
                sub_channel = 1 if self.stream in {"sub", "extra", "extra1", "1"} else 0
                try:
                    self.dll.CLIENT_MakeKeyFrame(LLONG(login), self.channel, sub_channel)
                except Exception:
                    pass

            while True:
                try:
                    frame = frame_queue.get(timeout=max(1.0, float(frame_timeout)))
                except queue.Empty:
                    if proc.poll() is not None:
                        raise Dahua37777Error("FFmpeg dung khi dang giai ma preview NetSDK.")
                    raise Dahua37777Error("Qua thoi gian cho khung hinh NetSDK 37777.")
                if frame:
                    yield frame
        finally:
            stop.set()
            if real:
                try:
                    self.dll.CLIENT_StopRealPlayEx(LLONG(real))
                except Exception:
                    pass
            try:
                raw_queue.put_nowait(None)
            except queue.Full:
                pass
            if proc is not None:
                try:
                    if proc.stdin:
                        proc.stdin.close()
                except Exception:
                    pass
                try:
                    proc.terminate()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass
            for thread in worker_threads:
                thread.join(timeout=0.5)
            if login:
                try:
                    self.dll.CLIENT_Logout(LLONG(login))
                except Exception:
                    pass

    def capture_jpeg(self, ffmpeg_path: str, timeout: float = 12.0) -> bytes:
        frames = self.iter_jpeg_frames(ffmpeg_path, fps=2.0, frame_timeout=timeout)
        try:
            return next(frames)
        finally:
            frames.close()

    def record_segment(self, output_path: str, duration: float, ffmpeg_path: str, stop_event=None) -> dict:
        output_path = os.path.abspath(output_path)
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        dav_path = output_path + ".netsdk.dav"
        login = 0
        real = 0
        started = time.monotonic()
        try:
            login, _ = self._login()
            # Recording stream is ALWAYS Main stream (Dahua RealPlay type 0).
            real = self._open_realplay(login, real_type=0)
            try:
                os.remove(dav_path)
            except OSError:
                pass
            if not self.dll.CLIENT_SaveRealData(LLONG(real), os.fsencode(dav_path)):
                code, name = _sdk_error(self.dll)
                raise Dahua37777Error(f"NetSDK SaveRealData that bai: {name} (0x{code:08X}).")
            while time.monotonic() - started < float(duration):
                if stop_event is not None and stop_event.wait(0.25):
                    break
                if stop_event is None:
                    time.sleep(0.25)
            self.dll.CLIENT_StopSaveRealData(LLONG(real))
            if not os.path.isfile(dav_path) or os.path.getsize(dav_path) <= 1024:
                raise Dahua37777Error("NetSDK khong tao duoc du lieu video hop le.")
            mode = self._dav_to_mp4(dav_path, output_path, ffmpeg_path)
            if not os.path.isfile(output_path) or os.path.getsize(output_path) <= 0:
                raise Dahua37777Error("FFmpeg khong tao duoc MP4 tu luong NetSDK.")
            return {
                "ok": True,
                "mode": mode,
                "bytes": os.path.getsize(output_path),
                "duration": time.monotonic() - started,
            }
        finally:
            if real:
                try:
                    self.dll.CLIENT_StopSaveRealData(LLONG(real))
                except Exception:
                    pass
                try:
                    self.dll.CLIENT_StopRealPlayEx(LLONG(real))
                except Exception:
                    pass
            if login:
                try:
                    self.dll.CLIENT_Logout(LLONG(login))
                except Exception:
                    pass
            try:
                os.remove(dav_path)
            except OSError:
                pass

    @staticmethod
    def _dav_to_mp4(dav_path: str, output_path: str, ffmpeg_path: str) -> str:
        common = [ffmpeg_path, "-hide_banner", "-loglevel", "error", "-y", "-i", dav_path]
        copy_cmd = common + ["-map", "0:v:0", "-map", "0:a?", "-c", "copy", "-movflags", "+faststart", "-f", "mp4", output_path]
        copy = subprocess.run(copy_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if copy.returncode == 0 and os.path.isfile(output_path) and os.path.getsize(output_path) > 0:
            return "copy"
        try:
            os.remove(output_path)
        except OSError:
            pass
        encode_cmd = common + ["-map", "0:v:0", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-movflags", "+faststart", "-f", "mp4", output_path]
        enc = subprocess.run(encode_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if enc.returncode == 0 and os.path.isfile(output_path) and os.path.getsize(output_path) > 0:
            return "reencode"
        detail = (enc.stderr or copy.stderr or "").strip().splitlines()[-1:] or ["unknown ffmpeg error"]
        raise Dahua37777Error(f"Khong chuyen duoc DHAV sang MP4: {detail[0]}")
