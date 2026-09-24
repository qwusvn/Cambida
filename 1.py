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
import ipaddress
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
    from camera_modules.policy import (
        parse_verified_license_grant,
        is_table_privacy_engaged,
        get_table_visual_color,
        reorder_tables_preserving_identity,
    )
except ImportError:
    parse_verified_license_grant = None
    is_table_privacy_engaged = None
    get_table_visual_color = None
    reorder_tables_preserving_identity = None

try:
    from camera_modules.discovery import (
        discover_lan_cameras,
        inspect_device_channels,
    )
except ImportError:
    discover_lan_cameras = None
    inspect_device_channels = None

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


_CAMERA_IDENTITY_KEYS = (
    "name",
    "id",
    "camera_id",
    "uuid",
    "enabled",
    "privacy_off",
    "table_id",
    "sort_order",
)
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

CUT_FILE_CAM_MAP = {}
CUT_FILE_CAM_LOCK = threading.RLock()


def extract_cam_id_from_filename(filename):
    if not filename:
        return None
    basename = os.path.basename(unquote(str(filename)))
    with CUT_FILE_CAM_LOCK:
        if basename in CUT_FILE_CAM_MAP:
            return CUT_FILE_CAM_MAP[basename]
    m = re.match(r"^(?:(?:cut|nvr|merge|merged)_)?cam(\d+)_", basename, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except (ValueError, TypeError):
            pass
    try:
        if "parse_video_metadata" in globals():
            meta = parse_video_metadata(basename)
            if meta and meta.get("cam_id"):
                return int(meta["cam_id"])
    except Exception:
        pass
    return None


def is_admin():
    return session.get("admin_authenticated") is True


def get_table_by_camera_id(cam_id):
    tables = CONFIG.get("tables", [])
    if isinstance(tables, list):
        for t in tables:
            if isinstance(t, dict) and t.get("camera_id") == cam_id:
                return t
    return None


def is_camera_private(cam_id):
    table = get_table_by_camera_id(cam_id)
    if table:
        if is_table_privacy_engaged is not None:
            return is_table_privacy_engaged(table)
        if table.get("enabled") is False or table.get("privacy_off") is True:
            return True
        return False
    if 1 <= cam_id <= len(CAMERA_LIST):
        cam = CAMERA_LIST[cam_id - 1]
        if isinstance(cam, dict):
            if is_table_privacy_engaged is not None:
                return is_table_privacy_engaged(cam)
            return cam.get("enabled") is False or cam.get("privacy_off") is True
    return False

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
_EMBEDDED_REPLAY_TEMPLATE_SHA256 = "680491b9a43dfd45d79fa2ebb818db9b5e24f0cd4b6aafca5793ca6bc732498d"
_EMBEDDED_REPLAY_TEMPLATE_B85 = (
    "c-rl~YmXaAb|CtFf&C9DPInEnm1L1DvK}n4>PjWGr8`o$tWx*%SQextnJngrugN4!s;T0Kg+KVi!niZIFc!we#ZKRWVT^YTuk8hlm9Q|dYWRQDKe^|`D^Em5"
    "CX=O7&y06w)I~Bgo+nP6cbvF&<L;M7Pk-|Go->?}M+g7@-`&DbXXH;#I;GRF#GV9xA3h!D);O5^PH*T(aWL<czIyt3Ww%7$O#E?xwSw2PX*4f6z3F5gOrYfJ"
    "us<Jm`oU?~3o7h~=Y*4R9{Qt7-1A34r|vnjUS$x@JH6>?5Q(<u;d~SvoS!>AXg~5_U^mWk>HM#McHm(g&V#CWbZe8BW!2*OnfU5B?PxlkJLmj|0|OgPBj|Y;"
    "jDxl_3Qvafefh3Z>7KNm9}Jp<ok5p8?fFq(ykh?&ujav<IXtfo>N^eJcwQMV=0P9cZZ~(DyIu2byy(Ka-Ns(SZ_*)*7BH|M)cZS)LBn`nIf?v!2yn%bPXa$u"
    "56bneRzEoLpyKv!ZLsUqKJ{cHPQ6z9)HR!mrvn^$&>yt48L^k$Y19uQc-<ZJyMutdK8b>00xuekUaOUQQR(~9D;#}mx3<+!z2z{h4f=y-`km~u-y8IT9XcZU"
    "CTIk^gIekhnMz|oE6x_tY=pqv+uGU+=<>YwqX}QmZqPNJC;bhA!B%g_d@YuIuhFd4C;;N2-=Ds&#N)P8a~iWZ&My2Hopk+jz2!NLEzfB-Jf~KzHQjWraUW|n"
    "p+=*YQLSaEHXHe8IMf=fF;v^is=KGvJ&Wf7jI!d-06gNS=iC95diBWf9kbt`!v^-8((&ic<7n#K4P)%8lstI~P+&iUlWE|5^#Hy<nRci1sps4eMyJ6%?D?K^"
    "JA!@gIk7*9D{&Bo6493Y<1^>n=}zBN;_&C;1aL#FCp_AhMUW<W@S=Z~?BTH=orJJ{2`Y@k2`4UXXA1$SUUdE5tCMKDm;iiF{iuvvJb?ux2J!{2Yw~IU^REp2"
    "aX31Y1vsDxBwq*JS0R+bqVX8W%#h8(pCEM${WwT=0MfL$7fnZ_N;eq#ry&rbH*IG!odmgMpmTpQpXy-a&|s4u21vo$xW(hsp@Ld>F`rK-NNHyaSVkX8<0#wk"
    "U<k`FPZh)xaz1l^RvALGj5t;VjOG!n;mnVK))<wcW^WP2SZFp4f$T@Bz_Vf4?+26TP^8Z>h)>zM5ss1G`=T*kx(fK1EV5Wmm}D907oaj?x2r9=G5ZvLP}r@)"
    "g_+;)v*iP()GODwVJF>mg2`z)_6I=)Chk|l2{6CPd^&SohZSH&*@m?jh5ay}j@_ga1%<&VNRSkNRQh2Q^yc9d#tk4}j3;a0jsoH4XU@~<%=rQeo%q<8bH|UI"
    "&o%{sVd#~Kf0_W<J{~~clSu`b>o|tN1Z+v=yAyxbcDBh5lWWjyl81Z+#QfV%@=72EaP8AdcRooi_tvhZY3-q)5y<xzFuYdnQ{xq-3v|gT1{(z62lb$_x7AHA"
    "(@7L2dl(*p3}Xk2!k(mWb5qFYY#bq9>12FBrR%NO34m(YLDe229w5Oab7Hi<1|**UV@yJ?2*}v)MT>Da0X42Q?3rXhD2=c+Pfd%Hw(#e`MC)7C7Fh=N=CxR$"
    "?OH7b(ygs#z132WUk?Fr6@Y&)K-7I5`7;f{2czlhw!`U#KC<L(FdBukIE*b&*HJ9l3@8%<V|q>Y06qd{#P(VOlpG)!$Xss_FwCZMDWhNnJMPq6y|E8fS)Y0Q"
    "i@>?DodxAx0_9OKpQGdh@MHj})T*?eTm(ZdMWHbvnt*V&!Gc_B9WN3-*o)@q1a>sCv15mfn}M0_fWb5ZU@m5}AnGMZpPpH*x|^O@!m)9<t}%_?V$MNciD2bF"
    "fY|+X5zoWHSw(0w>0O_-`Z{}RZl~y9GDO}a>}v}Ks^YT{i9c-oN)+H6l1U{)@Vhbajd?PJi1$u_j|)f%CRqsf73L89q+DrHbwNQboG|pcI;T*(6<KF}E1Ra*"
    ")ASx5LM51B*q6LJ(No+!7B6+345<&_yLNh41fT6hUM^dII%gfE*xZhV%k30oa@Hxj7UBn@`52d^?;OGKL4I(`Pov9s|Il;h;pN+ZnK_%zH^JB$UB3M`bQ1y@"
    "%W4emvD&$<?;iwv!C<hRHROhM<*AHdXR`PMHuiXixLDzm5Te8&!vA#S%8@wgX^}(B3&dN(3)L2FSWvE(Ik`rLijgN{7*+s^YRyc>+Nyo{MqEY37%VCF6_H&0"
    "(FjO$!z!^*NXMrqsC=M=8xJGUFcGY_lGQM-_&ubdc6_sKR|#6fETiw%7G1_SLUTruVGGHS(wG~2o>Q+kL2c$b^H#&Qr@jvR!PI%o)NbeV2yIVl<8Wo%Mw(OH"
    "AyeWJ^Z_-VWNqUaNGpJR&=|bh-rUQ?%PiIR2lZNgw_yi_P;RL~!6htX(HpXL!Fg!vY6#TW=g;}qAf*|P{rFWS9Q!AL<nkGg)gL8>D<&9%h7ZE^E27<znni15"
    "Noh-ceNJ%+J@vCXf`zHXa}borDLSpK1JO1=P4CL?Xxe*~EO7UyxMqV8w;P}%2v!9bMq5y+uIDlAt-jO%T!&dy%XK$hp)v(=AWRFluTiq-4gCp1T9sC>7ct6W"
    "!3ZW}EWcdO>=K!{V=Rci0upq|?jgdEsK_deram&2LZT2;$0<R-k|O@WM7Ex_Ke^3r8;OtSY%%(?v#pCY{RouzK{%QtX96-9m7CNYLP(){_VY?O>7%Z<M<iB8"
    "(ksC!3@<hpjL`Qf%HQ*|Od3y@Aj{0Nua}Nz;bcMyq}2Is<^=7bWXMbf6;dE4+AXwmsz0DZ@4Dt*LZe5H$d@B(`4gZ(92{x_sE@?L`VMF{Kwm~4Y?zWlbnjv+"
    "6XpAIW&6EgchJhy0I5{MbtA>=P<q~ytd1BAT88fQf4mCL2FTW8GG)+BruZizKGAdzl&8G4UF!!YXmZe)<d?wRP)PxzfZRPcStMvO{S$zY#_Mx-FVkBzwR;qx"
    "#b0}*kbO73$u%s$n$CiVSFh*jEREc)t(oJu8>_bVcE;9DNjU<Q>czD0oZYV^vL1S4P#9y&lec0y0;!_#i%}d@`qTmUF*G3NL2@(1yc*LtEd_Np#OzG)4b*bL"
    "{<%M%T@g2h+Pw-nKWtU0#Z7ZnjHi<+v$A;^UJDChXj%xvE^#*%KpO$W*FY5_ki=fCW1wj1x|hYub~W(rUD1$6fiBn}nxXdA1>sIH5M~Qm61YraRro2rF2w<?"
    "iyaccH5-8IHB%MYYTCGSGlvq^ic`X^50-J#<{`Ua&~U#W>%LBVG&e9ojXFUMf?_+xodli)I|GXLA`#s(JPI~^;_d}6<K<x5Tf~*qFb=!aFieNg&H#MNEzA}z"
    "FiE@=)5RRQOs3!KA@ZhU5K9)ZbL2;T!`MX(TteNzk}R|%&)y*87TlHgNE*f97P4$AtU_9+8-cFDRLz(`r5ns&2gJUxT1%$TK{4xtorQ!RC?SJ4&q3A;quwZO"
    "BCXr#PQAIli135@w!hW%Go=HyKO2CB^=&}MohGAA1al4hm31~YPfsBu-%eWCrznLXHExy*r(qvUyy)f)MHehG43yOCQ_ot$*Wn=K!eJq~umHNWGCc;39G<hC"
    "8}nh`n=4_hDQiXK>nt=;L`oXs@AwhXinvcln%e=KNQcowM4nu$8_P}`(k6-mHnngxvRKobMI1Q!q%zaSZi;*vKKx7*XFPWIKb}jF0dY6ONxD@~O-tO`8r8`2"
    "p|5mAvz6l3c#Ab8aap0;g@p}W&9J5<$%aM1St&ggHoNM!#j!sLLDgio8Y??{9nPV+j;w`zg}Q{3<9W=nFy;FQ%>i2DJTUZeFdzms&NQtdG|lF!=$rx}G%Gjj"
    "`Wte6vAfU~&O)VJmYsml4(`G3z#sJXjaZU#fc%V0mn5EqKh6;)O#tNs*^YsB7f%*|#(hg*8`2W-r&h>wYHqWcKdGFp71DTh_rcNx?|eEPt;+`)4g{0_l{n#c"
    "4vaMTkw}lNbfgEF!0g)NQHlxqwd_Hmpt$a!z6Ypk#F4QiWOf+y%Bu*&1`F@hgJe=@lH*WVp_bJwQlG_iuO#B>BI@BXpPT?`$>udb7}N)?!QQHXxuTRf1=Z}>"
    "tCagb!#<Fn3EG1vr%~2~^f-Arp>DIaRoly8r9T+-gY8{E3on)Ai(b`L*tw3|j<bciAI-C3N%F_SQ7;)${>~8S%H0*|K<=vE^vLf9qYRC0ozP1|k;_mS6{d^f"
    "QCu%fm2I%epOxNeO6ZkayOF&f5z+%hzS%YCoQga&Oum{vlTc(gr?;(nY{j5GR?<TU!_YGg4h99&7A$9DkG`rodltP+W6Q?iwyzN9)ZA&cw)fUkqA3=Cv^b|M"
    "Niio*XC=v8y;aCMNm;Xi&X6*dn&C;cZ8y84Rk2hV5_@Ju<my<eEdO$Cx6m-#ZGSM=K_k|%RN60~hDs44?IYe!d**E62D&ePRos~=lV{T^;7WfWRS1MaluBr@"
    "4ZAq@eN(P~V}yvT0y6`dHkXFY-rle|C1|Kyy7iAs<&u7`k;q+@me~gL@km)KP0Cu|BPsj&bUMD)lVhudW9%?RH`FOln^(G#Yj$3Px)m{R8T#7#=B;8b<V<XA"
    "y6bCp6r|i1EO$RHOtS6SFH0qJZyP$@-d3+Q=-Z))hXso5##NqPWr+J)qz!O3oTm}=<&+<crwiwye+ErBFvW*VA7S>or+DDswV3U=VW9vCPVCUlHF%3@X`pqp"
    "&%I?wU0;ty4VJU3dGd1zQW6`|FbR@okT-U>EmZj%<7|;4Sytcob+g?p!%opizI9I^6gU~zs^y$}+sd%?8et`=gA2rOo$oPP8P!Elr3hEch=nh}>?*A^w_9;e"
    "&BP@^$Z9F%N)deCev`x!=WSf!KPgt4pRglkttYf}0wj~4tmSXR|F5Pa*~BMpon{2IaKvLRwS+Ze{c5>RJ(94YfU__E)PwyjgUB^w9%)c&=wf6)tCO0qIURls"
    ")}pppC$ep!G%}<jOD>^OaWD&j4WYfvNcgf>WBFbPG9onSPoA9s#MZ5?Pn`;O=z7i%dfk32sOP^F&uxM_IhRqR2V1o=g7N`896PV9@OdH85ntCKOKj~bWB;eq"
    "s5r)Mt;w4T9NQ~ol+7l=mg%&+0Sqg+vhu)Ys;x?zm`ZG26@X2Jx4ovn)!oe=xBMRVC%SpO2&6wOUMNk|^s!WCQE-|BJ;?RUDm`JxHLM{LDb}3>3GB4erw6sK"
    "Vq?oT6zF?ISDwV62H|KN&!cd5bt>1|ElA}WIpGD{vts6`Ra{KD2{Q=i(wujt7f>Huah7JC73Emjlg&^KRv8@_TjyMz&Ce>>(V*c}n2mk*#?Krd9#`FHVxGd|"
    "tT%UHpnE$Q^1FotZCb`z&-F1PRrIVrG3C~?m>$ejLg^F>c#O0-2DEI+XBy4CaQ2<L=j^s%`ZXTx+*%dTUZ(-=3Odbo*q@x32+vhpHxYs9(Kmdn?L~b*S>C7m"
    "5iZXpp@h9B_ByXCO99#!*IC1YXB!mkTI((%hZo2<siO|t_6Xt((MzV+W2yFlJvIj0PLuuE?Y4UGL(jWMg}+8Z!;D0K2C*Xrd82EPkDMbA1+ff7>HfM_f*oTy"
    "nidwaUCem5l7+0c<>qS2&DIqm@_{B^2AEyTnVj7emhM=*BRQsvJW`dyhdomz<&)#l5sI!bMS&{`yGXmt(!r#!m)+je%9@HMo(?Ohd0Eacb5Y)A_HL!5UW?Fs"
    "1=!hetcX;bd*aJHgV4-G(M+=ehoe#$EGMtc8lZ@LU`%hl_Hj*=tIeqyw&B!7%@a#0h;!0lg?VGw)gwReu<hv_`J)jUmh(>+30~LZg>#}pGc&kyOF3sNL2b|&"
    "1XfO>Ny7bZ;17Z|j~%g4(CrU8cEryNGzuh-Nz>&d_sSN?j(dA}7ObUv{dCuc+IGe{IN7kxD@sqXq@7!q#WGzS89A~vIZmIO1gmv!^+qaNndm(1v0R#FsvO;~"
    "WOXQ|niBj-w1&brRbOi93h|;VQiRe_os`b3DXOiSr6F<XNI&?*4B!^^*~n6tTB}s+e#Bx`uL>iQ8B0o<4<>`D*w9CpZ~uPIv%@%BPVe&FU(L0wSfUEhLa1{7"
    "Az7@nOjdc}*B=rnUN7p;EEHv*p`0&M(CJOnv3}x&<F=We!k%tXH@p{^x^)okksrfG7>1)hqyDr_EddoBxDl#&U52UDA{RJ)M>kNdNFD(Dj6nCF(i}M%E}D#d"
    "k#rEP>_O}nHFv5Fc$<wdb~c<l%&$xi!};RXsvVYbwA42=NE)5rY8DIu|47Hev||GWVN1<{Y}53E-Zb(pVps93Z7C*cNrI3*@i~h^K;tu}28r(<mg_8?W-r5="
    "dPQA)i|XP#?1#VGY7KT$;uPiH+gc9x7RrWOHIxmvb?0ghSk##!W%*%*XA;D5xvuBv9S6~g42MKR(Ut6#rtLQEl9QbOgY6qztzs~xNXI%yx!OcK%fY~uy|9pG"
    "&Nn?>0*uF+MbjRDT+0!r9}LevF)F0Erahm2BZUR4M$iV5;)5nabOB3_1G$A?`{8`uDdwS4sw=(KL|!bgoh=7EKW=79jcVGQ5NjZaKj~2hpG|>`P|!TTMaFp>"
    "YuA~FJJF3{R8c77$tTbJDD*4xai=to7D4H`Wtq{ml|rh?6eTVo#N=9ne9$R&{bN6x0FHUiJrKYn4=;c?i2&~suKk*qNawgl<C_t0lau!%FIp-~7$$di<Rt>e"
    "Vrw1z&N?Zi;iKl&Q`dvI1p<MLQ#nmLKDAmVJ_$$*by3ghUNf&ekY+Div}B`E>`3qx&h2WurL`B-y16-g3T4UIP3<f}Z34teOLFyWw5zQ`yS&r`^^~z*GsFyn"
    "UcbMUJ_{Gr8(Y1r9*Rk14ZiO%#D5)snA*s9vBYD40z_~_=CX|fj6h1Zav#c6W~_`g1H6X+$uR)NFL$p=dCg8_TS#-I$DcDb%_W_$zMI6h*|u#P?V*0t$d%I5"
    "WaSxBr|9+UHS6xiWqMVI4Z}c#r^2*-vz@Zn>{4S7X=`RQwMEQEBuPKJ3Cq<xQ9;J$I;<4jXc~r-0x1}jYCHoR)>oRO_{6ttf7Dmx_ar$oRh@(oM;Ik<$(1QH"
    "WhOHGij<nR^^^Nw4_dYc*!gdyy|2}*Sx}Z)C>ho^tQ#8la^61V84|7>ZLeqSvG_8LxOevdYHiEi&x4D0@;f)aFq6dR4|{KKuSfv*>clzvxChoWI@-vKD{M+z"
    "bBoQQfUagAHM{w5#fgxDxdHODgezpwNmC#rvlGg8xQSrKG$z_Oqhg<SAEq6S*3RkM${nAekY*4U>zSwITWim6Tlc(VQ@aH-X=R&k=#Ht?YK3emx(;0@;Rrby"
    "?yuVvw@d@p%-df$F1c`$_nPf^vu=S^m#;Cus&II;oqjOz7o)i?F3ajnW$SAxT&R#OtS~#iN+`B*08{U4#z4gkNFcljEnb=RB8SHmQ?%81QfHM4<lxZk-&Al8"
    "zdUySXaVTr+yT*B$;#n0uAJawT_V@S<y~zf=I+q+Svbd*@uYw!_j-ACGsAO(!!?yM1rfYEHi$m>#NbX=E;ZYAS6kbAe!WoEo(v;<dK&ROn8hEMfh0F6T&`K5"
    "m2a7{5BEAoyJeFJcg@^UIjY%}PhZM8iG7uyTT&;Y9GQU34Xoyyf<CofTPi-&foW=Ync^f1;7DU?W4B3C=3M1kv@Pq6cX!Vw{y6M`avz7?aD;h)pDacJG1VCv"
    "-k<gTxnDuM@^msfd+ufAffr_{tk={QlT}8yUStO>@p|pabY7N~U27uxRZZk(vA0{TSLDHmYPzh_+}z5pqu*;{F#c6N@j!$XH)^o9bFbeJD(g7thrUxL{AX(i"
    "uTxES7QcPfNYBGIyP2AYKU9jW$N`#;9--$gjVfC58XB9@su?x6^@=o=gtBfn^)lMIaV@6=QK}8NBqP|X+2A67*`-5`vDZO`C)B{Sk!>Xx$2N;!9GlE8<NnGt"
    "x1-c5scy?stWn0Tnzdfet#I1xn@xT4rs`oIl_`SrCY8ypMlSa)I?;^0j42C8(=-Mb?I1HjuP)}*^5N0UfVMr_NNakhah0Z1@fG^=?b_P3$)&lHDcP_VYjS~E"
    "tlcA7SBxarbI2IOvFXz~#%=9dc7dL49%R)xV1X>B{HEnc)4o5VS>{=Z%9uSqCoa`N_$KJ@Ct<cE&Kdq!ESfDrV(l1()@jk(x6?H%^6ZK>ZWV?=hMt?!zq*Z~"
    "4mlS)Wb}^Ly-iQFzLguA`k&Kxgfr6X?x*EatAoLy%*;~3Ul_rv_@Y@$CE;Lk0M>h3ZQ^e;=ng<wAQopf+s6Q9dB;`-bpCydnOPM)x7e5Yop04W91Dr4aF!_("
    "v$`FQ1_bcCn=Gp!;!!1IZqNiF{Fg~^8sx|ib$g_iqZM?M=+8lGx6xQL&{ZF)3_7*DFoa05la4{%CNZd>_it_anHU+wJ)?tqst4x#Fx|Q>5=GnH>FsT$yMT2T"
    "BC!sq=3r;g-8ZZ$eLo%s+?=u<H1@5oOPhz?%G9hw5kZhCc{Qy)r7bckHHF<utst=L?{&MiecSc$IVzBKl&AiRF*5hI#ffE(kl%%nC}z7myMe#2U#heSE973K"
    "<If+#kIIwaWHG{2{YSt~rz1c1lFOH}@JKOV6Wg-_hO12xE}Ym9tj>j0f89BT^~}zoD=h~&iFvW(P9Z--&EE^e359xn*B`V*_?PD1w;1j%7^9mgtoEQxlWt<K"
    "&%N5Uj^|UiFb2QbLaL49G=e2F;kQOCpH_7=je|_s(}Np5U<|ziYoT(IOAxpG?e1=4HI%nmsC}XN;-E7^l=_3lU?&T$hao#`klbr;xot#^TjUZ*2f*;gZ*9i&"
    "vr%yH@BiJcO*A3mm+rKGhM%asI6c^Eai;`oS?PdPxKXJ%PZ<k%GJOp_N6r&|N%{uA6sS_+<@({NtgI<fB`556O4v4Q<ybsD;8VRdth4vroXJX*WHX5}RXR97"
    "2d!=#ME;8j-U_^2!tm>|4bnBM6EC{F8fp4iKEtL!?g^@@A*jx$Aik+-A2Zv5Q-`UFuZ^}bd+}TXK`M^eFTZz$7f^HJ<_L#>i0kG{mU(cMjZc7oXV)RiqEMJy"
    "Fz`M5_E=QF!ZEkGI5UvzranSHnEPmmDxu5=2-+!a#uI-Q52y1@04oRX3)nEr<spWvbTB#qVRdVhHJ<buj!#&hXdpRe_7OETVlyc@an$RSN<eSsozngH|8+8S"
    "F5mtd{?4^4()p4)wEvQDh9zAhRAiSA`L&;VpCkED)TD5vSEJ%|aD{IH6PegrV7|iA!FRv#CnwGv1{_waRoF35{D2~deX)rBi>@1@$sK%C(mPg{H{!5(7woU-"
    "ox;f&f*63X!C+5=#c0G)iU<5F_}H!+D>?JCS<orTUo7mAi6x!V)9L$PO`I|Q8RDuzS==<17ckn>6E41XO7-26pvC+<;?SMxn@)+H1Z={;5>j~R05zl%J2&wP"
    "S_Qg^=MRsNcFCs|(PpFCP>&Fsd;Sde6cSAB@lS!|%I62SX8wHW06-q?I$QU*PFs)a+s@Xo1z&cZMjf71*rP@ro>f|<O$7bv3H;^2kQ-53Ye50&^%Ui!=@_ID"
    "Q~?+j*br2~m4v{ghs0RIaYH`B=E$bTu8_>u+s<7ORh(Ja!{fZr6>97r>upm|AbzWfBhUc;@k9b@1Q}xx7bTROXo?FfGWzRi@ZB%M%Xj}_;k+8c=bufS$;ta)"
    "ojIrPf93RsfRN62zr1|+SI%ts{@Zync)dUUZh?~8!8olS;}cTP*iC5g>~lel+rsG819IFf?XN!Sqv7SdzYv4fn@7q_uyvbY>-IHaYZk5^+(Ji?1FdhBoU=};"
    "RkCgx7)JC4o7p5tjL3W1L|lfyTOe``p^n5^bACu#5XcC*hX|0;L8Y?E{#7c1g5<ztiwtJ#`gU+KoAI`@=WOHuE8AI<;5Q>NipJzYL>5KMlDm^61;Zz$>^2bH"
    "J}=yk$#+b+O629RRlFt*Se;C#f1iKqgac>j$1gBn%%@BH{z+$W;d_Wbf65+P_7PzUwB((6!V}p3(r)ue-$-Hd2*Cxu`^DwEzYJlAEiT{v-xE#%oYCdmZ^O3p"
    "aM}Z<v3xXq|96w2>ufq-eEq~J-vu^?<^FMU;_AEgi*zHoo3=?1wE2-YZ8n3Eo4LU@b3<+BMlBiMF4@a^c^vqP3XI9Tsdq|15zhF>n+AMTYjt>l-)yCjmgvO_"
    "Pzs75f@0{4Xhm*~Q)+vTqYU2|0$G`%J?L|eb<FyMrSnG%C^*8tO8FZlnDk+<XnTZUIBrQp;9<y}x|K|4Alc&2H^Eq!yo}w2_^5aMpoeWp9BmOCZGChxh;WK#"
    "br1N%UbVL4*U=xyZD>xdQg2r4%}4BwgN5|h4u8E<-Pv~P+to(XM*=PD?pB+-kJw`eOVD=?zC#2P*NgUGl64qp?~H#&4CDD+wb|KlQm3&Tu1}dHo?4F-MyBvF"
    "C8tY~3RJh_SrGISVd_sX<#u7YN!;bMmfSSIeD}+qkd{z-nhzI9@BRq|e9>-}{FHH5Pe%H#Y-!4JTTJQT{=fdW%Xk0J<iy-@8ADLlEaV7ZvdOemNO!Gd!OG8U"
    "?nV|aq0Jo-LF&@twVE|P$7|98#h@N@X7c`jg!<^nb6s(1kR98&t{vN4m#^@#a>^+Vo)N*RzU$O?Dy>S(X`%X3hhHQ4GfRb833x}P)c?RG7)+zrOs&zDfNj@_"
    "Z4tzq-@j!561jsQudr3v!c`Ez@)u#1Jh*z-uerOQ5~{;?W5I2n-29ZY&r^-B*y<z}<tm7V)d3Bvt?W_2%Wp~t__x>|pwTfOv<djw>e(Z!!?^0}kLv1UX=IP3"
    "0b^-=pJQoekEID?X@2vOHj9&Yd)m4?N%8A~TL^=r@-|OC|2O4ZSq24_FHv0;-H{Ni3`Gq)nhY=B{a;Bxseu^pIYIu|lsAvUK59Aj#t?L*f;`Ox(Sykv5PB?@"
    "VVVIU%lqE}2-O=->l7sRLXgVeOU5r7HYZjG`N*_5DC0a9xWle8DA8ub^#*8)YO{aV{Y-n1w~mxjqmh~XEqj<Y!0swZ*Jc^`Q=3gzR*8I7#pKCBdhe5(EFrb3"
    "#eZ39WY7;f@JB4=V0z?Z@!W<OMW`k1<9=uX><}MvniY@2KK3>DyDSa}uw363KYIV0WJfS^{%6h`(9*d5h8Jb}{TRinn)bv;C9$SGn@nG0se^3PG;Hy5LcM7}"
    "=!wJ$YDTZ+Z)u#P{uX+&zjSaz^SSV|+7mQ#$CvN^&goyi`)k<ztP|&cwXxmWynpZOqpI`uA_OhsAEkT9IR$mk8NdJbjDVth`Su@}9(4KkZzj$dN{o@{{|^V~"
    "?XPFfaC-UnpE)P|%TRj2V)Xf7N6o6m*`sFt+MoA^EK7u%GYt(VR>_nDgXdr46Xhzl-B%%FC^$!#Ar&R7+o`vh8|D|0A4(=LBKcGdGE9+_C<4?fhTnf{jwE8&"
    "*yd)9ZD!TjGK?PLCEDNQTct2GYEHAVTWvY;-=hWwx{bK*P_MSw!4!PK(o-oqPF5+yC&gU&T=%lLs<+w>O|+Neora~!bxrqB-q1|qmPml3#axuPBU4Z2EM%4w"
    "E3-+8@Te0FGlq3rEC87yi2}%v1jzRKTX>-^4Kw2*hHFT7y@cO=7FfV-o6NwTZCL!{g@2Z|aclIcKb^B<^@cn68%bOZ(^tOBG9!`z2K{(NFo7!ay@e7$j}O29"
    "Xadpe?@&O2PFO$xgG>J-y7UbKL=GCLjdH}F-SMMr<_ARKi17mIU*p^XL%xbqgpbbwn;!u8I`F97QDwWLyKd8#_dFl|72Nh89UHIi{rhOH{KORk=ykV{C;y{6"
    "N!Rh>6Yo3Hu=Snz*)dLnU>EWCUD?H_9DG`5x{vQ;1dse%_wi|H{cueuUtvma3A2Lkp4ua=?4eOwh*2>YpVWnWiB$RrpI#B0G1D;q;L!W6IrOaX|ARO058k|M"
    "^$aCg_B-avGY9rt^W&KV`K>we%z^yDgICCdm&N%l0_@X2IP_LI^b9Ed!Ke4pe0l`ZnQlE%=7T(Yq@OE0_edL8^6znF^olM%sQk!}Up)*4$-ve1m)doa#Tt6T"
    "=va13g3hgKN?o&~HKlS}tZv{wyJGGblQ?Njh<gd?eRV|}yNB4G*;K$3+7%5w`Cx}CL6(o{QzhMe6t`-R+r_^7<@ECHw-e4~z6TF0dU>|Q8ENZPPF7Eko>IlG"
    "XKQ!oXyk@L;o$o@kCJW8sMmCpUBlTv-5x<-2}x##iKnnI|8MA=g#HAby}<in!QTGOLK*h?bg21*-xI{JW)#lYmQ$8JNj`a@0<`?!A{<|V8VTCu@cr9fVK4%*"
    "`F#f>k(3(9{P7b%KsRkPPAko7eaCM)O@60hr4BzUjq2{!Lx5?$KB{cNcc*z<m2h@IkA-JnHyV$ig;Tx0gY_L%e`|!*E3`Vi-i9(?XROo+?LogrQM%v?-Cgjz"
    "l*BUCYHju}cbT#RKF}lczhle?Oni-q53MS@vqs(~l9<d%qCl&QlV5cs1S)%DpEE1FRvtHia)|Q3vvfsqdQPh;R5k7;eS>Q;djCI}qjYwVZS9qTDkx^iC>2M`"
    "wrF)Dur9A7YDI$!Z_ol2a$%4~v`9@y=HSRUcSQQqoD%=Lht1ee6A@@}PlE^?!GkzzCtFSw>4K8!<)peQyLIR&D+-FgHc{yQ<-5N*TQ9hW-GPu3%Y$jxWH65&"
    "vO^FoNfl?DH$PtV@ID9!F)DQmN;S_5ajc^)a?(n<<rXpjZe5%Cw{>+7_88FycwF;geb1o|Y$E_;*ih5uiAaDXz5g{e@1TJ4tI5#mz5nmX1&56USTh1D8FR!v"
    "Tg-{l6Ad6&l<WZ5>4I>R?)$%;IDJ++WVFvX#KyP)X5zs!YuHgL^vYN>oo)D_NX;GgSMm;)3I=@$JVy}AYO1F{VtK4e2i8M%;({BVTT<S0Q|=9?2(e!~%u)8|"
    "695_NFQK#HJi=Md`SIYaF|$UQIYPs-B&VQ8gOji&RaXqeR()oPYZ6)B=_6rBj4d%w=^!Q>VC40iS#!KCxs}ezw3%T$U%Y=icYYjno#UY&1<viq51jI=A;=H!"
    "|H--k^y%YcXA|X%|HQnfzt&)?)2VP8AN!{PYlcRu6y|$2Ov$j+(7x8?hs8PK?`YI$;;RRWbGqv{oQAv(9p(B4eNX}Up1Id&)6p4^vBT_9F@_a5+eon*&aR`M"
    "_C^H@k9#-xX>azk!Oj}5I_piBg)_s4jQX&Y9w7*$mv7${%4MMVmJL}sTLn-Dgsw4?knDX){ccbC(KPIvXzM4eyMK53dV)L9g0t!#0$BMd7<5WM5JnH<{=T#v"
    "C3U(zqSNjDF5pa~4noaoQ{hOx0^jbp=x@ZCrWI%KhET*A4A#Q$R<yx0=@f6avIVGvr@F3V5tIB3B={Ldwy}zt1PQ4i2G6Y{Hzfgc#m;<#ob^}0YGnvR|MKmB"
    "0k+B6Hy~vAc!(_2Mx(`EVHZ*)S{J>GA}hedyq+Nk{l4EYq7@-+mhds8qi<M@8_#pWL)x6^EEU-XNVhx0yc2e0;+p=^`)_+gA@RKb)zo<k3j>p862j%%{|^?W"
    "%vZ#1r>q0{K1Aa#iERI%Ua({(m7*xQ9HDgZuYbRA<{=1Y8A){T*`3R`e?4|4i2icMr8I#psi`>|ksn3-j);9U>M&1YQkL6Px)}0&0yRWqAZ5M#8=qaB!tD?_"
    "z^$&Grp9KG$<R(@vSyy4nWh8eDIM5b_L4+dll+D`I*UDRaw*mD9r-402eG!qQy@$0y!^%Z2P+_$2QkqDN9msFoeCYnW?W3C`prD>Lg+x_w@3#x%+SMJ>vema"
    "Ydu92v_=$x0CrUWddF`$EfHCWv5BWmt-MY+3{)I$+2KlZJX+S}5vXpj-v3>X3mo6h+juZQlzZ_iIO91ax2OhZ$CV)Nl@4&D{-)<lzWXz@QOPqIe0vl9um?NO"
    "hyBP5r9G{G#&s^={UyBp?l(R&JPIw3_n(JHB8PG2*^yL&VVwS-IX`;;7f1J<`(IwZ{nb-T+;C#6rQrKfa8|Oldy2_=EFjD<<*wcqHnoQuSEjQwL0Vu)R0K4W"
    "$YvYq44%6~uy^OT6+~{Z54qR&*k0Q!W%(Smtp;Qc>zQrQxQo{PrY<>tO@5>HG2ElSoyKV$o5dy!Hj!;$)0c>=6~{jAg7?2EkgCU6Z%mK-kP`K*CC7s1nt%5T"
    "9^;cC8Rl<sOt6cgLht`}48pEXHn@j?vDJAv!5Evje?M_fhT+(G8jOOI$R7*-JHCAPpPg63_y6RReVxuOdutO<rm-DzEAB<%j4vbbh<NTid~kH{i{pDQZXZ2;"
    "@bx{XgEZ@h$cui6IpgHk<8f|IFaoao!+{SUKZLEgpKj(y_kIFxlv2C4M9LoBesu52?H7-}yn9cS6^V8MHi1$0{_W!znAXi0m&|3CJs{D^gS$x=;(!(@^yv0C"
    "FQC{T-Fxxq!53dWy>|?SzgUdBK~z4+^v7>rFtzZ-IGilzL41K}k(W!?+QRX@qc6X>i!D3?jIKiE@^;O$bn%(9U87Chz4!U;uO2>qarE%PV=|7dT2>jer*KlX"
    "wt*da=@HVtkG}jGpak#!&j(MR-h1-m5w_Zp*BkOOpTMIss?D7Pr$6m2@Y<N_Nie@R3h?Wlvj=^AO=`^iKGvDAD#!CEoSeXqDm-R>=yJnzO0^QNF$&N`IAWHx"
    "7-~Ix?g5#ML(r(TXM!Yh6JW=gEJh>!;R6hFfH#%8YFBx_Z_kq|+>J|~uqD2SS0wUuaq5MPj<_57C*q-M8aDO(5i=Ql9*zQb#1rtW<dGNBmhe?HO1*o)41e;C"
    "Y8CZBVa7^^As;@Df*IOUfW}YYWi5Fl4vvk2$Q<S%nD>Uq0SJ{NUWIRDVcCbi#Q{@KoWACVZN-?dk-=gjtp4$XFOTmAr?9KbuDE3dHewlx;&k9l{L}EnpHE=~"
    "9jFd`Cm6sC`z04zo-d+_!#1h7f<-*q3?KXb3$BI1$F#p$g<Uo;tH!E$&E@SASQ_Wz0tV&c2YHRz_3?HA*69x)`Mn2|c`*8+v#~)-!B%{Vw{SjYmJ4j6<~j#X"
    "og?7Vm}t+Rd=&>USzH7LUNv7aYz-8W(nFUiP~6Z#kyEr9GlMIGog7JSc^Kt!p(-4evI@NpC;jQ`Y8W2}u$LGj0Z(NEPu9zXP#ny;VV7|}>1Qp^r*nVwxHp&3"
    ";<NzxC*ad3<&;U-^_b|Be#vaO@OJ)Yo_@(#zPv_DP#a2ek>E+9Sg*-nOb|*2sP5qk2W1$an_MY6pH!LQxhm|=xE08Ca;1G*QX2lC5Hcq19pm~B!+2ix`+b-~"
    "OfEq$8Fya8C=e1ZgNNv!-`m`AUVd^uTYma7881M^MbK1np^mtv8q0BCc5k@2eES;=5_<O^xq?uwRvpqt{#rf>{Qg<_`&+<kgFhuM<^n{)czPOqz#5clpO#iG"
    "fX(<~g*fb$U=9R?&~z5Dl2BC}y}#LU>d6?=gg=4dJUTm1zpwh;DR5qQf7!!~9_7G2S1C^O*+>GSG;`Yjz7fT`>;>1`j$KUg+W)?=z>m)+y#yC#e2FDCAY=)1"
    "E#I*5BII8;Y?6fjT#6+b{5J~+wH2KUQRC)K8%}CPdlYvB>$#06N0Cb7Le8Nx30|9pV18|iT2|alIuoTbI8v)7=YyLfxv~`p3<Z#J_^<s?h)HD&P(3=aL+3mW"
    "PbU7Tojk1aCrcQ$V)V%^kGO`|QFZzXuw@ucU$Z$gH+i}Aqaix=g$qkraJX4V2ILAPs<NG+&p@~-9|n^XhM+=mVf}GuK#XE~xg-i!MyzDXWhhjmH__sCOI^|d"
    "j9YL00CaW`7g*bPc?%IR)HCet3BMy+Uul<I$|r%(i`godU2+E?jx7p$0m!Vl-)sA3OTh3Zuegpt-n|~;PC}+k`<8Xi(HZ3Nq|i%+1AI(X{LqHLpn)}9rwcD%"
    "X|H(q)!8B*mRWmxO*M2N-|TgjufcwLP(1MnNGDXSGbNa^YH(9ueixKIDD1kHmV4827tu1W;a1JLNrvzlo{KXY%h+SGby}Dt&ql;TW&v1#2fmg$z&z(1v8U~1"
    "BP+h=@;=W$vN@hLOG|QrD%btV@@5Qhw?>C1#v_*;u@8r3xtKMhIfSylQ#h4Gk=aHit@Kwe%|NhHfDWVm&u}o$J&SfJA%+F2_|1Tfs59+{gD~i~F#|$%GJRck"
    "mpU~{YY^pg97WuIh9A$Du7yxNjaUYp{^IQN-Jj3V;)HZ72?R;#l<>qe!Lvkq)<p*_v|mK}(4nOwZn&_J3c%53s@_rxK-mDheOwl*#!(Mi{p!iXDyYT)yf3>y"
    "4SI9<T}FuOJ0Yos>b*uRgmm@7&F#j*Xl%P@nE=2lrmH#v!-x_hrM!p;h5!`!jg%WccvdP#rx^$4ky|;)Iz>^)k0uG*8gdS0DjeL`{pB%_A!-9=;wbi08>W-B"
    "VF>85Mb08}YZ1|uHfJa!>uM4?DhwAgtpsvi)Xqlkb@418kyG^0f*DI8**d`7B4wj|`ry&MhY!BE_u@~!{PGcuqPYv)YHQ!w+;r;O?Dr5K3_W>OAOO+Xa)#4I"
    "6id^e6Ji)&;K)OIvJYvJJD$THMYgXjGyKlMa~4DXpx-u<b9$;J`c54V^qgU+y}{#Ep2N><(dXiHm*?=ai1L*<t>`&FX9q}W4RNHDzxr}9H{ao*T>i2xAmusJ"
    "!2s{$1r{wM=iAkt*3uIK_hL>bnmVYc&((GoQcc%5P)28*KGH@ft)J4Q^G=_p)uwJalB~_Xo;iDlYt+aL3y%JIg$l^9ehdQIBY#$Qje?jo?`Ml3o^xAhUU9to"
    "7OVUiBm|I3a!Q^+dy6Q@9Dt_wgK%>!?k3@8W~%53FNY$(B_9-HO~2w<jyjnJc>BdCM1AHB0P$yEs$5uA7R}_DZlTp&HOMR`rx9vy{Il(dD^>OtAxI2M!|7}4"
    "TQjUm{1$CS?6i0dGGbQ5<t3{;V8b!YlB7Q<5PXdO6jRl^(;bmUXXx_kDAPD~jTshUTo3Zv1<2CUZKrIE7hUD<p=Na8hVkk=3ddzKn;S3)xxN}EEoSPzE*vED"
    "(HOchvjH<$S^TX!D~BhVfEA=oA}pjmmQLCmln=r_M`|!#%*$oBb8v30ul&2EeP`)0?^H@$UnUbL67DFRb*FfM6u@kGTzwtE7JsTuA-7l=VOj;gG$|7(x>YW0"
    "0cMVnARPhBly%Y2QfQg7VS-e+r57^RCPfIiyeen`$R;e)8Cj%cYeOxVfO?}^VEVT|n+22p5h%`mPAkkNIDsKT0tnN1@3CwpcoX#Sl0qNUNrpFQawA|W<ITE`"
    "l8=}6VQ>-4GK^(<4~Fp<r;#G2l4My3Z9=;CjV7Ql6M((|X}ElvG$Ndqr-w{VzPNxd1EGmsT$D<#*$zmrF}tgcHG<pk)D=xJfmDil9pn+B+`jCL8*ZvF9si4R"
    "A1OO-in8k-V%>H>YtT>p$qDO_Ztu85#&TF?XX%6f3xDo{Ag;fI^@itca`2TG+Q-4i8yf-qJBZ~R&RH|<*n@KIfNVA>Da=mLNMD@2d=Upd_n>C2tmYpl^SmjW"
    "&|CPdjyrv+5wCH_Sh4DuArvNp{nP%<7v(?gpEs6n#rS+NpJu-L)3WVNbLsx6yBTtL+!$+%NeA@Ii-%r3_F|ac&<n<15PN<9%o_v!8hU4eA3g7kJ%@oHS^S&$"
    "!n?@|RqEbNtZ@?>x(O}ajALA?Nq<Fg=<X);cQc6jAfd*s4(rE-udrQNHb;DnkY?ZAoAm_i__E-lEG_&EtBA#5D9vX(>13=n8-c>(&%+5&%<{zD*q9`EFp;|;"
    ">X;a;1{EDDPhQ<CM~Ih|h$FGP$?&1QT{HK?V9XJz@1Bch`iJN@1C@r&{XS|Iu$`7|^6(K4=`bH3vnYS|_+`5!PkEP89Wr=kb;yD3E;ng^xOi*@@G*OY33q=2"
    "D_a(Q$%LEs-X0(|BwfBtw^Fh9SFsH3Wx5Z!+_;|S@TWtZs>D<%FaS$kT%g4<84vctN)ATTDJ!zMwT)Iu31a=_r}%Pno4m(|@Y^!F;~~1Qu61ZdI-W*AU)U9e"
    "95HnG8!NMXdJRaInmMj7_99Un=~4gUB9m#z?FI-{G60}qKsK_NXysh{wBRM4;`=HZA#o9d1e&slMi&<^`H45!CBX#G#((wX0mv1z=>+8)gZ$fCCUG}<>wWR!"
    "h3H29zFJRQVEW@l5S=~tBY#|mRy~lfJg!=?b>d&e2;e<g5BRlYf2-l7H(K-oe5FH>?MoY_HhfuH$At3dvSWS4yD|sKL?kv11cgulqESa%fc+wp6Y3c#PlymH"
    "`WPp{NOb(;5YVW!2`@HFu64<S=#DQW5U!WYQ4ON$7#{mXMBxQs-QY2>v%_i^K5m1If^J$-Tmo1ACUYSihQ^7xLMXbpKxS@<hfqn7xd8g0@38>q2Ytl`+4IgP"
    "^1Toe*ssGXU2}`v2TM;x^5xTdgJ-CC>B_=9LC+^;*(+<E{czb|svmU{R%CL;*I~rK_?TVjt;Tw8pMr;3j5}fqEZ#K+WunS*8#93O>0Mmhz(;a=_#FdWSo9p4"
    "NP`J<g^4)O4i%h&N{5okX%zqtD*{-<WqMJag(?iBo-EXi;q)~hGL$AhMDs1E6HK+@&j+AH#7T4m=oiNGrMqv~C(b6l+nln-Wmp~j0#d&&OtVJf&NF&Bv}fd*"
    "_RL$Kr6ZD8h9N}vd>B*M2WaqS+45q@Bm!sT!jkd}YLj8?E4Ph?koq{1d$;X{&2ef8zU@_vsd&a6)UM4EGQ=$=je2+evvNNe!NwGG<um(48@elY$;E}nLDe2{"
    "T@UA`ZJP9U!6Hh`Wj_)*S4aYEBwQ2_0xYJT)Y&$CbN3Bwcr!<$2s%+D=T%mblDY}H${$yysWM#;b6lF0;2EjGq7QhmYgrLv9K?Q#gc2~{L6HY4d>EXOG9K&7"
    "Ndjz{J?EwC848T5mIxk|-R))#|L1y|@?t4^Qxz4nO>_}Ysuk_}PGh%LTjs%F4%|RFW)(aIKtM_A*ddmHSPT)0a-Kkj*c<udnQGLld3E<QEGNguwbSg;sk}AQ"
    "i0j*2Dp)l(j`-YM6GBlum<qHJn>b`o+eC%u0S8PVL@XB5s>%8BDKHGLs42ReY`UmeCC}LOjJXWLNuMF*?PP~Z>WE<(z~mnxvF-3EW%$V=usanQrv`+t%z~Ob"
    "wf*px7Sk=mAi|qBP5ahqM=SX(eD2v4Mq-1EHa2i<2OXg(@}IYu@~Alzk%>`xNOzt|wU`N??Ng7X&1;8CyW5WW(gg(cb`)Gr_l?IKK4FfREeS(YZ5!i~&&BX$"
    "t4rN%FAL+!@Pd`PP0PnM{sM%2VD=m+bdja#k{}$Xm5GVjwx%E<^fRp`Tnu;=cZkBWpP*D1#=(>=idf@t!k*m)@uKr!GC<qHnNjHr9T%YrJh4@{Hk`~y$s3W2"
    "aV($~nGqihLjeGhNpO((vFE~IqXCD{w2u?2c>;>KBAvcwj2F;7=!jl6HsqsQtaXa0yfK1{q*X;2Wp!e2=R#M~*)7pms%`PSA`7nQN;aeSWHu@rtFZa3Xi^yo"
    "MB5z16-Kx%0YAdRh<Nm6_op3`5O_%!9*zYma~r=bwUy7LFzB|2(zhr+>y$&obS$d}7U-Kpjx!ahhLt;=S~^;7nFU!gGO=w^<C5FSIx4mMEMv;1aQb}EMMLM$"
    "G|ZZ!etO}3tr6M1XhIB?H;`)fz-Uxf-p?6V`G(dFXuMjJw>qR-H-E^Mp0Pbk*`*OQPKC-Nyo<{}k^A#OUnki@+4>}TO=vcM=4y^X{!G&=OS=0xM_|D-mz|VI"
    "p@O&66uOIW)aTJCjAX`Tw!vbL8!1eh8?iDSUIupYtc==ZDv&gr3`;_s=0yfIwh#=9K<K3?Hb~F<X;??M+N)qo0UWI@?Y9<bDS{TFU=ZSQHU=E@xIg>i>nG10"
    "=5&9?a0PylYGRtu?g1O;A(5$>*&uE|Lo*<2cgbv*taB~^E}e>#JVU{0`SQ8P`d+p*&@J66*D%U=rqfa2PuyyZrrEOZb=}T^&!JT53du4R#K-gObV#@^UumT$"
    "v!9lwo?$4|bP;!W3(xq6;M$e?bA91u>5UE6p1F1v`Cea$Tb=ZQNSzkadKSd7H|y~+WUo1Cm#-C%DCX7plV`Q(T6~s4!yN%wQqdmHm5LhV^%5^D-S!i~$}qY`"
    "7nTGLoMQBV&hj+6!NNldG;t%<hA|9IIG~L*8Bv4uSg?j=06CS(06iXCl63y8TCJ+CJqLb~)}Ch~tcbb}Mx#!alfECG5*Ixd<xz%T@N=Ym-PO)lS>r2M($Rlb"
    "WF(IMxTLqvBN8!Td<Td?nd}Lw86qi1y*4>;rd8*80+Ra7IhVAQQ=i|KK+(C184AiJ=YRdPLm(j-5sgK_`YHy%W`E*78Xk$peI!JOk-D+zFA>(Fw97>J`*e1#"
    "9E?i;(TBjJ99SCp43^9+el~U-jC59Pn0E~}3<F`kpckey>>LQc&d^<=mB!F<FrX47eEhi8Y3{NwH#>Ec(Z-oripxnBCr)rwWXZ3L{hv;w5-DNe8FsnZ+;vT)"
    "fSI$=p`1!1`1QFn+mjY9WFD*Jqd?k;Xskua#scZs^?U55M`!r{zbDx-^>*W6etQm^r@H`ZUP_Yil)TLPdS{tNaExn8n@B5l*P>Gim^7L?P^^<4hy4k=wZSSo"
    "oE|f&tk}Xjs)l8SMK=t{C0iA3$r?>#$r7{cdLxoGaz=zjO13Jikcg5OISn_nglslry>6MSWq4=Qer2Vz7aev<_D(RsYBK*7omOCoX>}JU4V`9e23qMxer8H!"
    "21gG>S`TC$<ylp7E<E&<B!s27filNPD$biLSD<K7I{v~w`hb%Ap%0lcy)8}gImTC3zAWyUQtf^spNM98gQD>^KTRWkSqA+m=!d?FlF+@E(kd+DW0dPsJXbbn"
    "Ge$-g*w~O?4{C?X2u{OrG$*DpydFs6(QPeq*%;w#_ON3_H?uQWq98LQ^J0=G@n2PJeX_uyX0v)f08XP^xyGPy=RBst*J?=EllZQT);Uw4&YQ9=oSRMq-90z!"
    "`_^KyD&|QiPnNSKjE^|HZ8Pv<)}TTxp$dt&8yo!f#>T3#0v?K%jNb7*@A${VK%DmxT}QMvD(CR`7Z*43hpyMxrH{NdNGz^Ahe(GUH<G71=h0b*1@hu}c(HZI"
    "bDkiZ7}}B_0*x1=IrA9vuN__;Q-GXlj$c^|Jp-)d#zt~d6@Ie#UEQ~QPTrJ7|K11ABbf%eo+YurAxG^m;-F6p>8Gjra}$<wvBU!KQ21vhjA04-DrK}M9*7@3"
    "f5IJ?@*5uSTbv9_i3Y<GTOEj@@d#W^m^rMnA|6{{ml28_I&vuK1prxmb63#~F?+KZ*{>2WlmdI*^G-={+LV<(5XrdhjPRDkA8BV0S&T%IB3bZIc<&5`BA5U&"
    "I!5U>WqIbpYu5@nBn5laM~J~Soy;g5#vHJSB-pw~`&l+qe}1T3-G_%Y5}%XhDOFY#?2dS3SApU)Vc1^@f!lK_2Y4W)LlfUf7{oZ4jxjY+_PHmIRe>tU3RaVI"
    "RvbR^H(_dmF!tsh`4VTujHlt`<cM|ugvHf+T)igouUL&M-LA)ud;OS|U{5~dBfyhWhPzixslsZbCRX7^`LQZ@Gik}@^);dwB%I_5G6!2RXt912sKjwKHy4QB"
    "Fif7aBWfJ3>;Vv9)(CtWa8^)`r=!!L-_c$79X94e-l#fJraAUa(AT_Gi&bP<^d38Mc>8oYc&*JwMZWzkX>p%@HEk~hKa#!>^<7l3X%+ZBiP1XJdC9(xp!8P1"
    "{K+{Tz;gBmKu~jE3Lx?$ergfqi9fau{D;dwbd|%j!DNSN$cb_`0*maP!ylUhE-#ZSdEzt%iS$+AVo49&{k$e{pe650L&U(bRMeQPl@L<3ONe-@N$UbnQIJ7="
    "=oBH#`!WefqONRgkc@Wx2TNt=vPKiM_j0H!X~A7pjyIkPX`)0S6;=m}FzF4aQQ2o79!ABoJT&}R41cP^Bk>WGaL@Vks?4mvFMeCrRHpv{#?DeEyRs}7Tu5;l"
    "-<uSf`&|{JBIQJfc8xK*)~;1sUUCMg&={GJW*I?k-!4I@Bdv-(Bi3+Is~>qy>29o$%eQyuTwSr=R#&Whc*}Ad84J7e9v3uX-ks&X(KHdfc&qzM!DZOb{mh|1"
    "fww$KfkG2@wR2s!hejQ-r}5<tPNDhWT;234M0aI3`WePs^C;!19wB;YFH?S5CHGQ4%eaX8lXLNPS$h7ATuA-Nc?3^hx`_`gbLMv?yp8EwuDUn!30}t>p_jF%"
    "NJjQaf-br9WeTD((>SrSB<Zp8rgVEWx}oV`nhu&-XYE<Obd{vfD|HU|=V4+}JrraERGd<BpSx=l@iEj?pZRT8y2wiB!_7>i)>#Lwj1+0`pEf;EYmB+s(fA8t"
    "tdX0T?M!9hV7sBysa0Et^|ts?KWxaajl*WUZZk>A7Qc5)S#!@{H>!XdE2l2$sO&deQ%W0iT+8`wPTsS)1v#3@1UO3SxosuF9H|jVMz#|B>1T{M%LIA+wFS}l"
    ">3dG`#Uo;;k+`Ma<>H7oYrMWi-xwB7Qzz5s6<q#NWJfrwH+ZtGZEFsqA(!C33XL^q;onAB<w|=1OJK}}NFmy3`md?}f)t+^n-vtS0wfpf3akB0>z5}NCjH}}"
    "*GZ&95>lb7>-@MT6Jq&54?4WEdroa&Wp`grlE2<hPfxyCE|cgn22uQ@=^^|~9zQE68gQ4+rb8YpAT3|&WH%oUT6|S}58b)hz62aJ?1b^G)Gp18CuDK3osA6+"
    "Ui@x)akP_vSymA-P0je>6w_0zgbOr-Cw%uS0;UOaEW%dMNNx<!a`Cd3JK3(7ea%vBQ<B!Eb<fn%47Lu5d2c!&29cn#>g)}B_&95rv<e?9DX*Iw*o$NP0BI#`"
    "B`KRrkh6;IXS@)kS#}`C4IEQIdt-xCOo%ohtY`(~6KjcttY^|h+9M^5OJ$%nw)tvqkYX-!l!ljqYv-)0%z25j%Ed*(eJ(EQX^uo|7AE-$Qbf#R1zn4PkjYw#"
    "GddjUYEDO|LPjPkyW+DT+e_mWW`~tX$$Str41F9<kO$QE&1|6=hKh9h&qB0T0vpMr=#m)ds2yVk56TSnmW(HtuNv|D%m>NYu_#!nRcqUyC1~dH!FpMYJsFOL"
    "(=kV+3a81DC|0o_jq{XT?Q{Zma;w9Kxv}w4<iw>X@3=_mP$|X6qSCC%Q!GBvYvC*`Fy2Zv)e`0L%LFG>Y=suM@v(&T)ndAx4%^7*`ciRGa<?AA)5fMB|GEmz"
    "<1xb27rU_`eX}eVj;pzsMTRFv3YR$<iDG>Yn&k8$Lep9Zjx?Hw8m3qzX~+yjNooMqOMcv<5qfKE9fuCVW?ryt4*ns)#6K=B^3j5fdj0K9M1!QAH5m3f0)X1<"
    "D+r36CghTtGz=V`b!>g%=69Pur55RG#Ob5#e9X2WS4QNc{k^hwC0B5;2l?@=>0>yA(1->^(vGSS&FgyBzg1L{$ZSUN_eVAme;6E~`;IYxh2$3(X)JeE3Pjm}"
    "NM)lLoiS$cRJwi3g9sCcYi^u#dC2k2H&mKtC96Dgfcc(36Z<;u`J<pxcQ+d@#(e%HtI~aTZ=j`?papM)%q)I+6&wM!_>C-arj<BjC4R#CbfrnCkX!o=dbH(_"
    "Gv?lQ9X23Ug#B8&FH@lpu{cX+fKZQj(6YWV9~>`4TySFFlHNeam4K+J3Cr*h!T&itb;k~mN#z+>1oJAm()(%p#47y}AAZd1Jm`yBNoE-Kyz_~C?}`HOM3rPy"
    "W$!T`jKwx0T##()?Yj4-UGvW1KX!FE@C23xkY&mB1T=*gQip9UVJoL3lCs2tzzT-YIpCipZw=prp6|(Qn8~#h>=LjJrlu*Mzi9`j0)fAQ0jeKAS;`vS;3S*~"
    "knw0dv1I%bbUYy>!aT&(c*2di>^^5p#)^qEYUn4CKz`<X-RB+tn$M0W;W$?(i%FaZ`#L<EP3L95`lix_{o_~9;PcFdT^aVjY2)*o@Emp=KKKbfIJ=1j-~nF+"
    "f{d^pavz?x<wg-$DORAvZyPAh!bxvf^5itxAeTuvqdC92R3}+K2X*0$PPOV!N<5>;H*Ma;v)XeQ4SVzx^61Py)a7!YZ<aK57VCiSdMyEwc>WanM;<%J``}6!"
    "7Ydh>Bt@>iebvGuHYN?h#!=V=k3qr4&{RGur0KlWkw3$EYxQWXs<WgBhAEApAj(aX`Q2dPFGlRfChvR(0wzc`Z5B)b$hl(n=%q?#f5Vu_(2w;2U2(!!-w6ku"
    "hC~#B8Hemn3lxbt###4?+#8*B_D09t8y&JYG!jkuj~Etz^O@{;Q-3KRdv$LsVRU+#H#f^Tii-XeA0>=We|N@e{3Kli7-3P`Z$+b(n>M``cqoTbypGzM+0$i3"
    "U?~hhvfZ9DXI|~hR_#M4ZrMP}o5eW6xq-PNI$7VfN*hWxNci)>O7tki@-{{S$5jN<AdZ=YQN@{ChU)5FagA&hDOt=)c!Yc<$?4%n7_BG^?0K%rGs-9`=&}mN"
    "S!uCw-l<3Pdk_N(&9qF68~UZjiFR#uNvOxE0J1ou$NaQjmby!Vg#LFjkG&KK0oc!p98hwwK75icA)O3?ADv*M2>P7_h-Pjn(1EoBvPnrT9krep2EV#(*0YvV"
    "k@_Rx-NuF*0Sk>(zi!dP!^tRBFZA_3t$5H;2as*`6nn59{kI*)ujt-Wb?UsM9ZP4>OlQyJ*_!Aqv5Yl6XvtTRs5Gf;QqxWQq@nFYRiItg!JX99{cBSD<QX$N"
    "_;Hx-m-$xOrIJ#xq8^KY6hFnKFgCqNx-ODLk>c3g$MlpZlU?a8Pr*gTG0~m6pKZobqzA+vu1vs}u*W{orT`yOe;Po)@L||`1Ph>Alh=h`vN4ynhG`&s{3o`G"
    "XT(FUbfvT~>;`|c-V9^s*`&vm6Ww8?N1qV)rQ0oN*2p8Jj^&1rR63F@&s&yxmT=h&JL$FCBxj18a^q)VkIs@DG@Ho-T{FAckg;XbliIB|hRQ-2h^|=xSl4Et"
    "T?PJl{Xl_UZj68p00<=kf!u<K*F5zC2upzad1cMBVynu_Fd=?)e|34p30qlARgCfq{#mi>*u7rDw28%Ic1gQ9BJYxWHbO@0_-mO4my2A2yd|#>tK<g20ydzG"
    "JEGAVCAc6@hvvzXdi)Tg7YD&B^w6)6&8X9=bGlD|isXp+w}4<H<U#DxbY!U{@N7gNwGfk~`L12Fq$gxTCTFuYQG~=^sX_4Qt9JI0l2u7#^^`S!Ux<=K68@yD"
    "pHJ6hV!Z*yT`?VOa=@LRD3L#LFB4HyJ?BAG7?md;kT^a0BtuY63K-%?TJoSnPpS!lW109d`$;bMPAauK&PNl_WWGbzF0Gmp#>mSop^P*o!OOwPsawfa7g39Y"
    "xbXHhQhjliqPRke9+Q?5v3pK4#_+rr06*;^bo4|#Pof?L;@;qA+CXSGvJGnX<P^p*B|14%oPV4-!9rKJ?ZQsV4qCN@v~aVE+A3a+C655jGx5WrVd;!*je|~|"
    "8@MH1%Q~E!BIvA^cRoeiDS~`SrTIa40`%@^6wWvS(a=vOxRaZ;lXEJHR?JEeO`6Kc2CbFo4Fhx;_Jf{4X-QgLTM~fOqLOuz2SJlZR&uU&c<wx7*L=aRAN$dS"
    "-Dl}>JU`MNCSjGqNLW=_=5mVVOOXlx6b-B7Zl;TiQ*m(sN)wo{O8NEqPPSbiAhC|$&HZxIpN0Hlhzc8R(Mub|eO*!KAgWGZ9Y)ok#(2*;b2x~1;bbu7Q^IeD"
    "z)D^W8{4go4a%Q3Hng_2@C%rg*)coqmV&ICdLrS+Nr<Rp!rrw#)N48*QyD^MYT-G3`I;tSGcK;n$(hZIX&Kx){%B<6a#KR9B{8GG&KuFi1;5$nK&2i7-Hv~`"
    "C2}Q#(4wUkEbFcL$t4aow_?2P(wB+;XHB4)WKAoXdo?M;^XRK$Yte2+x^20J-Sd2ok~&AK%THQPVq5!|6)|BRVSLPF!iOl8OKRofR;AAw_fK2YGKa6RJS}OF"
    "xF@*x{-4Ojz&w-IcfWj(mquT{`-@4nl*KqX474dTB_#`Hj09y7<?2o=gBXi9T$fXPeg!8N7NY6v#OmaF@RR4kHB+a<3z|PA?7!*AvQ-9}L-ms<8#WUqPWntd"
    "#{Fdw8k6*B=)xtW{+nAh7yGoM$ZBa@&wbs0^PF44g!k33|4m<$s~ELOx}pLWk#=e>4#^iEZot#bBA^`b!O^&3-j|-YkW25ieNE9x&Dw)Mo>X^w)>MS_A_u9x"
    "b4mW@P4-f-Wobx!xj6!X1~0Gs<Xl{}`rz*JQ1Hqu4rgA{gGX67=b(2#h>K{To12}N8$8pkwZ*jPs}@3iv9^8lhw6riAG)@SAYf!0=wta_y1l7&%dSzd_G<?d"
    "fRGCscp6=a3`|Z&xxvNcF|XN;5sWC5U&>q1FW>$;1bT)^0e{^C209r!@%wLkLqYS(pPVz+HebZR(q1YqDCLl)=nREe-XBL85b+7ZsKh^#;dX9E5pc>dW}mFK"
    "dx(GA<@B3FzW-ugLi}hydoH<_Arb5=Ng`N1qC5@|RL3CU0t>ykC=~<ODrUfidLvI4;a=S6p|+kdpkOu&_Ej@L^J*%aocO60NT&3Xjc8$>Xw&k;Nz5WWN8%W$"
    "844V{GPQ_QIu+B50*ZF@U=q$le=dapdgVq&h7+F0CI8HyC252a);kU-kF#@Aq;p3AJuOn6_eGp&+7#qZPxLNZHDZDjc5_$QLRY2k@Wxh7oHm&Nz%;0DY-kuI"
    "&H8Q}%+rrOl`&Y#N#lgyZEV<uCi6fEyMCjSx{4z`RB?ENm7#`sb{h+S{8VyM_N$t-p&zPQ8%212O}z_wzYkkT?Iq#W)?+%J<@l)U+Amtspp+qA(Y}r2L$sn0"
    "L-efK{MrywN=wVFT`NI)+1GE55jb7YaIo@rgo4Kcyn=q`3{1bcP?m<PMfiX3zMADVxgRuOXC${#l;k5iGzn6y^k`Zs7bQ3HT=k(c*y<ZyDtza;`coc1e@QgD"
    "E-><mb21E(rGFce%LB+jm(`am__?lktvTS;X4mB^-G;)?%TpL|K31i{i_uYgRZ9`=pDb(aC;0+w1RFLHRf|b07N5#EQ7rdYco>MYz$1-no(5t*)J9*xa!H#{"
    "+b#DECovf%Rm|wP$keE2wrVIjB-Ju3^Wqv}bEg21Ql}lb!m6GH)N4KJxoY}p3%{`fd?IVP3+!2CKg44YC%%Gfg$}vB{^PnI3NC{Vxy;)Iju!LJqv==#7N-+f"
    "k{f=cXdL+`cu+8rz{D>u+gT39=G^iF>u`@<P=*WXzd7Q~h+7fP+$2F{v3#3eA!%OuN3nb8`_b)hUOc+@<d5#XIKFrE<rjC4ncsC^O9~=qUL6C8LY!dax@3wt"
    "cxnWIZro~V$!b#PH(L4;j678#_rf4sYiu4aJt(Ud$CRjTA$a;Y$a69si%6YZ+GYa~$UiCY$bRl=H+kA*`I{U-d;s^X_a8~zu`2^NZ7^YQTMHuhv<r9CaMp$f"
    "XEi)6v<?%Vf>W!c3bR8~FvG}y%~8ZQ){0j5B=WmovK*Y`B0F+j0&7NS$)Z)yhGr7F6`qnbAGK7=z{k^IR&Q@=Z3(U*=nz!<I*b=8-YXqIr(|iC!qo$EteY}K"
    "l!74J@Y=}hFVevB1h8HYgmx6UMKR@`L0P_Cn$w@Utj<s13WAB(52;f}#1_wv8;E!qERuw;;@j7L{1^*_AoX_m^+0Cbqv>>n(F9CQNm|e@f0^`g#d)Z?TpoRN"
    "_K<TT02;+Hp4++QG^jG0Jr$y8W;CTP%Of*3E-y^a*5#+9@%wZ!w-wkoMrQ&m=2(oinVew-xU=+}t(Gl~hp~qb6WHD8bTm*J;fR9W;iAw6lC$glVsb>iLp(2|"
    "ALlu&8s}%Q97quWj5sKK@<;VW2GG&4hfR<!ND@^^3iR2`6Y>?jDs60}u9-lJgE27Bj0!z4ZAi0@t=KhKj=oO=Bf~XmAk8u>rZ4mDsoY6oUb9;@V-mJYeD>4R"
    "WM-HoA8)PNx;dj3T6yj_$2=^X@KBQU=%r>F|4NVfpuTF%MTR`9z@V>x$mfL{n`N|OTFZ<Kft+P(R5sIfZ4iX~NN3xrq7RIN=BE<HeP`ymRfip0^+e#M=lGse"
    "Ww&_u_+dr(iB5+~{5k}3QkjK#>2b9eMnI1%-TB0H;AhPsgBPU^{yFE)o3=yF$~tG{=TGRRf*Jhfpri9DO7u)#<rrLmxZSf^(fMj#l*6yn2Ipt9y64Oq&!GgK"
    ";4<cv9BoWz0Kltfo#cdK&2z{Z#aX@j#;HKV)i=<5y?VxfoVh7hnJ{vF-yn|TMYC<zm6v+=C)4S;?PMh7XUXEphAM`r88Sis8m8N$%vGnoz&=5iKqk8C=&Wi^"
    "W2DqjwwRl7TdO&gyf&?n3Y%<$wY6ArwxBl`fw}knNq@u-RhQgD`&|PV!F_RYzQluasjBx-bz4<YDJi7-aiZm{s;(lWNOckpwW4}zgM={)jk+BLekycZ>#EwF"
    "0x=xBX<;UzJn%C4abT;i6OLr<4mxGE8tj$~7D-lbkOsfGMax8(;DX;R)o?YL?HpyVwy-mJ3R&9}b157o`_={{RySuTvc$@PpgzCGFm8|)p<N!7HN;UP(}rb~"
    "QDd`1NFiOu@I0y$x2-C)-JfXc;cc7k#aeY~rFfbR%@mUn37NE%MPz5czml_>8<zy-&w_HaebGT7$y#r5#F<DI?1>y~TlH_~B<jV|cCTTEb?1gukK9~~DxvTD"
    "b~*FW^fX9`nTCXrPO?DO^%~_ZinW^D0J?k;K`d9)%IEOEeL+bjp9-hY6l-x)lb)(dT(VqA`Wi(gsK-(LW;tDv75I#I;8y3;&%-xCzua&=2j;xDoeDMJEdrk9"
    "%3t~p&nwAoR@kT!Z(#LUJFDnX#;(oRZVJkUPFkV5nrSB6E{i)iCA5+2q_U<@FPKW*u_a#~=HK*$Nhi~nJ>{*P*AGT>zeBjZZvGVsG3a$%k^fqqK}naDuPUtJ"
    "eKY*$4KWr!wgiU#K5-#38~mEbeRLva+OG6`x;8sRZfyce`_{?Wi4!f&sXEOsuhz;!o8+=BJp(eoc1s)fhuy)IH|fJeHhR0=MOmH}^Q$sZdFhvI6L0QXk;X|_"
    "c9J3G*5S#IIF~M1MI<?4!d|f&B;84oS{aE?XjKvd1Bx(gMfeM(QiBy!k&EQKGL;??V9%PnxPU)N2rD;c8>z37`U3Wt@hP2&aMg%N9;)jc5w7gj2E-QndWR#H"
    "@oM8yX!N@<CM%MDj{_4sz`lcC1!org{+%`N%A5W)UY;eFfzlJqn4}O7!@>MV!P#NG)~>so^_|9Ut#F?GT;taF#k@I&z-r+TZ$}ZpE*Q*9t^x+VAfBssQm#QL"
    "xOO`UZSM)%7*Y-LFaxc@o2whVKgF0J)sz@gZWU&7OKR{w%^GRM&quLBX9cW2k$nF`dL5|0SOUI-fHExX;vG7{?2fP8F%#F9cF!uAf*JF-%wKNHxNqi+n`L3M"
    "Ill@FTfs$o!PXqMfmF49(=w5TOm)3vAs5aEA<!}_--p`ssCy>qh*9y-gE&6m!ekQdDz5i6ul3cAS|ZhB130K1sx7~_?cFTXYjCSAs;9d&iz+R+X%-}B7CCuM"
    "%Ic#@o>Sc-Ll4kz0A3X?Lt(uJlvQquWM~gCqts!3x|ys+u9IvRNsC`D=2vgBQg`i$W{)L2&pBf2%%>QnI2bd#@B?&{veUKFR~lfbkaoWBklUaxE?7;HjbD^M"
    "=&%>kfZ-_I5+~7#l^F*^;>q;PVyWWx+zWZRC@fb)<V0jTt3ooJ!L+xCJ2O8L_v?^=W0rs6YPo7Wr_bpR^nCZ;=eNIl`1Hlm!v~LTF_|a^1vo}!>7(I*e1HkW"
    "oSRA0G)9Y=KS&!H@s^^eO1cqD!en%HtE1otHCPOzeyI!Vk23_LuIE^LH=9cccQycIPnL-%`k<VaEPRU;KL4;zoDYYs3>mjY8;ZuQv4*Tcc>;AsP$4&MU?G9s"
    "^MD3C_Zj=p@>llt%$0(MJEcmc&Hk12%`4BY^wseiYX~+OCF%Zp0<2eWX8}!l`;^rYmKz|-BXvbboiwJ14sP1RtR)hV{tXYCY5<FiZj%epu7)34Vl0qgiz+ZU"
    "WRJNci>WYJo@9BFEj{$Rpr5-ILz}`z+Uvfk3vCvxsVPt?-@Lf;fKB}6C+GON{Pg91Ryl6nhJxh$5`#enYr08QE$~3=r00)%W!V$#_r2L0SFgonR$Cp`6RAX("
    "ZtTaePyrF~1CPW{cFZV##bkW)O33^P^_JCL`Nf>Dgp5{$HRGPEuLd;5cwg?D1#Dmh^r)~342nw)Q{<Tz>ym?{B9FXqoUTZ$*Dj~~{<L(1y#vS<Z<o2cvP)!{"
    ";;orga@w&=N1V()n)ZV`ejh}mFvi2yx<oYhyZ8{XMSLxxSm<}3FgZm$e<IEflCvSaFdn)TZxiMz-X$meK{Yrz83ptrCr=D!->d{uFUf@GN1<PlvSrB=x7>@m"
    "T4g?n{6243Iu-SbbbG_-cWDFqPZ#5vT)3yxFQ%{UI*1{NXUXAQ>CTeEZ*)3T499a_xA_@Y5IdO|u5E`|fsAY<w@!u@X1YJ^r%bIFoKzJWxjng%tp^SVS4#<_"
    "3msaRh<z*I>Z$X?PeIWFjf;pL_KZ&ExfO9{IrBKOI*{xZzl7LzEyp6$X{0hnVmU2+eS@)BGg28sJR4=fb1HhC5RYUKk>^h`(uR2KOeZNP4!X9DbbawW3j8rC"
    "hvPVA525TR`N5B(!Y($-KIBhDVfo9Iw+rfp#hGbdRvw@hMW5mZuocG_S{3#nNlC5G@N;~s4q5VFnsG;fklA+t67b1kVAxeeF*`jRStu|=Il786u7)p(Sjzo$"
    "Rs?ZgSxbM)Pvna9G5b6hjXXx5eJ|90ns&-22sVp#bj7EB0zT2h5GIyPW9rqEB`ob2_l{+^Z^ZKG9q65lR1U1b#D`V!M=T-%FSei$`7K|ryyk`pOMW~9rt<_P"
    "92A)>tt>aq$Gn~8OJYV%&o;pG{MR&H<E1#3#X3mvnI{lvYuN-0&jvnp&)J7H*WG-0`R>o0@#VY!><r)k*U8YqED1wr9$mishaN~?mv8@N=FBhO{THVP5RCk4"
    "X`eTyXGO?_3MS}(nNAnRX-d54ZlDRZgS+Jg+KS=F#)t_^=PZm8cx5@c_nzEkjXT^;zUp)PCKx*-O!81lR}__O(#VKO!(Qo+7yg-Zdim}@IB!%#<{hw@1YbyX"
    "X?paaXOJ-VK-*%nzAL{}!wDeJ{ilx}c7Aw^AB=E%Z#t!VqvZ6?_-6zkwbJIntr>{<PQO!nRBt%jr`w~Z(}2f6R2`UNsqTZx;ndb!H8TkknGYu~S=+2sDnMRx"
    "NC=DLGxiyMmXY2$XG;}FByB5P4WZJ_*w9I-16M{Up(P|=HKbBV)2!}oIrY72ZO5<I9QcR-uRFC$y;-d{AF($M7Sdlk{Pj+CXWOZ7R~t=#+u2ricdO0aN9?hK"
    "CFnZ`-yzy&qc!{m;d_Ka3_A#MG#vpkqbSW{ct~CnEyDt3hMvjqb1xS+cZ!xC%PO65CQn4O4rSto8{|s)+!Bkws195$Eo@jax@~M^#th`d1Y{%)Njg^&k=RMD"
    "RY1K_Y>XKWJQyBFS*0y7A?CrC$HzlI0v#%t`NAkctt>%w5-|4;&n!t4<ml|&e9KR*mFb~Y#kkC}9TGY$3hA@v;<kn^Mggg7X=Oes8>?-TZ-lkXC_(5K6zBsT"
    "%qbzPw^LL<8C&g$I&AldXB5{+Q5}lpg%U97vkclxi=YqyEPy_eiStF;Mzs=&ooWn-^E8}~f@5Vfw1A3d5siZrylEoni@Om4%^d4vTN9cLE>|r9^f2(53JoJM"
    "EM>eXdvkG7IzJa8(u)avILl>8Ibey{ZsZgaR~OJ_m+y>AvqaLB+mmq2wgJ;y%9^lQBxAHr_l%U#_J$HnE_Nrx3R<1Y2d|pfH2#E0liNTh{V8Bm50-NDbXq<K"
    "z_x2%HyHY-;WTQO;_-AkAC@wL1Q<big!^1v$*$At?i_W|(l{xA8f3`sFrrUChez;c&XrO6hHIVV+M+qI1NdX+|H4NJjpX}>QAmJf4*<#3W0GME2jSu$&DvY4"
    "eOe;MLKI`>DFNo_^6lTxozwTf5_wEfsx9e<Lq>gOkd;%B)+&DahIs3W&lI7t(eb0j{CAg#=f%~n$s>QyNo;yM5F3cLeQEHb7Q2W?cJecZniuG3DXMGQ8JkoV"
    "xFif|$-|4zR|SzRGp@$@&|7YbF!q(wK_=(d`MQPi8=}Dq&Y#OV^{ZX3!Z}F`I^)C{XK*|_y(fUt3K)zpJKrBg8T-{jdAF!!`j*5wrsl4*>C``*`x<vKSQ&8Y"
    "`h^VtRIzleW+$mwg(5sUC8c<#8T|S=G!o#ufZaTd3uQ6C5BrSUy&lCUK;a~5xt1-t9-giv$6ax!uNLtQTJSyX^js}jCj&U>l9^5_itbF`NcN~3D&?U#fc%dm"
    "f2Q$Jd|*kD1z%*b+x(|d=wt!4MUUY$KK4(8Kf*^wu`mY7q{IH+1(w+}Z>a706DGHG;LBHW0L5S<2keniGR9|jxBw|SJQk0PKKr9PM9%EX>%nBfMEhnt0CU}O"
    "Xi*-_!Ks}GU{CZ^_F~U~X7b%H-v8Qp1M=UQ6Zy{Q^4)(5ombra`{w<(J?Fb$UcUQ}lVP>ApK6f{oFKn4(^RQDn)Y6mtg+h?1g-*xS$QHbBgq3bV&&P#%EEF@"
    "UA4M-RZNK?fm+7QQvE56bQ9!U0S<Ad8rIpBcxu)djDj~>1%`bnM@eKea+s~2wlRo;ixEzciB$CQthp*_v25*ix+3zBfog{gt%J^(DmO*@F_(e+<f1-Yj^qXI"
    "`H7a@SVpjeIM5qTBR&w%v9{&eTGB?bT$x<`1G(yn6!S&ko*Agkk~XRkokLslOn&(o9sum9=$ylJ5>j4FkCVqMc5d)%uUQNMDZ#`3PzwRwHjLSoDsj2y?qgK4"
    "<?;0-e%+^Gs!1r|fvn~_qI>pSJ=Ln+>hP4>w4`~}CXJQibLThpA{0G&S!)|xtC0NM%dGPakEn89o_Wl-ps(d=hlGErU`Y%s6t{@ZTzbLS$25EBCJw*fPun+E"
    "m^WU2a(=4C$Js3$3=9_+FTeYRkL>L7-QPK*_y4n6tvXtLW$XBy&P>VBY;MBdfj?aFnKL5Q;=^vbK@nFcT7(iV|F907G;6LqYm_JMrt!PiXXMuveM|grm+h*K"
    "NkhmU=Ot42ukpuKiRcHZ5Zz?hTo$(G@Bi)>)62KtPMjXIV+o-pxs2!iuR>?|ukikp^Q8Sp55D;7>AmBnGaJ5tJFh0rW4#}1TtNnyQYNZQtCSSslqyr2F{>BF"
    "`RkKD^92ahe;7Mu+f>mGMU_ncT}VRBX1~Jhf~TGs8~zzyUduy%fk~F)nLjbZ6QHn*HFTpT-^;z>;_~fpCQcWGQ*qZ4?CQkmNx3A28^5KD4gRp_k6(ltw|S{>"
    "1b4aKU1I{XIniG4zA#Z0*&+72{-tSrqG>xP-tv|FW><n`l&wxD;*Kjkzl-;D6B~x)t#iL40y0?<b~`dC5(Wa`)m-!%)X8)XXxuJonS*kUd!`y-I*U<1XQyQq"
    "=@@B$lD){4DbL|Ok8sm%yX3Af77W8_jkw*b;K|k0R8ToJX2O9Bj4tSq{_#{_6pnKzbcH#uG`us@T1qtS;P9*^h)fIHOWsCpzta{Qp;0@uod--B=dCapuWBNd"
    "hDllna?S0;<o<Q~4kh)N+08A3+qah%CUs3>PmQk#{mfEeR%wKpN{*=?*|f9uHKgKN4qGpy?z#*imnl3|{wrtv{@b%sjt->(c?CE#cqC2U%};RS4hrOk!tGKP"
    "_aIJRY4LvPM)}<thSH~9eY}2cih`DPPADEXp}JYgfb0ZK#Io~)m=9n#BE9nJHI4k6IWA`wV%M9FyMS4Jdd#mS)22np49hd;GpAN<xyIhO+Oo<1R>5bjotm{*"
    "uNT3E21A(-Q(|U`%CXvBkj?Wgv8iUM!xWpzXEJHFT6%Q(_OC%qzkK^Ib7yk-?w4q147E8q_uv2e%y|_8qs5pwpNCh9DnA2>@7K-{ME-y7F=qUC{B^>HHTAeZ"
    "?JdxDZ8_t8_sd5Dun*a_H~hG_ek_Nb2T4NmZ6`dm(<1!1N8;*;m^1M)kDS!bj_jK+xeqA&J%&bU1*qA5t$VvkOiCv&cqI`!lIaL9o8|`bO3WK4ECb>fi$3&d"
    "u!g5a(|e@>APSEMQ#B253hglQq%cRyW35N|Ky<3Rpgc3IpFQ|I-8=QqXg7YS`#A20iF-&dLGJKig=jQwDjh;H&-5#E5Yn3@TraUvIsIBTz?<0IbcD9=j8WZp"
    "n6T~)*`dcMP8?0s*_kN#qu`8@x178PdTAP)gw%nW7gn_d8n#xRv^Ap5z!dw@bhdU|3JM{x3iP?ao^~D&r}Js-lo@+)Za;qDwwZ7O`nKfaeWx3MZU)7N@Si_("
    ";we-gfRgUK3W8be;H8t?t;J;Ksv366x05qsr|deN1I-0-1M5rq!MV6_ZrF7DRAx$Lr~@`?8-$0<B>hga%dtQc!&E=<Ps0-*SXC7#d|S;I+3t<3mv~PzbNCra"
    "L5<s{OA?^+0>*!wm|z7Be+u<{_=8@k-Z1o?`10+$YAO5D3mfZ%nG2HvJI287c|38BRCCp;;W$78r9<Pg(s^WwTklB_FGko&0ulDSe2!=px1Z5PeZF*8_hmWC"
    "k~Ob~|N7q+EPP8r$L2W^Lxg3UpUr~lfDW9?ne4iplC&$93}MPFjlMDscc~K%yMd1?i*jVm-tJB#X;w=ol9J$@q9N6fCS|E@v(-|zJhq3~o;@B0!Mx4g{u$Df"
    "F)+slI%_XSHZUrYQoq?}&1Jz$e;SQi%^dPFTXIhvX?&3oysl$N(Fxpmuk4ccpDla>F%Xqd?&_kfWC7fVqhWDaUOyC<B*sKh{9_rl;D`%G@SJhN@zgo%)%kyH"
    "Q6!AP6K+M0XRm1RyjcFeD`n^_67sSnqvqEM9QmUWtOmbr?Dt{xYK_0}Po1tGIsAN0vbaEpQDj)fe^V2(hTOQ?(Suatvm5p>oV;4XTRWQeIKLc5!Jt#xELm0v"
    "X7VH$faW?ZG8wbsDB$KOy}{$ak9t>XPYzUD1U^S+!#P^a9b7tb{v{@&Tyju%MUUj@Omg;H3y6AnwZ{0_ZH+)Mg;fmJDKjHH(3$stHyJvBT3<hL`j_wi0bNA@"
    "I5}}Ok2tIOlvTVVWiI2P;mV<Jnd>iINqPjeYQn(JVJt?>Yg*nAm#I@W2MIjcR);VXqegMLRdi@+oo7hkG5}%`%+D}G<y3M?lheqI$IvInVCJTwYw{6X&GKEc"
    "oO-k7%+r2p9LY+PTJBu<^si+$997Fl3|9+Jg`6Tt%_jM3%k7}&vsD`i4H4otHfJPoB-!+OrVI-%v5JBSYbF(=G6$i%ua9i0Q-#kzq&K7f>JCj~%-TJg4dis0"
    "&@0AZ&>}X8Ll!zl0Z@P>ijx-<a4c2xQQ_L+^r!~;`^<jC->l*(S#2P*K4K?7uhfDTnUYgEiIs5m5LzZI6yqsWUeKdek;3XvbSDbCLxX!^Oi_uQhsg<=vx*XF"
    "sW%lSpPmVZj7w9e$br*IovXN6-#5>3Jexds>4o*?OK&1<Udzmweam3DQJ4(|iqjkgIhhE6f`2P)#m?mYug+jcy#HI=7c4nJe{pvC?$76-(3>sl=?JJ@lL<NL"
    ";epf@{#@?hj010&Nh2MF&%%1NxEV{By}rBo@p{Y(=-{~k#Fx1|dLOG`^#_>99B4&3^MJmm5;dx(A54H;0K;Gb!)+c@=Y$h3tKn57x$*MUl$mA@V!1P6$jxXA"
    "<~8Av%=9T>OSd-TUKGye2l%x+?VqtP!})jwU;cl#?<98"
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
    total_table_count = len(tables) if tables else len(cameras)
    lic = get_license_snapshot()
    if lic.get("active") and lic.get("table_limit") is not None:
        table_cap = int(lic["table_limit"])
        if total_table_count > table_cap:
            return (
                f"Vượt quá giới hạn bản quyền ({table_cap} bàn). "
                f"Cấu hình yêu cầu {total_table_count} bàn (bàn tắt vẫn tính)."
            )
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
    "table_limit": None,
    "has_table_cap": False,
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
    table_limit = None
    has_table_cap = False
    activated_at = get_license_activated_at(license_key)
    try:
        if not license_key or license_key == "UNKNOWN-DRIVE":
            reason = "Không đọc được mã ổ cứng."
        else:
            pinned_text = _telegram_pinned_text()
            if parse_verified_license_grant is not None:
                grant = parse_verified_license_grant(pinned_text, hardware_key=license_key)
                active = grant.is_verified
                table_limit = grant.table_limit
                has_table_cap = grant.has_table_cap
                reason = grant.reason
            else:
                active = _pinned_message_has_key(pinned_text, license_key)
                reason = "Mã ổ cứng đã có trong tin ghim Telegram." if active else "Mã ổ cứng chưa có trong tin nhắn ghim Telegram."
            if active:
                activated_at = record_license_activation(license_key)
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
            table_limit=table_limit,
            has_table_cap=has_table_cap,
        )
    logger.info("[License] active=%s key=%s table_limit=%s activated_at=%s reason=%s", active, license_key or "N/A", table_limit, activated_at or "N/A", reason)
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
        "reason": state.get("reason", ""),
        "table_limit": state.get("table_limit"),
        "has_table_cap": state.get("has_table_cap", False),
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


def _cut_output_filename(source_filename):
    """Keep the source camera identity on new cut files for guest-access checks."""
    match = re.match(r"^(?:merge_|cut_)?cam([1-9][0-9]*)_", source_filename or "", re.IGNORECASE)
    owner = f"cam{match.group(1)}_" if match else ""
    return f"cut_{owner}{uuid.uuid4().hex[:8]}.mp4"


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


def get_local_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    try:
        host_name = socket.gethostname()
        ip = socket.gethostbyname(host_name)
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return "127.0.0.1"


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
        {"id": cam_id, "name": get_camera_name(cam_id), "table_id": next((str(t.get("id")) for t in CONFIG.get("tables", []) if isinstance(t, dict) and str(t.get("camera_id")) == str(cam_id) and t.get("id") is not None), "")}
        for cam_id in range(1, len(CAMERA_LIST) + 1)
    ]
    return render_template("home.html", site=get_site_config(), cameras=cameras)


