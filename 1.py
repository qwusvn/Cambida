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
_EMBEDDED_REPLAY_TEMPLATE_SHA256 = "3bc494441169fac6c93416d72f5236ea65b41447fa2d07427866949e7d25a2f8"
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
    "nB~OEY?2~8>O{kgLERP$KxRmy0P-UNvc3KmUZ_jM%y@|58q!@a;rEaQ7I51pGq7hH7XM`KpQmly8hz?bW-R5S;ST;<5?90YmG835h$MhPKUoq?po)BFp#;$5"
    "<L^G2K=k^16i}cO*3bUv(*KAqeS-jzg9d7&9P(%P{3x6G0Z}+)yny=Gl8fGpQiPAs0h=EH_d4*Xond9GqPuR>miIg#{x#h89~~R7@BRB|uKdIm0_b(WkSG76"
    "J4sjZ;uG&X(y-N?_}MW|f?yZ%_g&k?ryP7*XS$E?V+4=<Tlev4X#IFaCtqPoZV9u3?oRBHR`$>+EySppi%;sp-9#$=qff7h&6sHze{|^m&K!DH`2W$H_eXEu"
    "je3R>Ec-ok<(UKfo%!+1f&9*#c;-O<=)o)G!OP<O76JC;j}E<M4m|@(fAr~nG@l-Ubf#NRl=&df9_i=W&OOq`wfuWr8NH^94=Nw|@vFx{KN+~X=}o&Ctyn`("
    "86C@RNzl1vO{p6tw5C*Ui{%YGWVb0DV-hE=32_A%y_%_LV-FD9Gn)#SLc64)ryuN4CCKtIeX69JkK$JCa=X~Ke>u5&`^}hhneV{Eie8=#aYovDYl+p<qo-7{"
    ">)F~h5gNH+P}u)&&ZA^oGwL<nWY=)E&bEfoS3;7RVd4Z9=Kl+w)6gHIvln<jEZEz>oh!pWpAI#D@GCJG)(pcb+j7dXC&?!-RDhQMTZH2)P$NN`9K3(qEeu8="
    "Hh<_qB$84CnLmEw2k54a##yCVt#A8Hr^&AmtJL9VrBU74cnmPD*N2r2`0h0CsuIpN=&|tZt48ApS~%0|J6PXQ^*4rCy+W(Q>n$krRmMsU(H`_`6r~HU(ET~T"
    "Hbg8_t=3}ya+fJ9-~&A}|2xKfz{FRG_|U4d>ltL?{KRBV5(QdaocvY?AyC;H`;1xHweq+DltYyNou(^_({ox)p{j8&=^I>&;rsu=9HrC!Y-_I!R6#LAMyWVj"
    "wneKOfpvKkQ7al;c!L(Gkh?J~qD5*tG6zS-xg*k-=9Ku~J#5B)nutJ)dm2RG2p+^yJK1ugNEeh$FDKPi*##SiSy52@wTVIxuipLb`D(#EEQNkfEDxq#lfgWC"
    "$PPiUBvqVUI#W66bz|-ZG3IXEm@#+r`u^w<A^~u$=HvRVL*3EZvJDzQPu~BU+GbF?_|<sebl?9sWMNQ3m7WSG6!Uj<MR8<cl5zv*n-jv{dH;7~r^iZ%j7%Bl"
    "&BQeaJnf>q?V#KpOmIBEc9;+8&&Mzz6zxN2%{jtWXZ+-B#z8bAx1L6elJFfY#d;ULmMqn$miQiFJc})XKIzCE+Yvi?y;{cTQ*nf%+UM`z&YT|y9p`x9M}c$q"
    "$s?!yY5+X){XaPmPfng3JL||p{~zWd{Iv!geJ_TS_}D)SSTi&lqcClESYjOXsPK$Q44u<t`1g(CChlR_7H2zt!)eINW>J)E&<7O|DVaxiIvJkxur5qk6r(5G"
    "*}`qraCRL1fHBJSc*3~B4;Ztb4R*XWpZL=C;MDgq+e0jeL<rK))!TP@r#WqS!l^cZH6VMDZL4Je$Q^rk+>0h*&%_u%;S9a|lh<R!bql(vH21K|QP6Lfz9)2Q"
    "<0hm`{HX5|m0|by0Y4gb;F)Jlg%9-#e0#V_e<OZ0t@wd=ts#D3ED(OTq6i*&rZ}>ZjUxLex+rbI1U~}_eujZltYRiDKw5{fS*x(+Bp$2Skz!!ue+}$GddPcM"
    "Z~rr}1kPXpA;TvFWZEV*Bs+uMcaRA6^s0lb_yIHfe-8S6w^1rfBBm_SNk}u_un;PqMuA66InjA4a0-xaXMkx9?8w9w$;0>GbO(Y<zyH<5Ie~?NNi!~c_4faV"
    "MJe+Yaa$?nGv7sJTvd?me$oq;tXUUCQIdPVwEwSvKX+y!2oxDPTJYJutG9nM0$ml*U(UFcCZ{DeHD@jIqiD|&p>;+b=BP@_a+SS<5w)jKLo|lB5&x~vZsy=>"
    "ZVqs(D`!EmDf}72ugnF^69Cg}b39`Jd&^#uAYPI_FGnP{XXP!W8onprq}6O|OFYA_H2%t8jDN5Kf_V@NFmRO4x!x(yg=ZC}(wSs4&r=XO(D)tF0Sz<sD9U=>"
    "9!6PD5e2OgMIeA3)xX;IH=RupxQ3x`XHBiVPB=_eVZ#nrl0wk3E=QnTyn6rJE*ChynYA$PT$Fq9DmdpU3O9(3k%22g+y%`PHtKJ>&iLD3pb12V{PXQiG?*^z"
    "JRkNWv%__@{u$S~diPiG_S@h1%&sLg6W)Iw9*GpWnMV^+`D=0dzi@u^{x1$6IuF0Ndi$#rOIT`R3Z&rsQE*<ewR?uSU@RcauoN!e7B-E8>h6>2xgaetBq{=$"
    "IZCr_KnBm<CfK|EyNXdZ*oWL}yKJxRma<IR+ExQHhxN?1s9S&YVN;hJzaqa;(-`d1-%jJKj?H2d2AjxMA@n8UO2n~`yWstA3S@#Y)*G|0KBh!HYss;ox#r*g"
    "l82CFNQU`a9P_tgB+C208-cK^lMQZwW^8p9jxmJe?ca}`(?K|LPJ&@@8u=r^e@9pE{*&`+@cy5Cvai$0OYf}X!6&vu?!?_FobqJ^9ud!+$Bzyle181k#ofb`"
    "M_)Z~+DNm0fV}7jn37CxJsw8o1ViAuKj{1L@dMb3d+BC=^x!AZMk%#xOQh`K-J=Ij@4h(t;{F3sR^)L7*aSw|hj))(VAe2WTryQ!_JBkukM1X3i1R<B(9zwm"
    "UqG=RK6r8T=<_d69vnmA&*!5~5S1@5yY8D8Of7sd3di$V5MN@}-NnMSws8F5@QcsyV+%)s(N(Bi-l}<)E<Sa(YP5;_4?es5<>QkVhmRjUA>-JnWtA~|3ddz@"
    "8`zPT9wF`f@QbeiO7QOgd~|a1;OUDaY_%b8&*No2fk$Ojo7?+NZ_=IPtt8dcVD?}b;MaTSk9zo;)R_7`tTSd+j%QIgK7}7uc+A|K<%Z{!Y9(G{7|ifKcxIrA"
    "q1LnK9+1g61dUpICP*STqqUv!d^pq}K0+@#ys6YxyUMY8ca~J)UPAJOE%5`qfsbb+Q!k|R{eI-1iifIc*wnKlW-|CJ90u$JA>dibBQK;a;mc^4diRK#*yJ14"
    "D(Zn~9l<u$FyzB0Q7}cz3DEd4ysRZ}#JQwl5Shd52ea<rH~^t?$gA*;EDXocw>ZD(iNn48ET<R~HqxJug;6<v^u_W0;0$(k*%g;~z(y=1QJnOhv40kx`m+g)"
    "pbgc5?*x6AVXx#u%d>ejcGxBrH++ak>){i>cgeLd_?Yz8tFX&vWz|>}Z&AE^ikE_4Uc#VU{2;F}c{?VrXPy4w$nQQH&w}9(oV7Jt3bx`2Uh?>aSuU`Nn(OR4"
    "b&h}wW1?Mu{AC=#WN{G~c-4Hxur*LfN)KJ8Kygb4MNZLb%xs+uc5)=S<zbY^g{p8=$}03a9QP)#t6_W`z+PgA1U!`uJXtRjLQxP0GXMd>O6Jl{@WO_%x7+I8"
    "_po11qpquNhM7g@7wPv^zcT^a;qEQEc;vktxECr1VE#r>_sLkcmngvL7Vvs_{7E@wId*{<yqTrnF#*I)W(5tn7x1SlZGw)3BR*u-<f<?x<0fN5-`+x1hdx~R"
    "uZnAM$i048%%axQb@Ai#aW`3=DF?b-ADk=Mu;p7e{Kh?T%SN5_=aSTA?CC75&|o|h<m>ir8<uEAyL2@LI^RVkpv$abwsYW&gV$yum|u%7wiR2F&OQ=Kq*kBY"
    "f>+uzkOl>v<%q%E<K4mh>g{hZ?&#fr<Vs<+S}lnY;9g3uLc|%tI!&Rn|Jn})e<)j4-lNbza4zETbnFjX$-^ptvH;L33YXlThKPn8RVS}pXEul?ui3(|sD*Or"
    "M+5Y?3ERC=UAaCh1Kk9sS4;FRYPKT7Nk@&J^?^^yTHhSWkc!!m%!<CU`M#hHFj}^Co?+|HJqyG4oC`$C78a=DH^!sbEY?TUrsVgNUf2(VUJK7WR>zaqWp|;&"
    "DNWSN7dVQz^$b6rFI)=<Pa<~ay*EF<diUouR00s9iCZjzpm2^bydc0dXdxEbD@slkVp~ZaU5C{O8sV{Q0|??R=zT<31hfsmk%PepPvF2P0o(aJHYR74C$JGQ"
    "4k*FI0Y^G>BXPU^7suQc*@7j-QKT`gorP)SwzF*JQw-!9&@)&`>iRVc2o{2MICwL<QY=m$9X)vb=<^3J{`8A4j-bKjj<b#zeyy*godSOk@WH^7*T;ZN4?6q+"
    "B<5Jk>rRON>%ftL+p-TSM}sI1;vK4Tl4J0DXf9Y>(W73=NZ915mgqYbFzz{nP<w+Rx1Ph}c<FNy@#{G}K3%>N5$vAxb9ShJ))2=G_^U7GGxHrD)Zi~$Rsed="
    "q~FII+d$%$u~n_=_U1y|)Y`?H#*=BMLfQ41+oI*w<c*MHI{$RgGX;Sj$4{Y<K5wA`!g6TkTCiYvNVP^l1~j-UG(mFr<G_D)<WI}4Q4o`?{A>=&0#|PGisMyj"
    "SmnpSFoGzr<SDcVQgG$~G$|M~;A4@?n(K{J$`WP^MT1J9;+8bQf}e_SXKK{;MA<~J;0*u;WM8T*vRm~>p6M!=W_v-3IX;U}Y~r6SN2K81Q$#||Hy%u0Q)8K$"
    "E}sv-b&-ifngfQ6m=$q($tw52IEJ1<`hx<&$LLRS7QH*&vS@UMHmtVNb*Zh(Q1Ng*$ZHoMOG~$$vN2w?eY*!5gXEfSbryxAGMUXS7=&D34U^_mb-x1+k{MkL"
    "nU5)T3|1Bcs?N{g3FTV&J1us;|FsiKtLv>2VNtNpkv2@`vvS#O?_XH!D}!xm&sliP+?2X^cacn-$VbC)+L_>i0|2w-arJcsTl_?uLT<4#!n6uJYg{H!bgNwA"
    "0L&aBK{^DO$pJ37$x$^=%c#{7b5OXY7c$l+MF_f403#8!0F(=s8G|fRvbCWWOhCO+E#U24c#Co{?i~)oVUN=avk6XMh~Nym0^XA+TM6C-UA)xBANM%kz#u5H"
    "mGLGvhlA<`QO5OQM8Rls7FdR{NbkWg{`@Rb#8i?j3!zO&*PhV?6lT`I&p{+CpCye5E9BV$6BaKo;Y(l0@t2pSl54gDB3#Vw_F#?R_B(S$Q%ouoi`Q!-j}Ybd"
    "WM|xjT!rcQpO<?`*>O{pUH1U%wt88EehTUw>yU2mxJ|}#P-SPYg5C>%1{!Ffzk~IL=WKHDl^5E>!N+TB0sPyK<s2?pGp*Qza;<=DHYh0!;Lu1<#Ol9@gRZ+@"
    "vsPB~kCS=cmQCm_d|JnyzR-x*sBNrRb;J+~6Nmj-@AixGpY<*p3%6o?zL-riU;SCx_NKXT|IA$vIXrHSw8f+YdgjFgFCKX@OmE-?BQJ=(o`3F*fPM|U^T3au"
    "w@04CK#(l{ZG7R~W`!zs?>5%B4GrCfmTt!}F4efVq&ReU8~VE)#C(uY<4&9P<HA?it}OcxK1N8h@9yn-f^~dZa8VW({)Sb=Vlb5EvmLiHR-27L;qhnT7$|0W"
    "?5?ei6FeBpT@bZR3|51R4wN^f?v*3N%SyzN*j;D%(85b$^|AIx9FcnNg=nUC@bZ(3so&$f=^<(zu$>ky^6-fJXw1jQENqWGe%UI?Q$eLvhYX%s9dcm1i*?!`"
    "E*@I}e9RtUg0`Q)%9cf6GC`BQw?{|~NtZ9vtyJv&RV+h$neIa_H?HRe{AsJDsCn!w^AC$vW~DAKYp%POj0byRCHuq4gcVue*n%}vAl6@gA78F-k@xrzep^O&"
    "JV0yijSj6y$CC)?3%k;WBZdxtV`Ua6H-L1hndACmFA~L(9`!CSGnt0mZh%lF0{|KZWFw1-R?dx23tnPq`<KxWiHjH{(3E*Jyu5tLgT-N&1Y<l)_2tt?AXiK$"
    "W0Y?U@^5RIIMVdid*a0l(T)6ly`H$h^e6KmI)CCv{-_MCdLUnUT(x5B#J`FWz<aVD@N2>TR>N_3IPU@YN(Ug@m)1%x__DB$3FXg4+xm)kWe$>wNNgGi3ZVc*"
    "!?v~ndqpHC)H6_?5I#@(7$?C<bo}EG(5SQyFV;)0b;*P1o-ZU2u9wSE;l1hb*e4<iF97QXkAa;XR6Fo-7i1Iwtt>8qD}R%@5Dr5fzpfC9E-#Upo6ai1Ho3&g"
    "1kgu4k3}p!>M1tJp0_`d?}d=SejQZlnp@;PSb8FoFHY(WexmWED+}`j-Jg_YudH?U!)1S|e$-A_k;xTbhY<tg6LtZl8ta993La*?Yl|tc0G1e(i7Ly5#{kZ!"
    "cX@dWAIa(A_p0CPF?aamFoq#PS9ov}tx~}msB|cqoK*qfup)ppT&5S*ndh!A^<<%D3?{GnkfC(qs&UP?piVH=ia+mz5)miPKF}|W=L>hwuuq(iyLUNdjmxk)"
    "_yweXU6^K##O-JFGC|MCOX!)m1U{GIF{=zih#vSbrmzRl;LWn-#TW<#&d`M=<rkVI!`M?se+?n^aU}ON+6$ZG)DlA4s~A)9j5`Zmn<Zp`TTGhx?)&HEUND4>"
    "Ddx&&_K7xhSL~9@OO1o7J>t3^&Q04i>Ft6=l$gtYByz5h1lUNpC?Et_OjD$@ZTRNy8QAb<hD349?vv*8Dyz^3-8^08kE_yDnXZR9k<3c)jMQLWH6FcER>T+w"
    "v6mvD1kCqP<betw24|#<$GUQo09&56e(8FK0;8%Wf=6X%t69VUxt^xHSc=|OMa66rUBr`WMXSEu*s0YPc`%p(H;~7I%L<+XAfP0*?GQ^qEQSb0IZq%%><<0W"
    "R5j|=yt?}-mXqV-+G+Oa)H&x##Pw}16)YPYM|^Is385&SGy&R(O&qYNEuzA+CK+*NSS+Shlk?+KU>IIeQ*_tabWyQNp0Vi}bLofU9z)8z$qtj$5o3jb$sZxH"
    "ZS$}r_{l=5+7%hL0fev2qLkXTz3`3}q*WaT<I~w-FT8!*v~QiYwUW=m=blYrBsR!sZ4Jk^-xi7@|9OWgkD5mfnHZIa=u2hszF@*<>&#<mR$AeLm@5~0V-f;-"
    "I|?pmd&Xl9pU~FeLJvIYmsQ)wxa4y&JlX0(H`~j?xH7z8rEb&mag9F*As?7M2MS$eDY_&G$7y9^Vz#X*NC>@5YY7(vj^Z{^SoRW>>cBXdvPBVV6pq=m`ygJl"
    "AC3EHTR1l=eXipoRDmb93O9z6`6ziSaxwG))FLxpfMF;AATkLK5<m7_7;H4)@R{~-LN!l75m%($(~R)~y8CU>%i5ZJbceN05tTPaaFMjC2&1e{?Co6WN;<nE"
    "`bxDeo>yeSC0)s8^q$N{Wn&dKpCwHyBY|j}gSf&7x0`l;3kxIS(HEVcwoO9dC0%$p7NpEA{IbwiK9j=0ODuhd;<HXUG)%{`8UZmjha9I?M)U!abIo-+QQ*I_"
    "5*q`uWMpF7q{bz;lXX;T_gTi2P2u$Upo@mipJ|viMg8=`ds-v1d(ng#DsLdw?7q>cth|>quJSFd8_;;QChxRKw{HHBEj?p<ma+>YhLQ@Ehj@2`e=7Iqqn=K("
    "g|hWY@|w_W{>;@JgZ!DMSC(}5bB@4*hbY=9kz!9Cuu)U!%)?=iheR=w8JF1xi#=|nFllbY%CvbI*u~Q_YLf|KWtx(89vxYDkwJ|u1j8Z_dMSzx(zAZN#nG+y"
    "D%xHEM{7&_?YgOvO{1V6VthIS4tm_5eg4(c=MHnaKV!H8KS(t(O=x$Yjq`xW)XZ!Ux1OOHkhQyDwoBGI7XTMd#Yvu_;Iw%8++%$&S{mpUZk1~o<$IIKF!0B2"
    "HAd5H+4nkbd*A0!s&s^8xtAQY67-c}K>OMAB!r8tw9=E=Ps>tIF&)KZ9=CZ5&-jPn+Liiqec@&4wKdkBxpo!#USEhi?eqx;ofgu17R0eP>+vyUuQ_R#uT^zA"
    "j|C{ilV`Q(w1!E;9RXNU(Y2Cc*9@KG^%5^D-S%U_$}qY`7nTGLoMH5U&hj+6!NNldG;u4{hA|9IIG~L*8Bv4uSg?j=06CS(06iXCl63y8TCJ+CJqLb~)}Ch~"
    "tcXSqhQoH2lfD<8(LhxwT4m?HKIaER_`0i|ud<9*u%x5^tjI_l{c%Zeod*D7g3&e*fil?>R5L_Uj(Y9l`!8+P`N6-WK6B0`E#=hb*>x#8SMkVUx#axMe|88Y"
    "1S6uc2v}dm0NCtL+(*OX(zuU==rB?@HvJ{ST9kH~2!Ef>u9bsP=|B1qc$5Q6JDS0gnZ?h>j)S4jiVgFw!G>WVtQYjcv<K~d;nx|s3$)T0It~U~f`pGBciPPz"
    "_T_fFZZg_96H9S9$>PKbj*6_Rm689`NmL>w3_Qaw*PA=8i4-t%Hae732@#9+xij077B0j`fQDwYI+w*Lkai*(mt3u^Na>7unb^%h&fxujOHwrH?Z(0E?hH0h"
    "XAac7l$@$Bd71U~&N7YQ7}u0Gkyh%iMW+%lX*6}9SSLLSdt;X6*D5=l9W$w{*upxhhGm6CHw?%nTNQ1|8ck!#60_@SBa$_8MubI5wkpezh>{mM4L7rdY&K)P"
    "?wG4(cxTjpWu>zh9d=3fPB6e~GXEu=R$z!}br&cNon~wXTIoi9W=doRM-N0=4`dzXSygf_JoJ<#gr&HFGRH_N4v;BVplDG#{=y#mfRg(J5128%B~9@;##dIp"
    "Ebb*x?S3Mkh-P_%qVX0#iy?kl27MItLSIEm=-x|d6_)Wa%5^E8E1R<!Bclqet;w(ZwF6}Yr(rmn6Vn)87bNlMt`@m$jPNyk*fye@kqOFz%#h5BNuI=iRk8K)"
    "9D|z8>b(FsjdJB0gTkHjga%)$Az@GAyE0nmOo2LY%C>NBIt_I9+^+9gi^Zy#C!IW5&XzDf<nXr5z>8Uf3bBMLB;KyA@z-l>%f<?LC|WXl$M?MB9}j}@)4=bY"
    "%dR8Z8kKYS`^(E)`9s(1>(WQw8YC81ekiz2&fLhOVzcPH%>sFGJiOSd<GDzXO$=>;eMUh%AI_M^n15~a>UgI2Tyy-&TId;ICD+!3gPH$i@w>Wj`GUMDi~hY2"
    "oJTSZbUjOAe@l+qpT|Lu7SfM0^5-Tj<zk5i;Gyu(N*Kcu^i;M*PdpGmy8f6uF6B2o95z24loAbwWvbX0L*o&+nlN)vWko!;z%C;cIdtSe(hC5x_~tI78)Eim"
    "F|uDJUML0jrstiK;It_#eISx?+Zo|4i9gcLAhH;VBt^2|f$-iL3`H;jWOR(ut;_Pvh1aSTa!3mHsE-hXYucGnI*d7B5lOIhkM^@{rvCguxw;PyY9u}<%TubX"
    "D%c(I$SwoLr^2wm6asf=QV#GyNQWlAmN1BMGHqjOqU>`|9<l;ejuosXr>;1B<Zr^%1YzvX+VUmNh#61A@#!J!{wa&A_qcjZ;$N{ESGrw~9sc?;E5V+8%140b"
    "s0??nm{Nt+Mop~3i}GVt?t0Ra&FgDKFGx7a6=V*!V$fp!C{T&RYi=$Oy+N2fX9w0eT-gI4z^oDYG~leD98ZR4L9ea5?%Qn42fR^rqD*t_o1m|G%NDE1vgkdw"
    "<?#0Ca`0N~jf#BxY0}~z`)b-=2!14eA?mxRVACq_eH^28r2Ue89YX1iUip&?I)KIW4S=BLz7#;@2maI|$n$?}9rzEIf9NWQX@kiQ(~uM8Yy=kBy?{S91zcV}"
    "Qu4%U3=-+9z=acsrt+F#H)wm;8X^XcrJ}}Ut%Q)OT|&fLO<EUtih>N{L%RrBM(`Nw%Gw%9QNw?*+%zt0G(meWhq{s$+-2o><EfA)N)%FIb+8EI?qCv?efHsD"
    "R4hvZ!H>o8rz$)WA3+KCf<LdyRJnWNw`EP`0dipMEC-@1%W}bm6qoV6Ns*b~Q9&wFPIPG37^7?LTD9dRXMhTgkqK#*5!CkV5`;R^s@OAP4JWnwk=K;&#tON7"
    "d$%vtm7FbgC8vkC^0kn$uq#q=K_lkfS?n226TyqOy0;KqhW*^j9Qspu%ai6RG+|c()^&Sm)E0XhU*6&rnh(y^O|L?9S9YVHVZ1euQl97$q6hXe<(E}*kKwb7"
    "iwi%w5MLLi=g-ImhM!zS@Z_bN_`ot}eqX}dn7-v|R3kyqP0TU5#kKep$;duQ&?R@iOhGhe8Ygy^Bm+s_l<p3Pw>14r(?K)qtUSw?u9Eb5rS?AmJV;Ec2ZC&X"
    "ic?DN|8#94K8Bj=Gr!A97g_0ixS1(r+N+?Ikvs<e)20V%jWIVn8h<W~HF6WPov92QY&W#qwd&?Uy(NCs4;u1o<Dl89+e}ij#qS(bR^0P9jVhqV%Bf2_D*MgW"
    "l+wl=*K&TFllLrcL5^lJ0gjS-Zd-{k=~)Dlk*&mD`WYk6GPw|cZ9()s`kqsK@rc-IByOp9xj3TD8uPL18^gkB>SX%7g3CXO><DN122ZxNZOuV6<PzMMp|R#H"
    "{M!htTxt(s840-%DMUL>|25TLkm3_#vx0(EfaGFbVYQ!W{qp3(xOW_M+lh2YLMn81ogdd^LM$KXew$ZzFQ^Ty?C!})^4ELm>B%>X1xay*)5brV9>UM$@w0-W"
    "0e9i7JLItf(&D8~cJtw&#aG1-(4Cv@OTa<HP8dx~t<uzZLKX+xSzF`a#qXvUM?3kKWfc+A)QlfaF+IgfxIi;_%6GpaV45JuB5VbX<i-FB8s2g{+ZD5?S*mSH"
    "(#o{%nL3)m)*&(PO=g225;Rtwy<rcZWDS#6;bSG`b#nuIacmzTt%R*4WpfE~R<XT|7lJg)4#c>DV+v@mt&xfe(FTMSt$=)DEs>D*Oqxi0q=a#y47A2JU(O9u"
    "%temU@G@}ioK+Phk;y8TmkIZ|ysW1=60KR7<SR%KF^d&+EdoL&Ybnm?aHPvQ9i0jpnW*fF&w^|(jaQhOQ6eStLC`StQ8-2(P@~Ztd4>u%70OqL)=FR_c@$j|"
    "10A(vEa5?!q27}5<nmP`exLauIXe~wE46BE>(d0yJU&=2i?Juev2Z%(h*aS;ITFP(_M>s0lB=Cgz)tS8`7qbkK8l>U@MO~Jln#|rY%D6xiaf>Q1HBQ>!UE&1"
    "R8uWc9=}X*Ld8~Sfg2x7NMA0d+v%{4e6B7P7bSPA5j<^d`th&p&^#U^OntFyYtlE%a^bj|ds$?7Vx(}9laVOa=b%YWA0jlZgy2Y{d8lEEMUsZhK$N5gP`%{G"
    "9U7sx!q#!<5NzfJ%jV!80!;kl@-iPS$f#G}-b6G=+F5~NuOa}by}pE?*l9v8nMuRI;aSI47jAyP=~HTvu1B0c%Ff4Z3vy*dQr15xYnNJd{vbb|HGK?+5E{{d"
    "NZL^qqIq4<`nQZq5}C~i{{F}Y;tzuZbl*1SuaNxmGL7ZVQh_KN5UFf5!*j+APNdtn+>bErwC2XSkcS-Kd`+ckR<g<?2bk~qQ?ak(u0ISab$7ktV$A1HvMN1f"
    "7Xevn30m+*$jsuGSHTfri(ktU=URz#R^lhDPgk0R3c0o4phsK&IA`u{*I@%vMcA)}`!W^!5R0>91_<?d2QBL>^TF{##04kzE$Iz(TnUJpny?HH5&WOSQ+Mp}"
    "m{gvDMKG^|E4`PdPpr}p`S4?2=TT48N;1Q+=j~7Adsh^IC#ocyDtnLlU@W!~;eupSZ`Hjwt(tcZ|FOG3fhVvmfGkU{C!i_3kUDH*30pZOk(4DC1XeJF_CEh4"
    "d29F{^n6cd!<422zsGL%Xk%)c^2M80a3&D=YZ##V@sowD(Fsn&u>cv5#uH1%Z#BgeLL$sVOpPbph|BJCwq&fBIHQJs5((sIzSnu)=CAqecoL2ab+VYmd9bg8"
    "^XX((_N#9y9oRp9^&CFWUD%aj@9P#mzYWh}$KivY;DhtqSO6aIRUpU+>mm2yc}s2-ft6wf+B{c%X&R2ZgOVqw$p*Pd!Wqr^)ulSg`q{4wXLPDne_Y}jMZRwF"
    "CZ5%v!)Vx}pO8oA_Mt8odwjE`sk2xIbk}PMh{W>~=pT9P3@>;oU0y0&N|F@0`u0@|i`bYn1RF<T6Fde58$(n1sF0@fQiuK&=dIPFv8v93A{eGLf`TYFjc50R"
    "zCRzb3rxI=DF~P#)wEbJ0U+m+*`t>#nf)zeA_G6x2XxH|Uw<d;w;K{s1ZEtt8xc?><``$)CvtDJSJ@kFb8ocC-q1)i<v(Ir{LQDb<8}R|eC*Y|jfBzZW!~H_"
    "<0vZnQ+$*#KK<P}tMQX`4Pb;tX}=YXmTubgTHv7^O7YriYi3WE6@jHN0LgZH&YXF*b6d3!ow#KKDQ_0z1Q!P8is)p0*D7r&*&yN111r&^5X;*b2^^OZNP{?L"
    "5=IqgZW*epcf~cbWu#<2E#VRJr6fmOiJe~-*z-)4XOvM?&}9{jv(jSWyfcsH_aFuonrWFBH}qQy6YbjSl2DIR0c3GRkNIi6EOeIy3H@(p9(ySe0<f18IiTcV"
    "efT6@LOK}&KRUri5%k&#5Y60DpaW|MWRsFwI%+*H41RT6tY<B!BK1eWyR|hn0u~yne%+ymhm%pNUg+yRT5-Rv4j|j=DfVDJ`fodoU(vm%>eP8#JC@F%na-Zc"
    "vo+CKVi{|C(2}nrQE5`yq^6tpNkiKQsz9r(gFC6I``4uQ$unlQ@#7%fFY~RmN+qRWMO_vFDSqNUWXEvQdXaQpB#9!$vAK`wDNiQ5)LWi{i;QEUJ9RJHjH5^o"
    "h&^1HfG>DWHhiE>0Y0Su)Q5iI!?5)T7C^NouLHkiV=ikA(?E9lPiz&>h=*M1N@-!(4gO}m8OF}@ahEA4y2D71J|XT)w^Pupkw;1$%MBl?bR=1xw=DB4;j$Ta"
    "(i^u)P8B)j#?QhYodr2)HkAjurgpO-W6Pu`wOefrm4z}8U9$kNZp=Wt4E*uxfdbv!7y%mq5K009xdjogdFllamH_qh%9>}zmX()bLj36d^74oiwz8P2808iG"
    "vt-wyw^5olv3SgGT@^><U2@Mx$Y>pZEz{s~kxP)b<n=+7+#grK24ua!li&OyxFAo5=E;+K{1BoS2f-`!&@YkAsMD%_wwJn(x6K+aAlL|b5W6%TSt<!U8xcq?"
    "#AIo{Yt=0237L?|*{n?zA+c9#5Ip*-m3^dSS<+ZNWsTnxq9l=oKPl_y(>0k`Z$NQZOb44BaOWpV<WJnoMATHzc@Pyw<%tI*PES6`5R{VwhWL?|Jm}DqYC_;x"
    "B!0|ZlFPlFO6`vG(F8P^?~t`itEPl8@-j;(BTY&0a&U6$R&v!<hT<SDynT&SU)(1su8^X~q@_gcUeJs&Jg)`7PkRU*Jr&QBs7HagxA>Vh5ZaAwgPJ`#g)vNt"
    "PR<nPA7@Um(A90bu#>WbRxKed+^nLuidSRFBS7;^{BUSkI%ivBzg_1BZb{d&4(Fx_I;-VfOwe|UAYV{vz8{|Ab;ZMQ$_a>uelo$G+^p@KQ&F^HR)T2KR7N&v"
    "tweVapv$lqbOlOF((2lh0HhX`tdl$lnmn@KSWP>;aGtSizTnr7{b(GHPoYhY=ZD(EB&;$R3acv1Tu!lkAu{2gp<$KW&2)KrCN2&@X#x{gDZf77$+qhQB-Zge"
    "XJ4-S(~w^bQDLJkdTE2WuPbWrN7c!zgQ)t`7%v%S4hQir9QP-DO8D&nSjme)V{3D5jq<0pHLYze`~qfWcFaz@r6B93o=Es{5+drDuy<__^_mXIREE%*T6j)h"
    "zM@IkjEn1Xa%S^lS_ZeaKO7pl+?3F2Nz5p)^Hy|u$#3@ASE<K9x8q;#h+K&vv}kDs%X({ma*0FDtr#ym^kt&|Srcd`S<_19UQNpIJo>8GO0-*%Zd<Nl_adL8"
    "q|TA*@{^X6*w#K~MNF7S7#}m4@G(l|l3KaARq1oa{nHk;%;9S+PfJ=PF3at{|0i-gF3+U(?O(pfD~GS%{l&Oi%3_=x2HKRFl9B~8MuM`4a&>z%gBXi9T$fXP"
    "ehDWR7NW`P#OmaF@RR4kHB+a<3z|PA?7eBrvQ-9}1ND<98#WUqj(bcz#{Fdw8k6*B=)xtW-kUo$7yGoM$Z~00&wbr{^PF44g!k33|4m+#s~ELOx}pLWk#=e>"
    "4#^iEZot#bBA^`b!O^&3-j|-YkW25iJx$R`&Dw)Mo>X_b)>MS_A_u9xb4mX8ZT3>IWobx!xjqDe1~0Gs<U(Av`sn`RK=8^e4rgA{gGX67=b(2#h>K{To7?S|"
    "Ydq7fwZ*jPs}@3iv9^8t2kM51AGo%QAYf!`=wta#y1lJ+%dSzd_G<?dfRGCscp6=a3`|Z&xxqDWRDWr|l)mSW2TuI{o9;l+yz(a(jJ3_?F|f3kiVI3PWFb03"
    "A(r>Y5e7tjf-oxak7T&*yHNz3GK|?LtL+}(-&Q&O=78_Nn3oVgTF;(Ku4PCBdrFcBmX9cp0|eC(NVvd4FE2~Qz_pASaG~DF(?z%!H+rb8Ck!Z<&4PW^4A8uq"
    "$|fg%ss)lMy<{U=m?zq_{BRtzNY9}-25N=^2d_*mB9%_XG^2o`9X%R{v(TSOA%I@Fk&)qq=W)qD^JhsKVTARL!ts;r+!X2D5kOCil;?dBCz>_|`O_1<%T|q;"
    ";Dp`W7q-x4sXM%pl@q5;CIB!E>T7EnMoF{28wa!WV^3uamU7Z4;dg6mcA?2UP{OX?Xs52?NDoyU-e6^@A)ejF!XKYVPRf2&lQ#52H7lbC&#$R>A@BEL3#q*%"
    "yxMw9$Fm$CbzS>KD;ktC#4FmjaeRnY6k>>;HJe`>LP}|AxwUI0NH2T(%`pO}3mOiV-i}c4Sb$g1@0@|@mzT=YaJ>ls@7!0jxFPq02JDRFHj0vbM299pilrV+"
    "E9Ii(MxLuaPzGClqf3SFB3FOP<L57lM%MvGK6XwAA+q#uLUMTk8R(+=atS}z^{y2Myxi=%Sf<-h_<4B>1J1{)G<Y#OYOiZ4qWzO)jr}BFfQ?|oCZcLFX~p7G"
    "86}G4-VzT3aTa)_QO(mp%!k_O3s^2`^J%-~p5Y`Wqoj%%9T%Az)y$R+C5NP1hGkw{Lu~F808;9-16Nqpvw(V|M?IHKKW*VRR)9}rC3k^6tL%q(4C2IBaHG&6"
    "x7UAM_d~&D&>@$3yTIXm_E|I;iNNA?0!wnkj}(m~{}c}jCK8zV#brCoq1c>TeqbFQunWp?A^kUpycuyT!nvCyh%A<G(<>y+EB`2V4}Blq{rbhxgQq`y@Z$Kv"
    ";TNCZKW2W{JuNASoOyKwBnokYk?WEv;^3(f0J?Fjr6sFLo!@BbM=<hKh1?5+Y^||*xb&c`S{zfNwuRv7;~>w;bSxrua%r0lKp_95z$5#4pxxwYljU!70Pz7l"
    "u-<<pamTI<+_u4l!EGgoJkT!OQNvjo8l2Vew9q<CcnVIfk}Av&O~DK!|20Pu+gK}F-P6eLe8F;Xl8fxfbqTB)p(Tq}JsX-y=vH`2(tOlXEdw7<gIT@3t+ge%"
    "f}lfC?W-`Jt9Y+;0G*PhSqfJV$gyt95K#((Y{P3KufIqG%M-wQT@cz)<QB!0dj@6sR%u3m>asfEH3FJPc++bV$toNq5#G1jZ7ty2h+q>G5*wrqB*&fkUDhz@"
    "13Y1&9{@S18YeC=Uugu$<HteaqasF62$v#VVY%z~MW));?g7JSB?iD`;{>ltYip?s4iMKd`XU^kDyO$;y_mLb#csf6@m-p>SZJ6u&1D(1($~m#Rn{Icuj!qd"
    "F$vrCF?;E0G6O)83AR?<ygjA1L3!dd$1o|I@KA>F=yh5e<xG!xzrJkDMTR`Bz@Tq_$QOkhn`X3P+G&j3beve5gvRbPHRwuyjIHfF%Lhi_^7C-wrY-ZtB)_!Y"
    "6Cr<|<9kk(-5=fM$K>EAn!79U>kw!~Wg6mjyVY(Ob%#NvGaH+(<NO&QCDZl-kj1q%H9FYpm>*i?cFv_;1Y#0Jf$VYGSG2B;{_%)8GZJJu<&hUL6%vY*L#Qju"
    "#Ufg{I}@RP5B+g($d75_?eEs?<rousd3mwGqc^Fl4^S9fR#AyPr226pH!Z8KVlGH^5@4~Udg^kiFlNCPcR`Iy1%ql`RXY<v@{yZnQpw3*m*JoIv2UxcbERbM"
    "_S<E(8Z7TVi%qFFNP}PBp=BaGGS6?TX}B7ry+EGY5}J#rP_;$zc45JJZ7^bWbA}>IEFB1H2rCTZ7FiM6<$hU195pg+SVkE&HamnA(q#<Kqf&OuDwNwj6sBa~"
    "vT1YHstYU0)wFU`{6QpSTrlG<1r;d#2PhG#xp59Je-@OF?28V9X4ZO(LzCoOKYJnv+fw}-vZi{mu-%lFp)cN&Vy~NPqapNt&n`b4P0oTF(o?eaS{!GJMUm?`"
    "vXGBn(2=^MM7})8zbONgcBL=N$m$WVwD5YtaOSrOP1Dt9aiEx9X98-nis4O{m9HwS;XTuR`i3amPb`l0KcqcJW`kez_L{!MObtxWM`)9#b88b&+P8Ox?`^E5"
    "iBqQ`<<%rvXp>x4qGyog*Oq9*{;)et@+N(7z(#M^9+Yr8pIw(^$V*dXn<yPGNrNHulVnJ_b$Id!PLm2&5r@PXYb#cR92YNAE92letx8;oAoRmlgul1A0+HD9"
    "P#stmSoxLg(8{l41y_EROn7BfD-JL_z+VODB!$ZFy-q7H*3n3RmJkIJK`>>EB_4$R*^h$rgL<u1ch~FNjh$NIw6?jL-*-jpxzG=*g+sg>MF2Yxs!Of{2EC7!"
    "%RQ0}A{1P^5<%O0$}br<8st`et-;&N8+<rH7o2KJ3@Nt?(?^pUyic=+ffh?qtWap4)ew|#R7tPH1-&p?0KScY($`SFL(8At+Lt?KV(-(gH6v3nUF?he<+hCb"
    "cFwq2(i5A-ufVVsT%_r5#bFyrRogSg_AF$o>m^sPTU`@@7Fow@)t>)T5PXyDe<-KwK9di)pO6HXh}-ncTlBP(4M_Fa0QPGKYRm6#dAG~-*177YTb)fl3*Q92"
    "a>I3L(lT0b-7H9s2ypV6l+{O*Jg1^)hS;cIxVOwZMPa=Hl+{ttWN42tfx|)m>F%sWu9L&1NsC|1XV-61-7Rk-n%#AOk#nNOnN2Vzpg&?plSgP9W`|j%fjB_V"
    "oEA=hz~kvJFIi2J{8p6TZ?hM!V_qIE4t^063=W34Z}mhTsp9sdj(JBvEt!~e0wA1a5fDy)(w)ccsUL}S_9VWC9e2Fm(Qwb{ary&2-+%Dg-7g=Xyf}RP=!q=^"
    "0>z*J$LO@3wD+A4FoBqJJ87C8Ph#3KX(J=NLG)BfH-b42jIQpq75tzE3mMRJsI&e!Lon)kj<t8QxrA_M13>m<nRuc;$Z5&KcSzxj59`GFaM;QSEt_*+G;Ve6"
    "vj*jXk0m~Qt_foyf!(u!#uxP%`_R%w_4Gth^8VXWrP5;mO8VxNp`tw%&1#K!BBLbT-%LEy^6f03DKC4lT8VSxF?e8%=%}5>6w$$LdsLG|0@A->epU@&anWsZ"
    "-^lgwL(4+}5^PZg28Zl1TYWJV2FsJA3bLigeh0K%*J77Z*hqWb6Lq1@f;BY-D&@Em8HU-!Uw(3dpLnTmR=M<@yaEOltm!6IHLsl3N!K5C%d#if@4M4Cu3n4D"
    "thPF=CsK(n-N=t$p#mbDYe(WIi|UPEF&Uq{5;A{6y=8S*elaI3A*0n`&A1ops{u_h-j{o30UH<rJu0jMgW^KN6d6KlU2>3AWP~Zl>59a9?Jk(_PD?k~JAgd7"
    "5?X|{WtYgZ+*%JZ=d@#&jyRb;n)HHueh);WFvhb@x<oYdJNOW>MSLxxSm<}2GC4&&e=0)tNhl^SjAtCg+k|<FcgcY|Pz_E`hXK7e!4reoGb>qc+m*7Vx+q%I"
    ")hhEr<o9{Y(y6Fdq}y9YzY81Ce>xvc<-(mzKA*g{>mY_8o+XEKr8`RsztwJ2F&x9F@A4xhAa*h_T-y%Q0tryaQT8QQ#oJFL%0nGi3{I*Fjof~g-_`?%gR7mu"
    ")`bquhicynxO(ci&Iu@5pm7n=!=6CF3=<VcgfdV6sPjK=@jL%q*K!IWowXrjB$mTTH#ZoIH6wK(*RxR;3=7qd<$C16t_=6gIHl{cBNU{ZI9Jj#vg5?FDDX$5"
    "9FF6dJ%qBu<Oe_b0lU~J`<Oo!h2<|-Ugo107DtqLS$SSb6g|NWU@ML<v?}aDk^xem;b-_%ooV2|G~<o{A+zrQB;b?7z_6=`Vs?5sv`}D%a&#3Phm%ddBw{J|"
    "(uWYm@gObR7(W0bPHXS+qu=t>TJ|*zduiG!n;_UM*3lK8`hl}V4?~z(5__mu6P6#WZCvw_-M-d_*@51<I627*jD1)Yf5;rzm`9pE<d<r=GOIijmi%}MOy?;|"
    "I4Ck%T3M`{k9j+bm&A;ko^61~=Wb}a#!C^7%{oZ%nV;8dX@~a=&jvnpFW84QwajdA_3jtW=<3~nat81J%Xr}6p~QhRi>}`NLl-2ktGE9$b!J!Z{<G5s2u6Oj"
    "w8tCM4<Tkk1@qy)OlRldG$o#Q*3g97#@+G)ZN>0oZODYB3l?Jpyt15JGfS?H#2xM?U-h|t9gLhIo~bOQD~d`sX=KQxVXyQjbN}2qyL$H@oHwc=^U5{MIV+^P"
    "G(CFMHAonHplvZ(-<4mg;TRC;;mOhC_7CpxXi%s7rd_HxN>2Bje@5_8E3NO}nSz+_^xCDPdc)Z|+Zr~V20Z?O>cA9BbstO)r?y^Vl}V7ud^mZ@+GeFv0rHYV"
    "LRi?mvCrtUjBJWITdFuBX<OlX2$gQehE7TyxHduwEg|`;A(cX!W_5SNsqa>6+kU;~z(4$d-KkaT&1$`Q#NIeqNPq3{*W1<YEvLR!Z8ZHYXG_)HsWx|x*kcDv"
    "(02~LL$u9CYxoPo_Yj2`7A1T*83Hk*D4pDb&5r?V2O23e^h|!AdzYuVQ?zUpR_Tm0c_Ny%DHAu`AlFKtl34sjb>M1gVZ)L@Wo<1pW*{dfAmfZ~(z%j|#7=Up"
    "0_v?|W6W^i!SFcBDs6!YF^|4DJ|6fH=upAb7e)zk*Ssme>&Zln0#f(U%6w8bpl_2mg!RfOLFfq-=mA~IDIu(>6I2Hon`?_Y44cI>y7r_fQTwCw3-Uq<eDqlc"
    "Eu=+|DFaESk7V9hk+sOZ&@(*1U`Mc!C*f=u94i~11ynqVRvesSqUE3`t_Ff-cdU<XO=vQ)T(tzygTQAhEsVslT=Amp&E;k3;zEclFUIiUEEXl@5G7)?aim^k"
    "?xo8v-x+sCh%?7`$Ki;fCDSAHo1Tj#Ow{R+krC0}P=d+DQX(&*ovC!Fs(DQVPlzT-hCS|00Gqn7l*5xr`2qmjs(GDY;Gcz)s8xzblgVsQ%5d7V4Sp0vr}9Qp"
    "opyF+s2i3>NdeU8LYBgxKK%?H!J8RZ4(S`N1(K}&Ga%3K$4q~Lj}n^2_Yb330Ld-@lBugC!3GY(#Xp+$wN(3li5TxtteB+)io>h7e?N21-v3IZ@j@w6zngtB"
    ">@fqWoQkv_@!Zqmtt&oLgvLf!59hPuL^Ut&Y(*aO2Tt<R=t)4NZ6_Msr^WU;Yimw^0xL70yOuGr_I%G?jwdBa!gLlq{N`d=kkuk1Q~QS|T`$6DmvZY&POS4;"
    "3nMmI^%72;%gXdj<JaM2qy?Rk;fymlke&V#z-R>wMwFd?hN6r;(|ul2?+<2kM$TU=r%R>z&2Y>VOABUpl5)})VTLJjz%%W?H_r-@vfc-@Rdo7BcGPNUwu)GE"
    "&5gQTVm7w&ceGJ+mB0^N+Ed9yMA(EGCdV>VC<p0*EmH{3w2r~&ZRHz1NM~H(#q=me%qM({-|@vfIHUv*x<DQWl1v!0{IPMl=I&uknC0=!WVzX+K|)C&#=flP"
    "I-+~_Ts_sQ-D&guVYH-quL$*2^SSeDi4cmOyqTtjtyM@evPD*O=@Avh%2SV7cJ#G8qZd1?P!$a75Lc#5U7Ah(V+y&tK<oE<X=!$eF#Gb8i!&`c!7k4-FkD`~"
    "{Pvf?KAbmK?|$nH-~W$lwd!c~m1O%FotYx!Y;Hoj#UHNt%o&kt@o^{JponS^EkX&Ge^`f3nl;y*HOjM;(den0GqLDmq$T>Q!**5Mq#<OF^Af3K^ZapDBKm$R"
    "KuV?rv?eM1_Lq~Zx8IDNF4I<CaZf>#arphOLTB)=@cxsFq}`)OpMQDs;CSIo2k+m`sxkt{nzJyKvXDWVh3WEy+^+e6XIUugntARmF=_qDO+U(FJ9Il5*&P+{"
    ")k(sJSK^zXJD6X+{mt0vT)q2`A|rFPYNXZSdCOTZ2VH;kBFxC|tvn{~Vzun!V);2`TP=gQsERxTdtE(^I-jVyfGiS&{ANkQGs;#cW0Cb5j~L<2T}0ZH3~1)J"
    "Ma&2*!tP7Dye#QPah9x`UW_uH%wUJM(9xe-!X1wnA3}-pwkuCgEb`i8u83Ai%f^$5yJ?uvY(OSf(;Z0$w&jvk;LLf)cd1PNoz+~18#PnQU2;?>1qqbq3_$Ru"
    "0^SnP3E(k?C>&`WT^nRe?HNo%@?$<l<SGHP2DaI-tYydrSy1fdy1eJgep?PULmiZ~rAK2Fj$}J*ftEF50#BJspou)|39+DjmCn$t9-|oqTCxmr&t6{GNVJ2v"
    ">Et1>0MIJ?ybt5*2Ich8<KFPzY|Ljd(%^h=Hcm|g%j#2Lu@l(O+M3h#$6v+){J`a8D_?f?(hl!V2oONPQC)$~VgHYVk~Y`0?IfqVUX`(dyW{R)67eB>b~N%B"
    "vgt?07a51SExXZ#<mnui=HBED2WB{lkNvaYhxpKhqb#WdM?OtWZwZ8*1E<s(PP(s3PAd&b4KWgZRv*v`OE_hlIgooB%az@%Bk*|_rB3;Il1H%5kP3PA8j}po"
    "{54aSpWc-7t&;5&1V-~rxrK*W!a>YZ2L%aTHfJauUA_GqkkPK*{>#i6U%mUw5ahK`;|b2g_rE@OUWLFb&<*YLhz3#RXCSTo+8Kbn_s?C%Pkzf^6HZcqk7RNY"
    "O`%fvZ>G;=-T&f9%;+)83aj0sE9{jlelrQ7vqfZT(VaXzliu8k@1F2T2QAif&wR;ESK05;m8lh=c7&C#wIV*NcAj%9;hD*Fgw;>8>v;U`S0TKLUo2X{qs|;p"
    "i+=M;$z`dkrePkU9VVtBW*K^-^(Y^RPIb$fXNKXi2cM;Trv@i2A*M=K`7liEU3v+U;*u4j(J!gvi^)9GugpP6^O&$D#YW}yYuNyAVtw5a`k^yI1<_%Wt21DS"
    "6rwm$Z6?!mQSe8>IpdEx=NR?UR6|12LKV_wEy32-%9FN+v>BLUFPcnOZc9NS1SWt?3-+}0WH6acVyDcgymR--Be%uaC5RJ}2lSjy0E!9}AHaY9)QKlhz3)dr"
    "#DZWNJ9sApx3)9kud0SEm?LX@+3cON>$LYBEmH&5m)ys>ymW5a8KX7NX-A4XsX=(o?9D9wPP61N$CsnW5sv+{@YJ78qAE`Ku9`10)LU6E@fsuMhtS!pc^Okl"
    "0#x2l@Q-5?tnBi+*}#WC@4xB7uh4hmtGDl}rR>a{Fi{t0OGz0DQk<=_utRnY!l`qpnyYGBj4)|A)NczdSmPsO1mn{no)58;1S0Ht`2x`@Zat%m`h4Lo@5>T;"
    "nl-P-|N39&%)P6iW6XsP5teOsJ`E;)I&gk(l+$iQYtj-`($_9k8pAv_+@<lK*bRJCx!!}#-t9~x$<Nb?A0#*@XnOObaan4cY_*gXi0xsvXO9O#Fl)gEf?nLc"
    ")DUKGW~OlN*g$9P<<JI3C3_>ao&cw3vcjs%;w(I;(V)j(QwU5PJbDr$hc6O>*L4hyH-Q`Pm1%E3oBIS}AZMY_)<Hqb0=N%H!{V^Kc_^k5y=tWYSo&iekq#9P"
    "2qmmf9gtdnaNp)1W1OC_Epo1ENrM-~67O6qwN4Rd-hzypC&@hYheKEvo|XF#Vf1Q^Kljg^jvqNZFg96SAjl{?EMvr}30a+qt`;+x%2;yC?qJNTB^-UA;c~_~"
    "h=P8*v|h5T5X|Ih&<Cw&P-HS@!%@J^Dtd#*fgg3R)t(%vwg`NV&YE*LpE<a6B5*Y(GF@`QW=W4E<~2FArP<;iU#~HK=t?6HO#Kyub;`^%4Fu=?Z^r`%Q0uFw"
    "PVeg7KcJW4AIGPz=1gTZPgq4|Ycd`hhIsmxd5zMQq%%ps7L}X5IgG`Kc}>e3;%#%v<{*JN+v*UmV$>)uz={qnt@EReTo^zsg84Z{g754~4!eC^Pf%)1jKR!J"
    "L)T;|xSHkrWI6R_&6%fNnm7`TLtBh_`E%jZzmeH+R4pGeTrE5m5^k89P4d-tD3g$+1=&Dokr27#ouR;yWYg<MTrIf7DheX3*;eeAzV7KGTj*5bvk&RbsK2~J"
    "(<-xak7ffoT_*I3F&MO~jpGo>aZCYFfFz0#O$s=csqnCHZE@^FgZx8gNaAmn@szAKkXawHBOBLhLGz;KR8HLNt{xo3goRZ>kP$PNu8m=4(#|LjjJH!kmbdGB"
    "W>Dv|@pCu(mh16zZ!8Q>i_8E%%V3x`#s^d6MtJV|1FHFru<|-%ycB*Gz5hGJMRt;}H$T66_vf<$7Xl|nMJ8CJhX;~Q_;a~yGNRG#cf<256ls`)B{WapQT!|#"
    "rqFRPmJp?wnLMW!EBCv){W9}3o2RlGsiyhGokO0Uti?m1oN&zLBTVz08+}LZh-rc>mOB%M+=ex0P9YA-Om6_TbZ0&8M&Wd}k6$~J-Z}d+n2m<;<$nQ&a?Vc"
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


