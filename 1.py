"""Best-effort source recovery from 1.exe (PyInstaller / CPython 3.13).

Control flow and constants were reconstructed from the original code object's
bytecode. Original comments, whitespace, and a few stylistic choices cannot be
recovered from compiled bytecode.
"""

import atexit
import base64
import ctypes
import glob
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
import zlib
try:
    import winreg
except ImportError:
    winreg = None
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps
from io import BytesIO
from logging.handlers import RotatingFileHandler
from urllib.parse import quote, unquote, urlparse
from xml.etree import ElementTree as ET

import cv2
import pystray
import requests
from requests.auth import HTTPDigestAuth
from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    render_template_string,
    request,
    session,
    send_file,
    send_from_directory,
    stream_with_context,
    url_for,
)
from PIL import Image, ImageDraw

from dahua_37777 import Dahua37777Adapter, Dahua37777Error, netsdk_available

try:
    import qrcode
except ImportError:
    qrcode = None


if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
    BUNDLE_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
else:
    BASE_DIR = os.path.abspath(os.path.dirname(__file__))
    BUNDLE_DIR = BASE_DIR

def _show_error_box(title, message):
    try:
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x00000010)
    except Exception:
        pass


FFMPEG_PATH = (
    os.path.join(BASE_DIR, "ffmpeg.exe")
    if os.path.isfile(os.path.join(BASE_DIR, "ffmpeg.exe"))
    else os.path.join(BUNDLE_DIR, "ffmpeg.exe")
)
INSTANCE_MUTEX_NAME = "Local\\CambiDa_CCTV_Recorder_SingleInstance"
# Lưu trên thư mục Temp chuẩn của hệ điều hành để bản mở sau biết PID độc lập mọi ổ đĩa
INSTANCE_STATE_FILE = os.path.join(tempfile.gettempdir(), "cctv_recorder_instance.json")
INSTANCE_MUTEX_HANDLE = None

CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
EMBEDDED_CONFIG_FILE = os.path.join(BUNDLE_DIR, "config.release.json")

def _load_app_version():
    candidates = [
        os.path.join(BASE_DIR, "RELEASE_VERSION.txt"),
        os.path.join(BUNDLE_DIR, "RELEASE_VERSION.txt"),
    ]
    for path in candidates:
        try:
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    ver = f.read().strip()
                    if ver:
                        return ver
        except OSError:
            pass
    return "2.1.2"

APP_VERSION = _load_app_version()

def _ensure_first_run_files():
    if not os.path.exists(CONFIG_FILE) and os.path.isfile(EMBEDDED_CONFIG_FILE):
        created_config = False
        try:
            with open(EMBEDDED_CONFIG_FILE, "r", encoding="utf-8-sig") as source:
                initial_config = json.load(source)
            if not isinstance(initial_config, dict):
                raise ValueError("Cấu hình phát hành mặc định không hợp lệ.")
            initial_config["admin_session_secret"] = secrets.token_hex(32)
            try:
                target = open(CONFIG_FILE, "x", encoding="utf-8")
            except FileExistsError:
                return
            created_config = True
            with target:
                json.dump(initial_config, target, ensure_ascii=False, indent=2)
                target.write("\n")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            if created_config:
                try:
                    os.remove(CONFIG_FILE)
                except OSError:
                    pass
            _show_error_box(
                "Lỗi khởi tạo CCTV",
                f"Không thể tự tạo file config.json trong thư mục:\n{BASE_DIR}\n\n{exc}",
            )
            sys.exit(1)

_ensure_first_run_files()


_CAMERA_IDENTITY_KEYS = ("name", "id", "camera_id", "uuid")
_LOCAL_RTSP_ONLY_KEYS = frozenset(
    {
        "port",
        "record_path",
        "preview_path",
        "record_rtsp_url",
        "preview_rtsp_url",
        "vendor",
        "rtsp_channel",
        "channel",
    }
)
_LOCAL_NETSDK_ONLY_KEYS = frozenset(
    {"netsdk_port", "netsdk_channel", "netsdk_stream"}
)
_NVR_ONLY_KEYS = frozenset(
    {
        "host",
        "http_port",
        "rtsp_port",
        "nvr_channel",
        "stream",
        "backup_local",
        "timezone_offset_minutes",
        "connect_timeout_sec",
        "read_timeout_sec",
        "playback_chunk_sec",
        "max_search_pages",
        "use_https",
        "verify_tls",
        "nvr_vendor",
        "nvr",
    }
)
_LEGACY_CAMERA_KEYS = frozenset({"username", "password", "channel", "nvr_vendor", "nvr"})


def _coerce_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _camera_value_present(camera, key):
    return key in camera and camera.get(key) not in (None, "")


def _normalise_camera_entry(camera, index):
    """Return one canonical, transport-disjoint camera entry.

    This is intentionally a whitelist rather than an in-place cleanup. A mode
    switch must not carry fields owned by the previous transport into the next
    saved payload or runtime object.
    """
    if not isinstance(camera, dict):
        return None

    source = str(camera.get("playback_source", "local")).strip().lower()
    source = source if source in {"local", "nvr"} else "local"
    result = {key: camera[key] for key in _CAMERA_IDENTITY_KEYS if key in camera}
    result["playback_source"] = source
    view_stream = str(
        camera.get("view_stream")
        or camera.get("preview_stream")
        or ""
    ).strip().lower()
    if view_stream not in {"auto", "main", "sub"}:
        legacy_netsdk = str(camera.get("netsdk_stream") or "").strip().lower()
        if legacy_netsdk in {"main", "sub"}:
            view_stream = legacy_netsdk
        else:
            view_stream = "auto"
    result["view_stream"] = view_stream
    nested_nvr = camera.get("nvr") if isinstance(camera.get("nvr"), dict) else {}

    if source == "nvr":
        vendor = str(
            camera.get("vendor")
            or camera.get("nvr_vendor")
            or nested_nvr.get("vendor")
            or "hikvision"
        ).strip().lower()
        if vendor not in {"hikvision", "dahua"}:
            vendor = "hikvision"
        host = str(
            camera.get("host")
            or camera.get("ip")
            or nested_nvr.get("host")
            or ""
        ).strip()
        username = str(
            camera.get("user")
            or camera.get("username")
            or nested_nvr.get("username")
            or ""
        ).strip()
        if camera.get("pass") is not None:
            password = str(camera.get("pass"))
        elif camera.get("password") is not None:
            password = str(camera.get("password"))
        else:
            password = str(nested_nvr.get("password") or "")
        channel = _coerce_int(
            camera.get("nvr_channel")
            or camera.get("channel")
            or nested_nvr.get("channel")
            or index,
            index,
        )
        result.update(
            {
                "vendor": vendor,
                "host": host,
                "http_port": _coerce_int(
                    camera.get("http_port")
                    or nested_nvr.get("http_port")
                    or (81 if vendor == "dahua" else 80),
                    81 if vendor == "dahua" else 80,
                ),
                "rtsp_port": _coerce_int(
                    camera.get("rtsp_port")
                    or camera.get("port")
                    or nested_nvr.get("rtsp_port")
                    or 554,
                    554,
                ),
                "user": username,
                "pass": password,
                "nvr_channel": max(1, channel),
                "stream": "sub"
                if str(camera.get("stream") or nested_nvr.get("stream") or "main").strip().lower()
                == "sub"
                else "main",
                "backup_local": bool(camera.get("backup_local", False)),
                "timezone_offset_minutes": _coerce_int(
                    camera.get("timezone_offset_minutes")
                    or nested_nvr.get("timezone_offset_minutes")
                    or 420,
                    420,
                ),
                "connect_timeout_sec": max(
                    1,
                    _coerce_int(
                        camera.get("connect_timeout_sec")
                        or nested_nvr.get("connect_timeout_sec")
                        or 5,
                        5,
                    ),
                ),
                "read_timeout_sec": max(
                    5,
                    _coerce_int(
                        camera.get("read_timeout_sec")
                        or nested_nvr.get("read_timeout_sec")
                        or 30,
                        30,
                    ),
                ),
                "playback_chunk_sec": max(
                    30,
                    min(
                        3600,
                        _coerce_int(
                            camera.get("playback_chunk_sec")
                            or nested_nvr.get("playback_chunk_sec")
                            or 300,
                            300,
                        ),
                    ),
                ),
                "use_https": bool(camera.get("use_https", nested_nvr.get("use_https", False))),
                "verify_tls": bool(camera.get("verify_tls", nested_nvr.get("verify_tls", False))),
            }
        )
        if "max_search_pages" in camera or "max_search_pages" in nested_nvr:
            result["max_search_pages"] = max(
                1,
                min(
                    50,
                    _coerce_int(
                        camera.get("max_search_pages")
                        or nested_nvr.get("max_search_pages")
                        or 12,
                        12,
                    ),
                ),
            )
        return result

    result["ip"] = str(camera.get("ip") or camera.get("host") or "").strip()
    result["user"] = str(camera.get("user") or camera.get("username") or "").strip()
    if camera.get("pass") is not None:
        result["pass"] = str(camera.get("pass"))
    elif camera.get("password") is not None:
        result["pass"] = str(camera.get("password"))
    else:
        result["pass"] = ""

    transport = str(camera.get("local_transport", "rtsp") or "rtsp").strip().lower()
    transport = transport if transport in {"rtsp", "netsdk"} else "rtsp"
    result["local_transport"] = transport
    if transport == "netsdk":
        result.update(
            {
                "netsdk_port": _coerce_int(camera.get("netsdk_port") or 37777, 37777),
                "netsdk_channel": max(
                    1, _coerce_int(camera.get("netsdk_channel") or 1, 1)
                ),
            }
        )
        return result

    result["port"] = _coerce_int(camera.get("port") or camera.get("rtsp_port") or 554, 554)
    result["record_path"] = str(
        camera.get("record_path") or "h264/ch1/main/av_stream"
    ).strip()
    result["preview_path"] = str(
        camera.get("preview_path") or "h264/ch1/sub/av_stream"
    ).strip()
    for key in ("record_rtsp_url", "preview_rtsp_url"):
        if _camera_value_present(camera, key):
            result[key] = str(camera[key]).strip()
    if _camera_value_present(camera, "vendor"):
        result["vendor"] = str(camera["vendor"]).strip().lower()
    rtsp_channel = camera.get("rtsp_channel")
    if rtsp_channel in (None, ""):
        rtsp_channel = camera.get("channel")
    if rtsp_channel not in (None, ""):
        result["rtsp_channel"] = _coerce_int(rtsp_channel, 1)
    return result


def migrate_config_data(raw_config):
    if not isinstance(raw_config, dict):
        return raw_config
    cfg = dict(raw_config)
    global_ps = cfg.get("playback_source")
    global_nvr = global_ps.get("nvr", {}) if isinstance(global_ps, dict) else {}
    global_channel_map = global_nvr.get("channel_map", {}) if isinstance(global_nvr, dict) else {}
    global_mode = str(global_ps.get("mode", "")).strip().lower() if isinstance(global_ps, dict) else ""

    cameras = cfg.get("cameras", [])
    if isinstance(cameras, list):
        migrated_cameras = []
        for index, cam in enumerate(cameras, start=1):
            if not isinstance(cam, dict):
                continue
            cam_entry = dict(cam)
            explicit_mode = str(cam_entry.get("playback_source", "")).strip().lower()

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

            if cam_entry["playback_source"] == "nvr":
                if not cam_entry.get("nvr_channel"):
                    cam_entry["nvr_channel"] = global_channel_map.get(str(index), index)

                legacy_shared_nvr = bool(global_nvr.get("host")) and not str(cam.get("host") or "").strip()
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

            canonical = _normalise_camera_entry(cam_entry, index)
            if canonical is not None:
                migrated_cameras.append(canonical)
        cfg["cameras"] = migrated_cameras
    cfg.pop("playback_source", None)
    return cfg


def clean_config_for_saving(candidate):
    cfg = dict(candidate)
    cfg.pop("retention_days", None)
    cfg.pop("playback_source", None)
    cameras = cfg.get("cameras", [])
    cfg["cameras"] = [
        canonical
        for index, camera in enumerate(cameras if isinstance(cameras, list) else [], start=1)
        if (canonical := _normalise_camera_entry(camera, index)) is not None
    ]
    return cfg


try:
    with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
        CONFIG = migrate_config_data(json.load(f))
except FileNotFoundError:
    _show_error_box(
        "Lỗi khởi động CCTV",
        f"Không tìm thấy file 'config.json' và không có cấu hình mặc định trong EXE:\n{BASE_DIR}",
    )
    sys.exit(1)
except json.JSONDecodeError as e:
    _show_error_box(
        "Lỗi file cấu hình",
        f"File 'config.json' bị lỗi định dạng JSON:\n{e}",
    )
    sys.exit(1)


if not os.path.exists(FFMPEG_PATH):
    _show_error_box(
        "Thiếu FFmpeg",
        "Không tìm thấy file ffmpeg.exe cạnh ứng dụng hoặc trong gói chạy. Hãy đặt lại file ffmpeg.exe.",
    )
    sys.exit(1)



DEFAULT_SITE = {
    "name": "HỆ THỐNG CCTV",
    "tagline": "Xem lại camera",
    "theme": {
        "background": "#2c0b0b",
        "surface": "#660000",
        "primary": "#c1121f",
        "accent": "#ffa500",
        "text": "#ffffff",
    },
}


def config_path(value, default):
    """Resolve a config path relative to the program folder."""
    path = value or default
    return path if os.path.isabs(path) else os.path.join(BASE_DIR, path)


def _show_restart_prompt():
    """Return True only when the operator explicitly asks to restart."""
    try:
        result = ctypes.windll.user32.MessageBoxW(
            None,
            "Hệ thống CCTV đang chạy.\n\n"
            "Bạn có muốn dừng bản đang chạy và khởi động lại không?\n"
            "Chọn No để hủy, không mở thêm ứng dụng.",
            "CCTV đang chạy",
            0x00000004 | 0x00000020 | 0x00000100,  # Yes/No, warning, No mặc định
        )
        return result == 6  # IDYES
    except Exception:
        return False


def _read_instance_pid():
    try:
        with open(INSTANCE_STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        pid = int(data.get("pid", 0))
        return pid if pid > 0 else None
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def _write_instance_state():
    try:
        with open(INSTANCE_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"pid": os.getpid(), "base_dir": BASE_DIR}, f)
    except OSError:
        # Không làm ứng dụng dừng nếu ổ D: tạm thời không ghi được; mutex vẫn
        # ngăn chạy song song, chỉ không hỗ trợ nút Khởi động lại.
        pass


def _release_single_instance():
    global INSTANCE_MUTEX_HANDLE
    try:
        if _read_instance_pid() == os.getpid() and os.path.exists(INSTANCE_STATE_FILE):
            os.remove(INSTANCE_STATE_FILE)
    except OSError:
        pass
    if INSTANCE_MUTEX_HANDLE:
        try:
            ctypes.windll.kernel32.CloseHandle(INSTANCE_MUTEX_HANDLE)
        except Exception:
            pass
        INSTANCE_MUTEX_HANDLE = None


def _try_take_instance_mutex():
    global INSTANCE_MUTEX_HANDLE
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, INSTANCE_MUTEX_NAME)
    if not handle:
        return False
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        ctypes.windll.kernel32.CloseHandle(handle)
        return False
    INSTANCE_MUTEX_HANDLE = handle
    _write_instance_state()
    atexit.register(_release_single_instance)
    return True


def acquire_single_instance():
    """Prevent duplicate recorder processes before workers and FFmpeg start."""
    if _try_take_instance_mutex():
        return True
    # A second double-click reuses the running server and opens its existing
    # web entry point.  Never start a second recorder or kill the live one.
    _open_server_page()
    return False


def _server_url(port):
    return f"http://127.0.0.1:{int(port)}/"


def _wait_for_server(port, timeout=15, connector=None):
    connector = connector or socket.create_connection
    deadline = time.monotonic() + max(0, float(timeout))
    while True:
        try:
            with connector(("127.0.0.1", int(port)), timeout=0.5):
                return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)


def _open_server_page(port=None, wait_timeout=15, opener=None, connector=None):
    """Open the local web UI once the existing/new server accepts connections."""
    if port is None:
        port = CONFIG.get("server_port", 8000)
    if not _wait_for_server(port, wait_timeout, connector=connector):
        return False
    opener = opener or webbrowser.open
    try:
        return bool(opener(_server_url(port), new=0, autoraise=True))
    except Exception:
        logger.exception("Không thể mở trình duyệt mặc định cho máy chủ CCTV")
        return False


VIDEO_DIR = config_path(CONFIG.get("video_dir"), "cctv_videos")
LOG_DIR = config_path(CONFIG.get("log_dir"), "logs")
DB_PATH = config_path(CONFIG.get("db_path"), "analytics.db")
os.makedirs(VIDEO_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

CAMERA_LIST = CONFIG.get("cameras", [])
SIZE_LIMIT_GB = CONFIG.get("disk_limit_gb", 400)
RETENTION_DAYS = CONFIG.get("retention_days", 30)
DURATION = CONFIG.get("record_duration_sec", 300)
TIMEOUT = CONFIG.get("record_timeout_sec", 330)
MAX_MERGE_MINUTES = CONFIG.get("max_merge_minutes", 60)
# Keep this legacy flag for callers that still inspect it, but source selection is
# now always derived from each camera. NVR cameras must not be silently rewritten
# to the local RTSP archive.
RTSP_LOCAL_TIMELINE_ONLY = False
RTSP_PROFILE_CACHE = {}
RTSP_PROFILE_LOCK = threading.RLock()
CAM_LAST_SUCCESS = {}
CAM_LAST_START = {}
CAM_LAST_ERROR = {}
CAM_LAST_RECOVERY = {}
CAM_STOP_EVENTS = {}
CAM_WORKERS = {}
CAM_PROCESSES = {}
CAMERA_LOCK = threading.RLock()
VIDEO_VALIDITY_CACHE = {}
NVR_REFERENCE_CACHE = {}
NVR_REFERENCE_LOCK = threading.RLock()
NVR_MEDIA_LOCK = threading.RLock()
NVR_REFERENCE_TTL_SEC = 30 * 60
NVR_MEDIA_CACHE_MAX_AGE_SEC = 6 * 60 * 60
NVR_CACHE_DIR = os.path.join(BASE_DIR, "nvr_cache")
os.makedirs(NVR_CACHE_DIR, exist_ok=True)

TIMELINE_MAX_SECONDS = 24 * 60 * 60
TIMELINE_STATUS_VALUES = {
    "complete",
    "incomplete",
    "error",
    "recording",
    "pending",
    "missing",
    "failed",
}

logger = logging.getLogger("cctv_system")
logger.setLevel(logging.INFO)
log_path = os.path.join(LOG_DIR, "system.log")
file_handler = RotatingFileHandler(
    log_path, maxBytes=5_242_880, backupCount=3, encoding="utf-8"
)
file_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] (%(threadName)s) %(message)s")
)
console_handler = logging.StreamHandler()
console_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] (%(threadName)s) %(message)s")
)
logger.addHandler(file_handler)
logger.addHandler(console_handler)

TEMPLATE_DIR = (
    BASE_DIR
    if os.path.isfile(os.path.join(BASE_DIR, "index.html"))
    else BUNDLE_DIR
)
app = Flask(__name__, template_folder=TEMPLATE_DIR)
app.secret_key = CONFIG.get("admin_session_secret") or hashlib.sha256(
    (BASE_DIR + str(CONFIG.get("admin_auth", {}).get("password", ""))).encode()
).hexdigest()
app.config["TEMPLATES_AUTO_RELOAD"] = True