@app.route("/replay/cam<int:cam_id>")
def replay_cam(cam_id):
    if not 1 <= cam_id <= len(CAMERA_LIST):
        return "Camera không tồn tại", 404
    if not is_admin() and is_camera_private(cam_id):
        return "Bàn này đang bật chế độ riêng tư hoặc tạm ngưng.", 403
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
    if not is_admin() and is_camera_private(cam_id):
        return "Bàn này đang bật chế độ riêng tư hoặc tạm ngưng.", 403
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
    if not is_admin() and is_camera_private(cam_id):
        return "Bàn này đang bật chế độ riêng tư hoặc tạm ngưng.", 403
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
    if not is_admin() and is_camera_private(cam_id):
        return jsonify([]), 403
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
    cam_id = extract_cam_id_from_filename(filename)
    if cam_id is not None and not is_admin() and is_camera_private(cam_id):
        return "Bàn này đang bật chế độ riêng tư hoặc tạm ngưng.", 403
    return send_from_directory(VIDEO_DIR, os.path.basename(safe_path), mimetype="video/mp4", conditional=True)


@app.route("/download/<path:filename>")
def download_video(filename):
    safe_path = safe_video_path(unquote(filename))
    if not safe_path or not os.path.isfile(safe_path):
        return "Video not found", 404
    cam_id = extract_cam_id_from_filename(filename)
    if cam_id is not None and not is_admin() and is_camera_private(cam_id):
        return "Bàn này đang bật chế độ riêng tư hoặc tạm ngưng.", 403
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
    if not is_admin() and is_camera_private(cam_id):
        return "Bàn này đang bật chế độ riêng tư hoặc tạm ngưng.", 403
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
    if reference:
        cam_id = reference.get("cam_id", 1)
        if not is_admin() and is_camera_private(cam_id):
            return "Bàn này đang bật chế độ riêng tư hoặc tạm ngưng.", 403
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
        license_error = _table_add_license_error(cleaned)
        if license_error:
            return jsonify({"ok": False, "error": license_error}), 403
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
        license_error = _table_add_license_error(cleaned)
        if license_error:
            return jsonify({"ok": False, "error": license_error}), 403
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