_TUNNEL_ONLINE = True
_TUNNEL_LAST_CHECK = 0
_TUNNEL_CHECK_INTERVAL = 15


@app.route("/api/ping")
def api_ping():
    return jsonify({"status": "ok", "time": time.time()})


def get_public_base_url():
    try:
        if os.path.isfile(CONFIG_FILE):
            with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                cfg = json.load(f)
                val = (cfg.get("public_base_url") or "").strip().rstrip("/")
                if val:
                    CONFIG["public_base_url"] = val
                    return val
    except Exception:
        pass
    return (CONFIG.get("public_base_url") or "").strip().rstrip("/")


def is_tunnel_online():
    global _TUNNEL_ONLINE
    public_base = get_public_base_url()
    if not public_base:
        return False
    return _TUNNEL_ONLINE


def tunnel_health_loop():
    global _TUNNEL_ONLINE, _TUNNEL_LAST_CHECK
    time.sleep(2)
    while True:
        try:
            public_base = get_public_base_url()
            if public_base:
                try:
                    resp = requests.get(f"{public_base}/api/ping", timeout=2.5)
                    _TUNNEL_ONLINE = (resp.status_code == 200)
                except Exception:
                    _TUNNEL_ONLINE = False
            else:
                _TUNNEL_ONLINE = False
            _TUNNEL_LAST_CHECK = time.time()
        except Exception:
            _TUNNEL_ONLINE = False
        time.sleep(_TUNNEL_CHECK_INTERVAL)


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
    public_base = get_public_base_url()
    if public_base and is_tunnel_online():
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
    threading.Thread(
        target=tunnel_health_loop, daemon=True, name="TunnelHealth"
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