# Self-contained replay UI: includes the iPhone Photos save flow (prepared-file
# Web Share on secure contexts, plus same-tab inline MP4 fallback for HTTP/QR browsers).
_EMBEDDED_REPLAY_TEMPLATE_SHA256 = "47c6c9621efa6387d4908aff41da916d8d0f1bf3e7c6400a9b856a3d1639f086"
_EMBEDDED_REPLAY_TEMPLATE_B85 = (
    "c-rl~YmXbrl_2_kf&C9Dx?N_rk}Q(Nda=Z+E0xriu2tO{Rdu^P8bL~u$zoc3Ig>1@T*VIyf0z%8!S&!`a51>JSobvs1JAB8J2MLmR>ER{Rb&1~{gXW>UU?!i"
    "GMOxu+V-q%b&<@B=ZO>N9VhPGy8p%D$xog<a0au{aR1-`yF2*l4E^zGyL1+o*ptBT!KdTg83i-n=??rT4rcArmnWZ9c1q;U*dGO0D|kJfM6;69os4I}7)rhl"
    "d$U2i7o3INpu&E5PB;!{p+Bs|U4Iy~>z*U)Rr=wq-JP5Tk!X7s&W6GM#f8&__9OoVcH=A-&j0*p2Oh@ZEU1b{ch-4XRxO^Li?5E;iYAkpbHRT&FtFhyf}RJ#"
    "C}=su@N_WSlkX~(&S}f}UccGj?sv%3t{?TpEA~I~Y8Jei!Sh<bzTNPR=atcX7WCllR&%?#(=p%1^A5b*Y3w%qCLO|X4g>pMy|>-yH;m_%)5!0I09PFOIPfF&"
    "pj_YB>;<PDRNUIB^>>`w_dVH&Q?J#&@0v};lRl0-==C?X8L^k0Nz@A>c-`suI{kpWK8=E43@;ju?&fCdMWyFQuW<C6JGG5o>Me(1t>5c6)9+-Ly>7o7Y||0R"
    "H$fxV>DN+k$W$7AT5&p$rb7hg?#9MOK$qvWAC38Pc7l%aJn63=^f$WO=4-L!yNzb8Mgb5H{NChsB_6e$n$wuRadzOp=(OXP>zkg_*zlZY!*goYTGLI}8uhSN"
    "6KXVS8Pzr|)uu!L9EVziHHK;%S#@`{y65pMfKgWbDS$`(^qhNuQm>Bu?lJrQ8EjzBDII_2Jc%aG{V>L^O39NGfCBp&oK6Dg%SZ72>7+B6O+4peFgy!pVb}MZ"
    "yAkYj&x!qUT#17yl!&(AAD=oGPG|C_5{Ewz$ABARJ>k)wEP^!2gBQK?WDk%0=rn}&OHg4HjyZ8@IU5K-^`hf<U!6vi`554P=0|1R;t4DmF_15CU6WURn17}3"
    "kHX=(EWiOpAo)7zyb7Td7L7+hW(I5){urrS=*K~_1CXZ0-Dol#Ryx7JKMR2fy=gh)$vDU@1D*Tx*+d5$hX$MUAV3P%!Yv-34HVQm^Vw`NMoK%K!!r6%8b{fJ"
    "2Lo7!S*jqGkn@@O)5-vvWyG-}U^I(h4X1ttw8p3mHM{dD#zNCc2xLD}1)dGUUN0Cwhax?WL43;2t#E|&-WQGW(pA96WRb;k!X(Q`zW|jHyIpO`joGL0gTihZ"
    "E=>Jik1ZcCrEa;t1v}}s6O7NwvEL6WFmb;Uj)D1AW|OJwI;;RI$~LUMDC~#XWaK8DC@A!YL4u_CqtXkbpgRjEFm3?(d^BDGcN7TMKXpzfQ|EIibn0Vc&OJYJ"
    "K3x|8hM`x+{#gQK`*;9(PsbHtuA>+R6R;(f?@s+`%h@73Os+w*NgnbQ5c6+2$t!^vz_rgRo!L0G+#5TVrnQHHMj+pt!|*n1-#1=Sx<HqlVz5B~zE=+#yBnSK"
    "GMz?YvWMXj$S`)WDC|l4HaCTQ&c+e)l}^S7RJy(yI{{D)JE&R%!~-OlWKN9M*MP+He~d}!6#*H0-Dp1QB%sE%hCP!E2&ECW=Ba6M(iZ+4m}q^Yx=EISy?HGb"
    "XscFBfplY|S>N1Lk6#Y}a20@mH$c>V9r;rY!TZC>>z2dmg+8+6Z7>{$(>RPRP}fl`*$gNX0%Lkj_5eNtX2kYd0+bve7|2|25HL(9aw(%=2s`e~T)mMGRau{T"
    "{ENW3v6Tho9RlTHFq@&|1Mp-3sMM;oo?HY&E=8d+A)0`2w!wm2Y8@{UKG=)q=md5&vaw^EjGKX(?STFy0$|Rk(;(_5NS~frt-6z*Si-S!xUMmc?tI2UUWs7k"
    "KY-Z%bRN&b{&_`cGwEHQwfZ`HYHp?IUou4AB<yPo2CCxI5Q#r*{7Mwy9Fj>TL-0E>@QqnAgoyV}fR77E2_{(x_7&z3{G?pjr0Rl#S~zCtbA3*sb}O>Z`bIWQ"
    "uczrfJcLRx!LTQJccQ1bc`RP)IvG+QzIW~Pt_VKciM(94-ekr)NU^zX3zyq2$mFb3bS=aWMDr0YNzXZi;e-6(lux3ocmL3JX5rP_f0;V#&ey@n8D72pCUg@5"
    "8Ov%6?XlX0t?%yzyFtIdl{Mssb>*pyU}v)U12*<(inv(ek`SUqKf?cX<jRpa>S>Wf%nQU@!VA?FZCFsQmN~gbhKi9VV;EKdifYYF#=2Si@Qt{PiZNJH>?<O<"
    "_`@NP=7v>bp^%QxPEq+l2{#@@pkX3dZ6&K=T=Ba|L+$ux+pZF{hFM16t($Zi-w4eaMTQL|LrP<A?0QbU-UPLo>&zPs+n)L=>;)6&2~)eB&my!vsg1*xaT{q)"
    "afeKaN6-h<c#^e^ry#8W@<C(pYHNKr6ECw=-|y9H^__+t5JI`71_hU}jCps!)&=LGsjDGSW1m0gUxSopJo4jLm2l*r0+P#TI97j{7_OLL2pT>J*RP0nM`{+W"
    "ktL-q^z}KzCG^zK>JS#D63;+T9;N8Cwhlzw{C;{@c7~Jgt7L&YKgBichq&DU9YL@vxG>s+N_9PtVQ=)L2H-l(qFS!I=?awzhy!6-xP6V1MR(wj5z?x(dOeR("
    "77K<j8Dsh7dS;i%#2sTn^c9exOLh+thD1eHVL0)TsT2~0m^w}g`n44C4<@qpto_Mtc3Vh%JZFQ^pY1JOtm#Fdy!XT53^@~!!KmD%<`6;()w7>h!f_9Ey<H-)"
    "GLl{i&R}@4xnP98Pf-4zooCW`x&&Efo;|&EJPpTVN+6}qZ!;%o4<$opDyWbGInmxkJE!^sI`ocf?j<yO<cNGZqLx1f3dF&oCV=`#EUfQ<R(<qk<iUn1DMa@!"
    "rZQ2!FITqL4R-pQc^V*<O1N&McpXa58<N!#gTbbuJN+N8g7ZGIwU|s9bdw4GNr+E0nE~Y~Z*0|i!6}*?G$#24a5q#^fG8k$k4+W{noRE$Af)m7oZZXx7ESFQ"
    "259lu9w}tsNpEru%de-iAmY{QIXX)tcWZ0r_^rmWt-Y18wNp}#K&5&y?K@}pD~YU&-WU|d*z)A9SdKudDExdF2bCUmz<mr2h<T9Q3^A|9^i4}aoeeQN6MO@;"
    "9I$`pkEYkeO`&!#L(UIdRcdk5Tot3qc*3k~UWV7gLKvDB!mvZ!O$E?K!0<Itg$N|ESF0E(Hg(<0Vr9D?_;#;pNTWa(Y!J;*yQ_k5yBG+wg)9kNCb25~lwOzO"
    "0M^A03E-Ly!1bD`iflA(+_{-U32VhE;l>BcIBD~cT`*|4-;H%&r#+e*n4m_Tpawy)?cz=XPlBBRMSGEmZW$g08$NOOf|v2KKk3fn%2^nP9cmb+Luh3He$y??"
    "7A-JIycCo947p6E-|I2*rehFG=CO0=M?J&XMGRa*-N2G8v?I^nAmbL?rS?b~#o!jQY$~imTBjR<ZopK{m_Vfy%w7k?zOY<NrqDq#>w}$zgdQj%gE!AX)(xZX"
    "Fl{2O+vrZcxw?q(z515F(eyK=1GPUJfQ9ufK*#MSqfG>J4f~aKHa1UBAtT>*TG*#3g&{R=mJ6q0A4@#%<PAj^EHVs~)az5vTEf?1Kjgw;A-S*sx@=~83>rB+"
    "XDc`6!@f6{!dg?-ipbYlXrhReG{oQYBcc^?pN=%Q12~Zmqlbt*xluQkoi?OR6a{Q*;c#fNrZ<Z?aPmoIrj6Yc`7(U?nI_J7<nDbummmY;PKJ|oqoA6WxV1H^"
    "k>^8S>WF45#jWucYe?d<LbnSG8@irhO-Ygsi-5CKdMa#o)oqI-e;k6U$!s-NcJ?})L2(^f3;7Cl2`9(%m}6ne_Ys-{w8nX0=wrW63~HQdT0>}>&1KO!1wv?+"
    "Zr0T|<mzH~p)H(+O1UgM0iPY*{hhww@9r6~B%=WN8J8|eJPCiCBTAY8$_KI?1MM!JEC7vqmcTZoCE`!5kmuCgW>bG$IbSKH@%rwAg$dr-WHMZp4>BAG#=UEC"
    "!mS(_Y49VF9vkUM4>E!2jmM)D6Y^`>gF-=Z)j@p+P}PVdV@b&DFy@t45rz#G-l+%4q|hYCp|C_Pt68Kzi|JlT#FKf{#brJ{1=5nuYrfa7_c!~y%L3+#QsNX;"
    "vtzGP?)wb;Kzb%<4<4UISrgLZ<mH4q&CQM4ZU!s;UcVP??f6-EsU%<YsxHIMRor%*EzJCAmK95qKNgO9$%yiIhCo;Du1E)RSM{ccekT}aXl$#5UK)yAhRUcg"
    "T?~)ndSR+;gH8Ud^iESkuiV;=?DdF{9w72fZ$Rf%<e_2m)%2N!BD*=gZOvmV2JNwu9y%C?o@sC}D44cjIU9TQRn6J8=w%vPHU_tKjX0;~_U7i+?rKUj#o~_^"
    "=aeNW=EUhNC7H{&3Rx#9YZlNMQl?TfJgK(jW_PqKmMTMH&y0v%9!r(wU#{&G8fL5I_h&k2#2S`Ldj-@`DMF-u#9L|4oGsiy_r))ZJ2PeSY+40e>5rrefl!E2"
    "2@SSk7stM9%JpxI5Rp}2W+2n%(y-av8#bo|4RuSm{&A^X($5VNx$Dw0+h9H(Dcekwvd!<1l>K}%8Qti~v1P(Bc9^0Y>XfI=E8WO7JFh_9ikP<yeQkB~RxuZH"
    "CN?(R)ipZ`Qf>>DyB`-O+4k(0rINX~4V`Xxqr2Jf*`bJs1&Zy)Wu9JTi2F*U4RF?+lL-29%8y2qx%1dRhbA1D;$x<dFniqz9{6`HW;<?JC_sV}J9Kje-eOuB"
    "Xx;2{Z`o1TH)BzQ<?Lvl{2YRm#D+9Xf}|PbjoobvRsPyITck*q)%ShVY&XlWQ#6uq)e{H>PR4E4a?ZVNWLSEQu#(il1>(2P_ZTgW>LRF8gsWx5!WUq6mCZD_"
    "TXIg##3ez<YANJO5q#c$lf)9|ZCv3$DOQ@Fup?!yC$w||B$J=4<Zr|OFQ+5f#3yZ?W(2fw#A7YBgf(ORYPn85lCYtGvoHVDgZ(Un$TedgX;5nDd}u$blbWwN"
    "9exbfqPAEkvSpz(GNd9)E}>F!Fb#kWp}ovV__AAL`CbSzA~fhvo}B>1){TwtI~DBE^_=f@JH5@Ip8rxjw+ZUxTt<x^Y}LvL$_Map?7XtV=Y>Q^d|ibsv9+s="
    "{GU#u;uyQJB5x{iY_E_}Hk$-nrqk*KFs$I($^)CJwkm02DzSB005%ogcANf2XD55y@_X1D>*nzykp8fEp)^g?$5NR_!C4aYAlEai^q3vju!cyaSa%L2u+vJ9"
    "9@M^$jV;?ypzje~c@l%_hr>}ki^A#ksodsHK`PhC2`|{17BfdJ<6_E9m_axf=DaK2fcoHyvo!0hD96g4Y=&yE%;><_I_K(aepbPb1`VgeZ0xf)e&+b_xavj|"
    "^AsLuy}1no-QC8J-whmS(=yI_u8$F^qG$DqDYu@*^kAkEN~c)BW2D6~VAGa-rqRp`XWy=S&dw%Gzs7@|H<typ*J(hzf=+W4_9rJM!gJNuO+;XN^bOx?dr{v{"
    "miMWCgv&EYC}Ho3z0T{(Qh@fwb=I)p*#-r>%~cnX!wck_)KP~mdjxTY=q1zZu~d7&9vl5Fr^$ZobT+&2L(jWMg}+8Z!;D0K2C*Xrd7~?kkDMbA1+ff7>E5bV"
    "f^B0tnidwaRm^y|l!Yv}<>qS2&CP2<<O5B-3@|&EGdVjcEZw$vM{-OTd88_Z54)yH$|uL8BNSa>iUL;>c93?NrGs%#FT1s?l{FPjJRMe0^Rk>@=AyjK?7f+i"
    "dM!flHDG7Mu_97!?ujq+3_>##MKjF?9F9t1u$;U$Yk(s1fib=H+Q&6fE;pxU*oIRRHBT(1AkImH73Pf{SC9O>$F`?)=nsczSk6CPBzRqy7tV<a&CKA+E#;i8"
    "1hsynA6PkwCJFaCf!_~SJa)uFLAO5W*bzT7&?t~RCQX-<+$$R(JMQk{S+Gsr>!-Um)V4Fu!O4bgUQv3ACGFg@ESBlw$jFhU$#MGJBv`F;t2a{F%0%a3m*vtl"
    "Q|0J>C96Xz)s)~zqBRu0srph&SBU2wks_3a>ZEjLO;O#fSsD_Dj`V|1%m8jtpN%YaskKVA?nf+E^|~-3nX#m#`Domqhz)&s_4e;)JUfiD;dHOw{q;=CiY2N5"
    "Erco;ACkpN%Vd=oe*GbV;`O5b%tBH28Or%01)c6B9qT7fIBuEgDeUPMb;Emssaprp8u~G8gh4p$G3rm-)Dlq9fg7QUH)WVgEpma=cXR{QisS*X&j@t?3C)p{"
    ";iAdN7fA=PnLUV|qUKJu0q?RA#?G2^kNK6!VK`sBTDHS7j+Xj{21%pyH=6}Rz(3NlFzwhtLD*7rAlo#(pgW0ti`Z2>Yg>v*T9P27Pkc_J5YYIXsX^lVhvhm;"
    "r`gN!re0GQ-=MnqHv8f4Y;N|qQ{oim-dkD@_Dz%xH)<#wZt2d|9I&V}MauHS2+uf(<8ock(K`yFQyC74hN5fPD^1&N+9fAB{|DPQHa3gFlp-DL9OY^g?JNfa"
    "Q})6_nmOO}bO|sXYZ^_u0CFuyn0_!k`^2b_;+pn+`i&G8EE_=!NQw`d2+;*BIS%9&e(i^|Ri~JTN~x~&Rug%#z;?DA@cg)$DK)BTb3&|uApW>Z8GJScGD1P~"
    "{1zGKZLD2q9_~aphEYYKj3=Kw^P|wO$j9x{ESd+U=aywg(^d+p#uJpdfDn^w3GzXw*!7S7Xbd>!IS)Vpk375p;xq!hOStxHULu|28jWv8yiHEti@a#5EMb`3"
    "*^!qB7>liR@H^|IkcN+%S5I9J;uZ)5GEU_*?fA60Y2uTBv``oIobEOA$^&WkvPDZa8pVzTU*p`awp%uLgIXsyhfkp_89S++C8$k+IB7|)o{e_3WoVa|dZ3;%"
    ")@z2Ce$egpHqvL|f_h`4d)-4ZiLAl*9ftU?;tx|B`979-;*Ws{uE|`sQGgLh$yV+`naY%vv1WkR@IN^Q!1(3P4JohLiEImLuJrhGrlz@|^VN5g*fv|XZKFNZ"
    "ZyLE)dYY^}L+TX0p1o$(-MC1v>Y!m5Xz)~+wr{pl_L?1P>>+JUjixq;*@z_RXE$NFdM7H#n6}EZPw6qa*7!01jWlO%HETI5vQsc=OtzLnv9DIE6;c~?aj{N3"
    "Z#l`FZ(8eIqycNDs4g6WS2&+>&32McH#VqyhL{UM*!EgZFX;R8;mj6-VD%fY^|cf(RLB-VlO0MRWVjeq?{QDldliu*F`pueZs`GUOTvlOWJ)!jR9Jq2<K~*F"
    "hw_N<i(}`9b3hm89&j1O7Q#tfImO31^U*lNj<ykVcWC<9hhsb3U%+XR2gj8Uf#}&U2<yP%n(~K&2wsyIL?3)OW;-iwknLudjjdh3UMN9DhCL}gjd&JJ;}1M+"
    "pPT<ER|4M5SHkSWy@}Co*yNcVGfhg46mso@MsiMKU*)G!(}^fYCLnWttEN)We7s}JI%V4GO+h9LDBM?>#?;16lVpdu&J92t*2|slpO5`f*abN@4m;rxQ(iuu"
    "4+El=8p({G_57J%K@-YkJUoByWu!b78dlb8s&-_R(FOHv-#o9^0@b`ME4$X5%gdU`P1tNV&@4&$4Apd5rMbS5T}MyhZ!rF4J#nAglH~EM?c7ZBB7^xT=!L#h"
    "Cj4h(8*dIwb{4;A(8xr=HoKXHf<IiSiae`J+EGf678zBvJPp(hyIC`8Zs`^2scEIiH}x{w5lQX%v@Dc{OEQApnhh@Umt8v47<+wVi;_rF9o$InNNg6rBQcp>"
    "##L=;ZbzwAQr(u2Q=^O<HEX?`3y`$gH=Fw8P1VCbDpLgKO)BlOM((jJI?;?2WK?QGcGhT`c8{VRWagsO#k^WRJenEMwma6frnei{X*%Uf)R%9^)}~D^&6P~a"
    "hP4<@3d~~V9?805(4?L&!5EHBpVl#MW5==!^u)>_tHuHI*`D!BMUN&ue@GK5vl5jNdwfB>2mSC((A!HQ%1B5b{I6KxMuNmz#IV+B(Tir&H7hbqTN}6XxRIgf"
    "X0NVpp;s>_hj~Wtc(c*uRO?%LT&SxheH|_%z3yIGF16ay49d(b75s$}mxV8ywbXfY=JK*$QD_r?n?a`!!UDNJh0XQ}Kv`ZVQvscS&vNUCDhOOQ!h{1f0E=I5"
    "RNcj~5a*$@NL^5(E<&QdFMdslWfer|m1N8fdKRAlG6_zDM7L85UurovgH96Y)Zg4`G**o1(nl)etL)Y~A(CvT!>zVRxE1LA8ykKmMh0=$=%AkJftmSC55^`5"
    "80&0zcQ?{qz&Z<&Scg-yzuoWb8P=4Z9}fa<PT2|?dsZvG&6#9nYSy8Mc%PKKn%17u7MYZq!Y*J{5ZLi|JDu8|?Iz?L705cuGk?h#nJ33$uQf-=(|#w4+0OP("
    ";P2`8hs?tYxkBXlvm^LXc^aI~hnSW65ZLKt=*M1i&qx*?DduZpdsb{*wJ9Rw5*vaw0d=as&I~=+Q@j#gOLt4$yLQ|u6a!K7cLNcOTd(i<{Y??+pn0+^S)MHz"
    "qnjwK_Ml9YZjwGF_ZGuC9>v<gaMR`{Qf(Y(#^vlO_^lAOqE#JE;vf_DG)B1#jG<d#EmZnl3F1wEtFzNs4&^NtRa$7iIOq&-e6Qc=Z)c(PAY^g1$ra9)#QJL7"
    ";@E83*UKBfvmVdRhr#~8|95xR(S(R!I+NZxexmZ?bYZK-?GmVErF~Z6R;5Djv<B+oJmvQRukrgYDivO?7oN$=ni5rV!d|<CZL?O6#nXL0)jNYad(X|8tVBsR"
    "lPFWA{fi6G>PA82zZl~Mn2QArzb@M#U2E4#mq^pc@)<S-a!*iI4MBB21@TQy`<U4doH|TZd~LLh*^B2A2vTvxetC+0UO>%>n<E_lA#PSKS?0l2Ha-FVo!$H?"
    "i$Y;;!NB+I+Y?a%3&-5%;!HuVoA?O*VCJJCs)RBhAZWX^9*_NLJebVZ0jwOjFJQwg76<5=ZDV*3!s^aCYdq;S9G$X0(Li#{>?3Mw#AZ@*;;7p$m4M#N+NFo@"
    "|I2vbT)q7@{GDmH0rMqwX#XYQ3`@F1sK_oK^BeB;K8Nz5s7c{SZvn*V;0oUXCNj3Qz#NCA{cnHik58Q$3^=S-tFU9B_&!Ar`(hFM7hN|*lY984q<5_DDZ^p$"
    "F4$kuJB5=m1Tg?%gTbEq^Wl)A6pw9H@Uh*jQ*vhK)1Y0FzgUD16HD5qlgayEjhzwx8Q`ixS==<17cknhQ!c)?OZA<SpvC+<;?TXxn|6sEdTGMH5>j~R05zl%"
    "3lV+=tpeS|qc(>~yX4b~XtPmms7Hv+U4IID3JIq6_@_W}<@5bJQ-3ya03b&@&c?%yv(2OWma{R~gfBZzqYh6h>`|i*&nlaxbp-v{Dg5QYkQ-53Ye4~NCie2-"
    "WCYR(ssM}%tO=^%N<v`LLt-r9xFH{5b7WIvS4d{-E$2RnD$X?Q;>k7W3N?0*^|mf35KrLm2sD6y{LHKxLB<$FPVkZwO>kjF@@gFozWrr*_3l5+omT_+{KeQA"
    "pT7UqxpVgZS59{T2<d$Lmsjup+L;dCe=|!4ulJ{?(JrYSjMMrtJ|Xpt-Gm0u9v9TOEsSQ_mg8m_F7;6#4zAw)g&3^fJW^(Yty=_Jw{8eqvv76)4myGyXnmvP"
    "oVQDxCF`bvVMK4RnRSB1h`eV_#AW!q0V3A`>PVb5=ZB;PfsBxQi~uR^S1Rl5U!@`_NDfRk$Y3^ZZU-l`8ErYc&KCZ^vXwOnehCPpXiOeNWKpy%xh6zXFnm(V"
    "ZUfQnv%>9|e8+^VL|zVC#cR@l)yZ`F_r>>}u<s1~_ywj$`2NDaf6~bc_#WcVPuOG2J|awkmb^DhcmmsBTH^Y2@^zC(2rlsLFR$MHRR}w5e)aDE9&-ZV46ojP"
    "6SkbklP)NY<-@`I-;M{av+jKU)l;W@AJ`a{`^WLAtMAs&(~abA+9pBJ=11PN*$hT*<_6o$4YipYwPbiZWH0OGao|fTFedY+-Yx+}IOiX48t_rA)!_ktvz0<x"
    "q8BSbDJX&nilHx}6}dG|sqHzAGJInQWMzi-pvO7ZG3yVO&L7U9;1K&N<!_W=+=IQM?Gb|ExFrpNhaq?B0v?@#WQ#vv2P0kbGIkf@qu%kOF18_Ybd%ud=0_KU"
    "2&ZUPcY#0bR%_dS9sPmahUU~N^=7r+JYsJgETq47`0MTJ_LfuMsy3QF5@=a>r`p^(VvijxLEky}4iQY;q}PQ>)?uLCbN(4IjOTOJW@p1moyKywo-j!~wH_&q"
    "OyOfnPM0DTsBXv8Am}B+)SqH@*}`&@xVB^^xoLLw?k~GST0-e*Hkc#5`zI9eMY~z@Q^vIv8JTLbr76qB2&Mgp|N39A-u=JhQ**~<3_)cS&k??4lWD1t?pn!$"
    "m7m$%jVxS3n>!$a)TPC1HEVp1Hv$EUK|SHj<o$mS_0f^%y5iCxJGOCCJGQwhU*TirlvA9sAc9kU$Ej~uHY=OXCaN!W_%)P2vs9R+fOk~(=nq_i{v>+M)EaFG"
    "*mfP;7D2rE!&?R*k=qFJ3R{H@Tm|tfe-W0+gUfgQio5%SP#v}#3vTn|l9QZ$o@#u_RwuD2*FiL_4rox_%pL{2{JOM{e~aw_8XfaNn}Cn4o;|WUjH|x-sIEVj"
    "M)p`5FqX!5IhJPjSeh`F=GPx-vp9Jd1+BW16u&OGg)lfOZ}a5ye^b7dWl&K00@YQ~9SOn8P}H!a@!;y+|CRKU8i?_p6XcIgdGjdj(WX;x3_wRJ$kR*^JsPh7"
    "p(kP)CK(X2y#GCbP`%-7o`J+(2vYfb$@pc%=EUkCADI>hWt_(X_gL2M5^Y9g@<vlsi~Y0iXWD~IuwP1zMkcMc>|xpfOUs|G&5}*0Hk+)ha#pH}$&>x`-X}F#"
    "?p0Nb|FYD`pdWPLM=ZNmdgLSV+=duMs3q;=VQ2yD03ULi6%WH6_BHc6EDi{;T;CQydjFebM=)~!3+D}JY21Fpi!%Lwgkn`qd*Y*#Sks=3C$F*8el}_vws<+A"
    "?xYuV#Tj8Wqu27cG|o|f3q8bI+P|gwT==oXF&eq!t9QS3dROoM1~xzI#Cce4Y;CSTeDKv_)%j{3f)??Q(mmvyfx736-hXpWK+(B+`wvVHx_bM!V`l^<Mo9Gk"
    "y94z0*HdROxqAE0oD=?4C_P{?`h2jXX4T^CQL}#S&$<J4AX?3thK3WXWXgfTQw{NnauwU|tB^4i;(~DTvt)HUb>}m~{324J$Xp#Hb$~&JDUuRJfLg`y`|r$="
    "MC=+{+^n(1tQs4J(L=mM`<r~L6oy94X;yZsn-2VU)WATuA=e%1)lGIV1z)gCEQ*ekRm$*5F&949y)3Tkjg~`mO67Q`VQF$*(_NG|G?Ta`65wz?6Xos5)RpPE"
    "nB~OEY?2~8>O{kgLERP$KxRmy0P-UNvc3KmUZ_jM%y@|58q!@a;rEaQ7I51pGq7hH7XM`KpL1f75KC<f5JJ_P%vkP6!!P``tZ3N4@_m;XAq6n{CrhFVRFUs2"
    "rT}_;{N0BYh+cn>A`5g1``I6T`ybJ_Z%`s~;y`VbL;mcZA7yhvpbm$ODNsjSa^w3_(jX|p$7hAj4}g0enAOg(vQ^Q2xM>r7o-_X%{`-%Pjn{Vrel(wc;u8V%"
    "x?jkp|IxjrtGM%t8y;!c>fZe9U?)Mai#YzS?dwyXKCLs|+4nJ`NdB!m`!uwEyrQ?SFeSIdTS0dx_K+)kjFc8@RLs{WmEvw9p8nCfSHyVCRE<A+_I_ucJuCeG"
    "=+^tATkl3)L<yGtp8535f&I=LdgefWXWl$>Ab)h_6>{Zeaej*cd-6xm-ZIag0i{1W_dc3)k3c%pzbDFkkc*G>b8RmlY2#XsKCX;j)7J-;kNo)6<Dj1mTwVF5"
    "U6EF-p{I<FWw#{g+_I+B)e>4$D#^w21|G6&m5wo+lh%Z|i;LdTRJ5@Ni0zq81x%q`($Lcn_N)?Q`IydC(#=QluXedz?AyPbT)q8f%(={W;A%y8&xSZGZN0w4"
    ">hjTZtJw8y?Y;<&+%PEYe>VqHvaK2Qnr^adI9q31L+C3Z$;>cu0t@s1h0bZ{kJ0N3ydM_q?cdIoVV_Tjnm_p67z}HM;goGTW!aPDlNTyL%l|FH@fE0%piK_m"
    "zwH(VBM_TEbRZH*se#NNKk)-}(?;X0(yZ3E{if68_lQ;M@UzmW?rb~;nAYpV$_9LQns-$RXB+fbc=lDJaRe=#>Gd6~@2L74L#$q*)#3FPl=&)SrG{t^`ZbEu"
    "1y|_)oZlZJmZ?^2v46SClok1b9-03ggFj&6D};V%RoOiaGJ}3%GA9`Wtu9V}y@L>_?2UcKtn6BO+yKfU%KuK&6~!?+t)@`bxR>+|uEp^E|6q>N>3+7gS4OO$"
    "m?6Ve94+ml)s4WqyovA?4KBPv3slG@85YqZH659ZBjW%P=}U7;{O=w=V?S0zpv656B7y`@<fxr&Ia;I(N~V{S>Z<Ifjl-;PDE`_+p@&!R{`P#eC?A$fKPRXM"
    "^RLOM9zAx4AXt(!&Muv)<n+2RcY_#nH*U<ByLo+o^azmvI9Bs<eb=GxXl>aB4WK9Qe@$&OC|&$&JaD@2{~NL}D4|MEg%gVTJG!DcGB8QGf%DA?;qScvyRp+_"
    "r9(!hjDu(5z5||rQC@pc?hYn6o?ko6hxF%T7!Zo~p|j>3VXHHKgf`<unvrBrqeV&h4whoQi(X5X>QhU6k1(FamO!6$<c{sEoxEo)WAv$jLQ(DW_ity;kAseL"
    "Jn*Byx%=diQ+_o79{K*CoQEeTPmY~+<e~o$^AP@81CG8I!%2MXp9QQL8jVqywmU3C4ti90>LiBFX)^r#MsXAOFl>vn9lzl;<gK$PN;c?&3W$`<qdT1p&v{rE"
    "W-f}+lkIHbwrV&#j(*Y@WqLen+~6mT+0O<$=bBG^>3VSL`<U$^mP{f9>F4V0yS!taHay`}8^9Wny~wszvVY``y*utjldxxEjGu6Z-u=nzG2*%fT~xk%Smh|_"
    "w@cp>I<;{nQYL=XcZtfd`}=?&jXLnmv!=p_dIi2c+@!w|Kbltjz)RN<KQI;uzgtlR&puNe*~mtb{S#f3wqSyvfdoHOF_oFL0BIe@X05`OlX$FRXN!T2|241&"
    "=^^i3z5UO?5;%hagbbeykZGILkn9Y0@j)Wg(>o8c;s?xY0CLdpyNyy=5;0|oPC}abhJ{e^d<r~b%8AZXfm48VI|IybU`HmdNFKibraKT^`u(pa&Iv3GOqy}o"
    "tGE9@EJ~TLh}%jjpZP8-<IaL?_mf_*WKF*yijpM!rTu^X`?)g<L7>P;)`HLOUA_IA5$LLb{&L2pG&wD)sX1$rA4PkP2(2^fFh^BVmaFU?jHo?@8lo}0miTXd"
    "c0~tQb8~=OT{#_!P2tZFeq~Z%o(Y(yo#Uwk*jx6J1o4tQdO0GoJw0zJ)$l#}Caq>$TjHs9rSVt(V*G;@5X^&EfPtfQ&h<`tQar0Ll};&}d9Z@efyVEU4rrL6"
    "M^V=6_AttNiYRD}C;|cOsQ%Tqzv*m>z%>kgJ8Np?b;4n)3LAE~l3aq8bvXj%;??`#cDcau&8&rS=c3$;SHU^YRk%TPj0{`};x1^Wuu*^0b;jTR0!<(?<ezVE"
    "qQP`w=lQT7nH{dH_0PD@)w{ofx8MH8XLc>2nehJe@JQs!%{-crN@R=E|Aq6T_kVHt(0Ta9)!Sd4Si({hQy>N3kAm}(t=%(B3S$9bhNW=%wy<d&RCk|D&jo3L"
    "AyE;~Ojeq012TB-Ho@NQ-&KsV!9L_(+hu!gx0Gel*0vguIjm>4Mcw+F51YE=_!aq$n#N$4{&pH?b!--!FxW)43ZX9%S0avm+y(D{Qy?XbvEG=5^)V&tSxb%u"
    "%{Bk_mpp_dLo&?Y;+V)4BT?S}-3WwToosLeG-Io?aEu`wZ~uPmoDRZ~a}o@L)5sqQ{yVyQ_n(|sgZKaBlYO1eVtQvCPe8F9awqOa;gl~U@Q8TkJbrZe;Pc}L"
    "FYX?mJo@T^(?**01LQ?Nz}#eV>+vuuCl~_P{XyS{j~~ER+)Fp}qX$2MHcF{oTOwr-?;bsPdiTZA7xy2CvLc}?z$P%tKD>MU0@H^X<B~bkvIitOd2~PNLY)60"
    "g^uoi{Q`>p@WG3tN1uOr^57T>e?A{|f~b6fX?EYdU~1uuQ8=E@g7^~C?=BXuwT0schhKbtA6qyAjIKiE@>b2Wbn&UPRijPZfAHDeFCU-0IDGu*2^q&mEvt;#"
    "Q#dYL+rW;z^ayF+hhKaJP=a^==cAL82TxxdVXF;!jUF%a2|OyJ+T7lEdXw%PuP3RV2D1ml0KeWlf7HX*q{h_mVVyCnay*N|@hSYM!ei#<EH^x-R4efs!(fIN"
    "!!rX_47Hv;_kc{sA!yXvGeHu$8LjP%=fk1?@DX~^;Z3Ek+EtF#yR)PU_Y#sPY>6M>ReU@xnR+3e@Ao7BR6JBo!=|1cF_Xb(;V@uF2?5Va9(f^c313FT)VoK_"
    "#3tXUR#6W`>j<`~h9Mt5iGnFwPJqUb;bkp(BhDoagUB3aKbUm~#{meHLtce%WMMdlzQsXCPn`1QhdRZWu#x_JER4$Wqc4u{2WPOW%dWW112$qAiQ=U1jQz9l"
    ")SpdY1Z}7ed?)C`40|OPTAt0LvBNg0xavbZS`VN2y-Tiz!N;VxUWHvYE33w;c%9<iQ@kDg@)8E+;s<$+S==#;J?r!bM}GIwcoqzQ;H<6DQm_?I@V3V%%yNNE"
    ")LduZsdEHe7!&RK<1ga?CX0)}z^mpfhOL1@QhMkz1&UibC~}HcV`l4Qu#+RnEf1qSE>wl1QdXhY;kY+>T@B;o0QM3?B;cuR;K_QK5Q>60m;nd~Rx+1vf)_T7"
    "z1>z9z=!>E8g*TDMa(QZzevBY`ke{T4tH<S#k23_z`an(0P{D3x>&}ty+i>{w}98f<4?*d%drc@;LR-kjtL-cGAn4ny?{SeX%loL9PuHuCRc?q88;ad`t}y8"
    "I`rYfe^p$AL+<s%VivWYu8SX^kGsk0OgYfy`rus2hArQ+;WzGyTQ=&XKbNE~V^3#cg$Co9AYZp{+pt6{+NG;0(D^PR0bOPdvz-HH9K1FQ!Tefuv8~vWboP-@"
    "BDMPDI=s@Jfix)SEJqCP9`6q3S8soVaYyg|BUcKm)oMwM0QXXICnC-W)@cfr{nvgd_(R#U@*ai$fpZaur(=KEN*-4ElLdfQQMlw9HAFP*s5*J&I<rAEdCeAv"
    "MJ<#|KN_IFP1x?0>dN(58R#Z3y;`D|QnM8qPC9D*tPgxr*81j1hE&XkWLET*&G!XufYGw8^9);m?pYYV=UgCCwy;1IzcC)gX0bk+HYLBG^um4^^jdi4u{xf-"
    "F1rgIPHCcEzQ9q$t!Mc0eBoL^coMNQ@4fl?)w@5Rp%Q=)P26G$1ch^i;ROMvK?||aUQu$Y5Zg-X=sK)M&<KxZ8$b|mLGL5NBA{*fjT{U<cqRu%3E0l(u`xNT"
    "Jb{gfaX<+s4mi@88;RTPzc}Wu$QCRqjv|d|?JP_q*PdlFpJE`_fS$ohQunY~K(G+3!@(=km11%7=;*=YN1s1<@uy#WaRd!Ecbs*+`D=Y0?G*TXfDZ<qyhjFP"
    "deGqqATh^MUUx$DUk8p1+?IVvIT}Q95bscxlN^IzNOQsBiXQb^M&>3@wM5^kfN{?mgxVVnx%C_#$4j4!h+og)@#*rFh+y}epR+>+w1zllz+ZhapPBFQpay^0"
    "vI5X^CjCBM-3Ai3jIC-_w>KB!%GNGkIi5^A70Ry9+!igbCU1ls)A^@^o+$|QIDQI+^mz*n5SBwL*MbGZL#j0bGN8d-p$U?^9|!)cBY#?Uje?kM<!5tH7PxYg"
    "R~+w5!zw=ph7m-0B~PI}kb*M@ph>}?0UwK`)?9C-QkF1VC>m4(6}O}b7W`CvJ5!^!XUryo1#bW-Ap25fk=?34@=RB`G}{YO%<)--ViW&tIU*PLo+1)zzVTr4"
    "ni|X0boqSv^@~g#(i|{k#H@(ROIEoD#xe8^(jOEEK1P3vv*_LFmPMm8v|+WCu1jrQhKh&lL0-E6Sz5Z~l#TJC?b|)j7$nzptFtH^mC0;w!64-NYM3;is*4_Q"
    "kj&^}$b3wxW3aLqP<4I=Pbk;I-)XV){jZ%^T3v6I2#bP!j<jJipOwpQd;h{(Um0vmd(OgR=BCsIyo+SwL_QjZ)6N7B8~~UtkE^dE*y1PJ6mpA|5vEn(S>rN+"
    "qFdz>2Vmw93DP0JOb&3tO^&L0T1Kswn1jMCy^ygsDMHYV0vL&)1)yB8%ot>mlC2H3U;^rmY5}kD!t0cSaqn;t4ttzdm`!j3Lj-5g74X75*-G#x=;G}*{<z2S"
    "1_nWqt&CT)IUH0kh%&AZBML^7v%oTpMS2g0@#kleBBqjLSqN=Hy7r7FpfIxrehwmG`7CKfSRv02n6P+x319j`j=#Jtm0YtO5aD8W$p>o$x8Ip7nqpF!SiD{v"
    "d4wppCp+UN<SI<Z|GeBo%8r|&?79b7x7EuU^ixphSci0b$89o}gDN|F74%;CGtfW-{T-|~JZF=Gue{J64nAI63*g^=Eaz~+nrX!zlxqcKvq4E=0Eb3;B3A!J"
    "9CY3Nnzgc;f1J$owroOg;nO<q^o2&eMr~uosw0L_m^kdudbeMc|Ezb>ShyAA^TlkE`RdQgwl~d%`)BTY$l-Boq%9^L&@(R{c=5=KVR{2E7<obL_55>h1oUg*"
    "od<sOygl+927+YqZ{rK^HY-%Ad$+O1ZD{B=v~)X;ajC|=CB>n;+tA<bAm)RF8h6^P9~ZvDc4gUr@G(M~eRpry6RhLQf{U`S@Hea?7K5QQpY6DvvD$0|3XeYv"
    "$3QX5V|Q(BoZ!J&?t-XoVz3%ibfCN;b*~&DURENG#O^x7hZf!rtB<um;)v99FGMrFgO{INO#L3;O%GA)fbFzsk%vdzM`J!dW?_5m@yk|8o(d|ZI%M$7>W~B5"
    "U98joaPim*;A8d(6SVyVR<<nqk_npZy*)x|NV<HPZlz-HuVNY6%XA-dxp6%&;7?mEMa^ShnSWTUGAng?S##aJWIWglE7>1TCalQ%#uluZ0<r$``}lHwi@e8&"
    "@Y^!F;{jT8Z**uyI-W#8U)bF?95HnG8!NLoxdEh0%^cSkdyy!P^r&}vnaMQdb_0Ye8352QARAdsv~q5ITJRD>+rNy4NL<7qfu_u(;pOE^9xM*KBpBmasxO~D"
    "0=Z&38KZn-kbhgt#F3`A-V-lgh;HQX>-EG1razen(fJcU@<(N8)dTs;<Ej-~C;nB80N#`JfL{ytw;GPS!+8(DS2_UMzO+_q!Iy<~OelXY+SXURD|3)cL}JrG"
    "PzVJe8n(3s*efDAp`L;Agz$ON$2bW_qT?TjfJUWtc(Gn`txFz6_k1CNaJ^iP3NKKH$378JcmY^9cns|9pxS|tyC9<gXk~E;T=|>Kg>V?^_;rO)ba{!)+;mn6"
    "w#g+{CV)Qbc`Rb-QBSc!_PqUxd@qCq_UoWZ*W4oa!O|0vd~s55@Dq(MU0IkP=>DWEdu6S&A1?b#^`myeicGHfI*b??pRk)C)mSg=Q}8hBU0Y0n1+c`ROjKDe"
    "JO*$+z01p6_()C<zgPWUkGaDihcOHZy268-Xq5`iK&3;;<g5w+hZO;=;WE9b&OCR0sV56HV=#HmhYY0?SB-1F1$Ba{R{VJ%l!!QS_JMw3JYTqbhJE6E+`Y>w"
    "Yg~ra!7m{7>%uf^ByK;Ww+ebjUP8~jF7UY&k6C3HLiE6gF@-&V25*)vFUCM1aE2}{DZkk)8OEM6`fCWOk0ZIT(O%dbr<M@XUd5P-XIx(B+AJXh++xzici%rR"
    "_ktm8OfgqJvrn|4yJDAIUTPdv?Ge}YaBkYBNpBY{qQqSGBaw53B)~?(MFAneVwxhIZNoQr&%lN^GbD;*cCj>{S6PKV=;rAve_WNO%5*)<iDXuSXQT%6s`2QR"
    "vLePfh`kgEC1AdXA`evfFgPP+Jl2(y1laPl^-I??6c|-45j-k8Tg@8&&-FCr#ZvUPDk^51=pvp}D_ZsK#!jua$b-QQxPd$tTvqTD00AYbZHHI_VlhN0%6S4A"
    "Vt43|rm9h|=GEO#v78(q*G{uXr_MP~BCc<9sbJaIIO212O$bHtqzTYQY~p}DZ4nioHOYuG!(uV5nw%e>0>ki%nxebTri+SI@{CQ-m`gt#_ZU*%O?H^1ju<Nh"
    "O#TRoZJUQ3!A}-a)vm~}4Iq4F7Nyj#?S*%=Ag$^!7@y7td*SWdrhV(It(ANhKKE=2Be6k7Yil^R{kBjP`OiB{dDJ{=$i%2TL|-b4_XQI^TW20iv(gF|#9X=1"
    "8<P;w+fi^i+cO?>_=L6w7kc1HzpUCe#wDMN;mKAPy4hY9#+Bg(D|MTek8Aun2>HP5IZ)^#OVK4kI8G}Q6SHkiK|<(dT1&VXa1^(R!m^j3R0qbvlr4%_qj1cg"
    "-3Rfa{b<}r+rqg~>2n<yp$a^)Rk$&n%ty&vk&B@Zpca|&0t`a|0Fg;>kod9Z!eFBThtIT!6RLRvint=}o@R^}(A{r~Ue?y+qdTm1im1FXf{Ub8MHppuVsGa{"
    "SJK%X(O0T%@w_4nF6l}(qxWPsDjTb?`7CKt83{z&9K;nyxZSk#TUZzokG|;qv~3asFX_U=u^?q`;g^NB@|hF{USjDx6rXj<p<z0f)d+~OIpjFCGNKQVoNKPr"
    "i30zXmDm`NB_k8tCN(a(ovfo$yU#MFYzn8(2VFFD{!GKHDe9*e-qRY9-HRr~P<aEXX7`OoW#zq`ag}dr-GIicHF>8^x^?r1Z0Q->vy@#JF_cuOJjA;j{8PC<"
    "AN6#SEtIWKlGlW0^JlK+80611y|SdcpK}BjJVen>i4=SCfQ_0$XC4lFJS2*d%(%=pSnP2lg-LTGR;JC%z%HJaQJYK<E7O#$^XSOJiwtUPAs7~c&`VKlke>D9"
    "Esk!rSJCzYI9glUZ`VzYY#Igq5aZJsaM0ua?DMalK6jYY{Tag*_(7_PX+pdEY@7!~re<b?xb+OpfUMmGvt6>zxd6CuDo*kY1*gT!=N{{O(b7P-aI0LyDBqh*"
    "hJinJt1+5p%f8of+xtF;Ql%p#%e~~Fm7uQ-1KQ7?Cm~#HrInt{ep;4#is>jO^SI4hc*Z{j*RIr`>kBVSudT86%(bh?_xeKIX{S#(=(Ld5vmlPWS&xq)d(BC^"
    "e66a}c`QI7o;<5Pr!`C(?g+q=imsImyJqMdua|gP>9!vWR)*0ny09d0;0&V&be5;l4Hh0!pov?lHjH6#!U1ih$%q=H$AUF11IVdN2I%qFlBDxz)oN9B?K$v^"
    "wDvp`VMR1@FdVkCob<i$j0UPg(JDLl^*KKn!q;8xe3fOif+ZdOXGKQh=#NW!>pTDu6O6Wj2$ad5pqe3)a@1=V-+yVV&JX@2^_g=nX(^{Z&#p_+xr#>)%O&T3"
    "{<A|MAs7*jMZo$h2Eb;2;yxN4m&Sc0M2C^OvFR@n)}pk_MELu3cC8$YO8?P^z@r>k+R+S_%q)I3b{q_KR&1De4K@q|VZERirafry3%}05U7(f5&~Y%>5+r>5"
    "xYKU#urIgUb(7J?nOKU;Nfswga8zVft&IGiPNEViVc;2dx!&AyO{9RCv(cfPN{Cpj&z;$xv~VFl0yH$E)wwK2fwU9Rxa4YOMM`JP%fxO5at81JTauzlZ#NER"
    "cW1D9I&+}rrQ}q7$;+&-ca~`c$GE1niL_F8EjpEeNu#L)#X9Lx*c-DfzgF4d?3hVq#TM34H7qMEx?w;r*{Wzu)@T|_mY7{v8<DJ$Ga@WfvQ=4zM3lV9X}Fms"
    "WV0FTb;n#S!#kt)D=VG7=&(z&cY*;{lld>{v;sp+tGhsH=rm(9&`LM*GgBfnIC>z`dLZj4&#IDh;i0D_AuPoWlsQIHaez#@0!53`@fY^c2bA0=c)*P5Eoq9+"
    "F}||$WpOWoYWEZQL^R7A6pgp|Sq$;ZGU%hA7y2qnLib)utFVlZQLanzT-lt>7#USyZB2gNuN^2OI1R(moS4S&x*&;1ceTi6V}!5S!?qFKj7(4#WQJs3O!6fD"
    "tBS3U=NQy%R__JCX_PD17!>ZDCp7q44GDV^-<8oiXA0DLQ?`Y3(`lf)=XQP1S}az@Jn7`ga<+u=A&0kZ242h>REQ;1A@O!?jlW)7TQ*j}L(!7aJHF=~|9B9L"
    "p9X&KTy`DN)~KAr-(Ozd${)I3Uza}e)*!LC@<YLGa^^-J6`MupZ5GIj<Ke|t9nVFAY+`5&>@y1D`EbTO#{6rWSI0BG=bGbJ)<Vw!E4j8N9L)SDi{I6K%NOKL"
    "S@iFH;5?FPpzB!@`&)9<{yYwPw2*$3kv}(KDHlsD01t(KR>ByTpr^7edg6ij(e=mNaVfvy;jsDXpp<AZEK|k47#fej)r6UYDl6i#1$G&s$e|+#l3oCi#W!~u"
    "-4L@ki;?{*@j@xEH$Cr^1gA||=>w6B+s+7YN&Jy^29d=`Bq@>w4}|y5U?_qKAfscHZe5mVF1%K)kV8_iM}34CT+_~s(qYU2i%5d4d$gZrGxg^O%GG^vP$Tg<"
    "S)Nj5Rl)9vM|K$~J{5-jr4YC~lX8FuLOL|@wS+;8lW7}M6J?)!@{kp%a;#uAId#S1BYzX7CJ1A9)|M}EM$C8`j!zF+_fJ_|y~ovS690<TxYF%<?C{r*Sqb*!"
    "Q$7MbM`gHs#gr<nHfmxOUX&lJa@UiVY+hd@dO^ZTt{`);6@wP*M}bNlUUPGS=ncZ;IXke%;mRHW0cMTBrvYaL<#;kY3wmwcb>C)VKH!b26J?rX-voWlTeet5"
    "mPPNeEr+*9mxI?@Z&c*lPm>n+*jLl`LhvK$3sK)i1)ElZ@8cM)Bkh;$>kvwB^va)H&;cx_ZvX@}_oV<LKk%m(L7x9(>%f1w{6kkcOdCvgn1-AvXCtu4?gjj@"
    "Dd6(*k&-7)V~|K+1umR8G?mu`yFuH#)(|mpEEP2-YbAtK?Ghs1YSOyEQxs$nAKFF8GJ?lYSJu`@iW>fd<)(32qY2u3In<T3;4Ul28&8EaQKFCvtAj-tcL$TG"
    "?6VIKqheVS2!1SvKULw8_y|h47yNlurpnzDzb$Jj50C?6XE_jES(Xbfq_~XlO^VF?jtWwda-u`K#u!~|*QzZqIRjK^j7&(gjG(q>mmt)UR>htXYdERZkG!UI"
    "H&)2y+q-?CuH<Z~D>*&9m9K@2g<X+~3mP%+&SKAKnh0LJ)xCw_GVJGG=Fp$QTb?vmp$WSRu&&!fqqf-7`0^H~(0p*NZh94>yRsYo4CAeNl=4K65IwM$DZi|e"
    "dkmjtTwM6ch4{KCJ%2_nF#O~qf+sKC#0Qo+^ZOFs#`G;$qZ$c<ZeotfEw06<NJjQaf-br9WeTD((>SrSBpFEZrgV2Wyrt=1nhu&-XXRPGbd{vfE4BCe=Rsmp"
    "JrHCARGd<B|EFsc@iEj?pZQ%@y2wiB!_7<~(_RIwjN~!!pEf;EYmB+s(fD&=tdX0T?M!9hV7sB+u2nY=>Mil3e$bF#8wbr+-DZ-KEq>>ivf`e<X;c9<R!&{g"
    "QQ2>{rj$13xR&$VoV;go3vx7*32>CubK6RUNzWpXjBF+L($5%imdS<qYYU?9(f6F<i$}yxBXLW;%f%6G)|ii7-xwB7Qzz5s6<q#NWJfs5H+ZtGZEFsqA(!C3"
    "42?Bs;onAB<x+b9%SgzDNFmy3`md?}f)t+^n-vtS0wfpf3akB0>z5}N#=Ya9+fJlI5>lb7>-@MT6Jq&5_uIU(dqHhrWp__blE2<dPfxyCEJ%tgoHqW^^bmd~"
    "kDnD34Y&(u-64+^kQOg>vYQVFExsy#fbQIEUjhyqcEV^{YL%wO6S6qi&e|FWFMc<@INHg-EUSo^re^$bis>m<!UdYaQ@;BZ0n-FI7GW!BBsT_7(D0Vq*{+y9"
    "%~EYsl2)d5&(zTjwhoDTZ!#MMk)W~a><xSPBx{(o3Lh&eubUg#i(~r$X(enWDVs}>vx@Cyybz>Wb|A(L98*AhZH-h+h&CXsXa(dGYl(!cXVOI4BPEOrWuP^-"
    "`EqWMVlHx&hL?eB=d7wAiA+|xyiB;y<z+q1k!a1rBws;_h*_+lYY`AKSxa$7ha+9i>F89*$V6pVd=_MTX}rSJj1noC4}ylFkHRtXfEtbF$TL*9sZhQ`v{nKe"
    "$)o6!80e@SV+jw+4E2_bCzr1p@%zjN$=R_eSgBQOTc0Lq=JCOLS&Tgyj)l`PN2Cg;$&o0Qu^)}|lw9p}0(Nqz&4;<R_EF@-g(s6%r*x>4Vq;NhR^%xbALxy6"
    "78V$9rJ8Dq^7v(f6Dqbs3*7ivLi%zs-A;#X<a2eYxG1?>jo@ix(~o~$hvxAZVd{%rTa&(7mJ7$#+{+@v6C;I-oQy=VJ_k*5`VgUMB?Lzr%|i`SERr;22BIW2"
    "fa)bb?$8Lm6}FB;hhQ@=ST+a$5MbgTmzViyK}Nm$_9mi1(#{GDdldmd?e!%D#ZD7)$xIps4$nHax^VORO`lSWbUotqQFcCNTaYUwlCu6mS-aGt^9T9ytm$Jo"
    "gwTivMAD9`5Y6j)*1u&`lE`dE@b^bH5PujPp!>Ele}&|imuW0_mI_4KfJkMd8J;s{a3bBl<$i=|r!_aug*@c==4&cVvyxRFIlz3+pNf4Qcl}{dsk`e97h^tu"
    "l2z#;y9mfqOVENhLS`1fyb6v0Tl`v<IM+&?vl2gHeY(;lRLHIU20hyH$2oIvyAB(WD#Cs(+?T1)hgh5?GeD@vJ7`&7nGcQ^A}%<wZ%J>U<4Qo()P!Yth~WPm"
    "p1Na)$E5NMEP{CzT<N_uePWe<$cG>EI*)pyR+1TpJ#T*^-@Bp!JW(atRM~sX2V=2~2p1%qdaLfeY1O=Q_>bKM3Os>j0c2TnJpoPOh16jiOW4XOiKHyCAh3cV"
    "wD<WZ$y>wspyzus8>TcJ_&s*3M;lYqlrP@2f-`}@U&8>^kDn}LjZSbHjs?hgG@e*8eyb^-5E5Y?Vro3$MqGBEvn6B2#2Gd8lSm*x^S#dVHh;}$$CGeesFTGc"
    "&Vzj&oKGjSvR{2u>A?Q+tLN}}?!vANdtbNk`E7U(I}RWG1RtE=#sctwuL40vSP!`m&s%b%2&@z<(B`@7OVe=N9h5vdO*Y6y63%GOuP)U|*3W)jIHOao`r{JM"
    "DDriSH}S0Y97e+){e(O^w-0r(*yEceO`XL$pu1j6KqQ`@K>x^NXL!L&>GD$HQj(;|)wi!&Sj5JpA=o$yo8U1h*ch71M};(<mpb&PIB%^Uja79P6u~g15fntZ"
    "X*|0h^!@peU0~u}OhLc|siwt(2>>~l%pSc|$?R_#6B+oiKA>w(`1(6xzul0CA~55C-H3o9F~>OTK9PH)y~^Hbn|q^8_J&5HDgP0};%`2c9k1&z<zuhzZ6u6N"
    "FZ1Sh8AnmkpW>s0@#*i*S&g5hYXBoGO8c#7v~<&^*8&gaP>R=9TQhsQtOzWH0Z6vnbLPyeo!hE?=)^4>NO`juC%7;$S41c4yH;sK$p#639$1MUg;?IkNZ`1P"
    "KpMm`lQ60{bIVX&y(_MfEh8oKX$g;zFC{tRO6>fyz@BHSJfn=Ff-b9IoRt;}=bd>pzXvg(&`it3xS`)tm}u8lmxOwp3LuLkddyGjWudzyNa%k%^Vmy)5P-d$"
    "$N?n>>%%AM64J>K_|XYAilEm{fN18H0v%X8Ae)rb(oySqVeqTlVm)g)6{$Z0-mR^v5wOrm_3I8jJe-VD^+I3o(Te+RbpY8`Pq7E<(SO@v{EF^9Rj1C|+Oc#7"
    "&2;umo~?<_63bZAgO+?1iAs~oCN<r(Pa4`jPz73L9o$Jx-M=QaPo6QejUNZ;ewlBjRVpb3E9$ZcNbwW*Av=bX){CU;B1se}j?H~cPkA!grQY%sTx1**-Kl%o"
    "W*kL&K<wel1bo43vf%@53h*KIr#|!xABL?*umGwxc^&vA8*^D}m<F=Te`2e6Mm*$7S4s=RZtyqj%`kSJkGo7c(H%y5^a*iax}Ab%jXYB7SZ?@8r6bAmyk(hZ"
    "375^Vlis*Za;nHFH+~lO=q$)Vv#C7LHMN@!8CxbjsoiR0s4SF$=$Zw9bz=tFW#Erj4;1L;#t7H|fKU<;$SsI?%~LOcumq@|SJpf$wyeAi6XHkrmzPJJu$9GB"
    "#VD`fpC!8vy^YeeiN#}f>#8^+?~;2qLPqQOYncX@i(G=dC9e;v<o>t<HX!Q-p8Vzy!3B9bG*6z?<A)HvI0#;$hkl7{Mx9pev%S=PylvKa0l`MdgV?3%$Wlq*"
    "*@!@DAtp=nU8`nEPsoH!&Sq_*2#LK?gW%Cut?VNu%aX?GDQo<m5G9Eu{7G3qpRUQodIO5PVmjF5fIB}?B7fpuCZeW#&V#5hDo;EhaeDGehM=4jFvO3v<Uxm?"
    "R1*TnBJpGPl3ecXRBCsek0zkWe21)ES~Vq%k(XIQ8EHy_mxGg2x00)_G86}K;q7ar`r<x8afK8;CM_jm_kw1O;dw0pe%eFm=&5*~L_G?`y~WS8fzWPb8`SK{"
    "DU4xCbaJLR|2T7kg|2Sfg`Jcgv}y@y;bs-JRlFKY9s!zX;)g@S(mC52`|Ub6a7((DbvQRg&{-|-VuH3)1o?tW^ZoD?uPYvgQ%*oM^pgqh<YsN>oQk3qvl2v;"
    "rZTcYYbCma09}T?pes;Xl2+H21R%AjWS!(e(BzT*#%kK(h4YME^98?t>__8pd<t!HJU`SPCSjGqP*_!2=5mVV3y}%`3=ON~Zl=r2GjVYMN)wo{O8NEqPPSbi"
    "AhC|$Is0<mpN9Nmhzc8R(Mub|eO*y|KdMe%9Yoci#(2pvb2x~1;kZBHQ^Ictz)D^W8e5xdYm`5&t!Zs*;TJF~vtxGJEd^OO^+dvtlMqqIguQEfsMmBrrZR-i"
    ")WUQ6@)b?OW?WpClQWwa(=xcV{o&Ba<)(yIOJYWWowuUPOMbJ@zDhj?x*h*=N90Nbp+!q8Sk_zflS>?GZpC=np)V8t&ze9p$(mL&_i9px=h0WiR-)aCblY+b"
    "yBGN!C3TKem!Gtp#J2V^D`LVt!uXiUgpW}wm(<F|txBIW?w_`(We#6sd0NsUaanHn{Xdc0ad{@KZ~yW=UO9aA?k~pGQWoRnFwmyVl$0!(F%pzTl&jmD8N^t;"
    ";kumS^Gi6nun<jNCsrrdgP%MPu9-R=UeNq0Ved^_maQ_-9H^f>*|3=?aol6#G43ye(3qr0Ll-U~_1@g6x!9*2MV3q3dhYAqo9EmTCcLkP{crM`T*atO(iIi3"
    "h_q94aY(-Sa08xZ76Ije501tS^S<=Fg<N{C?P-cmYSte7@ua%bwWcDZ7dc4nolEk!Z?l(zElWe<%k?1$G<bR4Cl}(X)kpUi2ZC2-aX9mm9z4pzIS0M_L0m)w"
    "-P~@!T;rK;tu3ZSU$qeGi?!|BKTtPB{J^zc1OX#kLm$g`((P@nTXv0twO>1!0EAr7z|-hTWMFbK$_=i0qxwtxrSv_2JaFRo-*g9p=9NFWV61I6kAbDVR9sNX"
    "Aq&wN3bDLDjxZqN6NFKTe<Z_g-;E;Rlwr(1S#9?K|F+8MHwS$G#k_?0(R%h=axFt5*i({3uzW;$93ZHUK*9wUdU;tY2CikyfD83To-V?@xY0vxJz+q>Y!>XR"
    "W`O3^R5m&BQ!S88=_MP{!aUKY<%i>#MS2d!F;FuUICy1h5vg=4rWpkk?dZ`soQ3{O3IX)Wjf@N@JdaEMnLkU?2qUa_6po){=cY*KjsSXEq&)A7IMK8z$e*6*"
    "UAAh(1Sjm~zOaQZOWom(teiM)G68^TP+wcqFiM*B-8h(~AA2feu#}TV3BOxgvkOh;ff9E8Mmu#CM|!B@@CGYG4e{(Y7XJ7|a#HrInzW%Gs#zIDcz#X23wgf}"
    "TS)CC;nmh-I-ceDsO#D<TG61CAzsnGjpIYKq7Xy$tl9k95K>A@%dK52L3-KKZ;lZ-UC?l_^mc@T#{#^9e&-BKzr0kIhU-Q6f9JlM#SOV1G+<{Ww^5YjBRVt*"
    "QY`goS}7MLH}YKdfil?Y8(k`V7rFXV9zTCcG`bEj^09L|2$7|K6OzjV$Uqm>mrMA$u6M0C;N@o5#WLN7!q3Z77;rvTrNN8QQF~oW5$&HWYwRca0&D~uHW5{e"
    "Nh=ng$|zAR_m+4Vh_k>WjcT3-Vm{PHU%+xnn@`&<_Y5a786{QB=(xz#sAjfoC^;n6GA#4r8e(&&0FY9r9k{}(o(0q!J?gn^`e_Tlu>yP|E4d5oS!F-OV-P34"
    "f*XYnxxN15x*rNIgATdO+XW8iv(KW*NCXzA6IhZPexzs|`KNeLFp<E-FD~0z4#no&@&oJefL&0A3+cZ(<jsg%5zgHtL1eLfn_eMlUin9{d+7V<?$<Al9z6Zw"
    "gBQmS4!`*P{xS2r?rBLu<jkuhAW?`Dj9iyY5eH9=0MLzFEiGA1>ikAaKZ22`D&$@mWNVGh!=(ph)#8{EwJiis9|w6(rehJQlS|ud00Q|Z1s>VY1MMbHn=F5u"
    "1Begcf%X0)i92>>;I<7W3~nny<bihKjvCI&(BQ0wr-jyG!c%Z+l~iGNXbNT+`L8*O*v4AX>Yhe^=L?pDlU!s+u1jFe2rXH(>e<jtLbt+GlIEk9Y8m)=8qDhL"
    "ZLKZA6$BlEYF~x%T*Z5(1L%}2%~H5}K#p}&hKN!SWE)-^dHqEiSe^jZ>w?gZBDW}}+%qW4w@NemQ<v2NuMyBZ!kb=`NLJw>iSWMFZfgPGMg*Inkk}w?AUW>L"
    "@3MwLAK(cK{Q$^8)i`m1`AQ=|9zPBW9~CirLbw#^3d>!;FEZ7(b`KavD=`2j8z*>GT3bt9aDcdu(HG(PR5`s(>&3KfD|Q1mi|^93#X`fRX)ep4mA*!{tFrcp"
    "c}?%sj7iw8kJ(F4lNkV#Ot7`;=Itr94ayUzIfhBugoiSWN3YY;C}(=i`}JjGE;8h41qOZdL%t~7*fgUR(@taLrsKrgBs6xXsX<rrV{C2bSw1iVm!F3dH*J|G"
    "Ci$iHo(TE#9N%-Q?EdI3KPCr1(cE2$Uxz>|D$@|J+pTuPs5=ZQo!QuQ9p}#gDVeqxfGn=9snNk!$NbPDw{tG-A`p`(3S^JdzM^$)^p8i(nUNsNDUZB}sgO{d"
    "970`LE*8<s-I)mWd+3jQLw-yXZ-2LDFUOeB%gc)e9=%CbeSpH?vWiOVA=Qr)xoKH-6>~wVlK_h))l-*Cg)s}ZxC?4rDi~Djs@j<Vl8@XplS)qhx(xrsk9}Kp"
    "ohv14x8E+S)nIw|S!_zZK^pw}4lNVmk$HYwO~cg~?FI7Gme5>0g{m!zw+jo-Yl9K1n==$yV(CCoLs(%Lx5$dnF89kC;;4~n!!pXKvDqP{kS=3*9+k3NR-xSP"
    "p)e);mQ9<pR$W+0uBMfn;twJr<AND?DX2i<KR}5{&5d(-`Lm#WWM6a;G_%%Q9GWEO`q>jX*p}+wkTundh3%%a41MvI6nouV8x5iFdv^KZXmS?Zke-sQ*Wx%+"
    "EQ(ymk%fHpf{xT3CGzD#{!JN}v@3mCMpln_rG?iEhBLoSXqv7*ivz{<IulTnRSa*stbA2r4ey!m(>FxfeqwQ~{~_%;G8_Dwx7YM7W@=!1K0=!`om-oL(!RYj"
    "d~ahdO`JLnDX%8ULYw5W5<P<?zqUjh_J`eJk~ist12%fQ_Mn8*`RuwRLtdI9+eGPjNg52HpCm)dt;3T?aGF%GiZ~?BSX;3g<hXc|S{Vn&X;tDv1fd_cBK*C@"
    "6^O)+hw8wpz{;;=hgN<aE4cEjWWp<>T5*8c0sbmDCn;2Z?{!*vv5rRivxF#+2!bhNEb$=h&wdn~AJl8Dy1QQAZtT<wr?t)1{Jtw%&xL+iEga(AC<54lP+f8r"
    "Fz9`(T<(!<5TW4Ol?dA2Q+~;?(IB_#YYpCB-r&Oty5Lk(Vo14Fm_C}+;C-4k476B^VueEUtcIX`qe^-mF6f2H0`P4Fl)i@Y9a{eE*1p^^6MLU_tr?ku>0)2x"
    "FSljfw{ynLlAhQseg%fD;37?bD-PR0s@k3@wr3$zT`#$U-RhbMw8%PMtM>e#g5aBE|3f)d_nCaa{e&dAMBJul-lC_SY(T2V2C!c{P+NX?%e!5sx6V~J-Rf-e"
    "S@<UCl^d>0la|qf>t;c6M1Ygmq^v%g<T({ZGsH&y!o6kQDGKWqpsbF9CPRCK2^<dcPj_c6a-AG5O<Me7KD&OC>TY=x(d@4Ki<}cJ&TN7)0sRp(nmj_=Fgwg5"
    "4a5O@=CpA710GL*dC6*$<hP>yew)2;9rN;Vaqx?nU~n+ReXA$(NENpqb<8{ZY01Q#69C~Xi-2(YlkPljPyI-wvnTOA?6~9gj)r?qkJBIM`Tm2??tc0B<i+9R"
    "M^9`a5GV!(I7X-Kq`mKafC<E$+ey>(coNf=NgEmA4Wg$?x)IEIV03k-t>6bWSjd2$L!I@<8G=#QbF96a%_W388vwE=%fu7)K~75+zC#LMd{`&Whr?DzXxW_m"
    "qH(KhpEW2Cd@S+db4?fv3GAK)G`^_E*oT%bs;4K4lK0=1DwP)dSJF4H3>EFEXjW^)6B#Ax{$}EtmTzYPO?laa)k>TjkHG_DL`Us3ric!1+oPH!5|I84^RsFI"
    "i;HfP`$n#ZA6gy?kYI}{FgRq7+3Jg_Fj$@>Rgf(`_B){Ex)!^P!baNbo~R3L7Obf$P$|ci$S}+%{_>Lx{KQLjv&yCK<P|WeU`;ovs(IzKPP+cETb4b+e&3zG"
    "arIhEX0_E}J&{Uu=|+D13KbCHTssm!SyXTQipluom5})p>Mg6g@{2iP2^p;hYsS4$UkzxA@xI(M3)sL2=uu%67!(&8rpORd>ym?{A|p&WPFEz>Yj?qXcUro^"
    "-T~yvmCz!rExSaP<<@$TIj0@Fbi~Q*(WDpL^Lro~g)yFO(j}sq-@%87E#hkl#X`UHl*uXL`BM?9PeL(yVLam?-X_dbyh{$$fogDiIt=K&37#0to>|Fq+pd%?"
    ")kV>wu2z{3BEQdDmQF>zBHi9H`d!$7{?qwrDi`i#^7-VoT?a7)@hmx<E8SUA_^o!Eis2YWeU~380kM;b;o5eX7D#|Xj<PSgD&Bq~Q6B2BVsKJbXyo>@{I(uA"
    "99-=Lwk~vNK2-Zwz|~X7bxuIh0*#A^9`*zZW|*irB9wXhN1gw1i{JU@x|UN2>8uSIBe5J#y1BtvtQn~Txt@)(U|6VrEY~9kc4fF{#wlHo9ibrQ#JQ4|ksT+V"
    "MS(ve<!~Iw>>-pLCO`Pe57@;<*~k2;C@g=u@-iR2usEW`%gXagqUZ^309$c<p;ciIk_?dg3_ruC>P!Rwr5Sew2$_8cAOW8o28LZl6tmOAp@jl7l%uQYIGk+q"
    "B@s)xmp+6jjt6Pk#`pmkaawziAN`i6*0Qf**h|w+*#yC6v5v0z)DN5`dKkjQlGsDNny~y}ZR47c?Dn-b%ntO<#mPxlVC=)H_(SHv#yryWA-`0^m09JPu;j;6"
    "U^-7x!a<SA(#m4pe9YTfyd-AS^lSq>K6gXYHC~EvY}P@7&-}bzOFO)0csB5%d%-@esbyw^t9QR}Mpy6tlQVe#U&aFm4<!zqS#<U8AG#oUUA_I6sWZEJ_n)0E"
    "Krr&Fr9Ix5eh4uWDwq%VWjZ?trz!EgvxX+rHtv=eXe)*vYeOb1U9cD<;Faa%nptvnB<^rG`Kr(D>tN&z@l0hYT~SoBNh3oh4SS_OnfvF?+10!M;Ji@{nOCl1"
    "&RHSVrRmY5u0g`s18s}R`mX#^4aa~$4^NIBw|{VlM}s=uH|<irQF6NH{4;`&T4{a%&J@Iar`IkW)f>*%+19Y>G~n?MR0pP5s{3GaIJNZ>t4xAK=EKQL);24Z"
    "3Xqo^62ijfjeSO+Wn@##*;2(3N!tq7L#T8!Hgr<zz_k%dXbH(z4XG5;G^@KCPJOpp+xF`<2mayz>rSmwZ&vHgBlgC@Li%fmzuvBHZ#ng?YNP3IIa{jkPPMsn"
    "#2!0Xg1&R`9inYETEkxuzK1Bpuqfff$q<McMd{=gY<>(_JJ3j(p=a{@+`Bx@ouXx<uu5l~$rI76O_{ji2Dw)Hl*HmMssmR`3mcXMDr;+*F#|a<0U2j>lg^by"
    "BzBT(6;N*#8)Jq84~EB4R%r`Nh<WtI@$tZqK!*yZzA#FVyXH;#T~8)r6p*@)R_2qk0ezdiA*@$M2|`bxKo96rP6=U6ouE3%*j!uGVc0C5(X}T<iP|5XUyv6{"
    ";G@qnXdx|vOc_WreI)b7imXNMg`VL720Ma<JPBvR;8@x4ETG~^wBq0t6D<claWxPuyJLN9YeJKW<*Frs9t1v9X<;OW<%$<&Z!RxO7Z*Zgc`=3$XR#<LhbR%F"
    "jU)9Ub1z+X`OdgALYz6iI}S$-Etwvn-}GE0VWLiljEso(h7wFJmJ)dh?M$UZRn2P}ctSKuGVF100@&1rr5v72$`=6GR?X`K1OF_XM6FUhnoMScQiju>ZSbQY"
    "I+Zt)>a?>nL*1}6N(!Jx7qS%o^yz2t2;R)Na!B8BEs$jGp8<J>KW6$1e3Z~EzJD0S0!VfNkW5`A2{v#LF8<N1ucg}eOT>7GV#O>aP#j*p{rj17_WoBQjTcIp"
    "`rYi4VUHO|<y55gi07UbZ(Z@3A~ZI-dN`jIC#rdIXDjlMKX8(lMo$7FZ9CE6J}tJ-SzB}R6Ihw~+_j8}wdZ^Gay%(X5~j1@;Wrn{f~*!9nc6=z>3R`HyOdjJ"
    "a$=p&S{Sjxs+Vx$Tvn!M8ov%FBQ5BR3}>9df$a2`07ffdFrw`AGZba)neOwFdVesRGjje~IbABvZ-!&0SXwZ%la!Ob2s2EH1D<LBy?IuMl=VKKt)kO6vZGc*"
    "vsJ{RYi`u#60@<DzoU(ss|0@F(w<5tBElxjFgcc?LODneY?(rMrgaQHZ!6#EK|13KFQ!K+Vm{$p{Ejc?!67Ah&;{~1kYvJ`<&TZaHFpnV!Yq$(Cd<tp4H8NM"
    "G4^FO*Ad;b=jy3e?M|EL52Gc`dqt?Hn$MkION3DL<jpiKY^_3)ku9>KOOL21R-SsyvZJr%8NJw9g{ojshqy9j>e6iLA5+NH1zNw?OG~p$gxQy$T%2jq33hpw"
    "f#LG<<+r~C_Tjv_diPsr`2K%Xt5rv<uO!>g=*$!$XLA$ME&g!DXU>RJi;p|$21QhZXc0=d{KGnQ(yY1etWln&j7Cr0oQXvjBQ4Qa9k#35CJiBboR>%?o9B<K"
    "64Ccl0a7w0pfyS1x4)cRz5QnFbeXpDihBx@jKlAL6*_}|h4-IaB<&tO`uxk22geI%I(YwfR+SMr)|`c@l!Xk^EKHXt<aW&mJj+5^*UWQoiAn2EZu(Ic+o9Xh"
    "$nL0kuTBy+yb|98-NF3o?Qh0T=jz>m6d9STRU@qq&s)xVIq3SM7hy(zZ{;y@7pr9-7t7Bn+iDraMOEY(*z4+P)cHis1!R#J<Tpzao>8_s8H=pfc*F>A?jq8r"
    "WI!{&En-Gk5q4kF<z-1XinC<h^kS6pWClC5g^vEz67G1s_z+5zw_SO1Vv*M#b49dDS~i|c+)cxTW&<*@n(jz4uq~IQ0%y)UzDs5D@2uuB+^Csa?vkTADM+9+"
    "X8?jP74VjTP5_TFMBzy5=-MD#YR_OAk{|OaB3B8RHL%TwWi3N4$bw=o*X2D|_S<r>8S0><Ej=2ea3tGd3$&~e6L`v80!`#uPlyHOt8|8D^%%_{(2`|{d-n3e"
    "Mxq_WO(ze51%OuB=Y1GgHz=o%9`}a#W@A2!kp|~`vvFz~SXQ3`i=BY>^`{PRRtO3}s8KC}PGFypgOWC<w2dUEx?Yu$`@7@rU=s16d3JR17*gp+!xtHcwk^BP"
    "gxKjEmf+sx4F_5{iI4rW;D`9ogqJL-14lkhO^+|wfm7-XC*4;ir<I16w#<oks&~uVSzB|u{`ku{fFHQO*j6mN`dFrHYX)+sV!5)Lb)Y@(+S4gN9r6fv4bl{^"
    "USo2enT=-3vdx=PyH!q|g7{~i5Vvq7OK^u->YyOm%BJ(BqpP=n0}|KO+kcrk<EwXn8G`f`Y82ji`2N@D&Z`h8KRT6t9-knp{0wA|UpoVk-u}7ExWjMxYr+=_"
    "aD_~sps7ddHqG=2tNULZi5Wd+IbO9pafS7fMPnubakltMEmo5UThe<t@!b<1x1dFC?wK#SAu9VlIwrLO)GDyjB~`=&)y{KOB^)rBj<DfrRvM4r<SIl_@r(KX"
    "J?fhAv{*K;lw5tPY8u8K+F@eoVRoJ;T95L9=v23Zd1g=@d+=GhcWOS;(p#$JlMlnhdZm{j2`gD48q1P8jF`+b{mLAKG<FHgP;69Azm^T~Cf3&-q3t;%RQDVv"
    "hdKjx<RFR@MP)KQ7X^P5oHK5hb1G3UP4yilEmXZ+))H)OtvqRKNSlEv_M*vj<+c<QLSO>OnqW^mPX?3OBzDS-$~$+TJaSu%DFV-tJfP=v0#G}k_yGR%r%pV9"
    ">U}=~A{GSG*ufhXxb2*Ya#b~K!2(&^%VzJCU8lY8Xn7Z~zT`g6<)w4W%C{VA*3ym?UQ&bboY|XM`kiK%VQwr(j|v?7XW^+on?zNd@Le@uWT>~YUgD)h%&VZY"
    "SMzG6k_4!{ZQvirCRo{3a<hRCf8Kx7g<qlX#8+?MRZH1<HesSJ&X$rg6qh(#WkH1OQiD_HP&HT8bP{2pa;O&;TChet#t6ozK|CK~CkaH@^YR6vRor?;7xnqV"
    "UEY@^kTh#vkN@?*&Y81TLC2U29U?5-?0gzb`gGv@7AU9PhSsEAsid!6sx$^|YPd_IIk6k~sB$d^o4wnaM3SGU(>O?QPSB9%N8_^89ocFrn-1H<Y|kDKf?(Ey"
    "4FtWod#NGJ-pou9+_8bq+RLF0j7q{rYCQo?&t!#Fm&I9lPNP8&v8E81xNG#3LJnUf1h4BD`fCC=-Ye6^em3_B#6ZqMp{;|0mIZJhj)uiydGk<ArES$n|FQJ0"
    "I3l?z9`{LDpE~Ze{20E?+r>CNVO!*s)RG1-ilx!HR-&9DuDJynHBWhY=nsdmEIh~dAHwL>8h`GeIUPT8c$jOlxImCmc38%UQxmef23;)zFqMDgmfc;LS4+6<"
    "K*QyXa}Wjnc4@t2Ss|Fo)1VJp&!EU;%!Z?Y8%gvAj{`sIUaLJhP;C+T9Gx}ia6WTz=|mW6Ok}#`yv&jwNd#+h1WU85KfYdL{0NmsAej0q2J4iWp&1Cy``?ZS"
    "4xrXoPo3V?yMI8R!9R{qUClMhYM!u)%5r2pG|cMsE%O1TD@oUoe#t2}VsjXa5%ZdsH^g`5l+8f`bGFqXT*atST!0lFT3Y7^6}d2gSOoKPjC9@El^k}LxSnp*"
    "m>7eZn})8*P;fQN_sMeV&6+b$J1B7^8i%%+?DFTrr+*`};iy_ZWVl**DkOL?HJjwC?T95INei-p&>|r+!8=2NBgv-M53E{niB%LtShKCzuW;SdN4C(Z!e<}S"
    "n^Avxho)6#<sQuja=J|D6=N`HR~yG6lH-^Hpa4k}0hbhTEbri9;o9O5hX(nF%#g(2EaNFzZ6LEgWCt*=)q>^|&8eI?&0RgzhY1U-f*>OTE?pag#-yE59RF^o"
    "LMCt5_skH@XXEE?_MO$^=iXQtoEDkkc$UF1ZHy15$o=lzj|Wuq8)4;j#(2g1EPDTUh>Pr8UT=PW_3qDS1+Mo^jEYRKNDmJro$%*!*JQ+&+i!a37b4Q21xuiv"
    "zN7fbGE9i$U}PXlF*AAUELP5Rb(dx4*)&h(C{j)HYdD8I*;tDQG&$jz%SV{BIX9M$+7Z(PSuA%Z47m+!&Rjnnl9}EBZ0XK=+>OHNY#+aNCcSg^WiT5J;miL5"
    "$F#!d"
)
_EMBEDDED_REPLAY_TEMPLATE_CACHE = None

