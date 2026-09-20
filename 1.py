"""Best-effort source recovery from 1.exe (PyInstaller / CPython 3.13).

Control flow and constants were reconstructed from the original code object's
bytecode. Original comments, whitespace, and a few stylistic choices cannot be
recovered from compiled bytecode.
"""

import atexit
import ctypes
import glob
import hashlib
import hmac
import json
import logging
import math
import os
import re
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import wraps
from io import BytesIO
from logging.handlers import RotatingFileHandler
from urllib.parse import quote, unquote
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
# Lưu trên ổ D: nếu có, hoặc thư mục Temp của máy để bản mở sau biết PID
INSTANCE_STATE_FILE = (
    r"D:\\cctv_recorder_instance.json"
    if os.path.exists(r"D:\\")
    else os.path.join(tempfile.gettempdir(), "cctv_recorder_instance.json")
)
INSTANCE_MUTEX_HANDLE = None

CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
EMBEDDED_CONFIG_FILE = os.path.join(BUNDLE_DIR, "config.json")

def _ensure_first_run_files():
    if not os.path.exists(CONFIG_FILE) and os.path.isfile(EMBEDDED_CONFIG_FILE):
        try:
            shutil.copyfile(EMBEDDED_CONFIG_FILE, CONFIG_FILE)
        except OSError as exc:
            _show_error_box(
                "L?i kh?i t?o CCTV",
                f"Kh?ng th? t? t?o file config.json trong th? m?c:\n{BASE_DIR}\n\n{exc}",
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


def _camera_playback_mode(cam_id, playback=None):
    if isinstance(cam_id, int) and 1 <= cam_id <= len(CAMERA_LIST):
        camera = CAMERA_LIST[cam_id - 1]
        if isinstance(camera, dict):
            mode = str(camera.get("playback_source", "")).strip().lower()
            if mode in {"local", "nvr"}:
                return mode
            if mode == "hybrid":
                return "nvr"
    return "local"


def _timeline_playback_mode(cam_id):
    """Return the active replay source without mutating persistent camera config."""
    return _camera_playback_mode(cam_id)


def should_record_locally(camera):
    if not isinstance(camera, dict):
        return False
    mode = str(camera.get("playback_source", "local")).strip().lower()
    if mode == "nvr":
        return bool(camera.get("backup_local", False))
    return True


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




def telegram_command_help():
    return (
        "📋 **LỆNH CCTV**\n"
        "`/status` — Xem trạng thái hệ thống.\n"
        "`/reset` hoặc `/restart` — Khởi động lại ứng dụng.\n"
        "`/list` — Hiển thị danh sách lệnh này."
    )


def send_telegram_alert(message):
    token = CONFIG.get("telegram_token")
    chat_id = CONFIG.get("telegram_chat_id")
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
    cmd.extend(["-vcodec", "copy", "-f", "mp4", temp_filepath])
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


def monitor_telegram_commands():
    token = CONFIG.get("telegram_token")
    admin_id = str(CONFIG.get("telegram_chat_id"))
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
                    message = update.get("message", {})
                    raw_text = str(message.get("text", "")).strip()
                    text = raw_text.lower()
                    chat_id = str(message.get("chat", {}).get("id"))
                    msg_date = message.get("date", 0)
                    if chat_id != admin_id or msg_date < BOT_START_TIME:
                        continue
                    if re.fullmatch(r"/list(?:@\w+)?", text):
                        send_telegram_alert(telegram_command_help())
                        continue
                    if text in ("/reset", "/restart"):
                        requests.get(
                            f"https://api.telegram.org/bot{token}/getUpdates?"
                            f"offset={offset + 1}"
                        )
                        send_telegram_alert(
                            "⚠️ Đã nhận lệnh RESET. Hệ thống đang khởi động lại..."
                        )
                        time.sleep(1)
                        os.execl(sys.executable, sys.executable, *sys.argv)
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
    cam = CAMERA_LIST[cam_id - 1]
    mode = _timeline_playback_mode(cam_id)
    backup_local = False
    return render_template(
        "index.html",
        cam_id=cam_id,
        camera_name=get_camera_name(cam_id),
        camera_mode=mode,
        camera_view_stream=cam.get("view_stream", "auto"),
        backup_local=backup_local,
        site=get_site_config(),
        max_merge_minutes=MAX_MERGE_MINUTES,
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
    mode = _timeline_playback_mode(cam_id)

    req_source = request.args.get("source", "").strip().lower()
    if mode == "nvr":
        # A local archive is an optional backup of an NVR card. It can never
        # become the default or be used when the card has no local backup.
        target_source = (
            "local"
            if req_source in {"server2", "local"} and bool(cam.get("backup_local"))
            else "nvr"
        )
    else:
        # A local card has no NVR source merely because a stale query asks for it.
        target_source = "local"

    if target_source == "nvr":
        rec = get_camera_recorder_config(cam)
        vendor = rec.get("vendor", "hikvision")
        if not rec.get("host") or not rec.get("username"):
            return jsonify([])
        date_str = request.args.get("date", "").strip()
        start_param = request.args.get("start", "").strip()
        end_param = request.args.get("end", "").strip()
        try:
            if start_param and end_param:
                window_start = datetime.fromisoformat(start_param)
                window_end = datetime.fromisoformat(end_param)
            elif date_str:
                d = datetime.strptime(date_str, "%Y-%m-%d")
                window_start = d.replace(hour=0, minute=0, second=0)
                window_end = d.replace(hour=23, minute=59, second=59)
            else:
                now = datetime.now()
                window_start = now - timedelta(hours=24)
                window_end = now + timedelta(minutes=5)
        except Exception:
            now = datetime.now()
            window_start = now - timedelta(hours=24)
            window_end = now + timedelta(minutes=5)

        try:
            searcher = _search_dahua_camera if vendor == "dahua" else _search_hikvision_camera
            segments = searcher(cam_id, window_start, window_end, rec)
            videos = []
            for seg in segments:
                videos.append({
                    "name": seg["filename"],
                    "url": seg["play_url"],
                    "download_url": seg["download_url"],
                    "format": "range",
                    "started_at": seg["started_at"],
                    "end_at": seg["end_at"],
                    "duration_sec": seg["duration_sec"],
                    "source": "nvr",
                })
            videos.sort(key=lambda x: x["started_at"], reverse=True)
            return jsonify(videos)
        except Exception as exc:
            logger.warning("[Replay:NVR] Lỗi tìm kiếm video NVR camera %s: %s", cam_id, exc)
            return jsonify([])

    prefix = f"cam{cam_id}_"
    local_window_start = None
    local_window_end = None
    date_str = request.args.get("date", "").strip()
    start_param = request.args.get("start", "").strip()
    end_param = request.args.get("end", "").strip()
    try:
        if start_param and end_param:
            local_window_start = datetime.fromisoformat(start_param)
            local_window_end = datetime.fromisoformat(end_param)
        elif date_str:
            selected_day = datetime.strptime(date_str, "%Y-%m-%d")
            local_window_start = selected_day.replace(hour=0, minute=0, second=0, microsecond=0)
            local_window_end = local_window_start + timedelta(days=1)
    except ValueError:
        local_window_start = None
        local_window_end = None

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
    videos = []
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
                local_window_start is not None
                and local_window_end is not None
                and not (
                    metadata["start"] < local_window_end
                    and metadata["end"] > local_window_start
                )
            ):
                continue
            item["format"] = metadata["format"]
            item["started_at"] = metadata["start"].isoformat(timespec="seconds")
            item["end_at"] = metadata["end"].isoformat(timespec="seconds")
            item["duration_sec"] = round(metadata["duration_sec"], 3)
        videos.append(item)
    return jsonify(videos)


@app.route("/video/<path:filename>")
def serve_video(filename):
    safe_path = safe_video_path(unquote(filename))
    if not safe_path or not os.path.isfile(safe_path):
        return "Video not found", 404
    return send_from_directory(VIDEO_DIR, os.path.basename(safe_path), mimetype="video/mp4")


@app.route("/download/<path:filename>")
def download_video(filename):
    safe_path = safe_video_path(unquote(filename))
    if not safe_path or not os.path.isfile(safe_path):
        return "Video not found", 404
    return send_from_directory(VIDEO_DIR, os.path.basename(safe_path), as_attachment=True)


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
    input_path = os.path.join(VIDEO_DIR, filename)
    output_filename = f"cut_{uuid.uuid4().hex[:8]}.mp4"
    output_path = os.path.join(VIDEO_DIR, output_filename)
    cmd = [
        FFMPEG_PATH,
        "-i",
        input_path,
        "-ss",
        str(start),
        "-t",
        str(duration),
        "-c",
        "copy",
        output_path,
    ]
    try:
        subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except:
        return jsonify({"error": "Lỗi khi cắt"}), 500
    return jsonify({"output": output_filename})


@app.route("/cut_progress")
def cut_progress():
    filename = request.args.get("filename")
    start = request.args.get("start", type=float)
    duration = request.args.get("duration", type=float)
    if not filename or start is None or duration is None:
        return "Thiếu tham số", 400

    input_path = os.path.join(VIDEO_DIR, filename)
    output_filename = f"cut_{uuid.uuid4().hex[:8]}.mp4"
    output_path = os.path.join(VIDEO_DIR, output_filename)
    cmd = [
        FFMPEG_PATH,
        "-i",
        input_path,
        "-ss",
        str(start),
        "-t",
        str(duration),
        "-c",
        "copy",
        "-progress",
        "pipe:1",
        "-nostats",
        output_path,
    ]

    def generate():
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
                if "out_time_ms=" not in line:
                    continue
                try:
                    ms = int(line.strip().split("=")[1])
                    yield f"data: {int(ms / 1_000_000 / duration * 100)}\n\n"
                except:
                    pass
        except:
            pass
        process.wait()
        yield "data: 100\n\n"

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
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
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
                completed = subprocess.run(
                    [
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
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                if completed.returncode != 0 or not os.path.isfile(output_path):
                    raise RuntimeError((completed.stdout or "Ghép các đoạn NVR thất bại.")[-2000:])
            yield "data: 100\n\n"
            yield f"data: done:{output_filename}\n\n"
        except Exception as exc:
            logger.exception("Không thể cắt/ghép video NVR camera %s", cam_id)
            try:
                if os.path.isfile(output_path):
                    os.remove(output_path)
            except OSError:
                pass
            yield f"data: error:{str(exc)}\n\n"
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

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

        if _timeline_playback_mode(cam_id) == "nvr":
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
            try:
                yield "data: 5\n\n"
                if missing_duration >= 1.0:
                    yield (
                        "data: notice:Khoảng đã chọn có "
                        f"{int(round(missing_duration))} giây không có video; "
                        "file kết quả chỉ gồm phần có sẵn.\n\n"
                    )
                for index, piece in enumerate(pieces):
                    part_path = os.path.join(work_dir, f"part_{index:03d}.mp4")
                    part_paths.append(part_path)
                    cmd = [
                        FFMPEG_PATH,
                        "-y",
                        "-ss",
                        f"{piece['offset']:.3f}",
                        "-i",
                        piece["path"],
                        "-t",
                        f"{piece['duration']:.3f}",
                        "-map",
                        "0:v:0",
                        "-map",
                        "0:a?",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "veryfast",
                        "-crf",
                        "20",
                        "-pix_fmt",
                        "yuv420p",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "128k",
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
                    for line in process.stdout:
                        if not line.startswith("out_time_ms="):
                            continue
                        try:
                            current_sec = min(
                                piece["duration"],
                                int(line.strip().split("=", 1)[1]) / 1_000_000,
                            )
                            ratio = (completed_duration + current_sec) / max(
                                selected_media_duration, 0.001
                            )
                            percent = 5 + int(max(0.0, min(1.0, ratio)) * 85)
                            yield f"data: {min(percent, 90)}\n\n"
                        except (TypeError, ValueError, ZeroDivisionError):
                            pass
                    process.wait()
                    if process.returncode != 0 or not os.path.isfile(part_path):
                        yield "data: error:Cắt video thất bại\n\n"
                        return
                    completed_duration += piece["duration"]

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
                    if result.returncode != 0 or not os.path.isfile(output_path):
                        logger.error("Ghép các đoạn cắt thất bại: %s", result.stdout[-2000:])
                        yield "data: error:Ghép các đoạn cắt thất bại\n\n"
                        return
                yield "data: 100\n\n"
                yield f"data: done:{output_filename}\n\n"
            except Exception:
                logger.exception("Không thể cắt/ghép video")
                yield "data: error:Lỗi xử lý video\n\n"
            finally:
                shutil.rmtree(work_dir, ignore_errors=True)

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
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0,
            winreg.KEY_SET_VALUE,
        )
        if enable:
            winreg.SetValueEx(
                key,
                "CCTV_System",
                0,
                winreg.REG_SZ,
                os.path.abspath(sys.argv[0]),
            )
        else:
            try:
                winreg.DeleteValue(key, "CCTV_System")
            except:
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
    pystray.Icon(
        "CCTV",
        create_tray_icon(),
        "Hệ thống CCTV",
        pystray.Menu(pystray.MenuItem("Thoát", on_quit)),
    ).run()


if __name__ == "__main__":
    if not acquire_single_instance():
        sys.exit()
    init_db()
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
    send_telegram_alert("🚀 Hệ thống CCTV đã khởi động thành công!")

    run_startup = CONFIG.get("run_on_startup", "no").lower() == "yes"
    run_tray = CONFIG.get("run_in_tray", "no").lower() == "yes"
    set_autostart(run_startup)
    server_port = int(CONFIG.get("server_port", 8000))
    logger.info(f"🌐 Máy chủ web đang chạy tại http://0.0.0.0:{server_port}")

    def run_server():
        try:
            from waitress import serve

            serve(app, host="0.0.0.0", port=server_port, threads=16)
        except ImportError:
            app.run(host="0.0.0.0", port=server_port, debug=False)

    server_thread = threading.Thread(
        target=run_server,
        daemon=run_tray,
        name="WebServer",
    )
    server_thread.start()
    _open_server_page(server_port)
    if run_tray:
        try:
            setup_tray()
        except Exception as e:
            logger.error(f"Lỗi khởi động System Tray: {e}")
            server_thread.join()
    else:
        server_thread.join()