@app.route("/api/tables", methods=["GET"])
def api_public_tables():
    """Public safe endpoint returning table status and visual colors without secrets."""
    tables = []
    configured_tables = CONFIG.get("tables", [])
    if not isinstance(configured_tables, list):
        configured_tables = []
    source = configured_tables if configured_tables else [
        {"id": f"ban-{i:02d}", "name": get_camera_name(i), "camera_id": i}
        for i in range(1, len(CAMERA_LIST) + 1)
    ]
    for idx, t in enumerate(source, start=1):
        if not isinstance(t, dict):
            continue
        is_priv = t.get("enabled") is False or t.get("privacy_off") is True
        color = get_table_visual_color(t) if get_table_visual_color else ("red" if is_priv else "grey")
        tables.append({
            "id": str(t.get("id") or f"ban-{idx:02d}"),
            "name": str(t.get("name") or f"Bàn {idx}"),
            "camera_id": int(t.get("camera_id", idx)),
            "enabled": bool(t.get("enabled", True)),
            "privacy_off": bool(t.get("privacy_off", False)),
            "color": color,
        })
    return jsonify({"ok": True, "tables": tables})


# The table-access module owns the admin table list route. This legacy
# function is no longer exposed as a second Flask endpoint.
@admin_required
def admin_get_tables():
    tables = []
    configured_tables = CONFIG.get("tables", [])
    if not isinstance(configured_tables, list):
        configured_tables = []
    if not configured_tables and CAMERA_LIST:
        for i, cam in enumerate(CAMERA_LIST, start=1):
            is_priv = cam.get("enabled") is False or cam.get("privacy_off") is True
            color = get_table_visual_color(cam) if get_table_visual_color else ("red" if is_priv else "grey")
            tables.append({
                "id": f"ban-{i:02d}",
                "name": cam.get("name") or f"Bàn {i}",
                "camera_id": i,
                "enabled": bool(cam.get("enabled", True)),
                "privacy_off": bool(cam.get("privacy_off", False)),
                "sort_order": i,
                "color": color,
            })
    else:
        for idx, t in enumerate(configured_tables, start=1):
            if not isinstance(t, dict):
                continue
            is_priv = t.get("enabled") is False or t.get("privacy_off") is True
            color = get_table_visual_color(t) if get_table_visual_color else ("red" if is_priv else "grey")
            tables.append({
                "id": str(t.get("id") or f"ban-{idx:02d}"),
                "name": str(t.get("name") or f"Bàn {idx}"),
                "camera_id": int(t.get("camera_id", idx)),
                "enabled": bool(t.get("enabled", True)),
                "privacy_off": bool(t.get("privacy_off", False)),
                "sort_order": int(t.get("sort_order", idx)),
                "color": color,
            })
    return jsonify({"ok": True, "tables": tables})