def _get_embedded_replay_template():
    global _EMBEDDED_REPLAY_TEMPLATE_CACHE
    if _EMBEDDED_REPLAY_TEMPLATE_CACHE is None:
        raw = zlib.decompress(base64.b85decode(_EMBEDDED_REPLAY_TEMPLATE_B85)).decode("utf-8")
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        if digest != _EMBEDDED_REPLAY_TEMPLATE_SHA256:
            raise RuntimeError("Embedded replay template integrity check failed")
        _EMBEDDED_REPLAY_TEMPLATE_CACHE = raw
    return _EMBEDDED_REPLAY_TEMPLATE_CACHE

def _render_replay_template(**context):
    try:
        content = render_template_string(_get_embedded_replay_template(), **context)
        return Response(
            content,
            mimetype="text/html",
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )
    except Exception:
        logger.exception("Embedded replay template failed; falling back to external index.html")
        return render_template("index.html", **context)


def _safe_hex(value, fallback):
    return value if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value) else fallback


def get_site_config():
    configured = CONFIG.get("site", {})
    configured_theme = configured.get("theme", {}) if isinstance(configured, dict) else {}
    default_theme = DEFAULT_SITE["theme"]
    theme = {
        key: _safe_hex(configured_theme.get(key), value)
        for key, value in default_theme.items()
    }
    name = configured.get("name", DEFAULT_SITE["name"]) if isinstance(configured, dict) else DEFAULT_SITE["name"]
    tagline = configured.get("tagline", DEFAULT_SITE["tagline"]) if isinstance(configured, dict) else DEFAULT_SITE["tagline"]
    return {"name": str(name)[:80], "tagline": str(tagline)[:160], "theme": theme}


def get_camera_name(cam_id):
    if 1 <= cam_id <= len(CAMERA_LIST):
        camera = CAMERA_LIST[cam_id - 1]
        if isinstance(camera, dict) and camera.get("name"):
            return str(camera["name"])
    for table in CONFIG.get("tables", []):
        if isinstance(table, dict) and table.get("camera_id") == cam_id and table.get("name"):
            return str(table["name"])
    return f"Camera {cam_id}"