@app.route("/api/admin/tables/toggle", methods=["POST"])
@admin_required
def admin_toggle_table():
    global CONFIG
    data = request.get_json(silent=True) or {}
    table_id = str(data.get("table_id") or data.get("id") or "").strip()
    if not table_id:
        return jsonify({"ok": False, "error": "Thiếu table_id"}), 400

    tables = CONFIG.get("tables", [])
    if not isinstance(tables, list):
        tables = []
        CONFIG["tables"] = tables

    target_table = None
    for t in tables:
        if isinstance(t, dict) and str(t.get("id")) == table_id:
            target_table = t
            break

    if not target_table:
        return jsonify({"ok": False, "error": f"Không tìm thấy bàn: {table_id}"}), 404

    if "privacy_off" in data:
        target_table["privacy_off"] = bool(data["privacy_off"])
    elif data.get("action") == "toggle_privacy":
        target_table["privacy_off"] = not bool(target_table.get("privacy_off", False))

    if "enabled" in data:
        target_table["enabled"] = bool(data["enabled"])
    elif data.get("action") == "toggle_enabled":
        target_table["enabled"] = not bool(target_table.get("enabled", True))

    try:
        cleaned = clean_config_for_saving(CONFIG)
        save_config(cleaned)
        CONFIG = cleaned
        is_priv = target_table.get("enabled") is False or target_table.get("privacy_off") is True
        color = get_table_visual_color(target_table) if get_table_visual_color else ("red" if is_priv else "grey")
        return jsonify({
            "ok": True,
            "message": "Đã cập nhật trạng thái bàn.",
            "table": {
                "id": target_table["id"],
                "name": target_table.get("name"),
                "camera_id": target_table.get("camera_id"),
                "enabled": target_table.get("enabled", True),
                "privacy_off": target_table.get("privacy_off", False),
                "color": color,
            },
        })
    except Exception as exc:
        logger.exception("Không thể lưu cấu hình sau khi đổi trạng thái bàn")
        return jsonify({"ok": False, "error": f"Lỗi lưu cấu hình: {exc}"}), 500


# The table-access module provides the unique, identity-preserving reorder API.
@admin_required
def admin_reorder_tables():
    global CONFIG
    data = request.get_json(silent=True) or {}
    order = data.get("table_ids") or data.get("order")
    if not isinstance(order, list) or not order:
        return jsonify({"ok": False, "error": "Thiếu danh sách thứ tự table_ids"}), 400

    tables = CONFIG.get("tables", [])
    if not isinstance(tables, list) or not tables:
        return jsonify({"ok": False, "error": "Danh sách bàn rỗng"}), 400

    if reorder_tables_preserving_identity is not None:
        reordered = reorder_tables_preserving_identity(tables, order)
    else:
        table_map = {str(t.get("id")): t for t in tables if isinstance(t, dict) and t.get("id")}
        reordered = []
        for idx, tid in enumerate(order, start=1):
            if str(tid) in table_map:
                t_obj = dict(table_map.pop(str(tid)))
                t_obj["sort_order"] = idx
                reordered.append(t_obj)
        for t_rem in table_map.values():
            reordered.append(t_rem)

    CONFIG["tables"] = reordered
    try:
        cleaned = clean_config_for_saving(CONFIG)
        save_config(cleaned)
        CONFIG = cleaned
        return jsonify({
            "ok": True,
            "message": "Đã lưu thứ tự bàn.",
            "tables": [
                {
                    "id": t.get("id"),
                    "name": t.get("name"),
                    "camera_id": t.get("camera_id"),
                    "enabled": t.get("enabled", True),
                    "privacy_off": t.get("privacy_off", False),
                    "sort_order": t.get("sort_order"),
                    "color": get_table_visual_color(t) if get_table_visual_color else ("red" if (t.get("enabled") is False or t.get("privacy_off") is True) else "grey"),
                }
                for t in reordered
            ],
        })
    except Exception as exc:
        logger.exception("Không thể lưu cấu hình sau khi đổi thứ tự bàn")
        return jsonify({"ok": False, "error": f"Lỗi lưu cấu hình: {exc}"}), 500