def site_prefix():
    return f"🏪 {get_site_config()['name']}\n"



def get_camera_recorder_config(camera_or_id):
    if isinstance(camera_or_id, int):
        cam = CAMERA_LIST[camera_or_id - 1] if 1 <= camera_or_id <= len(CAMERA_LIST) else {}
    elif isinstance(camera_or_id, dict):
        cam = camera_or_id
    else:
        cam = {}

    nested = cam.get("nvr") if isinstance(cam.get("nvr"), dict) else {}
    vendor = str(cam.get("vendor") or cam.get("nvr_vendor") or nested.get("vendor") or "hikvision").strip().lower()
    if vendor not in {"hikvision", "dahua"}:
        vendor = "hikvision"

    host = str(cam.get("host") or cam.get("ip") or nested.get("host") or "").strip()
    http_port = int(cam.get("http_port") or nested.get("http_port") or (80 if vendor == "hikvision" else 81))
    rtsp_port = int(cam.get("rtsp_port") or cam.get("port") or nested.get("rtsp_port") or 554)
    username = str(cam.get("user") or cam.get("username") or nested.get("username") or "").strip()
    password = str(cam.get("pass") if cam.get("pass") is not None else cam.get("password", nested.get("password", "")))
    stream = str(cam.get("stream") or nested.get("stream") or "main").strip().lower()

    channel_raw = cam.get("nvr_channel") or cam.get("channel") or nested.get("channel") or (camera_or_id if isinstance(camera_or_id, int) else 1)
    try:
        channel = int(channel_raw)
    except (TypeError, ValueError):
        channel = 1
    if channel < 1:
        channel = 1

    return {
        "vendor": vendor,
        "host": host,
        "http_port": http_port,
        "rtsp_port": rtsp_port,
        "username": username,
        "password": password,
        "stream": stream,
        "nvr_channel": channel,
        "channel_map": {"1": channel},
        "use_https": bool(cam.get("use_https", nested.get("use_https", False))),
        "verify_tls": bool(cam.get("verify_tls", nested.get("verify_tls", False))),
        "timezone_offset_minutes": int(cam.get("timezone_offset_minutes", nested.get("timezone_offset_minutes", 420)) or 420),
        "connect_timeout_sec": max(1, int(cam.get("connect_timeout_sec", nested.get("connect_timeout_sec", 5)) or 5)),
        "read_timeout_sec": max(5, int(cam.get("read_timeout_sec", nested.get("read_timeout_sec", 30)) or 30)),
        "max_search_pages": max(1, min(50, int(cam.get("max_search_pages", nested.get("max_search_pages", 12)) or 12))),
        "playback_chunk_sec": max(30, min(3600, int(cam.get("playback_chunk_sec", nested.get("playback_chunk_sec", 300)) or 300))),
        "backup_local": bool(cam.get("backup_local", False)),
    }


def get_playback_source_config(source=None):
    root = source if isinstance(source, dict) else CONFIG
    configured = root.get("playback_source", {}) if isinstance(root, dict) else {}
    if isinstance(configured, dict) and configured.get("nvr"):
        return configured
    # Fallback to first NVR camera if available
    for idx, cam in enumerate(root.get("cameras", []), start=1):
        if str(cam.get("playback_source", "")).strip().lower() == "nvr":
            rec = get_camera_recorder_config(cam)
            return {"mode": "nvr", "nvr": rec}
    return {
        "mode": "local",
        "nvr": {
            "vendor": "hikvision",
            "host": "",
            "http_port": 80,
            "rtsp_port": 554,
            "username": "admin",
            "password": "",
            "stream": "main",
            "timezone_offset_minutes": 420,
            "connect_timeout_sec": 5,
            "read_timeout_sec": 30,
            "max_search_pages": 12,
            "playback_chunk_sec": 300,
            "channel_map": {},
        },
    }


def camera_has_nvr(camera):
    if not isinstance(camera, dict):
        return False
    rec = get_camera_recorder_config(camera)
    return bool(rec.get("host") and rec.get("username"))


def should_record_locally(camera):
    if not isinstance(camera, dict):
        return False
    mode = str(camera.get("playback_source", "local")).strip().lower()
    if mode == "nvr":
        # Local recording is primary; defaults to True
        return bool(camera.get("backup_local", True))
    return True


def _camera_playback_mode(cam_id, playback=None):
    if isinstance(cam_id, int) and 1 <= cam_id <= len(CAMERA_LIST):
        camera = CAMERA_LIST[cam_id - 1]
        if isinstance(camera, dict):
            mode = str(camera.get("playback_source", "")).strip().lower()
            if mode == "nvr":
                # If local recording is enabled, Local is primary playback mode
                if should_record_locally(camera):
                    return "local"
                return "nvr"
            if mode in {"local", "hybrid"}:
                return "local"
    return "local"


def _timeline_playback_mode(cam_id):
    """Return the active replay source without mutating persistent camera config."""
    return _camera_playback_mode(cam_id)


def _nvr_base_url(nvr):
    scheme = "https" if nvr.get("use_https") else "http"
    return f"{scheme}://{nvr['host']}:{int(nvr.get('http_port', 80))}"


def _nvr_auth(nvr):
    return HTTPDigestAuth(nvr.get("username", ""), nvr.get("password", ""))


def _nvr_request_timeout(nvr, media=False):
    connect = max(1, int(nvr.get("connect_timeout_sec", 5)))
    read = max(5, int(nvr.get("read_timeout_sec", 30)))
    if media:
        read = max(read, 180)
    return (connect, read)


def _nvr_channel_number(cam_id, nvr):
    if isinstance(nvr, dict) and "nvr_channel" in nvr:
        try:
            return int(nvr["nvr_channel"])
        except (TypeError, ValueError):
            pass
    raw_channel = nvr.get("channel_map", {}).get(str(cam_id)) if isinstance(nvr, dict) else None
    if raw_channel in (None, "") and 1 <= cam_id <= len(CAMERA_LIST):
        camera = CAMERA_LIST[cam_id - 1]
        if isinstance(camera, dict):
            raw_channel = camera.get("nvr_channel")
    if raw_channel in (None, ""):
        raw_channel = cam_id
    try:
        channel = int(raw_channel)
    except (TypeError, ValueError, OverflowError):
        channel = cam_id
    return channel if channel >= 1 else cam_id


def _nvr_track_id(cam_id, nvr):
    channel = _nvr_channel_number(cam_id, nvr)
    stream_number = 2 if str(nvr.get("stream", "main")).lower() == "sub" else 1
    return channel * 100 + stream_number


def _xml_local_name(tag):
    return str(tag).rsplit("}", 1)[-1]


def _xml_descendant_text(node, name):
    wanted = str(name).lower()
    for child in node.iter():
        if _xml_local_name(child.tag).lower() == wanted and child.text:
            return child.text.strip()
    return ""


def _local_to_nvr_utc(value, nvr):
    offset = timezone(timedelta(minutes=int(nvr.get("timezone_offset_minutes", 420))))
    return value.replace(tzinfo=offset).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _nvr_time_to_local(value, nvr):
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("NVR không trả về thời gian bản ghi.")
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed
    local_zone = timezone(timedelta(minutes=int(nvr.get("timezone_offset_minutes", 420))))
    return parsed.astimezone(local_zone).replace(tzinfo=None)


def _remember_nvr_reference(reference):
    now = time.time()
    token = uuid.uuid4().hex
    with NVR_REFERENCE_LOCK:
        for key, item in list(NVR_REFERENCE_CACHE.items()):
            if item.get("expires_at", 0) <= now:
                NVR_REFERENCE_CACHE.pop(key, None)
        NVR_REFERENCE_CACHE[token] = {**reference, "expires_at": now + NVR_REFERENCE_TTL_SEC}
    return token


def _get_nvr_reference(token):
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
        return None
    now = time.time()
    with NVR_REFERENCE_LOCK:
        item = NVR_REFERENCE_CACHE.get(token)
        if not item or item.get("expires_at", 0) <= now:
            NVR_REFERENCE_CACHE.pop(token, None)
            return None
        return dict(item)


def _hikvision_search_body(track_id, start_utc, end_utc, position, max_results=40, search_id=None):
    search_id = search_id or uuid.uuid4().hex
    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        "<CMSearchDescription>"
        f"<searchID>{search_id}</searchID>"
        f"<trackIDList><trackID>{int(track_id)}</trackID></trackIDList>"
        "<timeSpanList><timeSpan>"
        f"<startTime>{start_utc}</startTime><endTime>{end_utc}</endTime>"
        "</timeSpan></timeSpanList>"
        f"<maxResults>{int(max_results)}</maxResults>"
        f"<searchResultPostion>{int(position)}</searchResultPostion>"
        "<metadataList><metadataDescriptor>//recordType.meta.std-cgi.com</metadataDescriptor></metadataList>"
        "</CMSearchDescription>"
    )


def _search_hikvision_camera(cam_id, window_start, window_end, nvr):
    track_id = _nvr_track_id(cam_id, nvr)
    endpoint = _nvr_base_url(nvr) + "/ISAPI/ContentMgmt/search"
    start_utc = _local_to_nvr_utc(window_start, nvr)
    end_utc = _local_to_nvr_utc(window_end, nvr)
    results = []
    max_results = 40
    search_id = uuid.uuid4().hex
    for page in range(int(nvr.get("max_search_pages", 12))):
        response = requests.post(
            endpoint,
            data=_hikvision_search_body(track_id, start_utc, end_utc, page * max_results, max_results, search_id),
            headers={"Content-Type": "application/xml", "Accept": "application/xml"},
            auth=_nvr_auth(nvr),
            timeout=_nvr_request_timeout(nvr),
            verify=bool(nvr.get("verify_tls", False)),
        )
        response.raise_for_status()
        try:
            xml_root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise RuntimeError("NVR trả về XML tìm kiếm không hợp lệ.") from exc
        matches = [node for node in xml_root.iter() if _xml_local_name(node.tag) == "searchMatchItem"]
        for item in matches:
            playback_uri = _xml_descendant_text(item, "playbackURI")
            start_value = _xml_descendant_text(item, "startTime")
            end_value = _xml_descendant_text(item, "endTime")
            if not playback_uri or not start_value or not end_value:
                continue
            try:
                started_at = _nvr_time_to_local(start_value, nvr)
                ended_at = _nvr_time_to_local(end_value, nvr)
            except (TypeError, ValueError):
                continue
            if ended_at <= started_at or started_at >= window_end or ended_at <= window_start:
                continue
            token = _remember_nvr_reference({
                "vendor": "hikvision",
                "cam_id": cam_id,
                "track_id": track_id,
                "playback_uri": playback_uri,
                "started_at": started_at.isoformat(timespec="seconds"),
                "ended_at": ended_at.isoformat(timespec="seconds"),
            })
            filename = (
                f"nvr_hik_cam{cam_id}_{started_at.strftime('%H-%M-%S')}_to_"
                f"{ended_at.strftime('%H-%M-%S')}_({started_at.strftime('%d-%m-%Y')}).mp4"
            )
            results.append({
                "filename": filename,
                "cam_id": cam_id,
                "started_at": started_at.isoformat(timespec="seconds"),
                "end_at": ended_at.isoformat(timespec="seconds"),
                "duration_sec": round((ended_at - started_at).total_seconds(), 3),
                "status": "complete",
                "locked": False,
                "note": "",
                "source": "nvr",
                "nvr_vendor": "hikvision",
                "play_url": f"/nvr/video/{token}",
                "download_url": f"/nvr/download/{token}",
                "nvr_track_id": track_id,
            })
        if len(matches) < max_results:
            break
    return results


def _parse_dahua_key_values(text):
    values = {}
    for raw in str(text or "").replace("\r\r", "\r").splitlines():
        line = raw.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _parse_dahua_items(text):
    items = {}
    for raw in str(text or "").replace("\r\r", "\r").splitlines():
        line = raw.strip()
        match = re.match(r"items\[(\d+)\]\.([^=]+)=(.*)", line)
        if not match:
            continue
        index = int(match.group(1))
        items.setdefault(index, {})[match.group(2).strip()] = match.group(3).strip()
    return [items[index] for index in sorted(items)]


def _dahua_quote(value):
    return quote(str(value), safe=":-_/[]@")


def _dahua_get(nvr, path, media=False, stream=False):
    response = requests.get(
        _nvr_base_url(nvr) + path,
        auth=_nvr_auth(nvr),
        timeout=_nvr_request_timeout(nvr, media=media),
        verify=bool(nvr.get("verify_tls", False)),
        stream=stream,
    )
    return response


def _search_dahua_camera(cam_id, window_start, window_end, nvr):
    channel = _nvr_channel_number(cam_id, nvr)
    now = datetime.now()
    if window_end > now:
        window_end = now
    if window_start >= window_end:
        return []
    create = _dahua_get(nvr, "/cgi-bin/mediaFileFind.cgi?action=factory.create")
    create.raise_for_status()
    object_match = re.search(r"result=(\S+)", create.text)
    if not object_match:
        raise RuntimeError("Dahua không tạo được mediaFileFind object.")
    object_id = object_match.group(1)
    results = []
    expected_stream = "Extra1" if str(nvr.get("stream", "main")).lower() == "sub" else "Main"
    try:
        find_path = (
            "/cgi-bin/mediaFileFind.cgi?action=findFile"
            f"&object={_dahua_quote(object_id)}"
            f"&condition.Channel={channel}"
            f"&condition.StartTime={_dahua_quote(window_start.strftime('%Y-%m-%d %H:%M:%S'))}"
            f"&condition.EndTime={_dahua_quote(window_end.strftime('%Y-%m-%d %H:%M:%S'))}"
            "&condition.Types[0]=dav"
        )
        started = _dahua_get(nvr, find_path)
        if started.status_code != 200 or not started.text.strip().upper().startswith("OK"):
            detail = started.text.strip().replace("\r", " ").replace("\n", " ")[:120]
            raise RuntimeError(f"Dahua từ chối tìm bản ghi kênh {channel}: HTTP {started.status_code} {detail}")

        count = 100
        for _page in range(int(nvr.get("max_search_pages", 12))):
            response = _dahua_get(
                nvr,
                f"/cgi-bin/mediaFileFind.cgi?action=findNextFile&object={_dahua_quote(object_id)}&count={count}",
            )
            response.raise_for_status()
            page_items = _parse_dahua_items(response.text)
            for item in page_items:
                video_stream = str(item.get("VideoStream", "")).strip()
                if video_stream and video_stream.lower() != expected_stream.lower():
                    continue
                try:
                    started_at = datetime.strptime(item.get("StartTime", ""), "%Y-%m-%d %H:%M:%S")
                    ended_at = datetime.strptime(item.get("EndTime", ""), "%Y-%m-%d %H:%M:%S")
                except (TypeError, ValueError):
                    continue
                if ended_at <= started_at or started_at >= window_end or ended_at <= window_start:
                    continue
                file_path = str(item.get("FilePath", "")).strip()
                if not file_path:
                    continue
                token = _remember_nvr_reference({
                    "vendor": "dahua",
                    "cam_id": cam_id,
                    "nvr_channel": channel,
                    "file_path": file_path,
                    "video_stream": video_stream or expected_stream,
                    "started_at": started_at.isoformat(timespec="seconds"),
                    "ended_at": ended_at.isoformat(timespec="seconds"),
                })
                filename = (
                    f"nvr_dahua_cam{cam_id}_{started_at.strftime('%H-%M-%S')}_to_"
                    f"{ended_at.strftime('%H-%M-%S')}_({started_at.strftime('%d-%m-%Y')}).mp4"
                )
                try:
                    length = int(item.get("Length", 0) or 0)
                except (TypeError, ValueError):
                    length = 0
                results.append({
                    "filename": filename,
                    "cam_id": cam_id,
                    "started_at": started_at.isoformat(timespec="seconds"),
                    "end_at": ended_at.isoformat(timespec="seconds"),
                    "duration_sec": round((ended_at - started_at).total_seconds(), 3),
                    "status": "complete",
                    "locked": False,
                    "note": "",
                    "source": "nvr",
                    "nvr_vendor": "dahua",
                    "play_url": f"/nvr/video/{token}",
                    "download_url": f"/nvr/download/{token}",
                    "nvr_channel": channel,
                    "size_bytes": length,
                    "video_stream": video_stream or expected_stream,
                })
            found_match = re.search(r"(?:^|[\r\n])found=(\d+)", response.text)
            found = int(found_match.group(1)) if found_match else len(page_items)
            if found < count:
                break
    finally:
        try:
            close = _dahua_get(
                nvr,
                f"/cgi-bin/mediaFileFind.cgi?action=close&object={_dahua_quote(object_id)}",
            )
            close.close()
        except requests.RequestException:
            pass
    return results


def _nvr_camera_ids(requested_ids=None):
    if requested_ids is not None:
        return sorted({
            cam_id for cam_id in requested_ids
            if isinstance(cam_id, int) and 1 <= cam_id <= len(CAMERA_LIST)
            and _camera_playback_mode(cam_id) == "nvr"
        })
    return [
        cam_id for cam_id in range(1, len(CAMERA_LIST) + 1)
        if _camera_playback_mode(cam_id) == "nvr"
    ]


def search_nvr_timeline(window_start, window_end, camera_ids=None):
    camera_ids = _nvr_camera_ids(camera_ids)
    if not camera_ids:
        return [], []

    def _search_cam(cid):
        rec = get_camera_recorder_config(cid)
        vendor = rec.get("vendor", "hikvision")
        if vendor not in {"hikvision", "dahua"}:
            raise RuntimeError(f"Hãng NVR của camera {cid} không được hỗ trợ.")
        if not rec.get("host") or not rec.get("username"):
            raise RuntimeError(f"Chưa cấu hình IP/host và tài khoản NVR cho camera {cid}.")
        searcher = _search_dahua_camera if vendor == "dahua" else _search_hikvision_camera
        return searcher(cid, window_start, window_end, rec)

    segments, warnings = [], []
    with ThreadPoolExecutor(max_workers=min(6, len(camera_ids)), thread_name_prefix="NvrSearch") as executor:
        futures = {
            executor.submit(_search_cam, cam_id): cam_id
            for cam_id in camera_ids
        }
        for future in as_completed(futures):
            cam_id = futures[future]
            try:
                segments.extend(future.result())
            except Exception as exc:
                logger.warning("[NVR] Tìm bản ghi camera %s thất bại: %s", cam_id, exc)
                warnings.append(f"Camera {cam_id}: {exc}")
    segments.sort(key=lambda item: (item["started_at"], item["cam_id"]))
    if not segments and len(warnings) == len(camera_ids):
        raise RuntimeError("Không thể đọc bản ghi từ NVR: " + warnings[0])
    return segments, warnings


def test_nvr_connection(nvr=None):
    config = nvr or get_playback_source_config()["nvr"]
    vendor = config.get("vendor")
    if vendor not in {"hikvision", "dahua"}:
        return {"ok": False, "message": "Hãng NVR không được hỗ trợ."}
    if not config.get("host") or not config.get("username"):
        return {"ok": False, "message": "Cần nhập IP/host và tài khoản NVR."}
    if vendor == "dahua":
        try:
            response = _dahua_get(config, "/cgi-bin/magicBox.cgi?action=getSystemInfo")
            response.raise_for_status()
            info = _parse_dahua_key_values(response.text)
            cap = _dahua_get(config, "/cap.js")
            model_match = re.search(r"devType=['\"]([^'\"]+)", cap.text) if cap.status_code == 200 else None
            model = model_match.group(1) if model_match else info.get("updateSerial", "Dahua")
            titles = _dahua_get(config, "/cgi-bin/configManager.cgi?action=getConfig&name=ChannelTitle")
            channel_count = len(re.findall(r"table\.ChannelTitle\[\d+\]\.Name=", titles.text)) if titles.status_code == 200 else 0
            return {
                "ok": True,
                "message": f"Kết nối Dahua thành công: {model}" + (f" · {channel_count} kênh" if channel_count else ""),
                "model": model,
                "name": info.get("updateSerial", ""),
                "channels": channel_count,
            }
        except requests.RequestException as exc:
            return {"ok": False, "message": f"Không kết nối được Dahua NVR: {exc}"}

    try:
        response = requests.get(
            _nvr_base_url(config) + "/ISAPI/System/deviceInfo",
            headers={"Accept": "application/xml"},
            auth=_nvr_auth(config),
            timeout=_nvr_request_timeout(config),
            verify=bool(config.get("verify_tls", False)),
        )
        response.raise_for_status()
        xml_root = ET.fromstring(response.content)
        model = _xml_descendant_text(xml_root, "model") or "Hikvision"
        name = _xml_descendant_text(xml_root, "deviceName")
        label = f"{model} · {name}" if name else model
        return {"ok": True, "message": f"Kết nối Hikvision thành công: {label}", "model": model, "name": name}
    except requests.RequestException as exc:
        return {"ok": False, "message": f"Không kết nối được Hikvision NVR: {exc}"}
    except ET.ParseError:
        return {"ok": False, "message": "NVR có phản hồi nhưng XML deviceInfo không hợp lệ."}


def _clean_nvr_media_cache():
    cutoff = time.time() - NVR_MEDIA_CACHE_MAX_AGE_SEC
    try:
        entries = list(os.scandir(NVR_CACHE_DIR))
    except OSError:
        return
    for entry in entries:
        if not entry.is_file(follow_symlinks=False):
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                os.remove(entry.path)
        except OSError:
            continue


def _resolve_dahua_bounds(reference, nvr, at_value=None, chunk_limit=True):
    try:
        clip_start = datetime.fromisoformat(str(reference["started_at"]))
        clip_end = datetime.fromisoformat(str(reference["ended_at"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Metadata thời gian Dahua không hợp lệ.") from exc
    if clip_end <= clip_start:
        raise RuntimeError("Khoảng playback Dahua không hợp lệ.")
    requested_start = clip_start
    if at_value:
        try:
            candidate = datetime.fromisoformat(str(at_value).strip())
            if candidate.tzinfo is not None:
                candidate = candidate.replace(tzinfo=None)
            requested_start = max(clip_start, min(candidate, clip_end - timedelta(seconds=1)))
        except (TypeError, ValueError):
            raise ValueError("Thời điểm playback Dahua không hợp lệ.")
    requested_end = clip_end
    if chunk_limit:
        requested_end = min(
            clip_end,
            requested_start + timedelta(seconds=int(nvr.get("playback_chunk_sec", 300))),
        )
    if requested_end <= requested_start:
        requested_end = min(clip_end, requested_start + timedelta(seconds=1))
    return requested_start, requested_end


def _dahua_loadfile_path(reference, nvr, start_dt, end_dt):
    channel = int(reference.get("nvr_channel") or _nvr_channel_number(reference.get("cam_id", 1), nvr))
    subtype = 1 if str(nvr.get("stream", "main")).lower() == "sub" else 0
    return (
        "/cgi-bin/loadfile.cgi?action=startLoad"
        f"&channel={channel}"
        f"&startTime={_dahua_quote(start_dt.strftime('%Y-%m-%d %H:%M:%S'))}"
        f"&endTime={_dahua_quote(end_dt.strftime('%Y-%m-%d %H:%M:%S'))}"
        f"&subtype={subtype}&Types=dav"
    )


def _dahua_rtsp_url(reference, nvr, start_dt, end_dt):
    channel = int(reference.get("nvr_channel") or _nvr_channel_number(reference.get("cam_id", 1), nvr))
    subtype = 1 if str(nvr.get("stream", "main")).lower() == "sub" else 0
    username = quote(str(nvr.get("username", "")), safe="")
    password = quote(str(nvr.get("password", "")), safe="")
    start_value = start_dt.strftime("%Y_%m_%d_%H_%M_%S")
    end_value = end_dt.strftime("%Y_%m_%d_%H_%M_%S")
    return (
        f"rtsp://{username}:{password}@{nvr['host']}:{int(nvr.get('rtsp_port', 554))}"
        f"/cam/playback?channel={channel}&subtype={subtype}"
        f"&starttime={start_value}&endtime={end_value}"
    )


def _terminate_media_process(proc):
    if not proc or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _stream_dahua_via_rtsp(reference, nvr, start_dt, end_dt):
    rtsp_url = _dahua_rtsp_url(reference, nvr, start_dt, end_dt)
    duration = max(1, int((end_dt - start_dt).total_seconds()))
    cmd = [
        FFMPEG_PATH, "-hide_banner", "-loglevel", "error",
        "-rw_timeout", "10000000", "-rtsp_transport", "tcp", "-i", rtsp_url,
        "-t", str(duration), "-map", "0:v:0", "-c:v", "copy", "-an",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1",
    ]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )

    def generate():
        try:
            while True:
                chunk = proc.stdout.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        except GeneratorExit:
            return
        finally:
            _terminate_media_process(proc)
            try:
                proc.stdout.close()
            except Exception:
                pass
            try:
                stderr = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()
                if stderr:
                    logger.warning("[NVR:dahua] RTSP playback FFmpeg: %s", stderr[-500:])
            except Exception:
                pass

    response = Response(stream_with_context(generate()), mimetype="video/mp4")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-NVR-Transport"] = "dahua-rtsp"
    return response


def _stream_dahua_video(reference, nvr, at_value=None):
    start_dt, end_dt = _resolve_dahua_bounds(reference, nvr, at_value=at_value, chunk_limit=True)
    load_path = _dahua_loadfile_path(reference, nvr, start_dt, end_dt)
    try:
        upstream = _dahua_get(nvr, load_path, media=True, stream=True)
        upstream.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("[NVR:dahua] loadfile.cgi lỗi, chuyển sang RTSP playback: %s", exc)
        try:
            if 'upstream' in locals():
                upstream.close()
        except Exception:
            pass
        return _stream_dahua_via_rtsp(reference, nvr, start_dt, end_dt)

    proc = subprocess.Popen(
        [
            FFMPEG_PATH, "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0", "-map", "0:v:0", "-c:v", "copy", "-an",
            "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    stop_event = threading.Event()

    def feed_upstream():
        try:
            for chunk in upstream.iter_content(chunk_size=256 * 1024):
                if stop_event.is_set():
                    break
                if chunk:
                    proc.stdin.write(chunk)
        except Exception as exc:
            if not stop_event.is_set():
                logger.warning("[NVR:dahua] Lỗi đọc loadfile.cgi: %s", exc)
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass
            upstream.close()

    threading.Thread(target=feed_upstream, daemon=True, name="DahuaLoadfileFeed").start()

    def generate():
        try:
            while True:
                chunk = proc.stdout.read(64 * 1024)
                if not chunk:
                    break
                yield chunk
        except GeneratorExit:
            return
        finally:
            stop_event.set()
            upstream.close()
            _terminate_media_process(proc)
            try:
                proc.stdout.close()
            except Exception:
                pass
            try:
                stderr = (proc.stderr.read() or b"").decode("utf-8", "replace").strip()
                if stderr:
                    logger.warning("[NVR:dahua] FFmpeg DHAV->fMP4: %s", stderr[-500:])
            except Exception:
                pass

    response = Response(stream_with_context(generate()), mimetype="video/mp4")
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-NVR-Transport"] = "dahua-cgi-stream"
    response.headers["X-NVR-Chunk-Seconds"] = str(int((end_dt - start_dt).total_seconds()))
    return response


def _download_hikvision_reference(token, reference, nvr):
    final_path = os.path.join(NVR_CACHE_DIR, f"{token}.mp4")
    if os.path.isfile(final_path) and os.path.getsize(final_path) > 0:
        return final_path
    playback_uri = str(reference["playback_uri"])
    body = (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        "<downloadRequest><playbackURI>"
        + playback_uri.replace("&", "&amp;")
        + "</playbackURI></downloadRequest>"
    )
    endpoint = _nvr_base_url(nvr) + "/ISAPI/ContentMgmt/download"
    raw_path = os.path.join(NVR_CACHE_DIR, f"{token}.download")
    temp_mp4 = os.path.join(NVR_CACHE_DIR, f"{token}.part.mp4")
    _clean_nvr_media_cache()
    with NVR_MEDIA_LOCK:
        if os.path.isfile(final_path) and os.path.getsize(final_path) > 0:
            return final_path
        response = None
        try:
            response = requests.post(
                endpoint,
                data=body,
                headers={"Content-Type": "application/xml", "Accept": "application/octet-stream"},
                auth=_nvr_auth(nvr),
                timeout=_nvr_request_timeout(nvr, media=True),
                verify=bool(nvr.get("verify_tls", False)),
                stream=True,
            )
            if response.status_code in {400, 404, 405, 501}:
                response.close()
                response = requests.get(
                    endpoint,
                    data=body,
                    headers={"Content-Type": "application/xml", "Accept": "application/octet-stream"},
                    auth=_nvr_auth(nvr),
                    timeout=_nvr_request_timeout(nvr, media=True),
                    verify=bool(nvr.get("verify_tls", False)),
                    stream=True,
                )
            response.raise_for_status()
            with open(raw_path, "wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        output.write(chunk)
        finally:
            if response is not None:
                response.close()
        if not os.path.isfile(raw_path) or os.path.getsize(raw_path) <= 0:
            raise RuntimeError("NVR trả về file rỗng.")
        completed = subprocess.run(
            [FFMPEG_PATH, "-y", "-hide_banner", "-loglevel", "error", "-i", raw_path,
             "-map", "0", "-c", "copy", "-movflags", "+faststart", temp_mp4],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=max(180, int(nvr.get("read_timeout_sec", 30)) * 10),
            creationflags=subprocess.CREATE_NO_WINDOW,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode != 0 or not os.path.isfile(temp_mp4) or os.path.getsize(temp_mp4) <= 0:
            tail = (completed.stderr or "").strip().splitlines()[-1:]
            raise RuntimeError(tail[0] if tail else "FFmpeg không remux được video NVR.")
        os.replace(temp_mp4, final_path)
        try:
            os.remove(raw_path)
        except OSError:
            pass
    return final_path


def _download_dahua_reference(token, reference, nvr, at_value=None, end_value=None):
    start_dt, end_dt = _resolve_dahua_bounds(
        reference,
        nvr,
        at_value=at_value,
        chunk_limit=True,
    )
    if end_value:
        try:
            exact_end = datetime.fromisoformat(str(end_value).strip())
            if exact_end.tzinfo is not None:
                exact_end = exact_end.replace(tzinfo=None)
            end_dt = min(end_dt, exact_end)
        except (TypeError, ValueError):
            raise ValueError("Thời điểm kết thúc playback Dahua không hợp lệ.")
        if end_dt <= start_dt:
            end_dt = min(
                datetime.fromisoformat(str(reference["ended_at"])),
                start_dt + timedelta(seconds=1),
            )
    cache_key = hashlib.sha256(
        f"{token}|{start_dt.isoformat()}|{end_dt.isoformat()}".encode("utf-8")
    ).hexdigest()[:16]
    final_path = os.path.join(NVR_CACHE_DIR, f"dahua_{cache_key}.mp4")
    if os.path.isfile(final_path) and os.path.getsize(final_path) > 0:
        return final_path
    raw_path = os.path.join(NVR_CACHE_DIR, f"dahua_{cache_key}.dav")
    temp_mp4 = os.path.join(NVR_CACHE_DIR, f"dahua_{cache_key}.part.mp4")
    _clean_nvr_media_cache()
    with NVR_MEDIA_LOCK:
        if os.path.isfile(final_path) and os.path.getsize(final_path) > 0:
            return final_path
        response = _dahua_get(
            nvr,
            _dahua_loadfile_path(reference, nvr, start_dt, end_dt),
            media=True,
            stream=True,
        )
        try:
            response.raise_for_status()
            with open(raw_path, "wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        output.write(chunk)
        finally:
            response.close()
        if not os.path.isfile(raw_path) or os.path.getsize(raw_path) <= 0:
            raise RuntimeError("Dahua loadfile.cgi trả về file rỗng.")
        completed = subprocess.run(
            [
                FFMPEG_PATH, "-y", "-hide_banner", "-loglevel", "error", "-i", raw_path,
                "-map", "0:v:0", "-c:v", "copy", "-an", "-movflags", "+faststart", temp_mp4,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=max(180, int(nvr.get("read_timeout_sec", 30)) * 20),
            creationflags=subprocess.CREATE_NO_WINDOW,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode != 0 or not os.path.isfile(temp_mp4) or os.path.getsize(temp_mp4) <= 0:
            tail = (completed.stderr or "").strip().splitlines()[-1:]
            raise RuntimeError(tail[0] if tail else "FFmpeg không remux được DHAV.")
        os.replace(temp_mp4, final_path)
        try:
            os.remove(raw_path)
        except OSError:
            pass
    return final_path


def _download_nvr_reference(token, at_value=None):
    reference = _get_nvr_reference(token)
    if not reference:
        raise FileNotFoundError("Liên kết NVR đã hết hạn. Hãy tải lại timeline.")
    cam_id = reference.get("cam_id", 1)
    nvr = get_camera_recorder_config(cam_id)
    vendor = str(reference.get("vendor") or nvr.get("vendor") or "hikvision").lower()
    if vendor == "dahua":
        return _download_dahua_reference(token, reference, nvr, at_value=at_value)
    if vendor == "hikvision":
        return _download_hikvision_reference(token, reference, nvr)
    raise RuntimeError("Nguồn NVR không được hỗ trợ.")


def validate_config(candidate):
    if not isinstance(candidate, dict):
        return "Cấu hình phải là một đối tượng JSON."
    admin_auth = candidate.get("admin_auth")
    if (
        not isinstance(admin_auth, dict)
        or not str(admin_auth.get("username", "")).strip()
        or not str(admin_auth.get("password", "")).strip()
    ):
        return "admin_auth cần có username và password."
    cameras = candidate.get("cameras", [])
    if not isinstance(cameras, list):
        return "'cameras' phải là một danh sách."

    root_ps = candidate.get("playback_source")
    if isinstance(root_ps, dict):
        root_mode = str(root_ps.get("mode", "")).strip().lower()
        if root_mode == "hybrid":
            return "Chế độ Hybrid đã bị loại bỏ. Chỉ chọn Local hoặc NVR."

    for index, camera in enumerate(cameras, start=1):
        if not isinstance(camera, dict):
            return f"Camera {index} phải là một đối tượng cấu hình."
        mode = str(camera.get("playback_source", "local")).strip().lower()
        if mode == "hybrid":
            return f"Camera {index}: chế độ Hybrid đã bị loại bỏ. Chỉ chọn Local hoặc NVR."
        if mode not in {"local", "nvr"}:
            return f"Camera {index}: nguồn chỉ được chọn Local hoặc NVR."
        if "view_stream" in camera and str(camera.get("view_stream", "")).strip().lower() not in {"auto", "main", "sub"}:
            return f"Camera {index}: view_stream chi nhan auto, main hoac sub."
        if mode == "local":
            if not all(camera.get(key) for key in ("ip", "user", "pass")):
                return f"Camera {index} cần có ip, user và pass cho luồng Local."
            transport = str(camera.get("local_transport", "rtsp")).strip().lower()
            if transport not in {"rtsp", "netsdk"}:
                return f"Camera {index}: local_transport chi nhan rtsp hoac netsdk."
            if transport == "netsdk":
                mixed_keys = sorted(
                    key
                    for key in (_LOCAL_RTSP_ONLY_KEYS | _NVR_ONLY_KEYS)
                    if key in camera
                )
                if mixed_keys:
                    return (
                        f"Camera {index}: NetSDK không nhận trường RTSP/NVR cũ: "
                        + ", ".join(mixed_keys)
                        + "."
                    )
                try:
                    private_port = int(camera.get("netsdk_port", 37777) or 37777)
                    private_channel = int(camera.get("netsdk_channel", 1) or 1)
                except (TypeError, ValueError):
                    return f"Camera {index}: cau hinh NetSDK 37777 khong hop le."
                if not 1 <= private_port <= 65535:
                    return f"Camera {index}: netsdk_port nam ngoai pham vi 1-65535."
                if private_channel < 1:
                    return f"Camera {index}: netsdk_channel phai tu 1 tro len."
                if "netsdk_stream" in camera and str(camera.get("netsdk_stream", "")).strip().lower() not in {"main", "sub"}:
                    return f"Camera {index}: netsdk_stream chi nhan main hoac sub."
            else:
                mixed_keys = sorted(
                    key
                    for key in (_LOCAL_NETSDK_ONLY_KEYS | _NVR_ONLY_KEYS | _LEGACY_CAMERA_KEYS)
                    if key in camera
                )
                if mixed_keys:
                    return (
                        f"Camera {index}: RTSP không nhận trường NetSDK/NVR cũ: "
                        + ", ".join(mixed_keys)
                        + "."
                    )
            if transport == "rtsp" and "port" in camera:
                try:
                    port = int(camera["port"])
                    if not 1 <= port <= 65535:
                        return f"Camera {index}: port camera nằm ngoài phạm vi 1-65535."
                except (TypeError, ValueError):
                    return f"Camera {index}: port camera không hợp lệ."
        elif mode == "nvr":
            mixed_keys = sorted(
                key
                for key in (
                    (_LOCAL_RTSP_ONLY_KEYS - {"vendor"})
                    | _LOCAL_NETSDK_ONLY_KEYS
                    | {"ip", "local_transport"}
                    | _LEGACY_CAMERA_KEYS
                )
                if key in camera
            )
            if mixed_keys:
                return (
                    f"Camera {index} (NVR) không nhận trường Local cũ: "
                    + ", ".join(mixed_keys)
                    + "."
                )
            host = str(camera.get("host") or camera.get("ip") or "").strip()
            user = str(camera.get("user") or camera.get("username") or "").strip()
            if not host or not user:
                return f"Camera {index} (NVR): cần có IP/host và tài khoản đầu ghi."
            vendor = str(camera.get("vendor") or camera.get("nvr_vendor") or "hikvision").strip().lower()
            if vendor not in {"hikvision", "dahua"}:
                return f"Camera {index} (NVR): hãng đầu ghi chỉ nhận hikvision hoặc dahua."
            raw_channel = camera.get("nvr_channel") or camera.get("channel")
            if raw_channel is None:
                return f"Camera {index}: cần chọn Kênh NVR."
            try:
                channel = int(raw_channel)
            except (TypeError, ValueError, OverflowError):
                return f"Camera {index}: kênh NVR không hợp lệ."
            if channel < 1:
                return f"Camera {index}: cần chọn Kênh NVR."
            for p_key in ("http_port", "rtsp_port"):
                if p_key in camera:
                    try:
                        p_val = int(camera[p_key])
                        if not 1 <= p_val <= 65535:
                            return f"Camera {index}: {p_key} nằm ngoài phạm vi 1-65535."
                    except (TypeError, ValueError):
                        return f"Camera {index}: {p_key} không hợp lệ."
            if "playback_chunk_sec" in camera:
                try:
                    chunk = int(camera["playback_chunk_sec"])
                    if not 30 <= chunk <= 3600:
                        return f"Camera {index}: playback_chunk_sec phải nằm trong khoảng 30-3600 giây."
                except (TypeError, ValueError):
                    return f"Camera {index}: playback_chunk_sec không hợp lệ."
    for key, default in (("video_dir", "cctv_videos"), ("db_path", "analytics.db"), ("log_dir", "logs")):
        configured_path = candidate.get(key, default)
        try:
            drive = os.path.splitdrive(os.path.abspath(config_path(configured_path, default)))[0].upper()
        except (TypeError, ValueError, OSError):
            return f"'{key}' không hợp lệ."
        if drive != "D:":
            return f"'{key}' phải nằm trên ổ D:."
    for key, minimum, maximum in (
        ("server_port", 1, 65535),
        ("record_duration_sec", 10, 86_400),
        ("record_timeout_sec", 15, 86_500),
        ("camera_stall_sec", 60, 86_400),
        ("max_merge_minutes", 1, 1_440),
        ("retention_days", 1, 3_650),
    ):
        if key in candidate:
            try:
                value = int(candidate[key])
            except (TypeError, ValueError):
                return f"'{key}' phải là số nguyên."
            if not minimum <= value <= maximum:
                return f"'{key}' phải nằm trong khoảng {minimum}-{maximum}."
    if "disk_limit_gb" in candidate:
        try:
            disk_limit = float(candidate["disk_limit_gb"])
        except (TypeError, ValueError):
            return "'disk_limit_gb' phải là số."
        if not math.isfinite(disk_limit) or disk_limit <= 0:
            return "'disk_limit_gb' phải lớn hơn 0."
    if int(candidate.get("record_timeout_sec", TIMEOUT)) <= int(candidate.get("record_duration_sec", DURATION)):
        return "record_timeout_sec phải lớn hơn record_duration_sec."
    tables = candidate.get("tables", [])
    if not isinstance(tables, list):
        return "'tables' phải là một danh sách."
    for table in tables:
        if not isinstance(table, dict) or not table.get("id") or not table.get("name"):
            return "Mỗi bàn cần có id và name."
        try:
            camera_id = int(table.get("camera_id", 0))
        except (TypeError, ValueError):
            return f"Bàn {table.get('id')} có camera_id không hợp lệ."
        if camera_id and not 1 <= camera_id <= len(cameras):
            return f"Bàn {table.get('id')} tham chiếu camera không tồn tại."
    return None


def save_config(candidate):
    config_file = os.path.join(BASE_DIR, "config.json")
    tmp_file = config_file + ".tmp"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(candidate, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_file, config_file)


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if session.get("admin_authenticated") is True:
            return view(*args, **kwargs)
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": "Cần đăng nhập quản trị."}), 401
        return redirect(url_for("admin_login", next=request.full_path))
    return wrapped


def resolve_view_stream(camera, requested_stream=None, context=None):
    """Resolve viewing/preview stream to either 'main' or 'sub'.

    Recording is ALWAYS Main stream and NEVER calls this function.
    Deterministic resolution rules:
    1. If requested_stream is explicitly 'main' or 'sub', return it.
    2. If requested_stream is 'auto' or None:
       - If camera's configured view_stream is 'main', return 'main'.
       - If camera's configured view_stream is 'sub', return 'sub'.
       - If camera's configured view_stream is 'auto' (or unspecified):
         * When context is 'grid' (e.g. multi-camera /live preview), return 'sub'
           to keep bandwidth and CPU usage lightweight.
         * When context is 'single' (e.g. single-camera /replay page), return 'main'
           for high quality.
         * If context cannot be determined, return 'sub' as safe documented default.
    """
    if isinstance(camera, int):
        cam = CAMERA_LIST[camera - 1] if 1 <= camera <= len(CAMERA_LIST) else {}
    elif isinstance(camera, dict):
        cam = camera
    else:
        cam = {}

    req = str(requested_stream or "").strip().lower()
    if req in {"main", "sub"}:
        return req

    cfg_stream = str(cam.get("view_stream") or "").strip().lower()
    if cfg_stream not in {"auto", "main", "sub"}:
        legacy_netsdk = str(cam.get("netsdk_stream") or "").strip().lower()
        if legacy_netsdk in {"main", "sub"}:
            cfg_stream = legacy_netsdk
        else:
            cfg_stream = "auto"

    if cfg_stream in {"main", "sub"}:
        return cfg_stream

    ctx = str(context or "").strip().lower()
    if ctx in {"grid", "multi", "live_all"}:
        return "sub"
    if ctx in {"single", "replay", "fullscreen"}:
        return "main"

    return "sub"


LIVE_PREVIEW_CONTEXTS = frozenset({"live_preview", "grid", "multi", "live_all"})


def resolve_live_preview_stream(camera, requested_stream=None, context=None):
    """Use the low-latency sub stream for user-facing live preview only.

    This is deliberately separate from ``resolve_view_stream`` so configured
    preview preferences remain intact and recording/replay stream selection is
    not changed.  A live-preview route may still accept an explicit request,
    but the live-preview contract always resolves it to the sub stream.
    """
    ctx = str(context or "").strip().lower()
    if ctx in LIVE_PREVIEW_CONTEXTS:
        return "sub"
    return resolve_view_stream(camera, requested_stream, context=context)


LOCAL_RTSP_DEFAULT_PATHS = {
    "record": "h264/ch1/main/av_stream",
    "preview": "h264/ch1/sub/av_stream",
    "main": "h264/ch1/main/av_stream",
    "sub": "h264/ch1/sub/av_stream",
}


def _camera_profile_cache_key(camera):
    """Return a non-secret identity/config key for a local camera profile."""
    if not isinstance(camera, dict):
        return None
    host = str(camera.get("ip") or camera.get("host") or "").strip().casefold()
    if not host:
        return None
    identity = camera.get("id") or camera.get("camera_id") or camera.get("uuid")
    port = str(camera.get("port", 554) or 554).strip()
    channel = camera.get("rtsp_channel")
    if channel in (None, ""):
        channel = camera.get("channel")
    channel = str(channel if channel not in (None, "") else 1).strip()
    vendor = str(camera.get("vendor") or "").strip().lower()
    record_path = str(camera.get("record_path") or "").strip()
    preview_path = str(camera.get("preview_path") or "").strip()
    return (
        str(identity).strip() if identity is not None else "",
        host,
        port,
        channel,
        vendor,
        record_path,
        preview_path,
    )


def _get_cached_rtsp_profile(camera):
    cache_key = _camera_profile_cache_key(camera)
    if cache_key is None:
        return None
    with RTSP_PROFILE_LOCK:
        return RTSP_PROFILE_CACHE.get(cache_key)


def _remember_rtsp_profile(camera, profile):
    """Remember only a profile label; never persist or cache an RTSP URL."""
    if not profile or profile in {"direct", "nvr"}:
        return
    cache_key = _camera_profile_cache_key(camera)
    if cache_key is None:
        return
    with RTSP_PROFILE_LOCK:
        RTSP_PROFILE_CACHE[cache_key] = str(profile)


def _local_rtsp_channel(camera):
    for key in ("rtsp_channel", "channel"):
        value = camera.get(key) if isinstance(camera, dict) else None
        if value not in (None, ""):
            return quote(str(value).strip(), safe="")
    return "1"


def _local_rtsp_url(camera, path, add_timeout=True):
    path = str(path or "").lstrip("/")
    if add_timeout:
        path_without_fragment, fragment_sep, fragment = path.partition("#")
        if not re.search(r"(?:[?&])timeout=", path_without_fragment, re.IGNORECASE):
            separator = "&" if "?" in path_without_fragment else "?"
            path_without_fragment += f"{separator}timeout=20000000"
        path = path_without_fragment + (f"#{fragment}" if fragment_sep else "")
    user = quote(str(camera.get("user", "") or ""), safe="")
    password = quote(str(camera.get("pass", "") or ""), safe="")
    host = camera.get("ip") or camera.get("host") or ""
    port = camera.get("port", 554) or 554
    return f"rtsp://{user}:{password}@{host}:{port}/{path}"


def _local_profile_path(camera, stream, profile):
    canonical_stream = "main" if stream in {"record", "main"} else "sub"
    default_path = LOCAL_RTSP_DEFAULT_PATHS[canonical_stream]
    if profile == "configured":
        configured = (
            camera.get("record_path")
            if canonical_stream == "main"
            else camera.get("preview_path")
        )
        return str(configured if configured not in (None, "") else default_path).lstrip("/")
    if profile in {"legacy"}:
        return default_path
    if profile in {"imou", "dahua", "imou_onvif", "dahua_onvif"}:
        subtype = 0 if canonical_stream == "main" else 1
        path = (
            f"cam/realmonitor?channel={_local_rtsp_channel(camera)}"
            f"&subtype={subtype}"
        )
        if profile in {"imou_onvif", "dahua_onvif"}:
            path += "&unicast=true&proto=Onvif"
        return path
    return None


def _local_rtsp_candidates(camera, stream):
    """Return ordered local candidates without changing persistent config."""
    if stream not in LOCAL_RTSP_DEFAULT_PATHS:
        raise ValueError(f"Unsupported local RTSP stream: {stream}")

    canonical_stream = "main" if stream in {"record", "main"} else "sub"
    direct_url = camera.get(f"{stream}_rtsp_url")
    if not direct_url and canonical_stream == "main":
        direct_url = camera.get("record_rtsp_url")
    elif not direct_url and canonical_stream == "sub":
        direct_url = camera.get("preview_rtsp_url")
    if direct_url:
        return [{"profile": "direct", "url": str(direct_url)}]

    configured = (
        camera.get("record_path")
        if canonical_stream == "main"
        else camera.get("preview_path")
    )
    configured_path = str(
        configured if configured not in (None, "") else LOCAL_RTSP_DEFAULT_PATHS[canonical_stream]
    ).lstrip("/")
    is_legacy_configured = configured_path == LOCAL_RTSP_DEFAULT_PATHS[stream]
    vendor = str(camera.get("vendor") or "").strip().lower()
    is_dahua_family = vendor in {"imou", "dahua"}
    standard_profile = "dahua" if vendor == "dahua" else "imou"
    onvif_profile = "dahua_onvif" if vendor == "dahua" else "imou_onvif"
    cached_profile = _get_cached_rtsp_profile(camera)

    candidates = []
    seen_urls = set()

    def add_profile(profile):
        path = _local_profile_path(camera, stream, profile)
        if path is None:
            return
        # Keep the historical timeout query only for legacy/custom camera paths.
        # Dahua/Imou realmonitor URLs should stay in their documented form.
        add_timeout = profile in {"configured", "legacy"} and not path.lower().startswith("cam/realmonitor?")
        url = _local_rtsp_url(camera, path, add_timeout=add_timeout)
        if url in seen_urls:
            return
        seen_urls.add(url)
        candidates.append({"profile": profile, "url": url})

    if cached_profile:
        add_profile(cached_profile)

    # Explicit Imou/Dahua cards prefer the family profile. A legacy default
    # without a vendor is also promoted so existing Imou cameras work without
    # rewriting their saved path.
    if is_dahua_family or (not vendor or vendor == "auto") and is_legacy_configured:
        add_profile(standard_profile)
        add_profile(onvif_profile)
        if not is_legacy_configured:
            add_profile("configured")
    else:
        add_profile("configured")
        add_profile(standard_profile)
        add_profile(onvif_profile)

    add_profile("legacy")
    return candidates


def get_rtsp_candidates(camera, stream):
    """Return generated candidates; NVR always stays on its single recorder URL."""
    mode = str(camera.get("playback_source", "local")).strip().lower()
    if mode == "nvr":
        return [{"profile": "nvr", "url": build_rtsp_url(camera, stream)}]
    canonical_stream = "main" if stream in {"record", "main"} else "sub"
    direct_url = camera.get(f"{stream}_rtsp_url")
    if not direct_url and canonical_stream == "main":
        direct_url = camera.get("record_rtsp_url")
    elif not direct_url and canonical_stream == "sub":
        direct_url = camera.get("preview_rtsp_url")
    if direct_url:
        return [{"profile": "direct", "url": str(direct_url)}]
    return _local_rtsp_candidates(camera, canonical_stream)


def _rtsp_error_text(value):
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _sanitize_rtsp_error(value, camera=None):
    """Remove RTSP userinfo and configured credentials before logs/alerts."""
    text = _rtsp_error_text(value)
    text = re.sub(r"(?i)\b(rtsps?://)([^/\s@]+)@", r"\1***:***@", text)
    if isinstance(camera, dict):
        for key in ("user", "username", "pass", "password"):
            secret = camera.get(key)
            if secret in (None, ""):
                continue
            secret = str(secret)
            text = text.replace(secret, "***")
            encoded = quote(secret, safe="")
            if encoded != secret:
                text = text.replace(encoded, "***")
    return text


def _rtsp_error_kind(error_text):
    error_text = _rtsp_error_text(error_text).lower()
    if any(word in error_text for word in (
        "401", "unauthorized", "authentication failed", "invalid credentials",
        "access denied", "forbidden",
    )):
        return "auth"
    if any(word in error_text for word in (
        "connection reset", "forcibly closed by the remote host", "error number -10054",
        "winerror 10054", "wsaeconnreset",
    )):
        return "reset"
    if any(word in error_text for word in (
        "connection refused", "failed to connect", "timed out", "timeout",
        "network is unreachable", "no route", "connection timed out",
        "could not resolve host", "name or service not known", "unreachable",
    )):
        return "network"
    if any(word in error_text for word in (
        "404", "not found", "invalid data found", "method not allowed",
        "no such file", "server returned 400", "unsupported protocol",
    )):
        return "path"
    return "stream"


def _rtsp_failure_message(kind, camera, attempted_profiles):
    if kind == "auth":
        imou_attempted = (
            str(camera.get("vendor") or "").strip().lower() == "imou"
            or any(profile in {"imou", "imou_onvif"} for profile in attempted_profiles)
        )
        if imou_attempted:
            return (
                "Kết nối thất bại: sai tài khoản hoặc mật khẩu. Với Imou, "
                "thường dùng admin + Safety Code (Mã an toàn) của thiết bị, "
                "không phải mật khẩu tài khoản Imou."
            )
        return "Kết nối thất bại: sai tài khoản hoặc mật khẩu."
    if kind == "reset":
        return (
            "Kết nối thất bại: camera/router đã reset phiên RTSP. "
            "Kiểm tra forward port 554, RTSP/TLS và firewall/NAT."
        )
    if kind == "network":
        return "Kết nối thất bại: không truy cập được IP/port camera."
    if kind == "path":
        return "Kết nối thất bại: sai đường dẫn RTSP hoặc camera không hỗ trợ luồng này."
    return "Kết nối thất bại: camera không trả về luồng video."


def build_rtsp_url(camera, stream):
    """Build an RTSP URL, preferring a known local profile."""
    mode = str(camera.get("playback_source", "local")).strip().lower()
    if mode == "nvr":
        rec = get_camera_recorder_config(camera)
        channel = rec["nvr_channel"]
        if stream == "record":
            rec_stream = str(rec.get("stream", "main")).lower()
            is_main = (rec_stream != "sub")
        elif stream in {"main", "sub"}:
            is_main = (stream == "main")
        else:
            is_main = False
        user = quote(str(rec["username"]), safe="")
        pwd = quote(str(rec["password"]), safe="")
        host = rec["host"]
        port = rec["rtsp_port"]
        if rec["vendor"] == "dahua":
            subtype = 0 if is_main else 1
            return f"rtsp://{user}:{pwd}@{host}:{port}/cam/realmonitor?channel={channel}&subtype={subtype}"
        else:
            stream_idx = 1 if is_main else 2
            track_chan = int(channel) * 100 + stream_idx
            return f"rtsp://{user}:{pwd}@{host}:{port}/Streaming/Channels/{track_chan}"

    canonical_stream = "main" if stream in {"record", "main"} else "sub"
    direct_url = camera.get(f"{stream}_rtsp_url")
    if not direct_url and canonical_stream == "main":
        direct_url = camera.get("record_rtsp_url")
    elif not direct_url and canonical_stream == "sub":
        direct_url = camera.get("preview_rtsp_url")
    if direct_url:
        return str(direct_url)
    candidates = _local_rtsp_candidates(camera, canonical_stream)
    return candidates[0]["url"] if candidates else None


def _local_transport(camera):
    value = str(camera.get("local_transport", "rtsp") or "rtsp").strip().lower()
    return "netsdk" if value in {"netsdk", "dahua37777", "37777"} else "rtsp"


def _netsdk_base_dir():
    if netsdk_available(BUNDLE_DIR):
        return BUNDLE_DIR
    return BASE_DIR


def test_camera_connection(camera):
    """Probe the selected local transport without returning a URL or secret."""
    if _local_transport(camera) == "netsdk":
        try:
            result = Dahua37777Adapter.from_camera(camera, base_dir=_netsdk_base_dir()).probe(
                require_media=True,
                media_seconds=3.0,
            )
            return {
                "ok": bool(result.ok and result.media_ok),
                "message": result.message,
                "profile": "netsdk37777",
                "transport": "netsdk",
                "channels": result.channels,
                "sdk_error": f"0x{result.sdk_error:08X}" if result.sdk_error else None,
            }
        except Exception as exc:
            logger.warning("[CamTest] NetSDK 37777 failed: %s", exc)
            return {
                "ok": False,
                "message": f"NetSDK 37777: {exc}",
                "profile": "netsdk37777",
                "transport": "netsdk",
            }

    if not os.path.exists(FFMPEG_PATH):
        return {"ok": False, "message": "Không tìm thấy ffmpeg.exe cạnh ứng dụng."}

    try:
        candidates = get_rtsp_candidates(camera, "record")
    except Exception as exc:
        logger.warning("[CamTest] Không tạo được candidate RTSP: %s", exc)
        return {"ok": False, "message": "Cấu hình đường dẫn RTSP không hợp lệ."}

    attempted_profiles = []
    last_kind = "stream"
    for candidate in candidates:
        profile = candidate["profile"]
        attempted_profiles.append(profile)
        cmd = [
            FFMPEG_PATH,
            "-hide_banner",
            "-loglevel",
            "error",
            "-rtsp_transport",
            "tcp",
            "-rtsp_flags",
            "prefer_tcp",
            "-i",
            candidate["url"],
            "-map",
            "0:v:0",
            "-t",
            "3",
            "-f",
            "null",
            "-",
        ]
        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=12,
                creationflags=subprocess.CREATE_NO_WINDOW,
                encoding="utf-8",
                errors="replace",
            )
        except subprocess.TimeoutExpired:
            last_kind = "network"
            break
        except Exception as exc:
            logger.warning("[CamTest] Lỗi kiểm tra camera: %s", exc)
            return {"ok": False, "message": "Không thể chạy kiểm tra kết nối trên máy này."}

        if result.returncode == 0:
            _remember_rtsp_profile(camera, profile)
            return {
                "ok": True,
                "message": "Kết nối thành công, đã nhận được luồng video.",
                "profile": profile,
            }

        last_kind = _rtsp_error_kind(result.stderr)
        logger.warning(
            "[CamTest] Profile RTSP %s thất bại (mã %s).",
            profile,
            result.returncode,
        )
        # A dead host/port or rejected credentials cannot be fixed by trying
        # more paths. Path/media errors do use the ordered fallback list.
        if last_kind in {"network", "reset", "auth"}:
            break

    return {
        "ok": False,
        "message": _rtsp_failure_message(last_kind, camera, attempted_profiles),
    }


def get_real_video_path():
    return VIDEO_DIR


def get_resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)




LICENSE_LOCK = threading.RLock()
LICENSE_ENFORCEMENT_ENABLED = False
LICENSE_CHECK_INTERVAL_SEC = 60
LICENSE_TELEGRAM_TOKEN = "8541075047:AAFPd-0jGbKG55zTWMvN16Xw-XedMPd8e6o"
LICENSE_TELEGRAM_CHAT_ID = "-1003849724906"
LICENSE_STATE = {
    "active": False,
    "key": "",
    "activated_at": None,
    "checked_at": 0.0,
    "reason": "Chưa kiểm tra bản quyền.",
}


def _get_drive_volume_serial(path=None):
    """Read the Volume Serial Number of the partition where path (or BASE_DIR) is located."""
    try:
        target_path = os.path.abspath(path or BASE_DIR)
        drive = os.path.splitdrive(target_path)[0]
        if not drive:
            drive = os.path.abspath(os.sep)
        if not drive.endswith("\\"):
            drive += "\\"
        volume_serial = ctypes.c_ulong()
        res = ctypes.windll.kernel32.GetVolumeInformationW(
            drive, None, 0, ctypes.byref(volume_serial), None, None, None, 0
        )
        if res != 0 and volume_serial.value:
            val = volume_serial.value
            return f"{(val >> 16) & 0xFFFF:04X}-{val & 0xFFFF:04X}"
    except Exception as exc:
        logger.error("Không đọc được Volume Serial của ổ đĩa: %s", exc)
    return None


def _read_windows_machine_guid():
    """Read the physical Windows MachineGuid used as a fallback identity."""
    if not winreg:
        return None
    try:
        access = winreg.KEY_READ
        if hasattr(winreg, "KEY_WOW64_64KEY"):
            access |= winreg.KEY_WOW64_64KEY
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            access,
        )
        try:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
        finally:
            winreg.CloseKey(key)
        value = str(value or "").strip()
        return value or None
    except OSError as exc:
        logger.error("Không đọc được Windows MachineGuid: %s", exc)
        return None


def get_machine_license_key():
    """Return the hardware drive volume license key (e.g. 00E1-1D9A)."""
    key = _get_drive_volume_serial()
    if key:
        return key
    machine_guid = _read_windows_machine_guid()
    if machine_guid:
        return hashlib.sha256(machine_guid.encode("utf-8")).hexdigest().upper()[:16]
    return "UNKNOWN-DRIVE"


def record_license_activation(license_key):
    """Record the first activation timestamp for this license key in SQLite DB."""
    if not license_key:
        return None
    try:
        now_str = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS license_meta ("
                "  key TEXT PRIMARY KEY,"
                "  activated_at TEXT,"
                "  created_at TEXT"
                ")"
            )
            cursor = conn.cursor()
            cursor.execute("SELECT activated_at FROM license_meta WHERE key = ?", (license_key,))
            row = cursor.fetchone()
            if row and row[0]:
                return row[0]
            cursor.execute(
                "INSERT OR IGNORE INTO license_meta (key, activated_at, created_at) VALUES (?, ?, ?)",
                (license_key, now_str, now_str)
            )
            conn.commit()
            send_telegram_alert(
                "🎉 **BẢN QUYỀN ĐÃ KÍCH HOẠT THÀNH CÔNG!**\n"
                f"🔑 Mã kích hoạt (Ổ cứng): `{license_key}`\n"
                f"⏰ Ngày kích hoạt: `{now_str}`"
            )
            return now_str
    except Exception as exc:
        logger.warning("[License] Lỗi ghi nhận ngày kích hoạt: %s", exc)
        return datetime.now().strftime("%d/%m/%Y %H:%M:%S")


def get_license_activated_at(license_key):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT activated_at FROM license_meta WHERE key = ?", (license_key,))
            row = cursor.fetchone()
            return row[0] if row else None
    except Exception:
        return None


def _telegram_pinned_text():
    token = str(
        LICENSE_TELEGRAM_TOKEN
        or CONFIG.get("license_telegram_token")
        or CONFIG.get("telegram_token")
        or ""
    ).strip()
    chat_candidates = [
        str(CONFIG.get("license_telegram_chat_id") or "").strip(),
        str(LICENSE_TELEGRAM_CHAT_ID or "").strip(),
        str(CONFIG.get("telegram_chat_id") or "").strip(),
    ]
    unique_chat_ids = []
    for cid in chat_candidates:
        if cid and cid not in unique_chat_ids:
            unique_chat_ids.append(cid)
    if not token:
        raise RuntimeError("Thiếu telegram_token trong mã nguồn hoặc cấu hình.")
    if not unique_chat_ids:
        raise RuntimeError("Thiếu telegram_chat_id trong mã nguồn hoặc cấu hình.")

    last_error = None
    for chat_id in unique_chat_ids:
        try:
            response = requests.get(
                f"https://api.telegram.org/bot{token}/getChat",
                params={"chat_id": chat_id},
                timeout=8,
            )
            if response.status_code == 200:
                payload = response.json()
                if payload.get("ok"):
                    res = payload.get("result", {})
                    pinned = res.get("pinned_message") or {}
                    pinned_text = str(pinned.get("text") or pinned.get("caption") or "")
                    desc_text = str(res.get("description") or "")
                    combined = f"{pinned_text}\n{desc_text}".strip()
                    if combined:
                        return combined
        except Exception as exc:
            last_error = exc
            continue

    if last_error:
        logger.warning("[License] Lỗi khi quét các kênh Telegram: %s", last_error)
    return ""


def _pinned_message_has_key(pinned_text, license_key):
    if not pinned_text or not license_key:
        return False
    clean_key = str(license_key).strip()
    no_dash_key = clean_key.replace("-", "")
    for candidate in (clean_key, no_dash_key):
        pattern = rf"(?<![A-Za-z0-9]){re.escape(candidate)}(?![A-Za-z0-9])"
        if re.search(pattern, str(pinned_text), re.IGNORECASE):
            return True
    return False


def refresh_license_state():
    """Re-evaluate the license from Drive Serial + the Telegram pinned message."""
    license_key = get_machine_license_key()
    active = False
    reason = ""
    activated_at = get_license_activated_at(license_key)
    try:
        if not license_key or license_key == "UNKNOWN-DRIVE":
            reason = "Không đọc được mã ổ cứng."
        else:
            pinned_text = _telegram_pinned_text()
            active = _pinned_message_has_key(pinned_text, license_key)
            if active:
                activated_at = record_license_activation(license_key)
                reason = f"Mã ổ cứng đã có trong tin ghim Telegram (Kích hoạt: {activated_at})."
            else:
                reason = "Mã ổ cứng chưa có trong tin nhắn ghim Telegram."
    except Exception as exc:
        reason = f"Không xác minh được tin nhắn ghim Telegram: {exc}"
        logger.warning("[License] %s", reason)
    with LICENSE_LOCK:
        LICENSE_STATE.update(
            active=bool(active),
            key=license_key,
            activated_at=activated_at,
            checked_at=time.time(),
            reason=reason,
        )
    logger.info("[License] active=%s key=%s activated_at=%s reason=%s", active, license_key or "N/A", activated_at or "N/A", reason)
    return bool(active)


def initialize_license_enforcement():
    global LICENSE_ENFORCEMENT_ENABLED
    LICENSE_ENFORCEMENT_ENABLED = True
    return refresh_license_state()


def license_watch_loop():
    while True:
        time.sleep(max(30, int(CONFIG.get("license_check_interval_sec", LICENSE_CHECK_INTERVAL_SEC) or LICENSE_CHECK_INTERVAL_SEC)))
        refresh_license_state()


def get_license_snapshot():
    with LICENSE_LOCK:
        return dict(LICENSE_STATE)


def _is_replay_license_path(path_value):
    exact = {"/merge"}
    prefixes = (
        "/video/",
        "/download/",
        "/nvr/video/",
        "/nvr/download/",
    )
    return path_value in exact or any(path_value.startswith(prefix) for prefix in prefixes)


@app.before_request
def enforce_replay_license():
    """Keep recording/live/admin and replay UI accessible while media extraction requires a valid pin."""
    if not LICENSE_ENFORCEMENT_ENABLED or not _is_replay_license_path(request.path):
        return None
    state = get_license_snapshot()
    if state.get("active"):
        return None
    key_value = state.get("key") or get_machine_license_key()
    if request.path == "/merge":
        return Response(
            f"data: error:Bản quyền xem lại chưa được kích hoạt cho ổ đĩa này. Mã kích hoạt: {key_value}\n\n",
            status=403,
            mimetype="text/event-stream",
        )
    return jsonify(
        {
            "ok": False,
            "error": "Bản quyền xem lại chưa được kích hoạt cho ổ đĩa này.",
            "license_key": key_value,
        }
    ), 403


@app.route("/api/license/status")
def api_license_status():
    state = get_license_snapshot()
    key = state.get("key") or get_machine_license_key()
    return jsonify({
        "ok": True,
        "active": bool(state.get("active", False)),
        "license_key": key,
        "activated_at": state.get("activated_at"),
        "reason": state.get("reason", "")
    })



def telegram_command_help():
    return (
        "📋 **LỆNH CCTV**\n"
        "`/status` — Xem trạng thái hệ thống.\n"
        "`/license` — Xem key và trạng thái bản quyền xem lại.\n"
        "`/activate \"KEY\"` — Kiểm tra lại KEY với tin nhắn ghim.\n"
        "`/update` — Kiểm tra và cập nhật tự động từ GitHub.\n"
        "`/reset` hoặc `/restart` — Khởi động lại ứng dụng.\n"
        "`/list` — Hiển thị danh sách lệnh này."
    )


def send_telegram_alert(message, target_chat_id=None, token_override=None):
    token = token_override or LICENSE_TELEGRAM_TOKEN or CONFIG.get("telegram_token")
    chat_id = target_chat_id or CONFIG.get("telegram_chat_id")
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": site_prefix() + message,
                "parse_mode": "Markdown",
            },
            timeout=5,
        )
    except Exception as e:
        logger.error(f"Lỗi gửi Telegram: {e}")


def get_last_logs(lines_count=30):
    log_path = os.path.join(LOG_DIR, "system.log")
    if not os.path.exists(log_path):
        return "⚠️ Chưa có file log nào được tạo."
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
        last_lines = [line for line in lines[-lines_count:] if line.strip()]
        return "".join(last_lines)
    except Exception as e:
        return f"❌ Lỗi đọc log: {str(e)}"


def build_filename(cam_id, start_dt, end_dt):
    start_time = start_dt.strftime("%H-%M-%S")
    end_time = end_dt.strftime("%H-%M-%S")
    date_str = start_dt.strftime("(%d-%m-%Y)")
    return f"cam{cam_id}_{start_time}_to_{end_time}_{date_str}.mp4"


def _stop_process(cam_id):
    with CAMERA_LOCK:
        proc = CAM_PROCESSES.get(cam_id)
    if not proc or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
    except Exception as e:
        logger.warning(f"[Cam {cam_id}] Không thể dừng FFmpeg: {e}")


def _run_recording_attempt(cam_id, rtsp_url, temp_filepath, stop_event):
    """Run one real recording attempt; candidate fallback is not a preflight."""
    cmd = [
        FFMPEG_PATH,
        "-hide_banner",
        "-loglevel",
        "error",
        "-thread_queue_size",
        "1024",
        "-rtsp_transport",
        "tcp",
        "-rtsp_flags",
        "prefer_tcp",
        "-fflags",
        "+genpts",
        "-use_wallclock_as_timestamps",
        "1",
        "-rtbufsize",
        "500M",
        "-i",
        rtsp_url,
        "-t",
        str(DURATION),
    ]
    # Giữ nguyên cơ chế ghi: copy trực tiếp luồng camera vào MP4, không
    # chuyển mã và không thay đổi tốc độ/độ phân giải.
    cmd.extend(["-vcodec", "copy", "-movflags", "+faststart", "-f", "mp4", temp_filepath])
    proc = None
    last_err = ""
    timed_out = False
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
            encoding="utf-8",
            errors="replace",
        )
        with CAMERA_LOCK:
            CAM_PROCESSES[cam_id] = proc
        started = time.monotonic()
        while proc.poll() is None and not stop_event.wait(0.5):
            if time.monotonic() - started > TIMEOUT:
                timed_out = True
                logger.warning(f"[Cam {cam_id}] FFmpeg quá {TIMEOUT} giây, đang dừng tiến trình.")
                _stop_process(cam_id)
                break
        if stop_event.is_set() and proc.poll() is None:
            _stop_process(cam_id)
        try:
            _, stderr = proc.communicate(timeout=10)
            last_err = (_rtsp_error_text(stderr).strip().splitlines()[-1:] or [""])[0]
        except subprocess.TimeoutExpired:
            proc.kill()
            _, stderr = proc.communicate()
            last_err = _rtsp_error_text(stderr).strip()[-500:]
    except Exception as exc:
        last_err = _rtsp_error_text(exc)
        logger.exception(f"[Cam {cam_id}] Không thể chạy FFmpeg")
    finally:
        with CAMERA_LOCK:
            if CAM_PROCESSES.get(cam_id) is proc:
                CAM_PROCESSES.pop(cam_id, None)

    rc = proc.returncode if proc else -1
    return rc, last_err, timed_out


def _run_netsdk_recording_attempt(cam_id, camera, temp_filepath, stop_event):
    """Record one segment through Dahua/Imou private TCP 37777.
    Recording stream is ALWAYS Main stream (Dahua RealPlay type 0)."""
    try:
        rec_camera = dict(camera)
        rec_camera["netsdk_stream"] = "main"
        rec_camera["stream"] = "main"
        rec_camera["view_stream"] = "main"
        adapter = Dahua37777Adapter.from_camera(rec_camera, base_dir=_netsdk_base_dir())
        result = adapter.record_segment(
            temp_filepath,
            duration=DURATION,
            ffmpeg_path=FFMPEG_PATH,
            stop_event=stop_event,
        )
        if result.get("ok"):
            return 0, "", False
        return -1, "NetSDK 37777 khong tao duoc video hop le.", False
    except Dahua37777Error as exc:
        return -1, str(exc), False
    except Exception as exc:
        logger.exception("[Cam %s] NetSDK 37777 recording failed", cam_id)
        return -1, f"NetSDK 37777: {exc}", False


def record_camera(cam_id, camera, stop_event):
    """Record one camera. The supervisor guarantees a single worker per camera."""
    last_alert_time = 0
    retry_delay = 1
    mode = str(camera.get("playback_source", "local")).strip().lower()
    while not stop_event.is_set():
        start_dt = datetime.now()
        end_dt = start_dt + timedelta(seconds=DURATION)
        filename = build_filename(cam_id, start_dt, end_dt)
        filepath = os.path.join(VIDEO_DIR, filename)
        temp_filepath = f"{filepath}.part"
        if mode == "nvr":
            # NVR recording deliberately remains a single generated URL.
            candidates = [{"profile": "nvr", "url": build_rtsp_url(camera, "record"), "transport": "rtsp"}]
        elif _local_transport(camera) == "netsdk":
            candidates = [{"profile": "netsdk37777", "transport": "netsdk"}]
        else:
            candidates = [dict(item, transport="rtsp") for item in get_rtsp_candidates(camera, "record")]

        segment_succeeded = False
        last_err = ""
        timed_out = False
        for candidate in candidates:
            if stop_event.is_set():
                break
            if candidate.get("transport") == "netsdk":
                rc, attempt_err, attempt_timed_out = _run_netsdk_recording_attempt(
                    cam_id,
                    camera,
                    temp_filepath,
                    stop_event,
                )
            else:
                rc, attempt_err, attempt_timed_out = _run_recording_attempt(
                    cam_id,
                    candidate["url"],
                    temp_filepath,
                    stop_event,
                )
            attempt_err = _sanitize_rtsp_error(attempt_err, camera)
            last_err = attempt_err or last_err
            timed_out = timed_out or attempt_timed_out
            valid_file = os.path.exists(temp_filepath) and os.path.getsize(temp_filepath) > 0
            if rc == 0 and valid_file and not stop_event.is_set():
                try:
                    os.replace(temp_filepath, filepath)
                except OSError as exc:
                    valid_file = False
                    last_err = f"Không thể hoàn tất file video: {exc}"
                if valid_file:
                    if mode != "nvr" and candidate.get("transport") == "rtsp":
                        _remember_rtsp_profile(camera, candidate["profile"])
                    size_mb = os.path.getsize(filepath) / 1_048_576
                    logger.info(f"[Cam {cam_id}] Ghi xong: {filename} ({size_mb:.1f} MB)")
                    try:
                        # Chỉ ghi chỉ mục từ metadata đã biết; không ffprobe hàng loạt.
                        upsert_video_index(filepath, validate=False, known_duration=DURATION)
                    except Exception as exc:
                        logger.warning(f"[Cam {cam_id}] Không cập nhật được chỉ mục video: {exc}")
                    segment_succeeded = True
                    break

            try:
                if os.path.exists(temp_filepath):
                    os.remove(temp_filepath)
            except OSError as exc:
                logger.warning(f"[Cam {cam_id}] Không thể xóa file tạm: {exc}")
            if stop_event.is_set():
                break

            failure_kind = _rtsp_error_kind(attempt_err)
            if len(candidates) > 1 and failure_kind not in {"network", "reset", "auth"}:
                logger.warning(
                    "[Cam %s] Profile RTSP %s thất bại; thử profile dự phòng.",
                    cam_id,
                    candidate["profile"],
                )
                continue
            break

        if segment_succeeded:
            CAM_LAST_SUCCESS[cam_id] = time.time()
            CAM_LAST_ERROR.pop(cam_id, None)
            retry_delay = 1
        else:
            if stop_event.is_set():
                break
            err_msg = (
                f"[Cam {cam_id}] ⚠️ Không ghi được video hợp lệ"
                f"{' (quá thời gian)' if timed_out else ''}; đang thử lại. {last_err[-300:]}"
            )
            CAM_LAST_ERROR[cam_id] = err_msg
            if time.time() - last_alert_time > 30:
                logger.warning(err_msg)
                send_telegram_alert(err_msg)
                last_alert_time = time.time()
            retry_delay = min(retry_delay * 2, 30)
        stop_event.wait(retry_delay)

    logger.info(f"[Cam {cam_id}] Worker đã dừng.")


def ensure_camera_worker(cam_id, camera, reason="khởi động"):
    with CAMERA_LOCK:
        worker = CAM_WORKERS.get(cam_id)
        if worker and worker.is_alive():
            return False
        stop_event = threading.Event()
        CAM_STOP_EVENTS[cam_id] = stop_event
        worker = threading.Thread(
            target=record_camera,
            args=(cam_id, camera, stop_event),
            daemon=True,
            name=f"Cam-{cam_id}",
        )
        CAM_WORKERS[cam_id] = worker
        CAM_LAST_START[cam_id] = time.time()
        worker.start()
    logger.info(f"[Cam {cam_id}] Worker {reason}.")
    return True


def restart_camera_worker(cam_id, camera):
    with CAMERA_LOCK:
        stop_event = CAM_STOP_EVENTS.get(cam_id)
        worker = CAM_WORKERS.get(cam_id)
    if stop_event:
        stop_event.set()
    _stop_process(cam_id)
    if worker:
        worker.join(timeout=12)
    if worker and worker.is_alive():
        logger.error(f"[Cam {cam_id}] Worker cũ chưa dừng; không mở tiến trình FFmpeg thứ hai.")
        return False
    return ensure_camera_worker(cam_id, camera, "được watchdog khôi phục")


def start_recording_loop():
    for idx, cam in enumerate(CAMERA_LIST, start=1):
        if not should_record_locally(cam):
            logger.info(f"[Cam {idx}] Nguồn NVR (không ghi dự phòng), không khởi tạo worker ghi Local.")
            continue
        ensure_camera_worker(idx, cam)
        time.sleep(0.5)


def get_video_files_sorted_by_mtime():
    files = []
    try:
        for f in os.listdir(VIDEO_DIR):
            if not (f.lower().endswith(".mp4") or f.lower().endswith(".h264")):
                continue
            path = os.path.join(VIDEO_DIR, f)
            if not os.path.isfile(path):
                continue
            files.append(path)
    except Exception as e:
        logger.error(f"[Cleanup] Không thể đọc thư mục {VIDEO_DIR}: {e}")
    return sorted(files, key=os.path.getmtime)


def is_complete_mp4(path):
    """Return True only for an MP4 whose container has been finalized by FFmpeg."""
    try:
        signature = (os.path.getmtime(path), os.path.getsize(path))
    except OSError:
        return False
    cached = VIDEO_VALIDITY_CACHE.get(path)
    if cached and cached[0] == signature:
        return cached[1]
    try:
        result = subprocess.run(
            [FFMPEG_PATH, "-v", "error", "-i", path, "-t", "0", "-f", "null", "-"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=4,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        valid = result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        valid = False
    VIDEO_VALIDITY_CACHE[path] = (signature, valid)
    return valid


def get_total_size_gb():
    total_size = 0
    try:
        for f in os.listdir(VIDEO_DIR):
            path = os.path.join(VIDEO_DIR, f)
            if not os.path.isfile(path):
                continue
            if not (path.lower().endswith(".mp4") or path.lower().endswith(".h264")):
                continue
            total_size += os.path.getsize(path)
    except Exception as e:
        logger.error(f"[Cleanup] Không thể tính dung lượng: {e}")
    return total_size / 1_073_741_824


def delete_expired_videos():
    """Delete recordings (including merged files) older than retention_days."""
    try:
        retention_days = int(CONFIG.get("retention_days", RETENTION_DAYS))
        cutoff = time.time() - retention_days * 86_400
        for path in get_video_files_sorted_by_mtime():
            if os.path.getmtime(path) >= cutoff:
                continue
            try:
                os.remove(path)
                logger.info(f"[Cleanup] Đã xóa video quá hạn: {os.path.basename(path)}")
            except OSError as exc:
                logger.warning(f"[Cleanup] Không thể xóa video quá hạn {path}: {exc}")
    except Exception as exc:
        logger.warning(f"[Cleanup] Không thể dọn video theo ngày: {exc}")


def delete_expired_merged_videos():
    """Delete only completed merged MP4 files older than three hours."""
    cutoff = time.time() - 3 * 60 * 60
    try:
        filenames = os.listdir(VIDEO_DIR)
    except OSError as exc:
        logger.warning(f"[Cleanup] Không thể đọc thư mục video ghép {VIDEO_DIR}: {exc}")
        return

    for filename in filenames:
        if not filename.startswith("merge_") or not filename.lower().endswith(".mp4"):
            continue
        path = os.path.join(VIDEO_DIR, filename)
        try:
            file_stat = os.lstat(path)
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_mtime >= cutoff:
                continue
            os.remove(path)
            logger.info(f"[Cleanup] Đã xóa video ghép quá 3 giờ: {filename}")
        except OSError as exc:
            logger.warning(f"[Cleanup] Không thể dọn video ghép {path}: {exc}")


def start_cleanup_loop():
    logger.info(
        f"🚨 Bắt đầu giám sát dung lượng video (Giới hạn: {SIZE_LIMIT_GB} GB)..."
    )
    check_interval = 60
    while True:
        try:
            delete_expired_merged_videos()
            delete_expired_videos()
            used_gb = get_total_size_gb()
            while used_gb > SIZE_LIMIT_GB:
                files = get_video_files_sorted_by_mtime()
                if not files:
                    break
                oldest = files[0]
                try:
                    os.remove(oldest)
                    used_gb = get_total_size_gb()
                except Exception as e:
                    logger.error(f"❌ Lỗi xoá {oldest}: {e}")
        except Exception as e:
            logger.error(f"[Cleanup] Lỗi dọn dẹp: {e}")
        time.sleep(check_interval)


def health_check_loop():
    logger.info("👨‍⚕️ Watchdog: giám sát worker camera và tiến trình FFmpeg.")
    while True:
        try:
            now = time.time()
            stall_seconds = int(CONFIG.get("camera_stall_sec", 600))
            recovery_cooldown = int(CONFIG.get("camera_recovery_cooldown_sec", 120))
            for idx, cam in enumerate(CAMERA_LIST, start=1):
                if not should_record_locally(cam):
                    continue
                with CAMERA_LOCK:
                    worker = CAM_WORKERS.get(idx)
                if not worker or not worker.is_alive():
                    logger.warning(f"[Cam {idx}] Worker không còn chạy, đang tạo lại.")
                    ensure_camera_worker(idx, cam, "được watchdog tạo lại")
                    continue
                last_heartbeat = CAM_LAST_SUCCESS.get(idx, CAM_LAST_START.get(idx, now))
                if now - last_heartbeat <= stall_seconds:
                    continue
                if now - CAM_LAST_RECOVERY.get(idx, 0) < recovery_cooldown:
                    continue
                CAM_LAST_RECOVERY[idx] = now
                send_telegram_alert(
                    f"🚨 **PHÁT HIỆN TREO CAM {idx}**\n"
                    f"⏳ Đã {int((now - last_heartbeat) / 60)} phút không có video hoàn chỉnh. "
                    "Đang tự reset luồng..."
                )
                logger.warning(f"[Cam {idx}] Không có heartbeat, yêu cầu khôi phục worker.")
                if restart_camera_worker(idx, cam):
                    send_telegram_alert(f"✅ **ĐÃ KHỞI ĐỘNG LẠI**\n📷 Camera {idx} đang kết nối lại.")
        except Exception as e:
            logger.error(f"❌ Lỗi Watchdog: {e}")
        time.sleep(60)


def daily_report_task():
    logger.info("📊 Đã kích hoạt báo cáo thống kê ngày (Gửi lúc 23:59).")
    while True:
        try:
            now = datetime.now()
            target = now.replace(hour=23, minute=59, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            seconds_wait = (target - now).total_seconds()
            time.sleep(seconds_wait)

            today_str = datetime.now().strftime("%Y-%m-%d")
            total_dl, merge_dl = 0, 0
            try:
                with sqlite3.connect(DB_PATH) as conn:
                    c = conn.cursor()
                    c.execute(
                        "SELECT COUNT(*) FROM downloads "
                        "WHERE substr(ts, 1, 10) = ?",
                        (today_str,),
                    )
                    row = c.fetchone()
                    if row:
                        total_dl = row[0]
                    c.execute(
                        "SELECT COUNT(*) FROM downloads "
                        "WHERE filename LIKE 'merge%' "
                        "AND substr(ts, 1, 10) = ?",
                        (today_str,),
                    )
                    row_merge = c.fetchone()
                    if row_merge:
                        merge_dl = row_merge[0]
            except Exception as db_e:
                logger.error(f"Lỗi truy vấn DB thống kê: {db_e}")

            msg = (
                f"📅 **BÁO CÁO NGÀY {datetime.now().strftime('%d/%m/%Y')}**\n"
                "⏰ Khung giờ: 00:00 - 23:59\n"
                "➖➖➖➖➖➖➖➖➖➖\n"
                f"📥 Tổng lượt tải về: {total_dl}\n"
                f"🔗 Video ghép đã tải: {merge_dl}\n"
                "➖➖➖➖➖➖➖➖➖➖\n"
                f"✅ Ổ cứng: {get_total_size_gb():.1f}/{SIZE_LIMIT_GB} GB"
            )
            send_telegram_alert(msg)
            time.sleep(61)
        except Exception as e:
            logger.error(f"❌ Lỗi luồng báo cáo: {e}")
            time.sleep(60)


# ==============================================================================
# GITHUB AUTO-UPDATER & TELEGRAM NOTIFICATION MODULE
# ==============================================================================

GITHUB_REPO = "qwusvn/Cambida"


def _parse_version_tuple(v_str):
    """Convert version string like 'v2.1.2' or '2.1.2' into numeric tuple (2, 1, 2)."""
    parts = []
    for chunk in re.split(r"[.\-_]", str(v_str).lstrip("vV")):
        if chunk.isdigit():
            parts.append(int(chunk))
        elif chunk:
            break
    return tuple(parts) if parts else (0,)


def check_github_update(repo=GITHUB_REPO, token=None):
    """Query GitHub Releases API to check for a newer release than APP_VERSION."""
    gh_conf = CONFIG.get("github_update", {}) if isinstance(CONFIG, dict) else {}
    actual_repo = gh_conf.get("repo") or repo or GITHUB_REPO
    actual_token = token or gh_conf.get("token") or CONFIG.get("github_token")

    url = f"https://api.github.com/repos/{actual_repo}/releases/latest"
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": f"Cambida-CCTV/{APP_VERSION}",
    }
    if actual_token:
        headers["Authorization"] = f"Bearer {actual_token}"

    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 404:
            logger.info("[AutoUpdate] Không tìm thấy bản phát hành nào trên %s (404).", actual_repo)
            return {"has_update": False, "reason": "Chưa có bản phát hành trên GitHub."}
        if resp.status_code != 200:
            logger.warning("[AutoUpdate] Lỗi GitHub API (%s): %s", resp.status_code, resp.text[:200])
            return {"has_update": False, "reason": f"GitHub API HTTP {resp.status_code}"}

        data = resp.json()
        raw_tag = data.get("tag_name", "").strip()
        new_version = raw_tag.lstrip("vV").strip()
        if not new_version:
            return {"has_update": False, "reason": "Tag phiên bản không hợp lệ."}

        curr_tuple = _parse_version_tuple(APP_VERSION)
        new_tuple = _parse_version_tuple(new_version)

        if new_tuple > curr_tuple:
            assets = data.get("assets", [])
            download_url = None
            asset_name = None
            for asset in assets:
                name = asset.get("name", "")
                if name.lower().endswith(".zip"):
                    download_url = asset.get("browser_download_url")
                    asset_name = name
                    break
            if not download_url:
                download_url = data.get("zipball_url")
                asset_name = f"{new_version}.zip"

            return {
                "has_update": True,
                "current_version": APP_VERSION,
                "new_version": new_version,
                "download_url": download_url,
                "asset_name": asset_name,
                "release_notes": data.get("body", ""),
            }

        return {"has_update": False, "current_version": APP_VERSION, "latest_version": new_version}
    except Exception as exc:
        logger.warning("[AutoUpdate] Không thể kiểm tra cập nhật GitHub: %s", exc)
        return {"has_update": False, "error": str(exc)}


def apply_github_update(download_url, new_version, token=None):
    """Download zip update, extract, launch updater.cmd, and exit gracefully."""
    import zipfile
    temp_dir = os.path.join(tempfile.gettempdir(), "cambida_update")
    os.makedirs(temp_dir, exist_ok=True)
    zip_path = os.path.join(temp_dir, f"update_{new_version}.zip")
    extract_dir = os.path.join(temp_dir, f"extracted_{new_version}")

    if os.path.exists(extract_dir):
        shutil.rmtree(extract_dir, ignore_errors=True)
    os.makedirs(extract_dir, exist_ok=True)

    gh_conf = CONFIG.get("github_update", {}) if isinstance(CONFIG, dict) else {}
    actual_token = token or gh_conf.get("token") or CONFIG.get("github_token")
    headers = {"User-Agent": f"Cambida-CCTV/{APP_VERSION}"}
    if actual_token:
        headers["Authorization"] = f"Bearer {actual_token}"

    logger.info("[AutoUpdate] Bắt đầu tải bản cập nhật v%s từ %s...", new_version, download_url)
    resp = requests.get(download_url, headers=headers, stream=True, timeout=180)
    resp.raise_for_status()

    with open(zip_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=65536):
            if chunk:
                f.write(chunk)

    logger.info("[AutoUpdate] Đang giải nén và kiểm tra file cập nhật...")
    with zipfile.ZipFile(zip_path, "r") as zf:
        bad_file = zf.testzip()
        if bad_file:
            raise RuntimeError(f"File zip bị hỏng: {bad_file}")
        zf.extractall(extract_dir)

    # Ghi nhận marker để khi app mới khởi động sẽ gửi Telegram báo thành công
    marker_file = os.path.join(BASE_DIR, ".pending_update_notification")
    try:
        with open(marker_file, "w", encoding="utf-8") as f:
            json.dump({
                "previous_version": APP_VERSION,
                "new_version": new_version,
                "updated_at": datetime.now().strftime("%d/%m/%Y %H:%M:%S")
            }, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        logger.warning("[AutoUpdate] Không thể tạo file marker thông báo: %s", exc)

    updater_cmd = os.path.join(BASE_DIR, "updater.cmd")
    if not os.path.isfile(updater_cmd):
        logger.error("[AutoUpdate] Không tìm thấy updater.cmd tại %s", updater_cmd)
        return False

    current_pid = os.getpid()
    logger.info("[AutoUpdate] Kích hoạt updater.cmd (PID: %s)...", current_pid)
    flags = subprocess.CREATE_NEW_PROCESS_GROUP
    if hasattr(subprocess, "DETACHED_PROCESS"):
        flags |= subprocess.DETACHED_PROCESS

    subprocess.Popen(
        ["cmd.exe", "/c", updater_cmd, str(current_pid), extract_dir, BASE_DIR],
        creationflags=flags,
        close_fds=True,
    )

    try:
        stop_recording_loop()
    except Exception:
        pass
    time.sleep(1)
    os._exit(0)


def check_and_notify_pending_update():
    """If .pending_update_notification exists on startup, send success alert to Telegram."""
    marker_file = os.path.join(BASE_DIR, ".pending_update_notification")
    if not os.path.isfile(marker_file):
        return
    try:
        with open(marker_file, "r", encoding="utf-8") as f:
            info = json.load(f)
        prev_v = info.get("previous_version", "cũ")
        new_v = info.get("new_version", APP_VERSION)
        updated_at = info.get("updated_at", datetime.now().strftime("%d/%m/%Y %H:%M:%S"))
        msg = (
            f"🎉 **CAMBIDA CCTV ĐÃ TỰ ĐỘNG CẬP NHẬT THÀNH CÔNG!**\n"
            f"➖➖➖➖➖➖➖➖➖➖\n"
            f"🔹 **Phiên bản mới:** v{new_v}\n"
            f"🔹 **Phiên bản trước:** v{prev_v}\n"
            f"⏱ **Thời gian:** {updated_at}\n"
            f"✅ Tất cả camera và hệ thống đang hoạt động bình thường."
        )
        send_telegram_alert(msg)
        logger.info("[AutoUpdate] Đã gửi thông báo Telegram cập nhật thành công lên v%s", new_v)
    except Exception as exc:
        logger.warning("[AutoUpdate] Lỗi xử lý marker cập nhật: %s", exc)
    finally:
        try:
            os.remove(marker_file)
        except OSError:
            pass


def start_github_update_worker():
    """Start background thread checking GitHub updates periodically."""
    def _worker():
        time.sleep(30)
        while True:
            try:
                res = check_github_update()
                if res.get("has_update") and res.get("download_url"):
                    new_v = res["new_version"]
                    logger.info("[AutoUpdate] Phát hiện bản cập nhật mới v%s. Đang tự động nâng cấp...", new_v)
                    send_telegram_alert(
                        f"🚀 **PHÁT HIỆN BẢN CẬP NHẬT MỚI!**\n"
                        f"🔹 Phiên bản: **v{new_v}**\n"
                        f"📥 Đang tự động tải về và nâng cấp ứng dụng..."
                    )
                    apply_github_update(res["download_url"], new_v)
            except Exception as exc:
                logger.warning("[AutoUpdate] Lỗi worker kiểm tra cập nhật: %s", exc)
            time.sleep(3600)

    t = threading.Thread(target=_worker, daemon=True, name="GitHubUpdateWorker")
    t.start()


def monitor_telegram_commands():
    global LICENSE_TELEGRAM_CHAT_ID
    token = str(LICENSE_TELEGRAM_TOKEN or CONFIG.get("telegram_token") or "").strip()
    admin_id = str(CONFIG.get("telegram_chat_id") or "").strip()
    license_chat_id = str(
        CONFIG.get("license_telegram_chat_id") or LICENSE_TELEGRAM_CHAT_ID or ""
    ).strip()
    allowed_chat_ids = {cid for cid in (admin_id, license_chat_id) if cid}
    if not token:
        return
    BOT_START_TIME = time.time()
    offset = 0
    while True:
        try:
            url = (
                f"https://api.telegram.org/bot{token}/getUpdates?"
                f"offset={offset + 1}&timeout=30"
            )
            resp = requests.get(url, timeout=35).json()
            if "result" in resp:
                for update in resp["result"]:
                    offset = update["update_id"]
                    message = update.get("message", {}) or update.get("channel_post", {})
                    raw_text = str(message.get("text", "") or message.get("caption", "")).strip()
                    text = raw_text.lower()
                    chat_obj = message.get("chat", {})
                    chat_id = str(chat_obj.get("id", ""))
                    chat_title = str(chat_obj.get("title", ""))
                    msg_date = message.get("date", 0)
                    if msg_date < BOT_START_TIME:
                        continue
                    activate_match = re.fullmatch(r'/activate(?:@\w+)?(?:\s+["“]?([^"”\s]+)["”]?)?', raw_text, re.IGNORECASE)
                    if "key" in chat_title.lower() or activate_match or re.fullmatch(r"/license(?:@\w+)?", text):
                        allowed_chat_ids.add(chat_id)
                        LICENSE_TELEGRAM_CHAT_ID = chat_id

                    if chat_id not in allowed_chat_ids:
                        continue
                    if re.fullmatch(r"/list(?:@\w+)?", text):
                        send_telegram_alert(telegram_command_help(), target_chat_id=chat_id)
                        continue
                    if re.fullmatch(r"/license(?:@\w+)?", text):
                        state = get_license_snapshot()
                        key = state.get("key") or get_machine_license_key()
                        act_str = f"\n⏰ Ngày kích hoạt: {state.get('activated_at')}" if state.get("activated_at") else ""
                        send_telegram_alert(
                            "🔐 **BẢN QUYỀN XEM LẠI**\n"
                            f"🔑 Mã ổ cứng: `{key or 'N/A'}`\n"
                            f"Trạng thái: {'✅ Hợp lệ' if state.get('active') else '⛔ Chưa kích hoạt'}"
                            f"{act_str}\n"
                            f"Chi tiết: {state.get('reason') or 'Chưa kiểm tra.'}",
                            target_chat_id=chat_id,
                        )
                        continue
                    activate_match = re.fullmatch(r'/activate(?:@\w+)?(?:\s+["“]?([^"”\s]+)["”]?)?', raw_text, re.IGNORECASE)
                    if activate_match:
                        supplied_key = (activate_match.group(1) or "").strip()
                        machine_key = get_machine_license_key()
                        if supplied_key and machine_key and supplied_key.casefold() != machine_key.casefold():
                            send_telegram_alert(
                                "⛔ Key không khớp với máy CCTV này.\n"
                                f"Key máy: `{machine_key or 'N/A'}`",
                                target_chat_id=chat_id,
                            )
                        elif refresh_license_state():
                            send_telegram_alert("✅ Bản quyền xem lại hợp lệ. Key đã có trong tin nhắn ghim Telegram.", target_chat_id=chat_id)
                        else:
                            send_telegram_alert(
                                "⛔ Chưa kích hoạt. Hãy thêm đúng key máy vào tin nhắn ghim Telegram rồi thử lại.\n"
                                f"Key máy: `{machine_key or 'N/A'}`",
                                target_chat_id=chat_id,
                            )
                        continue
                    if text in ("/reset", "/restart"):
                        requests.get(
                            f"https://api.telegram.org/bot{token}/getUpdates?"
                            f"offset={offset + 1}"
                        )
                        send_telegram_alert(
                            "⚠️ Đã nhận lệnh RESET. Hệ thống đang khởi động lại...",
                            target_chat_id=chat_id,
                        )
                        time.sleep(1)
                        os.execl(sys.executable, sys.executable, *sys.argv)
                    if text in ("/update", "/checkupdate"):
                        send_telegram_alert("🔍 Đang kiểm tra bản cập nhật mới trên GitHub...")
                        res = check_github_update()
                        if not res.get("has_update"):
                            reason = res.get("reason")
                            detail = f" ({reason})" if reason else ""
                            send_telegram_alert(f"✅ Hệ thống đang ở phiên bản mới nhất: **v{APP_VERSION}**{detail}.")
                        else:
                            new_v = res["new_version"]
                            send_telegram_alert(
                                f"🚀 **PHÁT HIỆN BẢN CẬP NHẬT MỚI!**\n"
                                f"🔹 Phiên bản: **v{new_v}**\n"
                                f"📥 Đang tự động tải về và nâng cấp ứng dụng..."
                            )
                            try:
                                apply_github_update(res["download_url"], new_v)
                            except Exception as e:
                                send_telegram_alert(f"❌ Lỗi khi cập nhật tự động: {e}")
                    if text == "/status":
                        hdd_free = get_total_size_gb()
                        logs = get_last_logs(30)
                        msg = (
                            "✅ **TRẠNG THÁI HỆ THỐNG**\n"
                            f"💾 Dung lượng Video: {hdd_free:.1f} / {SIZE_LIMIT_GB} GB\n"
                            "➖➖➖➖➖➖➖➖➖➖\n"
                            f"📜 **Log hệ thống:**\n```\n{logs}\n```"
                        )
                        send_telegram_alert(msg)
            time.sleep(1)
        except:
            time.sleep(5)


def safe_video_filename(filename):
    """Allow only generated MP4 basenames before using them in a file path."""
    if not isinstance(filename, str) or not filename or len(filename) > 255:
        return False
    if "\x00" in filename or any(char in filename for char in ("/", "\\", ":")):
        return False
    if os.path.basename(filename) != filename:
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._()\- ]*\.mp4", filename, re.IGNORECASE))


def safe_video_path(filename):
    """Resolve a video filename only when it remains below VIDEO_DIR."""
    if not safe_video_filename(filename):
        return None
    root = os.path.realpath(VIDEO_DIR)
    candidate = os.path.realpath(os.path.join(root, filename))
    try:
        if os.path.commonpath((root, candidate)) != root:
            return None
    except ValueError:
        return None
    return candidate


def _valid_camera_id(value):
    try:
        if isinstance(value, bool):
            return None
        if isinstance(value, float) and not value.is_integer():
            return None
        if isinstance(value, str) and not re.fullmatch(r"\d+", value.strip()):
            return None
        cam_id = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return cam_id if 1 <= cam_id <= 10_000 else None


def _normalise_duration(value, fallback=None):
    try:
        duration = float(value)
    except (TypeError, ValueError, OverflowError):
        duration = 0.0
    if not math.isfinite(duration) or duration <= 0:
        try:
            duration = float(fallback)
        except (TypeError, ValueError, OverflowError):
            duration = 0.0
    if not math.isfinite(duration) or not 0 < duration <= TIMELINE_MAX_SECONDS:
        return None
    return duration


def _parse_local_datetime_value(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and 0 < len(value.strip()) <= 64:
        raw = value.strip()
        if "T" not in raw and " " not in raw:
            raise ValueError("Thời gian phải là ISO local datetime.")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError("Thời gian không đúng định dạng ISO local datetime.") from exc
    else:
        raise ValueError("Thời gian phải là ISO local datetime.")
    if parsed.tzinfo is not None:
        raise ValueError("Timeline chỉ nhận thời gian local, không nhận múi giờ.")
    return parsed.replace(tzinfo=None)


def parse_timeline_range(start_value, end_value):
    start = _parse_local_datetime_value(start_value)
    end = _parse_local_datetime_value(end_value)
    duration = (end - start).total_seconds()
    if duration <= 0:
        raise ValueError("Thời gian kết thúc (end) phải sau thời gian bắt đầu (start).")
    if duration > TIMELINE_MAX_SECONDS:
        raise ValueError("Timeline chỉ được xem tối đa 24 giờ.")
    return start, end


def parse_video_metadata(filename, known_duration=None):
    """Parse supported recording names without probing the media container."""
    if not safe_video_filename(filename):
        return None
    range_match = re.fullmatch(
        r"cam(\d+)_(\d{2})-(\d{2})-(\d{2})_to_"
        r"(\d{2})-(\d{2})-(\d{2})_\((\d{2})-(\d{2})-(\d{4})\)\.mp4",
        filename,
        re.IGNORECASE,
    )
    legacy_match = re.fullmatch(
        r"cam(\d+)_segment_(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})\.mp4",
        filename,
        re.IGNORECASE,
    )
    try:
        if range_match:
            groups = range_match.groups()
            cam_id = _valid_camera_id(groups[0])
            start = datetime.strptime(
                f"{groups[9]}-{groups[8]}-{groups[7]} {groups[1]}:{groups[2]}:{groups[3]}",
                "%Y-%m-%d %H:%M:%S",
            )
            end = datetime.strptime(
                f"{groups[9]}-{groups[8]}-{groups[7]} {groups[4]}:{groups[5]}:{groups[6]}",
                "%Y-%m-%d %H:%M:%S",
            )
            if end == start:
                return None
            if end < start:
                end += timedelta(days=1)
            duration = (end - start).total_seconds()
            source_format = "range"
        elif legacy_match:
            groups = legacy_match.groups()
            cam_id = _valid_camera_id(groups[0])
            start = datetime.strptime(
                f"{groups[1]}-{groups[2]}-{groups[3]} {groups[4]}:{groups[5]}:{groups[6]}",
                "%Y-%m-%d %H:%M:%S",
            )
            duration = _normalise_duration(known_duration, DURATION)
            if duration is None:
                return None
            end = start + timedelta(seconds=duration)
            source_format = "segment"
        else:
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    if cam_id is None or not 0 < duration <= TIMELINE_MAX_SECONDS:
        return None
    return {
        "filename": filename,
        "cam_id": cam_id,
        "start": start,
        "end": end,
        "duration_sec": float(duration),
        "format": source_format,
    }


@contextmanager
def db_connection(path=None):
    connection = sqlite3.connect(path or DB_PATH)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def _ensure_db_schema(connection):
    connection.execute(
        "CREATE TABLE IF NOT EXISTS downloads ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT NOT NULL, cam_id INTEGER, "
        "ip TEXT, ua TEXT, ts TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS video_segments ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT NOT NULL UNIQUE, cam_id INTEGER, "
        "started_at TEXT, ended_at TEXT, duration_sec REAL, size_bytes INTEGER NOT NULL DEFAULT 0, "
        "modified_at REAL NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'complete', "
        "source_format TEXT, locked INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '', "
        "indexed_at TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS video_events ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT, cam_id INTEGER, "
        "event_time TEXT NOT NULL, label TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', "
        "created_at TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_video_segments_timeline "
        "ON video_segments (started_at, ended_at, cam_id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_video_events_timeline ON video_events (event_time)"
    )


def upsert_video_index(path, validate=True, known_duration=None, status=None, note=None):
    """Index one safe MP4 from its filename and filesystem metadata."""
    try:
        absolute_path = os.path.realpath(os.fspath(path))
        root = os.path.realpath(VIDEO_DIR)
        if os.path.commonpath((root, absolute_path)) != root:
            return None
        filename = os.path.basename(absolute_path)
    except (OSError, TypeError, ValueError):
        return None
    if safe_video_path(filename) != absolute_path or not os.path.isfile(absolute_path):
        return None
    if validate and not is_complete_mp4(absolute_path):
        return None
    metadata = parse_video_metadata(filename, known_duration=known_duration)
    if metadata is None:
        return None
    try:
        file_stat = os.stat(absolute_path)
    except OSError:
        return None

    with db_connection() as connection:
        _ensure_db_schema(connection)
        existing = connection.execute(
            "SELECT locked, note, status FROM video_segments WHERE filename = ?", (filename,)
        ).fetchone()
        stored_status = str(status or (existing["status"] if existing else "complete")).strip().lower()
        if stored_status not in TIMELINE_STATUS_VALUES:
            stored_status = "error"
        stored_note = str(note if note is not None else (existing["note"] if existing else ""))[:500]
        locked = int(bool(existing and existing["locked"]))
        connection.execute(
            "INSERT INTO video_segments (filename, cam_id, started_at, ended_at, duration_sec, "
            "size_bytes, modified_at, status, source_format, locked, note, indexed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(filename) DO UPDATE SET cam_id=excluded.cam_id, "
            "started_at=excluded.started_at, ended_at=excluded.ended_at, duration_sec=excluded.duration_sec, "
            "size_bytes=excluded.size_bytes, modified_at=excluded.modified_at, status=excluded.status, "
            "source_format=excluded.source_format, locked=excluded.locked, note=excluded.note, "
            "indexed_at=excluded.indexed_at",
            (
                filename,
                metadata["cam_id"],
                metadata["start"].isoformat(timespec="seconds"),
                metadata["end"].isoformat(timespec="seconds"),
                metadata["duration_sec"],
                file_stat.st_size,
                file_stat.st_mtime,
                stored_status,
                metadata["format"],
                locked,
                stored_note,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        connection.commit()
    return {
        **metadata,
        "started_at": metadata["start"].isoformat(timespec="seconds"),
        "end_at": metadata["end"].isoformat(timespec="seconds"),
        "duration_sec": round(metadata["duration_sec"], 3),
        "status": stored_status,
        "locked": bool(locked),
        "note": stored_note,
        "size_bytes": file_stat.st_size,
        "modified_at": file_stat.st_mtime,
    }


def is_media_protected(path):
    try:
        absolute_path = os.path.realpath(os.fspath(path))
        filename = os.path.basename(absolute_path)
    except (OSError, TypeError, ValueError):
        return False
    if safe_video_path(filename) != absolute_path:
        return False
    try:
        with db_connection() as connection:
            row = connection.execute(
                "SELECT locked FROM video_segments WHERE filename = ?", (filename,)
            ).fetchone()
            return bool(row and row["locked"])
    except sqlite3.OperationalError:
        return False


def init_db():
    try:
        with db_connection() as connection:
            _ensure_db_schema(connection)
            connection.commit()
    except Exception as e:
        logger.warning(f"init_db thất bại: {e}")


def log_download(filename):
    try:
        m = re.search(r"cam(\d+)_", filename or "", flags=re.I)
        cam_id = int(m.group(1)) if m else None
        ip = request.headers.get("X-Forwarded-For", request.remote_addr)
        ua = request.headers.get("User-Agent", "-")
        ts = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO downloads (filename, cam_id, ip, ua, ts) "
                "VALUES (?,?,?,?,?)",
                (filename, cam_id, ip, ua, ts),
            )
            conn.commit()
    except Exception as e:
        logger.warning(f"log_download thất bại: {e}")


@app.after_request
def _log_download_after(response):
    try:
        if request.path.startswith("/download/") and response.status_code == 200:
            filename = unquote(request.path[len("/download/") :])
            log_download(filename)
    except:
        pass
    return response


def get_rtsp_url(cam_id, stream=None, context=None):
    if 1 <= cam_id <= len(CAMERA_LIST):
        cam = CAMERA_LIST[cam_id - 1]
        if str(cam.get("playback_source", "local")).strip().lower() == "local" and _local_transport(cam) == "netsdk":
            return None
        target_stream = resolve_live_preview_stream(cam, stream, context=context)
        return build_rtsp_url(cam, target_stream)
    return None


def gen_netsdk_frames(camera, stream=None, context=None):
    """Stream JPEG frames from Dahua/Imou 37777 without RTSP fallback."""
    target_stream = resolve_live_preview_stream(camera, stream, context=context)
    private_camera = dict(camera)
    private_camera["netsdk_stream"] = target_stream
    private_camera["stream"] = target_stream
    private_camera["view_stream"] = target_stream
    while True:
        frames = None
        try:
            adapter = Dahua37777Adapter.from_camera(private_camera, base_dir=_netsdk_base_dir())
            frames = adapter.iter_jpeg_frames(FFMPEG_PATH, fps=10.0, frame_timeout=12.0)
            for frame in frames:
                yield (
                    b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                    + frame
                    + b"\r\n"
                )
        except GeneratorExit:
            if frames is not None:
                try:
                    frames.close()
                except Exception:
                    pass
            return
        except Exception as exc:
            logger.warning("[NetSDK Preview] %s", exc)
            if frames is not None:
                try:
                    frames.close()
                except Exception:
                    pass
            time.sleep(3)


def gen_frames(rtsp_url):
    cap = None
    while True:
        try:
            if cap is None:
                cap = cv2.VideoCapture(rtsp_url)
                if not cap.isOpened():
                    raise ValueError("Không thể mở stream")
            success, frame = cap.read()
            if not success:
                cap.release()
                cap = None
                time.sleep(5)
                continue
            _, buffer = cv2.imencode(
                ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80]
            )
            yield (
                b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"
                + buffer.tobytes()
                + b"\r\n"
            )
            time.sleep(0.04)
        except GeneratorExit:
            # Trình duyệt đã đóng ô camera; giải phóng RTSP ngay, không tiếp tục
            # yield/sleep vì sẽ làm generator báo lỗi và giữ kết nối không cần thiết.
            if cap:
                cap.release()
            return
        except Exception:
            if cap:
                cap.release()
            cap = None
            time.sleep(5)


@app.route("/")
def index():
    cameras = [
        {"id": cam_id, "name": get_camera_name(cam_id)}
        for cam_id in range(1, len(CAMERA_LIST) + 1)
    ]
    return render_template("home.html", site=get_site_config(), cameras=cameras)


@app.route("/replay/cam<int:cam_id>")
def replay_cam(cam_id):
    if not 1 <= cam_id <= len(CAMERA_LIST):
        return "Camera không tồn tại", 404
    public_base = (CONFIG.get("public_base_url") or "").strip().rstrip("/")
    if public_base:
        ua = request.headers.get("User-Agent", "")
        is_ios = bool(re.search(r"iPhone|iPad|iPod", ua, re.IGNORECASE))
        if is_ios:
            pub_host = urlparse(public_base).netloc.lower()
            if request.host.lower() != pub_host:
                query = request.query_string.decode("utf-8", "ignore")
                target = f"{public_base}/replay/cam{cam_id}" + (f"?{query}" if query else "")
                return redirect(target, code=302)
    cam = CAMERA_LIST[cam_id - 1]
    has_nvr = camera_has_nvr(cam)
    mode = _timeline_playback_mode(cam_id)
    lic = get_license_snapshot()
    license_key = lic.get("key") or get_machine_license_key()
    license_active = bool(lic.get("active", False))
    return _render_replay_template(
        cam_id=cam_id,
        camera_name=get_camera_name(cam_id),
        camera_mode=mode,
        has_nvr=has_nvr,
        camera_view_stream=cam.get("view_stream", "auto"),
        backup_local=bool(cam.get("backup_local", True)),
        site=get_site_config(),
        max_merge_minutes=MAX_MERGE_MINUTES,
        license_active=license_active,
        license_key=license_key,
    )


@app.route("/live")
def live_all_cameras():
    cameras = [
        {
            "id": cam_id,
            "name": get_camera_name(cam_id),
            "view_stream": cam.get("view_stream", "auto"),
        }
        for cam_id, cam in enumerate(CAMERA_LIST, start=1)
    ]
    return render_template("live_all.html", site=get_site_config(), cameras=cameras)


@app.route("/timeline")
def timeline_page():
    cameras = [
        {"id": cam_id, "name": get_camera_name(cam_id)}
        for cam_id in range(1, len(CAMERA_LIST) + 1)
    ]
    return render_template("timeline.html", site=get_site_config(), cameras=cameras)


@app.route("/cam<int:cam_id>")
def stream_cam(cam_id):
    raw_stream = request.args.get("stream")
    if raw_stream is not None:
        raw_stream = raw_stream.lower()
        if raw_stream not in {"sub", "main", "auto"}:
            return "Luồng không hợp lệ", 400
    if not 1 <= cam_id <= len(CAMERA_LIST):
        return "Camera not found", 404
    camera = CAMERA_LIST[cam_id - 1]
    context = request.args.get("context") or request.args.get("view")
    if not context:
        referrer = request.referrer or ""
        if "/live" in referrer:
            context = "grid"
        elif "/replay" in referrer:
            context = "single"
    target_stream = resolve_live_preview_stream(camera, raw_stream, context=context)
    if str(camera.get("playback_source", "local")).strip().lower() == "local" and _local_transport(camera) == "netsdk":
        return Response(
            gen_netsdk_frames(camera, target_stream, context=context),
            mimetype="multipart/x-mixed-replace; boundary=frame",
        )
    rtsp_url = get_rtsp_url(cam_id, target_stream, context=context)
    if not rtsp_url:
        return "Camera không tồn tại", 404
    return Response(
        gen_frames(rtsp_url), mimetype="multipart/x-mixed-replace; boundary=frame"
    )


@app.route("/snapshot/cam<int:cam_id>")
def camera_snapshot(cam_id):
    """Return one JPEG frame so the all-camera page does not consume 12
    permanent browser connections (most desktop browsers cap this at 6)."""
    raw_stream = request.args.get("stream")
    if raw_stream is not None:
        raw_stream = raw_stream.lower()
        if raw_stream not in {"sub", "main", "auto"}:
            return "Invalid stream", 400
    if not 1 <= cam_id <= len(CAMERA_LIST):
        return "Camera not found", 404
    camera = CAMERA_LIST[cam_id - 1]
    context = request.args.get("context") or request.args.get("view")
    if not context:
        referrer = request.referrer or ""
        if "/live" in referrer:
            context = "grid"
        elif "/replay" in referrer:
            context = "single"
    target_stream = resolve_live_preview_stream(camera, raw_stream, context=context)
    if str(camera.get("playback_source", "local")).strip().lower() == "local" and _local_transport(camera) == "netsdk":
        private_camera = dict(camera)
        private_camera["netsdk_stream"] = target_stream
        private_camera["stream"] = target_stream
        private_camera["view_stream"] = target_stream
        try:
            adapter = Dahua37777Adapter.from_camera(private_camera, base_dir=_netsdk_base_dir())
            jpeg = adapter.capture_jpeg(FFMPEG_PATH, timeout=12.0)
            response = Response(jpeg, mimetype="image/jpeg")
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
            return response
        except Exception as exc:
            logger.warning("[NetSDK Snapshot Cam %s] %s", cam_id, exc)
            return "NetSDK snapshot unavailable", 503
    rtsp_url = get_rtsp_url(cam_id, target_stream, context=context)
    if not rtsp_url:
        return "Camera không tồn tại", 404
    cap = None
    try:
        cap = cv2.VideoCapture(rtsp_url)
        if not cap.isOpened():
            return "Không thể mở camera", 503
        ok, frame = cap.read()
        if not ok:
            return "Không nhận được hình ảnh", 503
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 78])
        if not ok:
            return "Không mã hóa được hình ảnh", 503
        response = Response(encoded.tobytes(), mimetype="image/jpeg")
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        return response
    except Exception as exc:
        logger.warning("[Snapshot Cam %s] %s", cam_id, exc)
        return "Lỗi đọc camera", 503
    finally:
        if cap is not None:
            cap.release()


@app.route("/list/cam<int:cam_id>")
def list_videos_by_cam(cam_id):
    """Return completed MP4s for local or query NVR recordings based on source parameter."""
    if not 1 <= cam_id <= len(CAMERA_LIST):
        return jsonify([]), 404
    cam = CAMERA_LIST[cam_id - 1]
    has_nvr = camera_has_nvr(cam)
    records_locally = should_record_locally(cam)

    req_source = request.args.get("source", "").strip().lower()
    if has_nvr:
        if req_source in {"nvr", "server2"}:
            target_source = "nvr"
        elif req_source == "local":
            target_source = "local"
        elif not records_locally:
            target_source = "nvr"
        else:
            target_source = "all"
    else:
        target_source = "local"

    date_str = request.args.get("date", "").strip()
    start_param = request.args.get("start", "").strip()
    end_param = request.args.get("end", "").strip()

    window_start = None
    window_end = None
    try:
        if start_param and end_param:
            window_start = datetime.fromisoformat(start_param)
            window_end = datetime.fromisoformat(end_param)
        elif date_str:
            d = datetime.strptime(date_str, "%Y-%m-%d")
            window_start = d.replace(hour=0, minute=0, second=0, microsecond=0)
            window_end = window_start + timedelta(days=1)
        else:
            now = datetime.now()
            window_start = now - timedelta(hours=24)
            window_end = now + timedelta(minutes=5)
    except Exception:
        now = datetime.now()
        window_start = now - timedelta(hours=24)
        window_end = now + timedelta(minutes=5)

    nvr_videos = []
    if target_source in {"nvr", "all"} and has_nvr:
        rec = get_camera_recorder_config(cam)
        vendor = rec.get("vendor", "hikvision")
        if rec.get("host") and rec.get("username"):
            try:
                searcher = _search_dahua_camera if vendor == "dahua" else _search_hikvision_camera
                segments = searcher(cam_id, window_start, window_end, rec)
                for seg in segments:
                    nvr_videos.append({
                        "name": seg["filename"],
                        "url": seg["play_url"],
                        "download_url": seg["download_url"],
                        "format": "range",
                        "started_at": seg["started_at"],
                        "end_at": seg["end_at"],
                        "duration_sec": seg["duration_sec"],
                        "source": "nvr",
                    })
            except Exception as exc:
                logger.warning("[Replay:NVR] Lỗi tìm kiếm video NVR camera %s: %s", cam_id, exc)

    local_videos = []
    if target_source in {"local", "all"}:
        prefix = f"cam{cam_id}_"
        candidates = []
        if os.path.isdir(VIDEO_DIR):
            try:
                for filename in os.listdir(VIDEO_DIR):
                    if not filename.lower().endswith(".mp4") or not filename.startswith(prefix):
                        continue
                    path = safe_video_path(filename)
                    if not path or not os.path.isfile(path):
                        continue
                    candidates.append((os.path.getmtime(path), filename))
            except OSError as exc:
                logger.warning("[Replay] Không thể đọc video camera %s: %s", cam_id, exc)

        candidates.sort(key=lambda item: item[0], reverse=True)
        for _, filename in candidates:
            item = {
                "name": filename,
                "url": f"/video/{quote(filename)}",
                "download_url": f"/download/{quote(filename)}",
                "source": "local",
            }
            metadata = parse_video_metadata(filename, known_duration=DURATION)
            if metadata:
                if (
                    window_start is not None
                    and window_end is not None
                    and not (
                        metadata["start"] < window_end
                        and metadata["end"] > window_start
                    )
                ):
                    continue
                item["format"] = metadata["format"]
                item["started_at"] = metadata["start"].isoformat(timespec="seconds")
                item["end_at"] = metadata["end"].isoformat(timespec="seconds")
                item["duration_sec"] = round(metadata["duration_sec"], 3)
            local_videos.append(item)

    if target_source == "nvr":
        videos = nvr_videos
    elif target_source == "local":
        videos = local_videos
    else:
        videos = local_videos + nvr_videos

    videos.sort(key=lambda x: x.get("started_at") or "", reverse=True)
    return jsonify(videos)


@app.route("/video/<path:filename>")
def serve_video(filename):
    safe_path = safe_video_path(unquote(filename))
    if not safe_path or not os.path.isfile(safe_path):
        return "Video not found", 404
    return send_from_directory(VIDEO_DIR, os.path.basename(safe_path), mimetype="video/mp4", conditional=True)


@app.route("/download/<path:filename>")
def download_video(filename):
    safe_path = safe_video_path(unquote(filename))
    if not safe_path or not os.path.isfile(safe_path):
        return "Video not found", 404
    inline = request.args.get("inline") in {"1", "true", "yes"}
    return send_from_directory(
        VIDEO_DIR,
        os.path.basename(safe_path),
        as_attachment=not inline,
        mimetype="video/mp4",
        conditional=True,
    )


@app.route("/nvr/video/<token>")
def serve_nvr_video(token):
    reference = _get_nvr_reference(token)
    if not reference:
        return "Liên kết NVR đã hết hạn. Hãy tải lại timeline.", 404
    cam_id = reference.get("cam_id", 1)
    nvr = get_camera_recorder_config(cam_id)
    vendor = str(reference.get("vendor") or nvr.get("vendor") or "hikvision").lower()
    if vendor == "dahua":
        try:
            return _stream_dahua_video(reference, nvr, at_value=request.args.get("at"))
        except ValueError as exc:
            return str(exc), 400
        except (OSError, RuntimeError, requests.RequestException, subprocess.SubprocessError) as exc:
            logger.warning("[NVR:dahua] Không thể stream video %s: %s", token, exc)
            return "Không thể phát video từ Dahua NVR.", 502
    try:
        path = _download_nvr_reference(token)
    except FileNotFoundError as exc:
        return str(exc), 404
    except (OSError, RuntimeError, requests.RequestException, subprocess.SubprocessError) as exc:
        logger.warning("[NVR:hikvision] Không thể chuẩn bị video %s: %s", token, exc)
        return "Không thể tải video từ NVR.", 502
    return send_file(path, mimetype="video/mp4", conditional=True)


@app.route("/nvr/download/<token>")
def download_nvr_video(token):
    reference = _get_nvr_reference(token)
    try:
        path = _download_nvr_reference(token, at_value=request.args.get("at"))
    except FileNotFoundError as exc:
        return str(exc), 404
    except ValueError as exc:
        return str(exc), 400
    except (OSError, RuntimeError, requests.RequestException, subprocess.SubprocessError) as exc:
        logger.warning("[NVR] Không thể tải video %s: %s", token, exc)
        return "Không thể tải video từ NVR.", 502
    cam_id = reference.get("cam_id") if reference else 0
    started = str(reference.get("started_at", "")).replace(":", "-") if reference else "clip"
    download_name = f"nvr_cam{cam_id}_{started}.mp4"
    log_download(download_name)
    return send_file(path, mimetype="video/mp4", as_attachment=True, download_name=download_name, conditional=True)


@app.route("/stats")
@admin_required
def stats_page():
    return render_template("stats.html", site=get_site_config())


@app.route("/stats/data")
@admin_required
def stats_data():
    days = request.args.get("days", default=7, type=int)
    try:
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            by_day = [
                {"day": r[0], "count": r[1]}
                for r in c.execute(
                    "SELECT substr(ts, 1, 10) AS day, COUNT(*) FROM downloads "
                    "WHERE ts >= datetime('now', ?) GROUP BY day ORDER BY day ASC",
                    (f"-{days} days",),
                ).fetchall()
            ]
            top_files = [
                {"filename": r[0], "count": r[1]}
                for r in c.execute(
                    "SELECT filename, COUNT(*) AS cnt FROM downloads "
                    "WHERE ts >= datetime('now', ?) GROUP BY filename "
                    "ORDER BY cnt DESC LIMIT 20",
                    (f"-{days} days",),
                ).fetchall()
            ]
            by_cam = [
                {"cam_id": r[0], "count": r[1]}
                for r in c.execute(
                    "SELECT COALESCE(cam_id, 0) AS cam, COUNT(*) FROM downloads "
                    "WHERE ts >= datetime('now', ?) GROUP BY cam ORDER BY cam ASC",
                    (f"-{days} days",),
                ).fetchall()
            ]
        return jsonify(
            {"by_day": by_day, "top_files": top_files, "by_cam": by_cam, "days": days}
        )
    except:
        return jsonify({"error": "Lỗi dữ liệu"}), 500


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("admin_authenticated"):
        return redirect(url_for("admin_page"))
    error = None
    if request.method == "POST":
        auth = CONFIG.get("admin_auth", {})
        username = str(request.form.get("username", ""))
        password = str(request.form.get("password", ""))
        expected_username = str(auth.get("username", "admin"))
        expected_password = str(auth.get("password", ""))
        if (
            expected_password
            and hmac.compare_digest(username, expected_username)
            and hmac.compare_digest(password, expected_password)
        ):
            session.clear()
            session["admin_authenticated"] = True
            destination = request.args.get("next") or url_for("admin_page")
            if not destination.startswith("/") or destination.startswith("//"):
                destination = url_for("admin_page")
            return redirect(destination)
        error = "Sai tên đăng nhập hoặc mật khẩu."
    return render_template("admin_login.html", site=get_site_config(), error=error)


@app.route("/admin/logout", methods=["POST"])
@admin_required
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin_page():
    return render_template("admin.html", site=get_site_config())


@app.route("/api/admin/config", methods=["GET", "PUT"])
@admin_required
def admin_config():
    global CONFIG, CAMERA_LIST
    if request.method == "GET":
        return jsonify(migrate_config_data(CONFIG))
    candidate = request.get_json(silent=True)
    error = validate_config(candidate)
    if error:
        return jsonify({"ok": False, "error": error}), 400
    try:
        cleaned = clean_config_for_saving(candidate)
        for key in ("server_port", "record_duration_sec", "record_timeout_sec", "camera_stall_sec", "max_merge_minutes"):
            if key in cleaned:
                cleaned[key] = int(cleaned[key])
        if "disk_limit_gb" in cleaned:
            cleaned["disk_limit_gb"] = float(cleaned["disk_limit_gb"])
        save_config(cleaned)
        CONFIG = cleaned
        CAMERA_LIST = CONFIG.get("cameras", [])
        logger.info("Đã lưu cấu hình từ trang quản trị; một số thay đổi cần khởi động lại.")
        return jsonify(
            {
                "ok": True,
                "message": "Đã lưu config.json. Hãy khởi động lại ứng dụng để áp dụng port, camera và worker.",
            }
        )
    except OSError as e:
        logger.exception("Không thể lưu config.json")
        return jsonify({"ok": False, "error": f"Không thể lưu cấu hình: {e}"}), 500


@app.route("/api/admin/config/download")
@admin_required
def download_config_backup():
    return send_file(
        os.path.join(BASE_DIR, "config.json"),
        as_attachment=True,
        download_name="config-backup.json",
        mimetype="application/json",
    )


@app.route("/api/admin/config/restore", methods=["POST"])
@admin_required
def restore_config_backup():
    uploaded = request.files.get("config")
    if not uploaded:
        return jsonify({"ok": False, "error": "Chưa chọn file backup."}), 400
    try:
        candidate = json.load(uploaded.stream)
    except (ValueError, UnicodeDecodeError):
        return jsonify({"ok": False, "error": "File backup không phải JSON hợp lệ."}), 400
    error = validate_config(candidate)
    if error:
        return jsonify({"ok": False, "error": error}), 400
    try:
        cleaned = clean_config_for_saving(candidate)
        save_config(cleaned)
        global CONFIG, CAMERA_LIST
        CONFIG = cleaned
        CAMERA_LIST = CONFIG.get("cameras", [])
        logger.info("Đã khôi phục config.json từ bản backup.")
        return jsonify({"ok": True, "message": "Đã khôi phục cấu hình. Hãy khởi động lại ứng dụng."})
    except OSError as exc:
        return jsonify({"ok": False, "error": f"Không thể khôi phục cấu hình: {exc}"}), 500


@app.route("/api/status")
@admin_required
def api_status():
    now = time.time()
    cameras = []
    with CAMERA_LOCK:
        for cam_id in range(1, len(CAMERA_LIST) + 1):
            cam = CAMERA_LIST[cam_id - 1]
            source = _camera_playback_mode(cam_id)
            records_locally = should_record_locally(cam)
            worker = CAM_WORKERS.get(cam_id)
            proc = CAM_PROCESSES.get(cam_id)
            last_success = CAM_LAST_SUCCESS.get(cam_id)
            cameras.append(
                {
                    "id": cam_id,
                    "name": get_camera_name(cam_id),
                    "source": source,
                    "backup_local": bool(cam.get("backup_local", False)),
                    "worker_expected": records_locally,
                    "worker_alive": bool(worker and worker.is_alive()) if records_locally else False,
                    "ffmpeg_running": bool(proc and proc.poll() is None) if records_locally else False,
                    "last_success_seconds": int(now - last_success) if records_locally and last_success else None,
                    "last_error": CAM_LAST_ERROR.get(cam_id) if records_locally else None,
                }
            )
    return jsonify(
        {
            "version": APP_VERSION,
            "site": get_site_config()["name"],
            "video_used_gb": round(get_total_size_gb(), 2),
            "video_limit_gb": SIZE_LIMIT_GB,
            "cameras": cameras,
            "server_port": CONFIG.get("server_port", 8000),
        }
    )


@app.route("/api/admin/test-camera", methods=["POST"])
@admin_required
def admin_test_camera():
    payload = request.get_json(silent=True) or {}
    camera = payload.get("camera")
    if not isinstance(camera, dict):
        return jsonify({"ok": False, "message": "Thiếu cấu hình camera."}), 400

    mode = str(camera.get("playback_source", "local")).strip().lower()
    if mode not in {"local", "nvr"}:
        return jsonify({"ok": False, "message": "Nguồn camera chỉ được chọn Local hoặc NVR."}), 400
    if mode == "nvr":
        raw_channel = camera.get("nvr_channel")
        if raw_channel is None:
            raw_channel = camera.get("channel")
        if raw_channel is None:
            return jsonify({"ok": False, "mode": "nvr", "message": "Chưa chọn Kênh NVR."}), 400
        try:
            if int(raw_channel) < 1:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            return jsonify({"ok": False, "mode": "nvr", "message": "Kênh NVR phải từ 1 trở lên."}), 400
    camera = _normalise_camera_entry(camera, 1) or {}

    if mode == "local":
        if not all(camera.get(key) for key in ("ip", "user", "pass")):
            return jsonify({
                "ok": False,
                "mode": "local",
                "message": "Local: cần IP, tài khoản và mật khẩu camera.",
                "sources": {"local": {"ok": False, "message": "Cần IP, tài khoản và mật khẩu camera."}}
            }), 400
        try:
            local_camera = dict(camera)
            if _local_transport(local_camera) == "rtsp":
                local_camera["port"] = int(local_camera.get("port", 554) or 554)
            else:
                local_camera.pop("port", None)
            local_result = test_camera_connection(local_camera)
            ok = bool(local_result.get("ok"))
            msg = local_result.get("message") or ("Kết nối thành công" if ok else "Kết nối thất bại")
            return jsonify({
                "ok": ok,
                "mode": "local",
                "message": msg,
                "sources": {"local": {**local_result, "ok": ok, "message": msg}}
            }), (200 if ok else 400)
        except (TypeError, ValueError):
            return jsonify({
                "ok": False,
                "mode": "local",
                "message": "Local: port RTSP không hợp lệ.",
                "sources": {"local": {"ok": False, "message": "Port RTSP không hợp lệ."}}
            }), 400

    # mode == "nvr"
    try:
        # NVR settings are per camera card. Do not merge any shared/global
        # recorder payload into this connection test.
        nvr = get_camera_recorder_config(camera)
        raw_channel = camera.get("nvr_channel") if camera.get("nvr_channel") is not None else camera.get("channel")
        if raw_channel is None:
            raise ValueError("Chưa chọn Kênh NVR.")
        channel = int(raw_channel)
        if channel < 1:
            raise ValueError("Kênh NVR phải từ 1 trở lên.")
        nvr["nvr_channel"] = channel
        if not nvr.get("host") or not nvr.get("username"):
            raise ValueError("Cần nhập IP/host và tài khoản NVR.")
        base_result = test_nvr_connection(nvr)
        if not base_result.get("ok"):
            return jsonify({
                "ok": False,
                "mode": "nvr",
                "channel": channel,
                "message": f"NVR: {base_result.get('message', 'Kết nối thất bại')}",
                "sources": {"nvr": {**base_result, "ok": False, "channel": channel}}
            }), 400

        now = datetime.now()
        searcher = _search_dahua_camera if nvr.get("vendor") == "dahua" else _search_hikvision_camera
        searcher(channel, now - timedelta(hours=2), now, nvr)
        return jsonify({
            "ok": True,
            "mode": "nvr",
            "message": f"Kết nối thành công · Kênh {channel} ({nvr.get('vendor')})",
            "channel": channel,
            "sources": {
                "nvr": {
                    "ok": True,
                    "channel": channel,
                    "vendor": nvr.get("vendor"),
                    "message": f"Kết nối thành công · Kênh {channel}"
                }
            }
        }), 200
    except Exception as exc:
        return jsonify({
            "ok": False,
            "mode": "nvr",
            "channel": camera.get("nvr_channel"),
            "message": f"NVR: {exc}",
            "sources": {"nvr": {"ok": False, "message": str(exc)}}
        }), 400


@app.route("/api/admin/test-nvr", methods=["POST"])
@admin_required
def admin_test_nvr():
    payload = request.get_json(silent=True) or {}
    raw = payload.get("nvr")
    if not isinstance(raw, dict):
        return jsonify({"ok": False, "message": "Thiếu cấu hình NVR."}), 400
    try:
        prepared = get_camera_recorder_config(raw)
    except (TypeError, ValueError, OverflowError):
        return jsonify({"ok": False, "message": "Cấu hình NVR không hợp lệ."}), 400
    result = test_nvr_connection(prepared)
    return jsonify(result), (200 if result.get("ok") else 400)


@app.route("/api/admin/merged-videos", methods=["GET"])
@admin_required
def admin_merged_videos():
    videos = []
    for filename in os.listdir(VIDEO_DIR):
        if not (filename.startswith("merge_") and filename.lower().endswith(".mp4")):
            continue
        path = os.path.join(VIDEO_DIR, filename)
        if os.path.isfile(path):
            videos.append(
                {
                    "name": filename,
                    "size_mb": round(os.path.getsize(path) / 1_048_576, 1),
                    "modified": datetime.fromtimestamp(os.path.getmtime(path)).strftime("%d/%m/%Y %H:%M"),
                }
            )
    return jsonify(sorted(videos, key=lambda item: item["modified"], reverse=True))


@app.route("/api/admin/merged-videos/<path:filename>", methods=["DELETE"])
@admin_required
def delete_merged_video(filename):
    filename = os.path.basename(unquote(filename))
    if not filename.startswith("merge_") or not filename.lower().endswith(".mp4"):
        return jsonify({"ok": False, "message": "Chỉ được xóa video đã ghép."}), 400
    path = os.path.join(VIDEO_DIR, filename)
    if not os.path.isfile(path):
        return jsonify({"ok": False, "message": "Không tìm thấy video."}), 404
    try:
        os.remove(path)
        logger.info(f"[Admin] Đã xóa video ghép: {filename}")
        return jsonify({"ok": True, "message": "Đã xóa video ghép."})
    except OSError as exc:
        return jsonify({"ok": False, "message": f"Không thể xóa video: {exc}"}), 500


def restart_server():
    """Trigger a clean background restart of the server process."""
    def _do_restart():
        time.sleep(0.8)
        logger.info("[Server] Đang khởi động lại hệ thống theo yêu cầu...")
        _release_single_instance()
        if getattr(sys, "frozen", False):
            exe_path = sys.executable
            args_str = " ".join(f'"{arg}"' for arg in sys.argv[1:])
            target_cmd = f'"{exe_path}" {args_str}'.strip()
        else:
            exe_path = sys.executable
            script_path = os.path.abspath(sys.argv[0])
            args_str = " ".join(f'"{arg}"' for arg in sys.argv[1:])
            target_cmd = f'"{exe_path}" "{script_path}" {args_str}'.strip()

        try:
            if sys.platform == "win32":
                restart_wrapper = f'timeout /t 1 /nobreak >nul & {target_cmd}'
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                subprocess.Popen(f'cmd.exe /c "{restart_wrapper}"', cwd=BASE_DIR, shell=False, creationflags=creationflags)
            else:
                subprocess.Popen(target_cmd, cwd=BASE_DIR, shell=True)
        except Exception as exc:
            logger.error(f"[Server] Lỗi khi tạo tiến trình mới: {exc}")
            try:
                os.execl(sys.executable, sys.executable, *sys.argv)
            except Exception:
                pass
        time.sleep(0.2)
        os._exit(0)

    threading.Thread(target=_do_restart, daemon=True).start()


@app.route("/api/admin/restart", methods=["POST"])
@admin_required
def api_admin_restart():
    logger.info("[Admin] Nhận yêu cầu khởi động lại server từ trang quản trị.")
    restart_server()
    return jsonify({"ok": True, "message": "Hệ thống đang khởi động lại. Vui lòng đợi trong giây lát..."})


@app.route("/api/admin/reconcile", methods=["POST"])
@admin_required
def api_admin_reconcile():
    return jsonify({"ok": True, "message": "Đã hoàn thành kiểm tra toàn bộ video."})


@app.route("/qr/table/<table_id>.png")
@admin_required
def table_qr(table_id):
    if qrcode is None:
        return "Thiếu thư viện qrcode. Hãy cài dependencies trước khi build.", 503
    table = next(
        (item for item in CONFIG.get("tables", []) if str(item.get("id")) == str(table_id)),
        None,
    )
    if not table or not table.get("camera_id"):
        return "Bàn không tồn tại hoặc chưa gán camera", 404
    public_base = str(CONFIG.get("public_base_url") or request.url_root).rstrip("/")
    target_url = f"{public_base}/replay/cam{int(table['camera_id'])}"
    image = qrcode.make(target_url, border=2)
    output = BytesIO()
    image.save(output, format="PNG")
    return Response(output.getvalue(), mimetype="image/png")


@app.route("/cut")
def cut_video():
    filename = request.args.get("filename")
    start = request.args.get("start", type=float)
    duration = request.args.get("duration", type=float)
    if not filename or start is None or duration is None:
        return jsonify({"error": "Thiếu tham số"}), 400
    if not safe_video_filename(filename):
        return jsonify({"error": "Tên file không hợp lệ"}), 400
    input_path = os.path.join(VIDEO_DIR, filename)
    if not os.path.isfile(input_path):
        return jsonify({"error": "File không tồn tại"}), 404
    output_filename = f"cut_{uuid.uuid4().hex[:8]}.mp4"
    output_path = os.path.join(VIDEO_DIR, output_filename)
    safe_start = max(0.0, start)
    safe_duration = max(0.001, duration)
    cmd = [
        FFMPEG_PATH,
        "-y",
        "-ss",
        f"{safe_start:.3f}",
        "-i",
        input_path,
        "-t",
        f"{safe_duration:.3f}",
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        output_path,
    ]
    try:
        completed = subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if completed.returncode != 0 or not os.path.isfile(output_path) or os.path.getsize(output_path) <= 0:
            cmd_an = [
                FFMPEG_PATH,
                "-y",
                "-ss",
                f"{safe_start:.3f}",
                "-i",
                input_path,
                "-t",
                f"{safe_duration:.3f}",
                "-map",
                "0:v:0",
                "-c:v",
                "copy",
                "-an",
                "-movflags",
                "+faststart",
                output_path,
            ]
            completed = subprocess.run(
                cmd_an,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        if completed.returncode != 0 or not os.path.isfile(output_path) or os.path.getsize(output_path) <= 0:
            if os.path.isfile(output_path):
                try:
                    os.remove(output_path)
                except OSError:
                    pass
            return jsonify({"error": "Lỗi khi cắt"}), 500
    except Exception:
        if os.path.isfile(output_path):
            try:
                os.remove(output_path)
            except OSError:
                pass
        return jsonify({"error": "Lỗi khi cắt"}), 500
    return jsonify({"output": output_filename})


@app.route("/cut_progress")
def cut_progress():
    filename = request.args.get("filename")
    start = request.args.get("start", type=float)
    duration = request.args.get("duration", type=float)
    if not filename or start is None or duration is None:
        return "Thiếu tham số", 400
    if not safe_video_filename(filename):
        return "Tên file không hợp lệ", 400

    input_path = os.path.join(VIDEO_DIR, filename)
    if not os.path.isfile(input_path):
        return "File không tồn tại", 404

    output_filename = f"cut_{uuid.uuid4().hex[:8]}.mp4"
    output_path = os.path.join(VIDEO_DIR, output_filename)
    safe_start = max(0.0, start)
    safe_duration = max(0.001, duration)
    cmd = [
        FFMPEG_PATH,
        "-y",
        "-ss",
        f"{safe_start:.3f}",
        "-i",
        input_path,
        "-t",
        f"{safe_duration:.3f}",
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        "-progress",
        "pipe:1",
        "-nostats",
        output_path,
    ]

    def generate():
        process = None
        done_successfully = False
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            yield "data: 0\n\n"
            try:
                for line in process.stdout:
                    if not (line.startswith("out_time_ms=") or line.startswith("out_time_us=")):
                        continue
                    try:
                        val = int(line.strip().split("=", 1)[1])
                        pct = max(0, min(100, int(val / 1_000_000 / safe_duration * 100)))
                        yield f"data: {pct}\n\n"
                    except Exception:
                        pass
            finally:
                if process.stdout:
                    try:
                        process.stdout.close()
                    except Exception:
                        pass
            process.wait()
            if process.returncode == 0 and os.path.isfile(output_path) and os.path.getsize(output_path) > 0:
                done_successfully = True
                yield "data: 100\n\n"
            else:
                cmd_an = [
                    FFMPEG_PATH,
                    "-y",
                    "-ss",
                    f"{safe_start:.3f}",
                    "-i",
                    input_path,
                    "-t",
                    f"{safe_duration:.3f}",
                    "-map",
                    "0:v:0",
                    "-c:v",
                    "copy",
                    "-an",
                    "-movflags",
                    "+faststart",
                    "-progress",
                    "pipe:1",
                    "-nostats",
                    output_path,
                ]
                process = subprocess.Popen(
                    cmd_an,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                try:
                    for line in process.stdout:
                        if not (line.startswith("out_time_ms=") or line.startswith("out_time_us=")):
                            continue
                        try:
                            val = int(line.strip().split("=", 1)[1])
                            pct = max(0, min(100, int(val / 1_000_000 / safe_duration * 100)))
                            yield f"data: {pct}\n\n"
                        except Exception:
                            pass
                finally:
                    if process.stdout:
                        try:
                            process.stdout.close()
                        except Exception:
                            pass
                process.wait()
                if process.returncode == 0 and os.path.isfile(output_path) and os.path.getsize(output_path) > 0:
                    done_successfully = True
                    yield "data: 100\n\n"
                else:
                    yield "data: error:Lỗi khi cắt\n\n"
        except Exception:
            yield "data: error:Lỗi khi cắt\n\n"
        finally:
            if process and process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
            if not done_successfully and os.path.isfile(output_path):
                try:
                    os.remove(output_path)
                except OSError:
                    pass

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


def parse_video_filename(filename):
    metadata = parse_video_metadata(filename, known_duration=DURATION)
    if metadata is None:
        return None, None
    # Giữ tuple cũ (thời điểm bắt đầu, tên tệp) cho luồng ghép video.
    return metadata["start"], filename


@app.route("/api/timeline")
def timeline_api():
    return timeline_api_from_db()


def _timeline_segment_from_row(row):
    filename = row["filename"]
    metadata = parse_video_metadata(
        filename,
        known_duration=row["duration_sec"] if row["duration_sec"] else None,
    )
    if metadata is None:
        if not safe_video_filename(filename):
            return None
        metadata = {
            "cam_id": _valid_camera_id(row["cam_id"]),
            "start": None,
            "duration_sec": None,
        }
    try:
        started_at = (
            _parse_local_datetime_value(row["started_at"])
            if row["started_at"]
            else metadata["start"]
        )
    except ValueError:
        started_at = metadata["start"]
    if started_at is None:
        return None

    duration = None
    raw_duration = row["duration_sec"]
    if raw_duration not in (None, ""):
        try:
            numeric_duration = float(raw_duration)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(numeric_duration) or numeric_duration < 0:
            return None
        if numeric_duration > 0:
            duration = _normalise_duration(numeric_duration)
            if duration is None:
                return None
    if duration is None and row["ended_at"]:
        try:
            ended_at = _parse_local_datetime_value(row["ended_at"])
            duration = (ended_at - started_at).total_seconds()
        except ValueError:
            duration = None
    if duration is None:
        duration = metadata["duration_sec"]
    duration = _normalise_duration(duration)
    if duration is None:
        return None

    cam_id = _valid_camera_id(row["cam_id"]) or metadata["cam_id"]
    if cam_id is None:
        return None
    status = str(row["status"] or "complete").strip().lower()
    if status not in TIMELINE_STATUS_VALUES:
        status = "error"
    return {
        "filename": filename,
        "cam_id": cam_id,
        "started_at": started_at.isoformat(timespec="seconds"),
        "end_at": (started_at + timedelta(seconds=duration)).isoformat(timespec="seconds"),
        "duration_sec": round(duration, 3),
        "status": status,
        "locked": bool(row["locked"]),
        "note": str(row["note"] or "")[:500],
    }


def _timeline_event_from_row(row):
    try:
        event_time = _parse_local_datetime_value(row["event_time"])
    except ValueError:
        return None
    label = str(row["label"] or "").strip()
    if not label:
        return None
    event = {
        "event_time": event_time.isoformat(timespec="seconds"),
        "label": label[:160],
        "note": str(row["note"] or "")[:500],
    }
    if safe_video_filename(row["filename"]):
        event["filename"] = row["filename"]
    cam_id = _valid_camera_id(row["cam_id"])
    if cam_id is not None:
        event["cam_id"] = cam_id
    return event


def index_untracked_video_files():
    """Index existing MP4s from names/stat only; never ffprobe a directory."""
    if not os.path.isdir(VIDEO_DIR):
        return
    try:
        with db_connection() as connection:
            _ensure_db_schema(connection)
            known = {
                row["filename"]
                for row in connection.execute("SELECT filename FROM video_segments")
            }
    except sqlite3.Error:
        return
    try:
        entries = list(os.scandir(VIDEO_DIR))
    except OSError:
        return
    for entry in entries:
        filename = entry.name
        if filename in known or not filename.lower().endswith(".mp4"):
            continue
        if not safe_video_filename(filename):
            continue
        try:
            if entry.is_file(follow_symlinks=False):
                upsert_video_index(entry.path, validate=False, known_duration=DURATION)
        except OSError:
            continue


def get_timeline_data(window_start, window_end):
    camera_modes = {
        cam_id: _timeline_playback_mode(cam_id)
        for cam_id in range(1, len(CAMERA_LIST) + 1)
    }
    local_camera_ids = {
        cam_id for cam_id, mode in camera_modes.items()
        if mode == "local"
    }
    nvr_camera_ids = {
        cam_id for cam_id, mode in camera_modes.items()
        if mode == "nvr"
    }

    local_segments = []
    events = []
    init_db()
    if local_camera_ids:
        index_untracked_video_files()

    with db_connection() as connection:
        segment_rows = connection.execute(
            "SELECT filename, cam_id, started_at, ended_at, duration_sec, status, locked, note "
            "FROM video_segments ORDER BY started_at ASC, cam_id ASC, id ASC"
        ).fetchall() if local_camera_ids else []
        event_rows = connection.execute(
            "SELECT filename, cam_id, event_time, label, note "
            "FROM video_events ORDER BY event_time ASC, id ASC"
        ).fetchall()

    for row in segment_rows:
        segment = _timeline_segment_from_row(row)
        if segment is None or int(segment.get("cam_id", 0)) not in local_camera_ids:
            continue
        if segment["status"] == "complete":
            media_path = safe_video_path(segment["filename"])
            if not media_path or not os.path.isfile(media_path):
                continue
        clip_start = _parse_local_datetime_value(segment["started_at"])
        clip_end = _parse_local_datetime_value(segment["end_at"])
        if clip_start < window_end and clip_end > window_start:
            segment["source"] = "local"
            segment["play_url"] = f"/video/{quote(segment['filename'])}"
            segment["download_url"] = f"/download/{quote(segment['filename'])}"
            local_segments.append(segment)

    for row in event_rows:
        event = _timeline_event_from_row(row)
        if event is None:
            continue
        event_time = _parse_local_datetime_value(event["event_time"])
        if window_start <= event_time <= window_end:
            events.append(event)

    nvr_segments, warnings = [], []
    if nvr_camera_ids:
        try:
            nvr_segments, warnings = search_nvr_timeline(
                window_start, window_end, sorted(nvr_camera_ids)
            )
        except (RuntimeError, requests.RequestException) as exc:
            if camera_modes and all(mode == "nvr" for mode in camera_modes.values()):
                raise
            logger.warning("[NVR] Kênh NVR không đọc được dữ liệu; không chuyển sang Local: %s", exc)
            warnings = [f"NVR: {exc}"]

    segments = sorted(
        local_segments + nvr_segments,
        key=lambda item: (item["started_at"], item["cam_id"]),
    )
    distinct_modes = sorted(set(camera_modes.values()))
    source_mode = distinct_modes[0] if len(distinct_modes) == 1 else "mixed"
    return {
        "segments": segments,
        "events": events,
        "source_mode": source_mode,
        "camera_source_modes": {str(key): value for key, value in camera_modes.items()},
        "warnings": warnings,
    }


def _read_timeline_segments(window_start, window_end):
    return get_timeline_data(window_start, window_end)["segments"]


def timeline_api_from_db():
    try:
        window_start, window_end = parse_timeline_range(
            request.args.get("start"), request.args.get("end")
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    try:
        payload = get_timeline_data(window_start, window_end)
    except (RuntimeError, requests.RequestException) as exc:
        logger.warning("NVR timeline error: %s", exc)
        return jsonify({"ok": False, "error": str(exc)}), 502
    except (OSError, sqlite3.Error):
        logger.exception("Không thể đọc dữ liệu timeline")
        return jsonify({"ok": False, "error": "Không thể đọc dữ liệu timeline."}), 500
    return jsonify(
        {
            "ok": True,
            "start": window_start.isoformat(timespec="seconds"),
            "end": window_end.isoformat(timespec="seconds"),
            **payload,
        }
    )


def _prepare_nvr_merge_parts(cam_id, req_start, req_end):
    nvr = get_camera_recorder_config(cam_id)
    now = datetime.now()
    if req_end > now:
        req_end = now
    if req_start >= req_end:
        req_start = max(req_start - timedelta(minutes=1), req_end - timedelta(minutes=1))
    vendor = str(nvr.get("vendor") or "hikvision").strip().lower()
    searcher = _search_dahua_camera if vendor == "dahua" else _search_hikvision_camera
    segments = searcher(cam_id, req_start, req_end, nvr)
    segments.sort(key=lambda item: item.get("started_at", ""))
    parts = []
    cursor = req_start
    chunk_limit = max(30, min(3600, int(nvr.get("playback_chunk_sec", 300) or 300)))
    for segment in segments:
        try:
            seg_start = datetime.fromisoformat(str(segment.get("started_at") or ""))
            seg_end = datetime.fromisoformat(str(segment.get("end_at") or segment.get("ended_at") or ""))
        except (TypeError, ValueError):
            continue
        part_start = max(req_start, seg_start, cursor)
        part_end = min(req_end, seg_end)
        if part_end <= part_start:
            continue
        play_url = str(segment.get("play_url") or "")
        token = play_url.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0]
        reference = _get_nvr_reference(token) if token else None
        if not reference:
            continue
        if vendor == "dahua":
            chunk_start = part_start
            while chunk_start < part_end:
                chunk_end = min(part_end, chunk_start + timedelta(seconds=chunk_limit))
                parts.append({
                    "vendor": vendor,
                    "token": token,
                    "reference": reference,
                    "segment_start": seg_start,
                    "start": chunk_start,
                    "end": chunk_end,
                })
                chunk_start = chunk_end
        else:
            parts.append({
                "vendor": vendor,
                "token": token,
                "reference": reference,
                "segment_start": seg_start,
                "start": part_start,
                "end": part_end,
            })
        cursor = part_end
        if cursor >= req_end:
            break
    covered = sum((part["end"] - part["start"]).total_seconds() for part in parts)
    missing = max(0.0, (req_end - req_start).total_seconds() - covered)
    return nvr, parts, missing


def _materialize_nvr_merge_part(part, nvr, target_path):
    vendor = part["vendor"]
    if vendor == "dahua":
        source_path = _download_dahua_reference(
            part["token"],
            part["reference"],
            nvr,
            at_value=part["start"].isoformat(timespec="seconds"),
            end_value=part["end"].isoformat(timespec="seconds"),
        )
        source_offset = 0.0
    elif vendor == "hikvision":
        source_path = _download_hikvision_reference(part["token"], part["reference"], nvr)
        source_offset = max(0.0, (part["start"] - part["segment_start"]).total_seconds())
    else:
        raise RuntimeError("Nguồn NVR không được hỗ trợ khi cắt video.")
    duration = max(0.001, (part["end"] - part["start"]).total_seconds())
    cmd = [
        FFMPEG_PATH,
        "-y",
        "-ss",
        f"{source_offset:.3f}",
        "-i",
        source_path,
        "-t",
        f"{duration:.3f}",
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        target_path,
    ]
    completed = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=max(180, int(nvr.get("read_timeout_sec", 30) or 30) * 20),
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if completed.returncode != 0 or not os.path.isfile(target_path) or os.path.getsize(target_path) <= 0:
        cmd_an = [
            FFMPEG_PATH,
            "-y",
            "-ss",
            f"{source_offset:.3f}",
            "-i",
            source_path,
            "-t",
            f"{duration:.3f}",
            "-map",
            "0:v:0",
            "-c:v",
            "copy",
            "-an",
            "-movflags",
            "+faststart",
            target_path,
        ]
        completed = subprocess.run(
            cmd_an,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(180, int(nvr.get("read_timeout_sec", 30) or 30) * 20),
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    if completed.returncode != 0 or not os.path.isfile(target_path) or os.path.getsize(target_path) <= 0:
        if os.path.isfile(target_path):
            try:
                os.remove(target_path)
            except OSError:
                pass
        raise RuntimeError((completed.stdout or "Cắt video NVR thất bại.")[-2000:])
    return duration


def _merge_nvr_response(cam_id, req_start, req_end):
    try:
        nvr, parts, missing_duration = _prepare_nvr_merge_parts(cam_id, req_start, req_end)
    except Exception as exc:
        logger.exception("Không thể chuẩn bị dữ liệu cắt NVR camera %s", cam_id)
        return Response(f"data: error:{str(exc)}\n\n", mimetype="text/event-stream")
    if not parts:
        return Response("data: error:Không có video NVR trong khoảng đã chọn\n\n", mimetype="text/event-stream")
    output_filename = f"merge_cam{cam_id}_{uuid.uuid4().hex[:8]}.mp4"
    output_path = os.path.join(VIDEO_DIR, output_filename)

    def generate():
        work_dir = tempfile.mkdtemp(prefix=".cambida_nvr_merge_", dir=BASE_DIR)
        part_paths = []
        done_successfully = False
        try:
            yield "data: 5\n\n"
            if missing_duration >= 1.0:
                yield (
                    "data: notice:Khoảng đã chọn có "
                    f"{int(round(missing_duration))} giây không có bản ghi NVR; "
                    "file kết quả chỉ gồm phần có sẵn.\n\n"
                )
            total = len(parts)
            for index, part in enumerate(parts):
                target_path = os.path.join(work_dir, f"part_{index:03d}.mp4")
                part_paths.append(target_path)
                _materialize_nvr_merge_part(part, nvr, target_path)
                percent = 5 + int(((index + 1) / max(total, 1)) * 85)
                yield f"data: {min(percent, 90)}\n\n"
            if len(part_paths) == 1:
                shutil.copyfile(part_paths[0], output_path)
            else:
                list_file_path = os.path.join(work_dir, "concat.txt")
                with open(list_file_path, "w", encoding="utf-8") as list_file:
                    for part_path in part_paths:
                        escaped_path = (
                            os.path.abspath(part_path)
                            .replace(chr(92), "/")
                            .replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))
                        )
                        list_file.write(f"file '{escaped_path}'\n")
                yield "data: 95\n\n"
                concat_cmd = [
                    FFMPEG_PATH,
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    list_file_path,
                    "-c",
                    "copy",
                    "-movflags",
                    "+faststart",
                    output_path,
                ]
                completed = subprocess.run(
                    concat_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                if completed.returncode != 0 or not os.path.isfile(output_path) or os.path.getsize(output_path) <= 0:
                    concat_cmd_an = [
                        FFMPEG_PATH,
                        "-y",
                        "-f",
                        "concat",
                        "-safe",
                        "0",
                        "-i",
                        list_file_path,
                        "-c:v",
                        "copy",
                        "-an",
                        "-movflags",
                        "+faststart",
                        output_path,
                    ]
                    completed = subprocess.run(
                        concat_cmd_an,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                if completed.returncode != 0 or not os.path.isfile(output_path) or os.path.getsize(output_path) <= 0:
                    raise RuntimeError((completed.stdout or "Ghép các đoạn NVR thất bại.")[-2000:])
            done_successfully = True
            yield "data: 100\n\n"
            yield f"data: done:{output_filename}\n\n"
        except Exception as exc:
            logger.exception("Không thể cắt/ghép video NVR camera %s", cam_id)
            yield f"data: error:{str(exc)}\n\n"
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)
            if not done_successfully and os.path.isfile(output_path):
                try:
                    os.remove(output_path)
                except OSError:
                    pass

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


@app.route("/merge")
def merge_video():
    cam_id = request.args.get("cam_id", type=int)
    start_str = request.args.get("start")
    end_str = request.args.get("end")
    if not cam_id or not start_str or not end_str:
        return Response(
            "data: error:Thiếu thông tin\n\n", mimetype="text/event-stream"
        )
    try:
        req_start = datetime.fromisoformat(start_str)
        req_end = datetime.fromisoformat(end_str)
        requested_duration = (req_end - req_start).total_seconds()
        if requested_duration <= 0:
            return Response(
                "data: error:Khoảng cắt không hợp lệ\n\n",
                mimetype="text/event-stream",
            )
        if requested_duration > int(MAX_MERGE_MINUTES) * 60:
            return Response(
                f"data: error:Quá {MAX_MERGE_MINUTES} phút\n\n",
                mimetype="text/event-stream",
            )

        cam = CAMERA_LIST[cam_id - 1] if (1 <= cam_id <= len(CAMERA_LIST)) else {}
        has_nvr = camera_has_nvr(cam)
        req_source = request.args.get("source", "").strip().lower()

        if has_nvr and (req_source in {"nvr", "server2"} or not should_record_locally(cam)):
            return _merge_nvr_response(cam_id, req_start, req_end)

        files = glob.glob(os.path.join(VIDEO_DIR, f"cam{cam_id}_*.mp4"))
        candidates = []
        for f_path in files:
            metadata = parse_video_metadata(
                os.path.basename(f_path), known_duration=DURATION
            )
            if not metadata:
                continue
            f_start, f_end = metadata["start"], metadata["end"]
            if f_start < req_end and f_end > req_start:
                candidates.append((f_start, f_end, f_path))
        candidates.sort(key=lambda item: item[0])
        if not candidates:
            if has_nvr:
                logger.info("[Merge Cam %s] Không có video Local, tự động lấy từ NVR dự phòng.", cam_id)
                return _merge_nvr_response(cam_id, req_start, req_end)
            return Response(
                "data: error:Không có video\n\n", mimetype="text/event-stream"
            )

        pieces = []
        cursor = req_start
        for f_start, f_end, f_path in candidates:
            piece_start = max(req_start, f_start, cursor)
            piece_end = min(req_end, f_end)
            if piece_end <= piece_start:
                continue
            pieces.append(
                {
                    "path": f_path,
                    "offset": (piece_start - f_start).total_seconds(),
                    "duration": (piece_end - piece_start).total_seconds(),
                }
            )
            cursor = piece_end
            if cursor >= req_end:
                break
        if not pieces:
            return Response(
                "data: error:Không có video trong khoảng đã chọn\n\n",
                mimetype="text/event-stream",
            )

        selected_media_duration = sum(piece["duration"] for piece in pieces)
        missing_duration = max(0.0, requested_duration - selected_media_duration)
        output_filename = f"merge_cam{cam_id}_{uuid.uuid4().hex[:8]}.mp4"
        output_path = os.path.join(VIDEO_DIR, output_filename)

        def generate_merge_process():
            work_dir = tempfile.mkdtemp(prefix=".cambida_merge_", dir=BASE_DIR)
            part_paths = []
            completed_duration = 0.0
            done_successfully = False
            current_proc = None
            try:
                yield "data: 5\n\n"
                if missing_duration >= 1.0:
                    yield (
                        "data: notice:Khoảng đã chọn có "
                        f"{int(round(missing_duration))} giây không có video; "
                        "file kết quả chỉ gồm phần có sẵn.\n\n"
                    )
                total_pieces = len(pieces)
                for index, piece in enumerate(pieces):
                    part_path = os.path.join(work_dir, f"part_{index:03d}.mp4")
                    part_paths.append(part_path)
                    piece_dur = max(0.001, piece["duration"])
                    cmd = [
                        FFMPEG_PATH,
                        "-y",
                        "-ss",
                        f"{max(0.0, piece['offset']):.3f}",
                        "-i",
                        piece["path"],
                        "-t",
                        f"{piece_dur:.3f}",
                        "-map",
                        "0:v:0",
                        "-map",
                        "0:a?",
                        "-c",
                        "copy",
                        "-movflags",
                        "+faststart",
                        "-progress",
                        "pipe:1",
                        "-nostats",
                        part_path,
                    ]
                    process = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    current_proc = process
                    try:
                        for line in process.stdout:
                            if not (line.startswith("out_time_ms=") or line.startswith("out_time_us=")):
                                continue
                            try:
                                val = int(line.strip().split("=", 1)[1])
                                current_sec = min(
                                    piece_dur,
                                    val / 1_000_000,
                                )
                                ratio = (completed_duration + current_sec) / max(
                                    selected_media_duration, 0.001
                                )
                                percent = 5 + int(max(0.0, min(1.0, ratio)) * 85)
                                yield f"data: {min(percent, 90)}\n\n"
                            except (TypeError, ValueError, ZeroDivisionError):
                                pass
                    finally:
                        if process.stdout:
                            try:
                                process.stdout.close()
                            except Exception:
                                pass
                    process.wait()
                    current_proc = None

                    if process.returncode != 0 or not os.path.isfile(part_path) or os.path.getsize(part_path) <= 0:
                        cmd_an = [
                            FFMPEG_PATH,
                            "-y",
                            "-ss",
                            f"{max(0.0, piece['offset']):.3f}",
                            "-i",
                            piece["path"],
                            "-t",
                            f"{piece_dur:.3f}",
                            "-map",
                            "0:v:0",
                            "-c:v",
                            "copy",
                            "-an",
                            "-movflags",
                            "+faststart",
                            "-progress",
                            "pipe:1",
                            "-nostats",
                            part_path,
                        ]
                        process = subprocess.Popen(
                            cmd_an,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                            creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                        current_proc = process
                        try:
                            for line in process.stdout:
                                if not (line.startswith("out_time_ms=") or line.startswith("out_time_us=")):
                                    continue
                                try:
                                    val = int(line.strip().split("=", 1)[1])
                                    current_sec = min(
                                        piece_dur,
                                        val / 1_000_000,
                                    )
                                    ratio = (completed_duration + current_sec) / max(
                                        selected_media_duration, 0.001
                                    )
                                    percent = 5 + int(max(0.0, min(1.0, ratio)) * 85)
                                    yield f"data: {min(percent, 90)}\n\n"
                                except (TypeError, ValueError, ZeroDivisionError):
                                    pass
                        finally:
                            if process.stdout:
                                try:
                                    process.stdout.close()
                                except Exception:
                                    pass
                        process.wait()
                        current_proc = None

                    if process.returncode != 0 or not os.path.isfile(part_path) or os.path.getsize(part_path) <= 0:
                        yield "data: error:Cắt video thất bại\n\n"
                        return
                    completed_duration += piece["duration"]
                    step_ratio = (index + 1) / max(total_pieces, 1)
                    step_percent = 5 + int(max(0.0, min(1.0, step_ratio)) * 85)
                    yield f"data: {min(step_percent, 90)}\n\n"

                if len(part_paths) == 1:
                    shutil.copyfile(part_paths[0], output_path)
                else:
                    list_file_path = os.path.join(work_dir, "concat.txt")
                    with open(list_file_path, "w", encoding="utf-8") as list_file:
                        for part_path in part_paths:
                            escaped_path = (
                                os.path.abspath(part_path)
                                .replace(chr(92), "/")
                                .replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))
                            )
                            list_file.write(f"file '{escaped_path}'\n")
                    yield "data: 95\n\n"
                    concat_cmd = [
                        FFMPEG_PATH,
                        "-y",
                        "-f",
                        "concat",
                        "-safe",
                        "0",
                        "-i",
                        list_file_path,
                        "-c",
                        "copy",
                        "-movflags",
                        "+faststart",
                        output_path,
                    ]
                    result = subprocess.run(
                        concat_cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    if result.returncode != 0 or not os.path.isfile(output_path) or os.path.getsize(output_path) <= 0:
                        concat_cmd_an = [
                            FFMPEG_PATH,
                            "-y",
                            "-f",
                            "concat",
                            "-safe",
                            "0",
                            "-i",
                            list_file_path,
                            "-c:v",
                            "copy",
                            "-an",
                            "-movflags",
                            "+faststart",
                            output_path,
                        ]
                        result = subprocess.run(
                            concat_cmd_an,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                            creationflags=subprocess.CREATE_NO_WINDOW,
                        )
                    if result.returncode != 0 or not os.path.isfile(output_path) or os.path.getsize(output_path) <= 0:
                        logger.error("Ghép các đoạn cắt thất bại: %s", result.stdout[-2000:])
                        yield "data: error:Ghép các đoạn cắt thất bại\n\n"
                        return
                done_successfully = True
                yield "data: 100\n\n"
                yield f"data: done:{output_filename}\n\n"
            except Exception:
                logger.exception("Không thể cắt/ghép video")
                yield "data: error:Lỗi xử lý video\n\n"
            finally:
                if current_proc and current_proc.poll() is None:
                    try:
                        current_proc.kill()
                    except OSError:
                        pass
                shutil.rmtree(work_dir, ignore_errors=True)
                if not done_successfully and os.path.isfile(output_path):
                    try:
                        os.remove(output_path)
                    except OSError:
                        pass

        return Response(
            stream_with_context(generate_merge_process()),
            mimetype="text/event-stream",
        )
    except (TypeError, ValueError, OverflowError):
        return Response(
            "data: error:Thời gian không hợp lệ\n\n", mimetype="text/event-stream"
        )
    except Exception as exc:
        logger.exception("Lỗi chuẩn bị cắt/ghép video")
        return Response(
            f"data: error:{str(exc)}\n\n", mimetype="text/event-stream"
        )

def set_autostart(enable=True):
    if not winreg:
        return
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        )
        if enable:
            target_exe = sys.executable if getattr(sys, "frozen", False) else os.path.abspath(sys.argv[0])
            winreg.SetValueEx(
                key,
                "CCTV_System",
                0,
                winreg.REG_SZ,
                f'"{target_exe}"',
            )
        else:
            try:
                winreg.DeleteValue(key, "CCTV_System")
            except Exception:
                pass
        winreg.CloseKey(key)
    except Exception as e:
        logger.error(f"Lỗi setup autostart: {e}")


def create_tray_icon():
    image = Image.new("RGB", (64, 64), color=(44, 11, 11))
    ImageDraw.Draw(image).ellipse((16, 16, 48, 48), fill=(255, 0, 0))
    return image


def on_quit(icon, item):
    icon.stop()
    os._exit(0)


def setup_tray():
    port = int(CONFIG.get("server_port", 8000))
    pystray.Icon(
        "CCTV",
        create_tray_icon(),
        "Hệ thống CCTV",
        pystray.Menu(
            pystray.MenuItem("Mở Cambida", lambda icon, item: _open_server_page(port), default=True),
            pystray.MenuItem("Thoát", on_quit),
        ),
    ).run()


if __name__ == "__main__":
    if not acquire_single_instance():
        sys.exit()
    init_db()
    license_active = initialize_license_enforcement()
    threading.Thread(
        target=license_watch_loop, daemon=True, name="LicenseWatcher"
    ).start()
    threading.Thread(
        target=health_check_loop, daemon=True, name="Watchdog"
    ).start()
    threading.Thread(
        target=monitor_telegram_commands, daemon=True, name="BotListener"
    ).start()
    threading.Thread(
        target=start_cleanup_loop, daemon=True, name="Cleanup"
    ).start()
    threading.Thread(
        target=daily_report_task, daemon=True, name="DailyReport"
    ).start()
    threading.Thread(
        target=start_recording_loop, daemon=True, name="Recorder"
    ).start()
    check_and_notify_pending_update()
    start_github_update_worker()
    if license_active:
        send_telegram_alert(
            f"🚀 Hệ thống CCTV (v{APP_VERSION}) đã khởi động thành công!\n"
            "🔐 Bản quyền xem lại: hợp lệ theo tin nhắn ghim."
        )
    else:
        state = get_license_snapshot()
        send_telegram_alert(
            f"🚀 Hệ thống CCTV (v{APP_VERSION}) đã khởi động; camera vẫn tiếp tục ghi.\n"
            "⛔ Xem lại đang khóa do bản quyền chưa hợp lệ.\n"
            f"Key máy: `{state.get('key') or 'N/A'}`\n"
            "Hãy thêm key này vào tin nhắn ghim Telegram."
        )

    run_startup = CONFIG.get("run_on_startup", "no").lower() == "yes"
    run_tray = CONFIG.get("run_in_tray", "no").lower() == "yes"
    set_autostart(run_startup)
    server_port = int(CONFIG.get("server_port", 8000))
    logger.info(f"🌐 Máy chủ web đang chạy tại http://0.0.0.0:{server_port}")

    def run_server():
        for attempt in range(20):
            try:
                from waitress import serve

                serve(app, host="0.0.0.0", port=server_port, threads=16)
                break
            except OSError as e:
                if attempt < 19 and ("10048" in str(e) or getattr(e, "winerror", None) == 10048):
                    logger.warning(f"Cổng {server_port} đang bận, thử lại sau 0.5s (lần {attempt + 1}/20)...")
                    time.sleep(0.5)
                    continue
                logger.error(f"Lỗi khởi động WebServer: {e}")
                break
            except ImportError:
                app.run(host="0.0.0.0", port=server_port, debug=False)
                break

    server_thread = threading.Thread(
        target=run_server,
        daemon=run_tray,
        name="WebServer",
    )
    server_thread.start()
    if run_tray:
        try:
            setup_tray()
        except Exception as e:
            logger.error(f"Lỗi khởi động System Tray: {e}")
            server_thread.join()
    else:
        server_thread.join()