# Deprecated implementation retained for compatibility only. The module below
# owns the public route and its verified channel-inventory response.
@admin_required
def admin_camera_probe():
    data = request.get_json(silent=True) or {}
    clean_vendor = str(data.get("vendor") or "dahua").strip().lower()
    host = str(data.get("host") or data.get("ip") or "").strip()
    if not host:
        return jsonify({"ok": False, "error": "Thiếu IP/Host thiết bị"}), 400
    user = str(data.get("username") or data.get("user") or "admin").strip()
    pwd = str(data.get("password") or data.get("pass") or "").strip()
    raw_port = data.get("port")

    if raw_port:
        try:
            port = int(raw_port)
        except (ValueError, TypeError):
            return jsonify({"ok": False, "error": "Port không hợp lệ"}), 400
    else:
        if clean_vendor in {"hikvision", "ezviz", "hik"}:
            port = 8000
        elif clean_vendor in {"dahua", "imou"}:
            port = 37777
        elif clean_vendor == "kbvision":
            port = 8888
        elif clean_vendor == "onvif":
            port = 80
        else:
            port = 554

    if clean_vendor in {"hikvision", "ezviz", "hik"}:
        try:
            from camera_modules.hikvision import probe_hikvision_device
            res = probe_hikvision_device(host, user, pwd, port=port, vendor=clean_vendor)
            return jsonify(res.to_dict())
        except Exception as exc:
            return jsonify({"ok": False, "vendor": clean_vendor, "message": str(exc), "channels": []}), 200
    elif clean_vendor in {"dahua", "imou"}:
        try:
            from camera_modules.dahua import probe_dahua_device
            res = probe_dahua_device(host, user, pwd, port=port, timeout_sec=5.0)
            return jsonify(res.to_dict())
        except Exception as exc:
            return jsonify({"ok": False, "vendor": clean_vendor, "message": str(exc), "discovered_channels": []}), 200
    elif clean_vendor == "kbvision":
        try:
            from camera_modules.dahua import probe_kbvision_device
            res = probe_kbvision_device(host, user, pwd, port=port, timeout_sec=5.0)
            return jsonify(res.to_dict())
        except Exception as exc:
            return jsonify({"ok": False, "vendor": clean_vendor, "message": str(exc), "discovered_channels": []}), 200
    elif clean_vendor == "onvif":
        try:
            from camera_modules.onvif import ONVIFAdapter
            import dataclasses
            ad = ONVIFAdapter(host=host, port=port, username=user, password=pwd, timeout=3.0)
            res = ad.probe(require_media=False, timeout=3.0)
            out = dataclasses.asdict(res) if dataclasses.is_dataclass(res) else (res.to_dict() if hasattr(res, "to_dict") else dict(res))
            return jsonify(out)
        except Exception as exc:
            return jsonify({"ok": False, "vendor": clean_vendor, "message": str(exc), "channels": 0}), 200
    else:
        try:
            from camera_modules.rtsp import RTSPAdapter
            import dataclasses
            ad = RTSPAdapter(host=host, port=port, username=user, password=pwd, vendor=clean_vendor, timeout=3.0)
            res = ad.probe(require_media=False, timeout=3.0)
            out = dataclasses.asdict(res) if dataclasses.is_dataclass(res) else (res.to_dict() if hasattr(res, "to_dict") else dict(res))
            return jsonify(out)
        except Exception as exc:
            return jsonify({"ok": False, "vendor": clean_vendor, "message": str(exc), "channels": 0}), 200


# Discovery is exposed only by the authenticated camera module. Retain this
# older implementation without registering a competing Flask route.
@admin_required
def admin_camera_discover():
    timeout = 2.5
    protocols = None
    subnet = None
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        if "timeout" in data:
            try:
                timeout = max(0.5, min(10.0, float(data["timeout"])))
            except (ValueError, TypeError):
                pass
        protocols = data.get("protocols")
        subnet = data.get("subnet")
    else:
        req_timeout = request.args.get("timeout")
        if req_timeout:
            try:
                timeout = max(0.5, min(10.0, float(req_timeout)))
            except (ValueError, TypeError):
                pass
        subnet = request.args.get("subnet")

    try:
        from camera_modules.discovery import discover_lan_cameras
        devices = discover_lan_cameras(timeout_sec=timeout, protocols=protocols, subnet=subnet)
        return jsonify({
            "ok": True,
            "count": len(devices),
            "devices": [d.to_dict() if hasattr(d, "to_dict") else dict(d) for d in devices],
        })
    except Exception as exc:
        logger.warning("[Discovery] Lỗi dò tìm camera LAN: %s", exc)
        return jsonify({"ok": False, "error": f"Lỗi dò tìm camera: {exc}", "unsupported": False}), 500


@app.route("/api/admin/camera/bulk-add", methods=["GET", "POST"])
@admin_required
def admin_camera_bulk_add():
    global CONFIG, CAMERA_LIST
    # The legacy bulk-add path accepted arbitrary channel IDs and could create
    # camera entries without a successful vendor probe or usable stream mapping.
    # Keep it explicitly unavailable until the modular add flow verifies both.
    return jsonify({
        "ok": False,
        "unsupported": True,
        "error": "Chưa hỗ trợ thêm camera hàng loạt qua API cũ; cần xác minh kênh và luồng SDK trước khi lưu.",
    }), 501

    data = request.get_json(silent=True) or {}
    device_data = data.get("device") or data
    channels = data.get("channels") or data.get("channel_ids")
    if not isinstance(channels, list) or not channels:
        if "channel" in data:
            channels = [data["channel"]]
        else:
            return jsonify({"ok": False, "error": "Thiếu danh sách kênh (channel_ids hoặc channels)"}), 400

    vendor = str(device_data.get("vendor") or "dahua").strip().lower()
    host = str(device_data.get("host") or device_data.get("ip") or "").strip()
    if not host:
        return jsonify({"ok": False, "error": "Thiếu IP/Host thiết bị"}), 400
    user = str(device_data.get("username") or device_data.get("user") or "admin").strip()
    pwd = str(device_data.get("password") or device_data.get("pass") or "").strip()

    raw_port = device_data.get("port")
    if raw_port:
        try:
            port = int(raw_port)
        except (ValueError, TypeError):
            port = 37777 if vendor in {"dahua", "imou"} else (8000 if vendor in {"hikvision", "ezviz"} else 554)
    else:
        port = 37777 if vendor in {"dahua", "imou"} else (8000 if vendor in {"hikvision", "ezviz"} else 554)

    current_tables = CONFIG.get("tables", [])
    if not isinstance(current_tables, list):
        current_tables = []
    current_count = len(current_tables)
    add_count = len(channels)

    lic = get_license_snapshot()
    if lic.get("active") and lic.get("table_limit") is not None:
        table_limit = lic["table_limit"]
        if current_count + add_count > table_limit:
            return jsonify({
                "ok": False,
                "error": f"Từ chối thêm hàng loạt: Vượt quá giới hạn bản quyền ({table_limit} bàn). "
                         f"Hiện có: {current_count} bàn (bàn tắt vẫn tính), yêu cầu thêm: {add_count} bàn.",
                "table_limit": table_limit,
                "current_count": current_count,
            }), 400

    if vendor not in {"dahua", "imou", "hikvision", "ezviz", "kbvision", "onvif", "rtsp"}:
        return jsonify({
            "ok": False,
            "error": f"Hãng camera '{vendor}' chưa được hỗ trợ thêm hàng loạt.",
            "unsupported": True,
        }), 400

    current_cameras = list(CONFIG.get("cameras", []))
    new_cameras = []
    new_tables = []

    existing_camera_ids = {
        int(c.get("camera_id", idx)) for idx, c in enumerate(current_cameras, start=1) if isinstance(c, dict)
    }
    existing_table_ids = {
        str(t.get("id")) for t in current_tables if isinstance(t, dict) and t.get("id")
    }

    start_cam_num = max(existing_camera_ids, default=0) + 1

    for ch_idx, ch in enumerate(channels):
        try:
            channel_id = int(ch)
        except (ValueError, TypeError):
            continue

        cam_num = start_cam_num + ch_idx
        table_id = f"ban-{cam_num:02d}"
        suffix = 1
        while table_id in existing_table_ids:
            table_id = f"ban-{cam_num:02d}_{suffix}"
            suffix += 1
        existing_table_ids.add(table_id)

        table_name = f"Bàn {cam_num}"

        if vendor in {"dahua", "imou"}:
            cam_entry = {
                "name": table_name,
                "playback_source": "local",
                "view_stream": "auto",
                "ip": host,
                "user": user,
                "pass": pwd,
                "local_transport": "netsdk",
                "netsdk_port": port,
                "netsdk_channel": channel_id,
            }
        elif vendor in {"hikvision", "ezviz"}:
            cam_entry = {
                "name": table_name,
                "playback_source": "local",
                "view_stream": "auto",
                "ip": host,
                "user": user,
                "pass": pwd,
                "local_transport": "rtsp",
                "port": int(device_data.get("rtsp_port") or 554),
                "vendor": "hikvision",
                "channel": channel_id,
            }
        elif vendor == "kbvision":
            transport = "netsdk" if port == 37777 else "rtsp"
            cam_entry = {
                "name": table_name,
                "playback_source": "local",
                "view_stream": "auto",
                "ip": host,
                "user": user,
                "pass": pwd,
                "local_transport": transport,
                "port": port if transport == "rtsp" else 554,
                "netsdk_port": port if transport == "netsdk" else 37777,
                "netsdk_channel": channel_id,
                "channel": channel_id,
                "vendor": "kbvision",
            }
        else:
            cam_entry = {
                "name": table_name,
                "playback_source": "local",
                "view_stream": "auto",
                "ip": host,
                "user": user,
                "pass": pwd,
                "local_transport": "rtsp",
                "port": port,
                "channel": channel_id,
            }

        tbl_entry = {
            "id": table_id,
            "name": table_name,
            "camera_id": cam_num,
            "enabled": True,
            "privacy_off": False,
            "sort_order": len(current_tables) + len(new_tables) + 1,
        }

        new_cameras.append(cam_entry)
        new_tables.append(tbl_entry)

    if not new_cameras:
        return jsonify({"ok": False, "error": "Không có kênh hợp lệ để thêm"}), 400

    current_cameras.extend(new_cameras)
    current_tables.extend(new_tables)

    CONFIG["cameras"] = current_cameras
    CONFIG["tables"] = current_tables
    CAMERA_LIST = current_cameras

    try:
        cleaned = clean_config_for_saving(CONFIG)
        save_config(cleaned)
        CONFIG = cleaned
        CAMERA_LIST = CONFIG.get("cameras", [])
        return jsonify({
            "ok": True,
            "message": f"Đã thêm thành công {len(new_tables)} bàn mới vào hệ thống.",
            "added_count": len(new_tables),
            "tables": new_tables,
        })
    except Exception as exc:
        logger.exception("Không thể lưu cấu hình sau bulk-add")
        return jsonify({"ok": False, "error": f"Lỗi lưu cấu hình: {exc}"}), 500


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
    port = int(CONFIG.get("server_port") or 8004)
    req_host = request.host.split(":")[0].strip().lower()
    is_lan = False
    try:
        ip_obj = ipaddress.ip_address(req_host)
        if ip_obj.is_private and not ip_obj.is_loopback:
            is_lan = True
    except ValueError:
        pass

    if is_lan:
        lan_base = request.url_root.rstrip("/")
    else:
        lan_ip = get_local_lan_ip()
        lan_base = f"http://{lan_ip}:{port}"

    target_url = f"{lan_base}/replay/cam{int(table['camera_id'])}"
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
    cam_id = extract_cam_id_from_filename(filename)
    if cam_id is not None and not is_admin() and is_camera_private(cam_id):
        return jsonify({"error": "Bàn này đang bật chế độ riêng tư hoặc tạm ngưng."}), 403
    output_filename = _cut_output_filename(filename)
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
    cam_id = extract_cam_id_from_filename(filename)
    if cam_id is not None and not is_admin() and is_camera_private(cam_id):
        return "Bàn này đang bật chế độ riêng tư hoặc tạm ngưng.", 403

    input_path = os.path.join(VIDEO_DIR, filename)
    if not os.path.isfile(input_path):
        return "File không tồn tại", 404

    output_filename = _cut_output_filename(filename)
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
        if not is_admin():
            blocked_cams = {c for c in range(1, len(CAMERA_LIST) + 1) if is_camera_private(c)}
            if blocked_cams:
                payload["segments"] = [s for s in payload.get("segments", []) if int(s.get("cam_id", 0)) not in blocked_cams]
                payload["events"] = [e for e in payload.get("events", []) if int(e.get("cam_id", 0)) not in blocked_cams]
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
    if not is_admin() and is_camera_private(cam_id):
        return Response(
            "data: error:Bàn này đang bật chế độ riêng tư hoặc tạm ngưng.\n\n",
            mimetype="text/event-stream",
            status=403,
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


# Install server-side table privacy guards after all legacy media routes exist.
# The module receives callbacks; it does not alter recording workers or camera IDs.
def _update_table_access_config(updated):
    global CONFIG
    CONFIG = updated


def _verified_table_license_grant():
    from camera_modules.policy import verify_license_grant
    return verify_license_grant(
        _telegram_pinned_text(), hardware_key=get_machine_license_key()
    )


def _table_add_license_error(candidate):
    """Reject new tables exceeding the Telegram-pinned, hardware-bound grant."""
    def capacity(config):
        cameras = config.get("cameras", [])
        tables = config.get("tables", [])
        return max(len(cameras) if isinstance(cameras, list) else 0,
                   len(tables) if isinstance(tables, list) else 0)

    if capacity(candidate) <= capacity(CONFIG):
        return None
    try:
        grant = _verified_table_license_grant()
    except Exception:
        return "Không thể xác thực hạn mức bàn từ tin ghim Telegram."
    if not grant.is_verified:
        return "Giấy phép máy chủ chưa được xác thực trên Telegram."
    if grant.table_limit is not None and capacity(candidate) > grant.table_limit:
        return "Số bàn vượt quá hạn mức cấp phép của thiết bị."
    return None


from camera_modules.table_access import install_table_access

install_table_access(
    app,
    get_config=lambda: CONFIG,
    set_config=_update_table_access_config,
    save_config=save_config,
    get_nvr_reference=_get_nvr_reference,
)


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
