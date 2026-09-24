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
_EMBEDDED_REPLAY_TEMPLATE_SHA256 = "5eca183251b4c2c3c10259d4dba4ec99dcdb3e844174a868ac0dde8ca500c97b"
_EMBEDDED_REPLAY_TEMPLATE_B85 = (
    "c-rl~YmXaAb|CtFf&C95PEQT9MY2d1>mgFCy6TeJ(vGB-RqCD|%Yu|7lf@kI<xH}qnks%U{J|d<#?IivxbR*qcKQwsW4t@?+Frm|2@3<OhW|(HFS+N$D^Em5"
    "CbLRX&y08M9u+e)o+nP6cbvF+?e-UkPk#L94jU~d<Nbg8Z*JmWHuk5--O@=|;%@?f0Dl>~ISCd%>yP{>4i??gmrp*g?v==wsXqy@R`6;*ixwr;pG_CR6iU7d"
    "2a8d65S)blpvoUAES!dm&>vUhzCRASjS5rss>5*6?axkvNVdHQ7vo_6?2PrH{m6fg-LTb){jYyw@G=e;K~288xhcx>YVqP!{$#8j&1MUBCLS0JY&?sg=TR^T"
    "+H4#ij}{&Ety=9Jx7qiGt>NylN8a}RXdplF|0ACk!RrOQuMZo$P2YN7oh%o@0KV?Dc3XQr`)j=H!MDBUji%qCLl`e%VBc#DcALYd^}c!>`GXMPiX)!}exzTN"
    "8(Z6h;J5-6clPSTJy!pIMK!`2_4@ZcyQz3K#E}Ps;kGd&{;@ZU20;X$d&5C*7?97$Q4mbwL$lf6-cEg}4*cjPj(&TuzBNdF6)>z12g6qSo9c4VANGS?IwJKY"
    "Xa;-3dg=?AN^?jn&X>`AjKI9HwY3${<$2{tQ?Z=Apl7{L`Wptrt^Th4SuXjFW~*MO0EkEaVD_pSPui@`n)BCe5B?V&_xy5WyTY1V71nB2SiM$ndFfh{0oH0k"
    "jb=Tg+P0(GeC(g%Q0uV9P;D!#?hT{vX}kzvlvRHY;E|6N_8Fkm%Ljh{h(CW08@R$sN1wAt(Tv>=W9+Jwym<mp;E%!aEMQ;WgWr#5z1d<`VRwV^Nw5g}eudqN"
    "V4qi5>`&ur97Lf)w3YbtDLZ4m+3RW?{xqBdZpih7R~=OZX;K9~3{I0hJn^IB5Y{h2g-JLS#HG!)5P<qa&+orHj%Ld#!1u(D%DBZ7Sa4#XKH$10pN261>d>Er"
    "<5N{Y0E$5JRnU7GLMbd7Pk_vf_$>S>Qn%2LgJcIFO^f@{Y&@>^f{}j`0ug%MX4Ba;$Snh%`^&}51e<^cpY$j|3f9Ig9-oXf)OyRsVm3ueJ72;w`cN82*@hP*"
    "ScXNaAeK<`S@`qn2%6=@u_|G-h+qxpegw3}sth&z%P7V|^H~UFKhg!Bj>5qpm_CCd1A#$e%IsP=L3;1Y#zg5F;A67Layem=Wu#w#%81>bvE<h5Q}{t)w+<KP"
    "{$RkD513NF+}MGgbe#p$lXC12gDOniuZB}#e$~Zn?s<$C;6?d{br*&Gu$WD}q!SH=;W$W;6d$UCFbevMa0cTBkS{0GOW=+M;pV68$!yLZLZM?H8)KjO5&Lvg"
    "0vLu~o%$yUklo_}<UO8Nfw@j%7)-#IRDC=4=WVt_c9>d&R*Sq8D<J3JX2~as7{Ikps=dWDwcJ~Kj;4*5l13okTf*?R>)*FNQMy2voMNy=0KV4<nm4w3>18^O"
    "!ekG_E0AH_VA0r<^lfhn^`4I-6f2#K52$owJ7xh;4L7LTBg6wFm}E|z*4Kf=i~l&2Fe?Hw4*Jn@(n~;%YYlrQ84yY%e9cqS5~MBsDKOE-R&ARs1OM_$F3?WB"
    "o&xFCR;#hStzW+y0pO|t|9*g|`zrG127(XAvsY~<=!H45<ZCb<hx0g$9Z)w>EZGbw69Qv;MfL!`0%pYbS^|_DAXv!UXc92YXKE><U<^C%#9qCL4^?@edHjpO"
    "xw(@C<vjxBaj;mR<OA^J0I1e$w4Pc7OD;vBF(I0OaIV3ET<RPz5<b|A_UI&bG_$c|myDZ(neTw%ECOII=kp-yCrF>3S-rNGo>;=Mak!o}js9{WKwgbt<==zY"
    "{bU(0!r^IEYBT9wpSAiXdur{Z=wC8K(Io6^2L|fm^AL$YZ2W2z;2e@kB}4FgG4PE=GK5I<PJoXKNC_rc2>ui15d64Y-KOe-hFUn~=yP>Wp?52?&c;?YO>d;>"
    "J-mcUFu`!3cz2?wczG;d={gxwAAa}T^sWd#yNSG7w!v({J4ms)T?d!jEy(1YQ}i6f4@C0`F3Er$!tg<UVC5&##oNE{vqgCE=AY+mlYJdb*!bejH=&mh$XHe5"
    "Xphy-Tz!8pxDgD8J6S_+I#-^`2yP~e53sQ(bHv4}kc1E=h7tbHM6Mi(qmdRl<h($<CA?5?(WV3C>Y0;kW~dl>GKOUZps3c$WUSlu_uq)?s2GPO#l9kvi$5L%"
    "X>K|t77FS3<QSC?lyKuw1R5rS)m5?<##O(MG}Mi6uI;KoYnf%t-MUSe@wL>PQDoRcGNd);=8Xz#G+LlG3!QnZ>Dp6Yg@a(m9&xpseIB9hNpBpXjJrs4iaTUV"
    "Jd!@3#*?gVJO^n7kPjMzPdl48GVwA?_5EI>-q>rp0U?!JYEbY9%UJeDd|hxJhPoO8HTK1O@iRzi#uGn&Sq&%tF(A2mhhz1}iQ$S1hM?htaQ%{Kccf;~8d*}>"
    "%3PllT*8Wetc_t|s__B@<w=T88|y%{&F`moWp6y|zf2am_Y+*RVTjue&=CZyf(v6Ts8ZMS81~jcX#gJM7S(dYOIN7QKpY6u!tE=REczpVijdZ&_2*@bvRE*N"
    "$ym#;)-$_AChk}ZVy=J!U9x+KFeEGT3gelNOr?-0#MN<1(66M3zc-O>WbIF3v)e)9Q(;@2{_O6UV$C1|<$V~A7s#1_3`XS^HHQ#VsNemx8cqkO>)jv{D<|pI"
    "-~@&j+Y3hM`wZpp#c3vur%RA!=INNF<9RrpQUa-TewR7Hc&QjNS3#u|D2Vnp+Bx+<phNF@_Fh7xM~=vsBWn3mpg;l~dIG4A#M1f>Xf;G%MjmXql0tOvVk#5m"
    "`)Xwe{a|mnou>g(sYK{Tn%AN9yrozjF&J!Hy3_ylGB_O~TZ_q*K{uJ<KMC=PW(%M^<*l9iAUH;ogTW-f0PcoL3J?Y4?y=1xL6aFA1B47-pR;?p-eRcT;{YxG"
    "#w(5Nd+AMXVENT_7DT*8BS&Xx=5B4n9KX|Cx3zaNwsuO&k*L%trhVt^el3yp(Hn!p7+;>e70VGwHHBY}<DfdA4!Dn@0dWtKmm%iWxxVQrXtE)0XM$g#mIL-L"
    "{K@=^xGB}{b;$XCt4b|yhO1&So6fkEt&-uja1e%;gD~t7cT)khkuZD(R3QRM?B!()6x*im<*>3{4SYAQXh@?#muwKtP&Y0M!rfvZ%oef~aJj^)@l$zSiUZgb"
    "I~0KHE&w;`wkopKa&hNY4kfG?r-WPYE#sukLvF#K>3uiWeUtVWZeW5M4T2g3#deE32`UQg94N+zM0CsWD7f%RxECrJABVI4GOnJ4aoD4VVLF6%2H>~7!feq2"
    "lf+9gTP~2xWct1CBX2qav1A#uLq8f=#x7#ulIjMQ<e?pT_68NV;H|Ys(kKS6kY!V271BD>2=oC=)tm`bd%@yWK<o?awPXq%6tmvjSxD%C7BWQh0%ZL#>W|YV"
    "(uRxfG+LJz5x&>h@wZxjrgWh9XA`inu><J1+v2o|V6J7qa?Zx(=_zF7+f57m6s0hv#>;ZyG~Hu~m%Y59n1V%yfs%TC8d*#DDjbGFI4mR=7C@KnOpie`hv)3%"
    "#(cQ<=2}=A%32ZmItxuSky3{E&-{pJMZ%|}%<TY9q{HYTB5yvZ8_P`_(k6-mHnng(c39I}MI1Q!q%zaSZi{>wKKx7*XFTybAI~Mofw-69B;6{grX_A|gKFgY"
    "(APSm`AP|Eyu%ujxUA6a!o!BHW?0jbWYZzwtd*V$n_W%Y;>4eZplWhkjgy_d3Kvk^MAkySLQ}%Y@jT{OnDTvu_5hu69vJ#K91???V4BVlnpSIFbWVW~nzftt"
    "@*DE<Vt1h}oP|ocEIR?88{EUap+D?*tXPssfc#8Imn5D<yv`9NZ2;v1*^Pm27f%*|CId%c8`2W_PrZ=m)ZS)ue_B1gR7m61-3Kchyo=dvd|5uoaUhruuEYs<"
    "a$sb@k3@QGr6WDa1m+(+9;KL&U(X&C3W}E<)OP??gE(@Qgv<_OUPTpY*x=!vW{^w@O>!IxYt*v3L+W#w?zKcbTSk3c=Hp`^E!n*0d&9<Xdw64Az+6#EoPuh0"
    ">{ZHrpJ5+J&jjtk(~~G`LS~%2nozH`y;Z-F!Aie390WUieimM8$rrt<>#*}OZacvi7JjtIiY3V(i$J|(MEN^IqN{LMqyxEYX47N87mPDBw#$TGT8dnT%BV42"
    "4v*q`VXEwcP5!LRPE$g!+S<+R^@xxjAo9&WfX=DOL(}G~=`#sMc5`~$+Q(Kb+T$cWbTAw})8ODxux-I=Hty)FI=kV}%M7+`4Q}TOaZat>?d_c#ms6rC7Jsxj"
    "rz}Y^Cr)QA$y~ox$T}%mvw+T!GL_olN%b8syQ6inR2dR`W<=!rSgI`la(%DRFxzc^xG+H@*RWLT6i`Ez2$A*?@1#9*u5bg>7r!p<%$CWsX%%p#-;*i?LLo{e"
    "HQ1(G9Q&>**S|7CL{@>Dfoz*g(`9dOx||X;)Ggim$E9*fKOd0DU6q!(2J`Vq*>;+gZGVTP?5DHY<b$3ZTPGZ2hbg+DPkFk$(#>46^ChTT5%ZR%uU+1}Rm_E)"
    "iH$Aq@|qn5sjvmB-H!{CY<vFXsATVLOQ*ZB)!!Zt+)%{B0>yUYI!~`Q#C;{x2CxnGB!a$J`QBu<WcU43XoA5M?{j^G+v}d-fq&0owiAYh0wg%GL$@x$TWm`M"
    "t($%BEj#M^!&uZ{IeUgDKZhVCu^~;HAQ=XEYj?XsmA|&m7HN{@^nHJ5wwq<xDH_Rl*%Jr_PR4E5bI!eOWmtNxu#(il1>(2P_ZY2>>LRF8gsWx5!WUq6)$KI5"
    "TXRm$#wAI}>M7((5q!~pi^LM=ZCvR;DOOsXup?!iC$vlgq>`Up%HM|nzn+d{6Q8toniJ5%5s&rM64tHttLHlPNWz8!&c6Ip5AL%JGS`fKq(Q5p%dz{cPHMjP"
    "bi^@OhuY$t$c}^3sE~>*xr9o^!8`yqg!VEk;meIW&-X%*5urgJMRo!ZTer5p&#Ks=S7G1l_XgWRBmbj(?-JC>xr{nJ*lLuKln>y++`O_X=7mH@{=5uX;%ir("
    "_&=FN#W8m4lDw(FvAse@*=!Q*m`=MFz_5ZVD-V38#;T-=slwKE0oYV{yV3HudVAU9R=>l+)HIJ5f%N;u3#DnAIhN`?3Qm%s2eqDArKkM3hBHJW#kzAKft^+d"
    "^q}@tY;4(v0&|a;%99+_FdR?fMHJ4jPUW`u3R1adPI$r2yqGy^9T(GX!VJQ>vgcjx2h;~woTWKuMLAaXWHVHQbw&ry)&*DR^K%MzG-x;#X5*f{^_b(s6RI0c"
    "%u{%rjn*y<^u{iR{BGeuTaIxya(#?Q6)R4kn0D(qOb>P{p>&D`JVsg^1GZhsXPT|NaQ59sh3##_^y?znd3#+zdxHkFYv{Bt!~WF7WO%OLx`_x(kG|noV=tQf"
    "$?-kak92t^2_@V;@y~f(ISSCegw7fkJlmjQw|&_~6z~H1CUw+d#~neOA$qCwdOX!0u*c?bhqd^_UT?b(4`$vyD*QDQ8fGQ>vxprjC>p&4`N%l}Q4q^Olpb8x"
    "O0a7!N6W!Nc8VGA*0PZGw%lAzxwU;oh`gtXmjh<caVBRkg{8X=??{g6B9Bz1@ZpB7l8VWR=m<?$xS}AGggvBPcIjX`Fw5@TFv{ABCY}x}sChZgFAGuLW%k}q"
    "NxcrC_X@Cc;aCx=HuuDreFmYGiK3Zi0|7^^FgQ+L+ciKDdC!>MM*ZWODA$`)Gi<}DiP|TYQV<uU!3p!`o@YjWe#W;aJM_n6G%V+zE|R>iFAC>Gg=S`O6_#?r"
    "R)YGlISibf#E^ssy}%y^mppdFLqT`m>)4SvGtexMJSI(-liVv?AUoc;foH+CO|PHn+R)q1ItQm3c6mkVDVDTz%duFtiz6dPmLbQPb5me-&aKf*Wh;}Nhkc$)"
    "(@vFR`jwmxrBqXrAIa8G_@?ShJzXJQ_GF4s8mg1hnGHpCyY6U69y&4)K5+xMLw&Zg)TP!c)w&<?Sk<e-h-Ai6lIFeXa3(kO;l-Q3TZrs1Y>V|T-u~6X$ciPa"
    "04;<nXYZ56%E)Au7k>Rdf#Qv#{>(yA_8H3gDg~YXEFJ47PdM(_=_%al7EQx@fvGzO(H{FTY=lub9&qYU+cXkT(Sci`iXX}_m0IKir|;+nsu#%v;GPla{xg~*"
    "C&NXPkuQ=CVmo^fdqvHidIR3#BaGPw`;7aQ$zeEOep<J~GLDw|mIg_q^S4_CL%=`Mu`uJ<Ktb42b0FU|gP=c)e23UoJZoEuNjj1sq)&X#qY%*el&eAV_xt5K"
    "OQ+e(@TOi-7vG|~_%46&_qMl(yD4#sa_=1@2m3b4hFf)%4R=iEY7SWRnIdiZ;e=-z#BsS{=IEUS(Xk4LL_^V)?3K3dHtmvAod3P;8(Z7OU`mmWcaCzkjdqTM"
    "fhl`oA<bNDdb$J{k2Q~GeE_+UBg{M)o_%6eN^vcBKK*723)YRG4J5?}O@!zIjvNPS3%~Ni#bu|Mhf1lg%vKY5vA}kA9Ps?OnJEpb>2g9`0zv#~pECGt3S@<X"
    "=J_o$&f7S<&OF?SZVao6MwyCw^VE+*zp7q$ON(e3l%6@38BJR$q?*o9;sQcUt|iC^oo3fR@}nu>ScTmI0X(YU1rWy(;9bJCUx^aw9M@=kv*K-X@?PXcOJxbe"
    "<j#(~M8H_=QU|}YP6`?LXn6JX^&nn>Kp^W>PRor?+uJriNk~g|(ah;yx34@<W-nK?WV2c9NbnWT?Hapf`$kaj<>v4ylqF*?wX-C(2@oe8$<?#buD%ZK@=^~p"
    "QpS425Hk$=gTYq%EL_lNZuPHvC?=6L#J<B2|I7Hp^hUmoB_8=xAc7kzmu(ba1X8k<2T-Ou=VhE3;0^pwjsb9fx%YvT*X%^Lg)~=s{5ez8T+#WOyGd@F9oM!o"
    "UYa+JTq!+GR-Pe!ir&m#bJ^XvO0VjuX&GqnRG6`Ec2f45J!<SBZOyHwwusq?B<bfjVR>dJI>?x|DzZ<RF}cq8G5@tPXYI7=IV-YPFlkJ-mO`<wUauEY8%%Mr"
    "K|F6c$(%p5*11Xp)=p7fI0Ub7KI6LUB%f()(Dw{+7lO3ywb>vT`pfac6@uXO8}RjY6fRWA6+x37N+4yp7*y}+z|ea&ks~pmGKy~P0dGgbiOb29dOWGH`~t_#"
    "4O0*05#bj{><^ZJF6=YlGMX)fv$%SUuTAD-aE3i&Bj)bV^sx`-I^18tX^{uVOCJI;vtJO_fy1@s4-FB#CNYTK`*6%|R@xxf%`RIzH~dDS1Qi+fr1Ui6MKF)w"
    "^RRtx{-az8cspMSa}W1JjCRW<&+OT0QgWn_D<3pcbCUZiKaHA6L<KSdnHxGam4fEuJy+H#+g5K2GFd<ozREPFHuqX2JH%CP0NQe1?sWTf>QBNx$gy$Q3&)u9"
    "^6_#U5UtcoX8d&EFZ?Q+P-fHd>9a~k%44ZvWqqb<M^+hAP|x<wi+XKP&C9B?=ghghu8G`)&29tDnv~B_&6HJIn_Jm+%oP3><6qa4@VTu?9?#p(%``7Fm`{R1"
    "=(956KU=$ab6~Qw#6^QvCJMgU?JN}H<w{o+SzXeOQhKz=s$%46pl;ahx>a+>tVmByYel|gmNAY<8po$qp)_2Q5!|S|;G&+~(xJiF8(TY+MB3`$R&qyTtN0y>"
    "$?P(&YD;rFO0AOWu7sQhW!$Pe>*ZX4WX!(RGAD1V9_~@uBDiQ$YnOF$k7dz`W~3maQWLVXX3MsF6zw207o{oY)$`%e%7C`pv2HZI+q_ECDOaMoe0#1oU2<uz"
    "WJ)%y!*Ehy7MJdktSbghn&}d(;kfi^6XUk_9J|0wtPHYh9B`lQiMUks!EE4<X+mXQqB`NP&xrS67`_e$og|`+g!IAx6${)*kl2VAHaabO(QLYARfTCA<JKNG"
    "GW6W+)wLb;>gD7x&*&X*Hkut9eQS>kb+x3g!{wyc>!jsUr!CE*%-m8TK3H*C_@Px#oj2z$FXt77F7dY&^oAfTko!~kY##xX)pase(D^%#TSs(3;Ia`W9H0SM"
    ";(DXn4IB$`9<o*Hf)afZ67_wFYf2oeAVaSdV{X#3@Z!lPI874WPAz<?<=75-Nubkkd#~BNWK5SiQWan2w$@3JWH%jdwL`+KK=0q$@-s0qi2GItjZ_cZ%x8Nr"
    "wn@NPZ?}JAE8PXGvlNL<IJJhm!(PX-rVRXe6bN(5PSET)t@JKul9Q=9ha%&BQu1nAdrDhmQfdmnfK@|a&%e>@)jO`6kaJWZ=O|D7HDlzS9EZKu9-&D4ohW8|"
    "yL*A(G4BsqhE;Nf$kFEy;Gz0BI9`r1EA=6;)7jXME6F`0S$L$GuZ`_lv2nGQjEqZc2+jo5ss4Hk^kC2NN^~RLEphL<ai>rWMBTp;$Y9(?W6vLM%SZ>qljX?r"
    "?7$e)MB%gtWtw!8^eMTw7&h=I))t1FwziRK<2W-eXHUWJ5@9Pw)$uG2GGR|+l>5LK`UTcPr{7f|-u8ETd(HJw-r-TDh31Qc&hW+$hRxw_7Fv%&9#@-O;p|AP"
    "ug5Kq&8B_5qVb!X@#1tG?El+;b8{0-i1?#78=T@_R9;vgwp!dRfm&AD=M}D1tK?2=pdRe8xDR+k+<#H6igJVSL{&DFs1geY-4eFVTRD<%_r+9ijvD;CFlX`-"
    "CDlx#OqKS}&Ooc11d;!IiWgw6RxtdAYJ+rbTqj*3O&_Uu*c8Y;K}|OV)m2MAuQB$gM?5?B{c!rS#70pt?3TV)VjOhclG;<k3XrS?4PX|ID8G+esRH}e->%m^"
    "BiuM4%Ot!G&>@I$=}aucxaZRT|Nf8Pum=}!zL_4g{^;V(uO|oWaI`$Vc>8Bl7W?paB0f$p-u_j{dhjnR-@o<HySeF$C6Y7M%X-9ZBdh^SDStNG*6PalYLS&<"
    "j8GC+2#5lD&4jsyi9hn@=#pcdgi0zFNc`qEo2sHvSXgNAJOA}aR=~osuqm-Q$gMLUu`5{kXlN^;ObZ*ZTiT4L{yZMd7Mp-J0(qVTL0GL0(DU5I@Fm2Bo147x"
    "q}OnAEKmR@f5h!SdTPWb1gH@8yQLD)(?z#*_uYS;j@ZSUU&6l&<F;dgvW)g$lFq)QON5H*^1isi&+K!oUdoyjj`Ws8oDQz=O<<B!R}0)RTH62iXNW8d7;spt"
    ")qo;E@qLOM?!_YZJ-Tj)CZFM#lG(AohYg1%y5RrH-YJ}{A;<wp8x!_4T#m;Ar9|wmhL7v!pb}f0&S9IXCmzAX#jbAY$?V-Prfh=$jBwSUED{CB2N><iu@KL@"
    "rN&-K@*43Qap<$z>u!l3>S@7$CFEex0n&sL4}pFOtpZ=bqeO>D64cwOY_nNw>Q{)(eSZ#n3K@d&`X|6r)cgILbAK^n0FVcJZ0qjU$@YWB4%-@S!;d}IY`~i;"
    "f7NWjyXtmn6G4A+4F3vXsEuf>wWRPg(|q}GHUVi1RUA$QHY8Q>6d|zbJuwCe+)%HuIkKs-D`X>$HoFa?7@LQEJedbwp+%y?d)t&0NL*0BBpSef;!Lm}LB<$l"
    "&hrwBX1K62Ik$-h-~KGTc>AYI_Hq<*>N!1r_lr|@^6nR$u>Ldq_UA}E=c9MuERw;S{h4XPOL_<6w0?wdNIh#ep+R>b1W{q@qglk&xOoOobJT~Ui?@Fv2WvKu"
    "l$l`b4#C!)4}`5*xVnE69bF7s-zu@wZfU#Z+%zzZ=ruO8Nst(k_oRim4F7I{NI8N!3TN&4A!$J%BjoNQKuY`7>L&lMT9p(e2PRu&Fk2sP2Pd<c?64bb2mimi"
    "lQju($qA=uT;4@w(X=eN21QXYd{fG91JUjC!tL06$A+syUIAOpYtn!<$#nYp?E5SnvXLJ@$J7tsU%B^BIvIo5L*o4t{@SsR2veXXKU*X`f$uLPF@HL_yv-vd"
    "7x?yP7jOSEgdMiLc>8}(1p#1VkgCEqyFcrLQd~Y9z5Dfa<grcm@T<qHd>hypmive4v1jhqhv`OgH*Jd`XzL?y+H3}+Hgl72=BD1v&3ZDtJ+hb0@;LA{6*!Z5"
    "-RPEpBAkjpubc3vR&T%y{N*Zzw8Sh{fKpHdkrcyRL??0^oYL5H0%gR;kjTm%3Bf>ctRvnZES*1ILcuZiRm$He!E^w7#n>YR!*NSm0uM*-)CEH(1IZSDz78g)"
    "<YnzH#7DE^dwpy};pjHO(d~~e1`$rts@(woaHCe=^&9B#6gEOuuQpn>M(Y9p!myC}nTgN4wcQ=o*r_#JJ`!kEcdypkd%#~aEJ439{Duf7Zwl<gBpWc${;BvI"
    "af}ypHD>3+Nt4D3xIW>McxpXT7@5Myl$@?aDp1{y=0Pw>gsDHm?74;ICV4H(rR1i?#oM3vrL=_7(_*wldiReg;LCQi<fn{lH!?EuWJ^<yiyun+cmL(TUA+B&"
    "r^oh=%NT;rn4TkisV37>A>Fl#1#3ULy&HKrh%t9S1f@&M&wAGQ9&bDfG=q91n8~~U5t^eT?@h&}Np@`WL+#ksW%-I2tDv0n3<(jO8hfm<Tivd1vu#vg8t^n$"
    "k69|rTEH`%9s50(U^t6jaka)+0=`|Ru0@bve)pCENaQYpyvkQ$3s*rt<uAfId2s!%zvS+ILZ}YkjRm)Pa*0dMK2J5iW~-A}o2wuiRtGewZD)@HK7L)=$A63M"
    "0UG`DUYkISt&u&l28^q5`B7bcEY0k(G+`{w?{X}y?6I_9EUm9U(q>_K7a?7CCux3Na0_AZSKj8y=l`~RtID9D@&&4^vO5xDmZ7L&N7K>8+y9XClNyNiT@d7t"
    "O?mSu?1OFAXpTTfD#+7p5Z#+z0z!}EGR!g{<oNzu0HH>cZJ&U|UI<e8d&zooVRK@2P_Jx@gEr1%fzNnW@)B)EW-3QhRGa_j+|RTJb%8)BH5!$a+p&jf1N_>7"
    "bZwqYJGI$VWu22#S4`gQr}sXo$#cu<TH?u3BZGd>fj{8c!_p(4$oDS9C_*i1A9q6sU`O~;(5!eI4zRC<-{WykfaT`4_~E->B|CzX^FL#+K}!?%8&Q<&_Y)MW"
    ">c$%%mBhO7ZaRB~rS`K?)3U{@3H4`#pfArr>lwXL&&oJQ{Vnv6ZfXCT;d2qkE~jYZjxXN+nhh@A{xy0Lu@iQ;*4)|NynE-X!y5Z)8G;t^56V5nPC(sblXu^o"
    "5>WIm-uyk+gD&3u&6G``!~}`{|6o9Ge>rEP*~OcG5}fcaL*)VE?n&&ZRdYCd^sHa`i~fio_||i#q3^^hnQ~x=R7zr^LdACbDpZV!yZ~K3mYi;<{&HcNUu3Em"
    "m5YR=MzF{*MN*;&P^%dJ{*5`3h+Shxm^F5|Rb$ICddQDxe^Xzz!qBX<R&}qo&ES6zni$A87P>>Dw#^UT;0K;*M$>V!N*O*W9-<(0FNdpotIcT6tQ_w&EKRO!"
    "x{vaPVG?&lIvg$+vb-Ca`YQbxx12bcO;SWe)fkvDYPez@$P7u8Lw=+~UQ`2#Y=F_+|JNDZ;9&Wt(9dn87<Hqf5)?r0^7s&8!sUL1Z2RdSF8$NADc>N&gV};-"
    "?zFt#Un?GBS@`ljvl#&sFz82XVkUHv?<`^hQR}-8nvlKz7R61N{NwZAM=pHC$OVhelJg>Zqa2ENpZQTXj|ZY~$XOnBBMK&nk$Xic!pCRyt@nU?16c9ixVlp{"
    "J=AFjLtd!D6`~V9IyPS2$@$SD77~vSpx5m}0Sh197krsu1rlU{H0<)R3fZAtl3*8cYhKxtsr{HnXQnIjW5me(Yj<TDXnp^ZzRbdu+!0L%-92#!l({3oj7X|t"
    "o=m0k_Y>j!_pZw#rg5%F{oZf+8}nN_;s1N5<?o%AAJhSsVA*e($I>3yZ_Hh359BxItF#C5dq-s<M`af0cL=age($$j=eM+=^n2IkM{`{gNN0L4WtsPKV3K~W"
    "?878&T*-|ol+i1CGNJMVKYn>X7$yVP7xoz!+!br+F{fkMElE1Jt|@&nh|!czQL?^)yZlnDBh1BRG$C(Nqc?07ZR`$WduCGsS7_HX^!UB~#spbDrt6q=^HIFV"
    "ePI{-_UAKnU<fYr9XOKFnYAU4x;rnQaXPfj%s6g6+qmt+AU7Ne``^v6n`~=Fqi&k)nr!D}XAFHMB$*o~p1{KVr;r_o{uF)M!24mr-u%r{8}`L?sQE+OY{Ic-"
    "9M1Wc)0RC&K1HDdwESNq9AAPO3EJf7-J5=4FaojpT?ZnOl$yx=@h|=X-L%;}skUm3UBAUz;ug4S10Ji*+TPZEfN7&Ku5Q6^*1DxjuwBq&;oVow<^yQq#H`P-"
    "KGXHL##p^dtHb9VDDzduN{!JT^h*?_3$D=ZrSRa&WvbWP{J-2~%8F}2kIesxp)D}+O9ZwURrxI&>i&boWKQxuI$fONat<j_xf}bOTiK2BxB;|7RQx+nSCoeY"
    "jha$b6JFBSxEAAg|C2jP=lj{#UKNjnVulLFVMfwSryGHH`5}UH47i8}9Z(@xoH#^_)O1uTm5k$6q%XrM@qc=Bko(XSftK(z$oLpMZ=`p!<KUDjDA`_4s;lyQ"
    "QVz3%y~Jl5h3;Ow{hQOv#W3-k5OPA6FngYgSu!J&2!bUU|J>4<zFNRE=57&V?$!r0=5AlzA3Z@N0FKqV-?+i3JK9*bQ4{FNyI)e<3`!Tjn2uQg-G4_G1|@Xq"
    "xpYEte@9;yM+T-SH*mfsA^g2}e>-IZUOMDt$~smjZ>td5Gu5RP<^E`f<M}1yKBPaN!hle;57`ELfUPdX0pX1Ec2<f+gBB&>J6MYME_*FGs?QzqJ<@m<I|6;u"
    "kvp!FgzA>OjM1mUC1tgT@7^rfkAfaM8u?McZauok$}dO2Bj5cayZhwHqa(J7JoNwJ9>QN5z%ln?IE#<`lYlovqcIB8c2^vmgAD(+QQX8m4BO&l&u_A(x-%F>"
    "$tHbK1(A|_bmz12sR--Byi+lHve^!9t0voH<~eGV>G2?SQ=Fsbk4=7xH=p>@^$^teKHoz;RYnNX&&8X!d52bAcp|7afHfd{k#DPH|EL{%YdVN#;lRciKj936"
    "+p||w#B~R{=-UcNoTKYPt4#c8+#o8$jo$|RXf}Xnp0qSRG^+6H?lyf!{AfAx1Fy_O{J>Zs{Ov>$JlRcgWGfp*_Me!dv;!0T3?%py44mQ>Gid?RI*iS_3|mg("
    "v5K9X1~&dzz#f!`d~osRzW_@R3<eM~d^AF)ZBs+CGx)U?iBL~(tjLNVurG1QLBH=dN@Y#Nlp{I`Y36GlLM5_Ah=?f`ou&e(0O|Hdn7zS`OhS=7eD_U%B)Rmv"
    "U(DDOSQwZz=du@X{y$ihvRDzXol-vYT~yYM6WQ)3vtY@YTtXHlS16SB|K;zNY!QM$k&%i9-+gxR=C3E9s{;C~8JE)Jw4|rTHX=WYI!uPvS#`LhDk&>e_8vym"
    "9zzY;7+xy<H$J~$MX0$sz^z?6*^5iz&k%l9N@tPBnI;Vsi4pi${*eUnk}QZhBC$KUaw*mDXX;B@&33jV5+y6+uX^JAgBOs@gIIupqx4SAPDM&Xr!bXH2%LFr"
    "gwTQJZ;%ccm|;dyHX80Q%0`MP7>#HG0qm&p)vmwIwq@WNhQ6J&jPfSouvLXEH(W_3NXNQ70OjK4yI=Q(!12wZjdAC)-1C>gsmL_BMRbe|TnXYnXr{1Hf7NHx"
    "Z~qKUAS&cvY;U5$^kL`uuphY{u5a|uxz5Ggzl5*f{>tZeEvcD^{`2riW;)J1nvhCEjMM)!_QQ97ad?;A{o>-yFP=EUQWH}k1>X;Y(~_&*6HIyI0AYruaQ(J$"
    "X&h8{pUqDtX@Mb85ztOGn{5NKc<wI2-re6+jIzZ()Ly&6_u7q8mPy;#YCz_&p4}F8>u=v}nUdpI<QX-M(GB{{nkNlx7MrlxM79cHE|E|oj(pq&?|xMv;f=N4"
    "m_+wJCF)sAjs@*C|Mq7hgd{^U%-`aeh8H7I-u>+agk6(t2m>@{tBY`oAslc1Zpw~F;e<U2#=&vqPbB}HT)h3y?B(d)Kl)@}Q{E|l-Q2|UU3`b!jQdeI7t07d"
    "B3`ij_YUtoJi7Dz*5Q+TU)^C{q**^eUi1UZyr#CE2%}=b7`W~ahCclH0c^!ix|tu|`7yLnO6}SbDSLS9!JWsqo<I2F_8nPPrsD<J1XkI*w~n4;ax`mPDkEI>"
    "fMh54ZYN#H^FO4}gIixehhl$l=lO$s55IhJ=LiZvTuypHR6fHb$*-Stwea~QoGuqZe2&SJS1Zri!qJ_>FCN~;79Idb*PwEFr(SV%@hRJ>(<W};`TW+G_n$mJ"
    "ynpWz8OK&VtBl=KI4wKdz>X^E5z@X7zxWEE1mFJMy(drZJbwNFTWzXK5Jj0!;8hvb=I%Zl%=$~bT&8v$Ebfd0{Q24Gy#anEHRk>R>r8o-qeT=>kKv&PueqDE"
    "+^n!ty(DUkg9Y=)V{V{|q1Mx96(Eyw2pYBVPLf1nM(eWaay&L)-a{`se5p2cyV|jOYmrnDUPAJQFYz6`NKqtR(;t-c{dVLZ%a^)o*wl*$++^^1I1czhNx-vG"
    "g?v!9gfF9U>f1eTVpCsqtEdN}bp+eg!%#0DMZp{`CqUz;@UfnJk>`@eL1Yhe7%cjuqX2};v8W<8vNRk+-}3xoMV?_6M^@#Su#w?%DviqVy)TY#2Pd$r%bvW8"
    "1vX+CiQ;U?rv6EI>@Q|8f-Y3Y<C#O4;h^L}%Zp_+Wqgy$i(2HX&G3;wI2T$N{Fx0lYp}}}W!+c}FT=cbjCZ@ApTnR${6jut4t~6YfOq<X2Y&zFbP<eyz&18$"
    "DcFin@UG5B+;V|U)IGM(8Ug`V)<glb@B7ow0Vh%Vhb7X}UC^5|C8i_5gIDa!$M=r{KY~7dAkT66RX82YUez#dpS<c4KYF-r%9s+ol-w5;4$E_oTy6us0P)0p"
    "e@8HQ|2Tlki+hV;QZ4}v>xBdVIW+Km8I57p=9tvJk0;&Bfp<ny|49hX*>z@*1sN_PZ2Ha}F{i%Y$9pTf<{*x<24T!Z)YWS)n_4@D)D&cWjm!2T_5JCWaR3;J"
    "JB9B7y-KpGfv;+WfEOuzy=LNTPSIM-4bmLJ)kt#7!zhmmRT29=tB?d<7#{^d@c3>A!G{k#*;F=!q5#q6iz7lhIOvnj>mn=HyG&im39Ff=FA1U|nx?<k{N4-*"
    "yw`CcWj?jiXy~AUvVcHO5Xj*5C*_oR-vf&MdXfIdr9>~86*S<T!9O)+xMd2C_>!CaYtrJ6l#om2ot3T*eR%M{8m_^iaLr;dhwX)~iyxm(`^oCe1<=*{;9Ln+"
    "EnjotHxjOEE*4_GSNtf0@Md9!0po?_G1srVu*4|Zr>iN^`4%DpU1kHb*#Re+Rw0;Qn=ZB!TawN`5=x|2pWHZE>R3pF(&&1`5HSq>(emQWuP`|5?Y|eAd#zS0"
    "bu?qK_zJ8OvKIf9A4(=(cC37bLjQoB#o_VPAGed2HSuNzpw$%4px4*1quT7H#}=b#_KGhI4=pK|emFv>sC2Ms`+_h!sAxoq>9rEQ_noiEc-GV7=Y8OtveCCd"
    "GNfWIBy*y#YJMnb1B{k$ou}CPvx-A1tFSXf$~G3L;V&*o@L6n5<}D?Z%?9Bx3<hmHm|C08UX{I-38yqsFQ4Hk;`USg^K9igK=>r$i3tXndg9L)sHq`D6W?V5"
    "LFvKbctJU;Nei)1rzknqi0veGbR8~7&<Ky^8^Gk?5n&O~HasH-gFhmf2&V*m=ZgTQoK>E|M#NyP1QSOB>FkXpoGV`(34d`LmJ~;k#<Xr0rV-&QoB0$2xdHSH"
    "R+19C93WUp*1_0`A8GCH$-M`6?%#WO=lLIf@x=pZu(iiF@p8S*O*AIq^9WyzD(b2xP`p@^jX<r5l~Tn*3^E9qimq3EC}juqC6K{U-&Pz$-1&6IgTL+#+E((g"
    "if)O1(^35uHVTa|7#Ce(BG{U~mtolzCJqs(Px6>Sh5eMD{Gc`DSrGB*i{--phNo%7$F>uI6*e0V@peQ|i&OwyySBT%lD8uFmkT=W)OiDQ=B^_Rdh%9WIGukw"
    "GM$3JJZwRskUkV+0K##y=t{8Qct}-yKnB!{G@2l}`%&P(eBjT^o>dSt;rz)GG&-Sy<`u_l39-tLfMEntUddx<52WDC0ocmik<6<ulzS>=N!ya9K_$@nN`_z|"
    "QWtbH%^vQg?L@E;4FCn?KWaSuUiU}dnR>BdAVZ2dJ&90k5`WuFri1TjBBAFSk7lo^MN>~#%tzdC%EckWOGZY_i+G}BO}NRJrDu@-pg{03`qP}n?9Md*TAiVx"
    "v8xRd)F5f8c(@+qvj>o+rQ57*jTaph-hsg&g*l?Oh{8#k%;p*lLancXNz1vuYXk?$En}9<$CWw`D~FZa<Y(~4Fc}0cvGd(8S*%Rs*Ghy%!9GXYFk3FlWv{z`"
    "=B%%+>ZJ}_Rk%$sb;tE8nK+S;#^Jm-!&4yuX2<K=s|dFE6JrXw#mWfN8t|-XnLyF235jDU4K;@VGc~}KunX(vX&Jp*at<1|%tF@MqzFMj3ScCX7JzcW)0dD%"
    "O13uCf(hs^x&^!`4{t0Frh~&#I35UEVK*TN3=y2U5h`gVcpdcdf+c@C5O@QFpvYE60}&ITdQOyaa~M%DnVkfVVXV@7FpM9bM4Ff?lI0+@3F+!sO+aC8vV905"
    "VfiF!M4F0E4!E#*ehxo|QjR}AFO@vI9T4GSe$NbV1h?ObM=eKk@djPw5wcuIbtdfmHJFb7uslG@j+>(Fc?VdxJ;)mLV^HUKhje?#T{4z~8b2Tx44(T7&_Dz8"
    "8>~0H=aYk<qEH71A8%{~@ZWx{=5WTFX~z{P*AB>LgObuq0gVjgfr{sG(D(N1&dPfJaWc>AstL1&PaC+?R|fH#bgdPuO*lefcDFwsTz_8v<H1>T<yEb}&lj`I"
    "Pk&r?eQB+{KlV050gr1F&o%?90X?n6qe?uf#4x>4C74u#xH9lhD-)n!qsnREN6)&G3gaM17XLbas9fiTs*TEZtZ^M0x(+Q}k7Hb_>0nK9=<Yi7cRh&3Afd+1"
    "F7L;KpJ=M(b@BJH{Ow(DBv>bw1s7%I;BQz(E(S+wG23Z3W3~ARG#-B*PJv>Ur{2cKG{J+Z+67V9#$Y|D=s>%y8kKT{cv+1E5__8*AKG{UvpLq`L?F_@JCn@}"
    "4qkk6Hunc&H{C_81GdwuO<q0_jx_uA5s$6puV1uF>gZM})ggyxR)+%E-fENfhl|Ho0AKT0m__f$u(D;@m&&5$?(H5@L(=7obSqVNe^tlOUZnd_%Z=-K2LE*R"
    "Qq(K_CwJn@RpzD6&+DGoNydY{@RGyvY{rXhZtcLDX%L$qzmFd`cgS~q3D1tv9gWbM`$30Rq~lox^o3uqBoM=dzqK-}Cm#UmQZvW(#a<MOBRv|NpJy@+wcP-r"
    "N)`YN49G?n8?D#}pB8+?^9ShCI?(_HnzD?>=jSg(L_h43V2TIczI=QS<cj%hit>#`{#`8-Z>QP%Kz?{GyHU?q>q!Vqf4B^yQ|@XjL#q{#uPQ>d;_D=S$`QbK"
    "vL5iX;{VpdX@9&N0QgD=AlsKVN^SVDa*he*&sEp?Npxiol8Z=u8VCxh07T=iu>hSSk`w9~C{IYoG<}Ve;3PW!Q3z;M+Jq0ACC|C!LG+m~B@m&Pt5FT3*#utu"
    "L_`q<VBO#~u(N|&5B}T&83jPAiYwr%XEGPUVW@ZBlS0w?IWlwGStZ$~kXX3@dT&tSVP*FQnho;z-A~kaDJ1ZxgBo3PhujBCPek(7lSWe<JAL7)!s68ZCuP+u"
    "Z=FAQ{9mdcbrV)(bH!I-#KHK8Uq-6Odgh)&1yd7t<rH|7Q4GpNjVCPR02k9cKfi{r)b#Lo%^wVS%)<RJh9N;$c)Av?Qo#wRbSRme)BxbHB7ikQrkB;Z=WeL<"
    "WT|G1X0ODMp>*P^@$9dlPH@#qydQ!R5hu<*&@Zg_E3aeOCr+pRTY|F2Wmp~j0aCvyO!G$K?o)cPWyMM!S+Q@de5S->UKxfE-SJ^e;Q*i^n&r!jfhq``u?I^k"
    "F4s$j(a}bK10l_EBzK0o3)|x~vUa+wSW~H3_h))8OUMYfm@@I*_D{=$U<?~m&Q;9p6JzL}+$HDd1_#x9#PcdRH)GSJw+j~0VlID3<XkBU@R0~nKnk#!OUh*1"
    "@XPC1*zkIRL~+XRZWr?^>-ZGYJY5s7Ysyrau7`=M>`L&C)Zkt<5n@!<#25$BNs&+j=Fd>%feP;jXQYfrrgD-1TOEmi;Z-aJMpsJ&kLun|tB(KYRSf0DQS`bl"
    "DrcMMA{C`pv>Ut4y?T9>2ZIH019f)0tl=pE0!mWX4Y35oa)?k=@B}i%{@9<)b)%JfrQv;w<<$6ucA7mpeHeWf34L2g1?$Ep5MP*ULMV#Ifq*t*69@clo2c--"
    "Nlu(O7RzbX)%?U1IEGjC6unJ8T~w@+cYJ!*T!!Ivz>)G+vcnX0#DFef@(+;Mc13I$Jo31=ZdHYC0O2e1P_k~l6W%l;>1yL(db}8Q!t2*<`_@U<DETyeR&gne"
    "#0D8{Y~a}TyHZgUk2ksUXn53+iBWloe$*E4GcJ6#PbxeYPCHx?bLGlxOhLeGN5kc$W4#ve30)1Y%)pajS+{MCOTCxFQ?0H{v%M;eD<cY48!jy$*Z3g_`M~T2"
    "Q0O8n(Ir7RK`RpzvujO3LKtLPON1EkAnp={rIVmk55~cjEs9u^aLV7^2JxbMZ#qQV!l_m1p^1x71>U$Sd@!8MN6BlEhoKLk7TNIv976#Bkxg)r`0@ABU}FGB"
    "%yfVgs#hcwaYecV!x%52yWf?)Y;34kH+kz6QAJ|}7fGv{Fskar-Y$f$q_dl{uT<OeeN`1))0Jw*?8$CaHCE;GS<|F863Dg%h^w4%ds#7u+@APF?<ZZG5LA*b"
    "DmWIT%x(O!GFCp5!oWu?eUsv|NjVHm$Fc?iu{MVqr%^`s0g`jwW4$QwUpk461z9pOxouM8QrpQnD!uz0W6Gv*=6ujaL+8&m%(|w2X5o&}i0WQ8A%`j&NHx1}"
    "HL5Cia>iA@W^@A@uhHbqF6q|GAF`uoY|l}4WyMfZq4E$f*zk|l{=7FZNw!qBK1n_knl0XWhGS5?GxW-m>3+@;Sn#AvHziVZ)F~)Eh2An84@5{5Cz)}XZ?L!`"
    "j1)G_jd+=^C<D8AUPf&)L99$uvdN<(2QRXyv4dbZ1j0&+VuSQ-o<U-!)m|Tokiaq8GM+szHL`gW3`0Cbz=4Av_oolPdi;!Wr~6ZmEAY@sg;_(p`+S@SM5g9u"
    "gSh<^&49e!6}MgT&V>NDVpW#BL&0hF;#r0Fy=ohvTX{91VU$0c&BlR0^=dJiX3Ku1=XLje0i|kBN|v4EG@PWb90R&fpCut&e5JLX%pV;~J;$6Ovt`^BEj$%}"
    "B-gGso|y};N^flN_UyH*s_*7P-0Y^0p_sIg)^i|^yV;5uL-v}JcEwuN=F3=uLcV!ge@1KAG@MDml8T;_410FyoT!(0S?RW)N>+x^EvB#}ao_}_2TYb{&<!3Q"
    "QlN=zsWz-(2*Lqvq{)aLWQ7N7I0leYnGCQZVoQ?FpVn$M-F1b*6KU;PCc?^S<X}ARW;y8x;Ry{?g`zcnIPal2y(89L?|hvKwu&X0`B+tvIOgk;*}4b-#4NB~"
    "AOdBwC#YtKq#X6y)%Rbxs*BT^Nqz2|OIpgQFH#^=bgtpK&~l0WuYY0$5|R<oSOl!EV*p(CC*h+JacROwLUb6V8{7U8X)Q{-Or*chWY^lksP!Ln2qMaX=Z4K-"
    "$=u@SV#mSQWW|<w*J8sk5Z()VVY;L4zVz#iycJq$EFA}fEkVM^KR3IrJ^tf*w_!8dI1@{8ImzP0369Ftx7CUNlUY<EB`iF{E;n0yo{bbRb3QthQwb4^&AD^i"
    "lMyZ?Mu3K9v^tmND3Ep{8kanytjxQO$+P(NO>Fe;zbARL%y#2oaccpar?&)ZUP@9Qlq#9^&CW87;277GHj!2vo<paSFln|-pg1Qz2?tZ2irFbUoE&kftk}Xb"
    "UBj`$vKtQMlB<fbWX+bfWQp1JawAeTaz=zjO0FvFkcg5OH4QJbglaZpy>8m8WqD`xe&waJ7aev<_D-<CYBT>eomOFpX>}JU4V`9e2U?j%er`(S2FD7Bv=xwb"
    "v}aYzx$v^0B_S*&43s%WQh9((y8_i&LFxDl2j~MzE`2=U#`Lx_#pf7bdHJ%uJVLkoiFzZO6%ES9+u|&Sd~yu>K`;n?9VKCUFO^kT#m8vZrF^e#&UTE9F0iqo"
    "e(u)~v=N+!;TTR#Yj}N-#G_kA<gzuwSNvtyif-n|KxIL0NEXEuPZCcxY<;@Kpk}-JAOKFIUAfkvaOXUt!Pk07*pvLNjn+9+pw64JE1a871KmB>8y#n{SQo1("
    "Cr_5MC5#URyj?S>#JoX`SW*>|UpF?y=Z%eZV+A~vEm^(echT{WM#1!P;15n!*AZ=v$~ofu`T4c{p_}zh=_79q5{s)ix!omaZq!k+MReNbfxI{#QS7qgIZKdD"
    "4sC^fMnSwBFSy58{OpSAc<}nvaQv!T=o#Q8H#Ve$Sv>OiUDLOGM!u9~|CRTgM=}j`Ju6~=O^w=L#=(FVGLJHf_ckomV#x&%q3};j7{d|_bV|&Md?6qD{!}<F"
    ")h|38wmcq{5)Fo@6WNzT6A`$EFmq7jMJjxOJx(Zc=*WSh7XW1W%Uee`<m~NYWWOrB&<gB_o_9)uGp4Ndfk?$|XN0#T{zx~2$YLal6sdv-(tBqy6v+gT(Q!(*"
    "smgN~Ub|k%At~9TIYJDs>1IahaOQwT6u~w<+E25Y`m+P=>OMHAllYu0PpP)5V0XkLyABkeO2htI2;5pIIiLbUIyCXMf<cUv=~`2hWuH~lAuCYjc)?n7>Po;z"
    "JrkxT31ffJRUdIi+;|#Jj}LkGk9k~uMX1*#{uQeUrQ56U!(Tt*CHR|9#R%{mmF4c0Q>yaXsEJiYQE{xw+e}(=d3~+u1qCOyg51GY3|hP&4Jvtf&C3O1WfUgw"
    "`GGY7SN;MBFmD9@T5#4-j%VYOV9+&P_gy~b1JS5HQD!*yZP3>%>lUlXvY0(~)$lrWIYh0^W>tOtG-<KJf7-Sef*(m=i25EX*t81#p2lb$>Av7U$547}Q2ykM"
    "4q!EZ4Irp{FC-Affj_+n>ii#H2l3#EKc>oI+hDT8G}J@|8-Ydk&fp)H0<La|DOKcY3=-+9!G#ltrt+HL*PK_bG(-#>OGS;TS_vW5yM&0hhO{p66a^W?hi(zF"
    "jNq}<m5mLOM@T&I1VSEfG(r1H4s|6hc<aiE##13plqjUa>R=J3{n0Ec`}|J@qhfgy2yrY%yw%~6_zFsRXX1TL<umTcXUCeV1LVNid7?^Bl@)>uDX!vslOhYh"
    "r-M|KoXBX`7^Ca#TDMh6&Hxn}BNx&fBdB-W5`;d|s@XGQjUct=k=K;&#tV63dw0+Dy{T<|Z)ydvPHZD%;rH0$f=1lCv+7t)6TwTg+F401!yh}DLw^ikMP_4-"
    "Cj7p}hG`Ftx^hqB$7_N@i@|xO=~asE+HUkG7;i12lxJpy=z+UT`9+OfHu*H;cF9l9<j+;<*;8`M<R@nlym{fZY5pJg%x^1rThn*kUuz`@`VezWuG%d=MKZEa"
    "5_HL(FINz)nZ}8oCCNaNH>F$S@ijyLGIY?)I+vd13r|b>qEdHX{5?oast1y6fQnN}E@|~#B0h$inlrz}OBY$`e7M;uWV)9@D=T>n{Lh#ks5REy+-Uqz8f(-h"
    "<~vgxIQVYpcI&n6gGO6EG!B~TXY-)dZn#WRs>SadQ!csZKQyX<8mp$R=&0&9TT@CKb3DiSZ9(3%xCJ?y$ptt{>V<73!pw9LNJg#_o%B0SoK<om@!5gs9r|5R"
    "eEEvlX%uehZ@D;P%o>yCn;XNyX&PkuqJk&>6xk7M{RU6AwQJ2mG!zotm!Y-hJp9`Vt6Xaj;Ata;5Gh4FL;tnaUy$MxW3z^WQ-Bm=U17DKYyIlv!gO#H^t*|4"
    "NJ1)1bzL0S<U*_%=zdpJ_Rgpctn78vB*o`WdV1>1YDH39;k5A|Lk|&W^2Av|*?_lVn~c2HKw7;p$!<OzjQFbf4!U!*eF->d*a?$)sa={|Z^+_cI~yAUy!hMp"
    ";ut6YvaBL<nuhU%71L9kgbOr-$71(u0%izuJi=DeNMQ`1pb;&1vt2PA!&2>1k}gf_p6a6+d>s<=-fS@nB1vQQ*&F`yQPwbN6){#)-ZVGx4@d3+(n{D$QZ|<$"
    "XBF#Ye2}DBbs)zL98*GjV}n#oh&CXsYz5>KXNiQYXVOI4BPEP0ZJ@Qb`Fd`UVlHZwmX|?j=e(*UiCk7WKTo*N`FSJFk!a1rBws^{h*`X#=MWGwSxa$7ha+9j"
    ">F89*$Yf<t{w>J%(s+fb85L4;9|R3UpM+E70d*tWYL218ONH_kqO}UxNFHUE#6U;w7;AV?W~jGfJh^<;ir?oxNWqR}!D_u$-}y8_vx*pOC5y2q!*Osr?ugXk"
    "G&vH*I`(65o|0#rPQXrXcEvC^Ha?1+xT>hk;wc>}rPw%BnoIH&hY$3Fa26gI@1&Y~iSqbmf)hHn!U){>SVH=GG2KmvUF7rfQgKmocR7M*jLkg$brqT?VuYzL"
    "c4I^NW_d0g&u}lx3{RXCu5vOG#pWCg$?1KBrb{6>(r6KCm|~HnAv+KysR2|kdALa<^e(Y=7#)Jkyx`az;*SKA_;Y@qj}~Oqm*3t*G)UUH1jD|J0HF8!8iL}c"
    "3Atn@4Fe{!j$K~3`R%4psYSXPar!7bAG0l}m61tVf2XWndeOy;{CL*%F&si@WCJp3M@@?64KwTCIx0zIHY@o1BO8d{4-U|M*P6dZ^7HdFmb0}2Q8pk_*=WY6"
    "oEbb(Zr}1S!nD(d8|O?Na(w+Ym8N;gnur|WzUNQnzK;9;IH)$f&8CMjpFhs3beG@O<ftWS!D}fq%cn{eM}RGUtxB94B~E#XAM-vvWfCgn)_#p1ZS`=<-P<1H"
    "1JXtK)5?313Vn#>SuzWRX1s%u^_BbJL?PmWllzwR20E?+L|so<g@;J~FW_l9c0^37$iO0*SHYF;r0El{^ao=2v8Z!zAZsO=Vfg#*C+fQ=3&0y)l24U?$9ymj"
    "+lX{Q@~O8QmDlZh<rMzMFKh*#z_S4IEV&g4P3eU+VH+#hswv5&EU_f8k|A{W#b1)QM(n|gUs2gGmFd7A@T*t4n3|@1_PQOMNCf^G2B;r?yizrK!Erd1AQRDe"
    "a>>Nivv@*ChIz=TiG&+**?Y#9j2Dw<)X-0&fc(_2^qzIaXE8gGgyT$~EGBUt{O9O&K3kOi+Usf$_K#mXg}<jB?8<QPbsOJbhxf4K@Wqeu#p!h{058NU5M+e)"
    "Q2X$-tu~6pO1T1Ek*mHm52yW6siLOI2f0eZ8SVKslsd`#*>6Z^bgET<S`ryWzHW;qp4OkiX!xrilUJwip{`aPv02j8S*`=R>x~3N^8FL&A9?HoZ{aDOpKDx7"
    "k`#I7_SFlE*qAf~8%JXkyaojuLsP}5kfw`LhyEPrZPcT&s%%9O3{x6GL6n=Oi`&7_Uyk_&CY7@}2$&$%w0STAAm^Idqn9d~{WWVMBR@6=bj1l@eJAX9n+j1R"
    "W*qPv5l|!+80Xw4YHxHevp2f--sqCOVUTFbf8?;lmrqs4o90LLy3(j@C5+B2^ZI%jM^QE3;;V%5ncq%%jUT6L03$3)`<-aCcGITU0uSX-ir3X!Gkdzc2rPvK"
    "NWR;1<}9k6x~jeJ#2p(*d$TwvII}QUMkkxQR%=7a1_^%}IEfyGSl-4+;JA)JTEsDvFzPsS$51`9E1{9CBPGju36GGkB{}lm^8B*Eo)@}2r;M_KDXU<dl@SY9"
    "IjPY69>jn`Gc6P2hIy4_qFp;(68d#2fGm#aF+Zc1mFbcoq5s{?V=o0l06IC516mF?hfmTaq>~|tqZ51-!JwM}(atReI<Rp-HYsVOqc-!x;7_m3dp2?^Qhx+|"
    "+t|<};GvQF=_WlqoQzWULO*wC#r>{6fb6QL*@N@wzw0o5)%2d~Q|DddSUQJhI(w$b)<kECWt{0jOTLaorAcLznqJx`4Q(Ij0`0O1?xd#aUsKwr$e7v1KL_c4"
    "xo@RiDrp5P>hlOl`H1_FAHzxOMap%NB#M;B=02vUJellTZ+QwXDvpWn)K0b;N0S~9dxSCpKkzPd_=7eD_@nfvA@mD>EL)Fc0aR<M^x#P~=JD1r4P;+DVyk#Y"
    "JQPY-N(;ko@R|2!89Ps>eXg9C4kI)Agt#xgUO}@)9w}vx8$MF$NU}U{S>{>7Wi#xgAKWH6*W^?fKMQ+wR^*`BTpj3|yUm82Et8(~ZgnwK9?C#;%>uysU<TTC"
    ";EyjKDA3Q15pV$jp(G)YTM+SDq+S4F2~a<;tbJB&U3nEIB#!Q{FON9kDvPO#QC`7+*6cd;HY(F57LWPWxbld+NAB4OIjs|)RT^9&a!K-*d_Jg=`{N4OfSeb2"
    "ikm+q7ZmBxD(a-3ID}Y<gWx54=-0?*)M?c{>7*`A?()V92sTn4#4c?|mQDiCM+8y}F<IK*+I2^ILM~(qHtP~aNbHp!gowUsXCEn9mo(N)S>q3+C`lyYPs--`"
    "bVDXK8_?Vp)4?VO+{K9!^@w|!h?@Gn2%^HMJo$pe>8Upvf^t&85<k+C2OWA+O$r>V#E&^ha=CX?soilthJdE>9rAW*)s!$sK4uAJq$ven0Zu{PN}j&TP#(mE"
    "udk5m%lic76;kw=w33LuGnz3*<h20!884xu$MSs=^(YbdnmE%2Lc5i1(6A?`ForA9$(iE(<ID*bdZuj`c2aiGsw1RDm{s&vsnmJ$2+%weKO7pCPWjf@?>2;i"
    "ThX<w!?`Jf&TCc9W@tM_kgupTKMarYy5ey-7X-vWKb7E4ZPsqisVG`8D?v1ADkB@TQKCNz&}BFX`VyrTY4u!508)!e)=3=%O<uWgtfn2Fv8Vi+FL?TqA5FvQ"
    "F|;Z0{LpxrgjEJ(X;tN!%PE$xWG4I*G^~=lna<Bo<i!CfO<=+*?bjDO*>!z@!a8y1?2Ao*9*T=0s(iFXFKv+bbw%C%s5X0f5Y>JX<0Zq~;UK?-)8R}^34a{`"
    "D|tR@?rd*tQ2w;BVYF?8U%;&Fj`?Y~6lA^B8wEc>LS!8q_MYpdS(AZGWeJ_Bg%|YYE1HDOy0|VUXSOJ2WN_>H<FS>?O$)7##EcR<uSMtQ;%1+Hoq7y(JMrVD"
    "%#{d2i;-5aY_{enmpIhDs`ar)KPLL0Gl6E3HLYat)ufEbqpyoyigs(#?aDRmo#k_s)Hza9ell_ryV}RBhzauu<0CE;-bbliQ7aF(Dt*tnf7+s!IedfV8A*%e"
    "Wx4%#|444f6`8cY{rNk*a`@uyUrcMIEXFBdU`&}SDOoV%Bq)n0*LJrvh_U=4bUDrE*Kl%aA)38PtWI799z`BpJ9RpIp!rk6!RxLnTjQWP(2pY7u$?GzI^g0l"
    "?k|hb*rdlm7a<`HUf-;H*ryvs)=S%F?(4zpXTlODy|0%2Z}y5@#b`{@lNGRtvQrCjNPT#J1D;_P0p&mpj>Zj(zRbLZLVB-v3`Hk3>k9l+QR+_LnTn8J<e;>7"
    "A<19A&Ob`FtPF`SHpd{);N^9noXM+J@7-P<NM4!6;p|I#@F<Jm9Q5u7c@YhCbG`dwLu9&jwwM-u^+M<$&bF`rK;IDY1J8941dMD0eJtNex7Uqs`85j8e%)XK"
    "5DGy9Pory*fy>D#H+c4q>Mz`v(hvOUh{f-|>5nAMD}QpvS=(Y61510MxuB9mR<bh`Vnu%fVL-$u2%{7KD2Cg;6-B@)!<hf&wY>xUw_Q$uIS~6V7A53E`{^^q"
    "wJeFCqa}%8{fP27Kv0{2gbOV6{Jc~QT<e&D5bCWwU4(lHqleyl(tv{5Ecj2u04=KNY;y8ZFOW*<r5Z89Jkh4*htrrxdXD8WP&*Vjcxh`9sdOr~83h#W=-xD3"
    "g#JPa0rbj^j0`6tk4yfUKS$CCE39`CP9J6Grby?G0D4-ayy%NK(Tpjm$BOJ-wQ9u#C+z07w1uuq-BFo1IdR%#0su3hzOi9olrrmkaj;0guIP-xN=}+2{BC2z"
    "Ei~B&O8E5~-PBba>7mNQ8@voP#Pi!&#Oo)Dlk%rp(uR4c=F%u4@@tx1sQZ2RLK+_luXY~OsW^_0dY=2D6$45c;uY=NIzB`zN-@ODnk}vkA*GD8+{U#Mq?e9)"
    "bBx64f`)^&w<8oh7EmeZch11{^K)%!xLSn&ckZiMeIWOP0ql(AHky)rM299pinSh1C*`8#MxCoZ&<0y`qbr5)ELVTZ<L57kM%M#IK4r(F5Lx;+A-Oz&40Kg{"
    "v4)?Ude<cfyx#1(TBqAk_*L>02Aq#mX{f~LsJ*JCi1tsGHTIKy0WN|Kn~18#q!ouxWs)eCoi!c?;w-3;Mh#B`F&`SEFJQT(&8O{_9m`2fMoASjIxaFbs+p}D"
    "N)AbN49mW_hS=OG0F>0}2ClTKX94wt9`#%|{j`POS^+VUOSuc&S!LhHV-P34f)5HEa(n&9bw3nb1`~3bw+kFD7oSJ7i3}`GC$J<p{3y{l@{jSLU?PFZCn4K8"
    "4#no&@&oH|hhI>J3+cZ;6wSz65l+1%L1eLfo0US+y!MY`_t5u)TVFqaaOd$K+<AU<=kSY%w~x5rwPPd&Q8TYifJ7lqaB^L;MI1ae0zkKJwRB`PX^0yw{Rl>$"
    "s*rnOkgGL5504&{)r(_G)UFUba~$M7nT|uGPA+Y80SM%u6nIoWcZ{1nU9$Xj0U$AeJI?!$6z;f{f$J`qaJXFxB6o}ncl2;B4GqC+L|SMQCL#r=QAro(ho)eL"
    "k^f4dh-<7>qwaC!_rBmcILSqJ<hlgjjMS24s}&cTDd<*3O45AP(k%lYPlH*1y>7H6xq_raQ0=QQUg~(SbO4=}rFjb13Xo&dlp&)OB-w_~R$hOZ23911t@J@?"
    "N0D0=)9x9R<=dqNeKcitz-t7wi14P@B$8D)NFuzib-PBuw-v!ADI_*X8%T~ji@U60&<A+J!aM+SP_s^4V7}4_kjIaM!be4{o)9iYy25f-?~6>eYup2d(MlYE"
    "$;Js@mNqt07aSn2WAsHhJ=RWd+j=qYx{7@Oo5go&+Tx*M$~2c{&`Mt;+t*op<h<rL>((S(*T;0y)8qz#Bol1Cwtan0ZG-B>X^vr1HBmtsrb4gNGAL(y%=?XX"
    "V=gk}c@+lz;fH)yxUqRgE4H1+%1tMTwM}T;PE(7n<j2^$&a=E{1g<y_CvV!aPfUtS>nk$kufqHatMU7z`{I}!JfgX~8ovsGR#fL9UbkE8hf#kVRC|l5?K;k%"
    "0a7w!F92EG*wCYct&aJjWp3wO+C?CyP!!0XpnXN_y6B&Xn6o26)>9sJ5mO<dI5~uRX}MTNEB6;N)bFl89gM{>O}zcxnY|oiLeJ07R(SL#RrL-EgX=14v4>PY"
    "O5~<>)pg7TsZIhc)>KbjE)~W+*y0wbaj9TXqpMnP21q{f(o8Bj`Rj4~lMnl@>Lyo8)^5LB)~mtu?(^7`Mw2x7^-WqP!XxwIwwk7AFxoTZscosbR5Yr#Dc-Ie"
    "IByI_u5QjyREf0%K@H&&!?;FPgm$@KHV{XT%ovtcMvu)6A&qny!>dp!yX_Rp-5v^CvTwVzIcL?CljIs&xh?)65^^q>ahHM)6#gBQi1ge9hgXjU<s<i^gP@tU"
    "-ty2SIoHqMsKK^%|CX$&Kdf9grDf=g*Ob`n<=SWneebyChX=Eh-~;I?*?KLGGsU9FbsSm9M=$6|-BF@G9^~JYfl0g4mt|!2h*w%v2ElmYcL_~1)n|F2m|kZB"
    "YO;>uO_x=ls=VQj?LK`?l<h|j$NKNmo+GotpLu)D++wx{X67SwNz=Kt2`Js$JHz)jHPXcC(~#<Fk}R}IE-NuJNQ!GqjA6gu9VU5`J~-f`cWVz?I9)ETN;1@?"
    "DXLABj@P8Ykork7q})0pd4!-z1*^zI;+(Y=t3i&77pawTaGX{pE<_OeVJpJF*MtI**zr&uxGb>pE4iVSU&jfq{3@C7%BWTxU~YiF3{FW3mEU@uR$Z)Pkp3(o"
    "3M7JH&KXNQ3Wtjy2B!y&db{CmHg=nP^}=awb2Y#3iq;FEA65&8cq@tkb|6%jJPizbAFGgi6dOb+cy1+vw)a?EGHf*{tolZS*Vi|AcZM!F-IN?sZWXSNCN)H#"
    "b`1+H)}mOU&^)IhDBq}(UWYS!VX_2#7Xf9iq56iFKex3ncg)1zXIyJWreM3+SNY5B829y@ar2}nE{k7*VQaW3)88eBZ6Q^?V~g!s$kZ@PUczp5MFd)99k11U"
    "{*OTLO|t)?oND?^-s65k5?ms0)3a~UGfp-j)#C%$uOH|we`BX|y-aVNt8II=#q9I&bucJ5J&z_WqXjqZg5-#RAg@VTb2Q0&DvD-^jpl`W>%3DG)|Y^?J_?!)"
    "?H(p@ILJTUowdjfa=0{U@r&i+>P_mq<!wZByYA0&PPDMa3}XU@6K*uQhqhsUm_-?g1N6)p;q(V0p8out*CferW%>Os|KKtE@^E?Zi<n?=FvNXpCh|xXcOP}k"
    "JNoI!#GDfV!PZ4Uu;Hw~jJtC`lIiS8d=EeFc(tS96*ds`2YSAJ=kr@%-hcA^@cz9=t`G<mg902Q>$0@>T?{aR7`vV{O^+vWZJD%@5#Atss-_#koCj7{H@g~s"
    "P=kjIm^svWe}W-cbt}x-yWLzuxU&JEdU8xWQ6JQ_RN<SX@Y(xy!rmXYDniTU+?S0zUHiO2b>L%-4_|1)SV&^`BB1d_1I|8-bWsB{QIxv>wp6XQ`F|yI^QutM"
    "fsST%Mm&*GlI}lDJk$E^ETO3`dvIEbbK@~YV2tdjo5mE`!F6|3lR^U0zh!>b4PbHEZF1kp)$qf}Lje+OQ3VEvT;aC*Vk!)lCrK4lOZWXAXt|!lE~BxL_S%tk"
    "q0NFd4FxLYxRM!$`NUs*a)y8LQr)a_={tEP460buOR5@PIir)lKkk=RPq5!_%wKzEEiSXV>hPXOCAxGIKYob{h;*(!kdHj7H-5=weDX=k{0a3|)jjpYp0I+9"
    "QG++*o#~$zH05|-bnF5yFamm1c?Ax|m4PWLgw(m@AgQPbQ-RY}h4sc=FyEb)Zn1X&d2%JR3~S3Sk!87c9%RmG$1NQRGW%dQ2tM-%AR2`+o^3KEqJ`hXmxwL$"
    "X9>kZzxSBSDf0be8LCf0F-2iK;~>8#%u{|#4%C5aaC|%t=)DOQIhc-J$#L7Rk}dT`(Xy^lSqvh-&uflOWxXQZUbFgLxq$wY<z%iF?#b-o?3G&wu>|ofIb0~+"
    "SyK45ZkLMT7)E_d94P^@lZ)ZTc9<7PfI^P4uemDTeIijE>TqIkQdMf??z8-^9t0db;{>)TbQnHV_g28w)5mq5fT9H&7ZE+&2^8!wQF%lt^Yo8C|Kk<E^Uw1f"
    "rx4Ov8!|@XIGpt12IH`1qz>d(T$BaFLd|2j6>?x#g?nb4(yj0#6r`LySJJk!<HU<7@F%1kj^l{GgtFu0L7e=6U2K)TFW$<+>d8}=`Iv>}5hYPpomY}YpWp^?"
    "6~_-o75*a00BO$fb9}4MG>9j|xFbQx?K=Po_$y#w*;QmQH$5CXC@@Dkx{8bmvMH8CE@dZu2vHsnGO~?{12FQmc1IljR;SjouVLt<X{Ty}V6$9DPyRIzoF#e~"
    "!o-o-Lw}m_{9s+{nvd-EjW+BK%+BS>NnT*;!>agW?!d-8()6XcRKrtQ<+-rr$8%shk5R%wk;&1@YSVr#+F89IX4Ld-13W(Wfu?JHl;POCg9M+&dA+uAc+c`|"
    ";7jj}|8b_4S&S~;{u!HGy#3E?^zOe-M+^@oj@Tl)c>DK#ki0J5{PUbGF5dnZ)&~eiey!9IjhTlKGoga{a9^aea|oIeFMAtkLha&id5*SXc-R<oVd;#=7y+*="
    "C)do9t0QrTd&y68ZeIrzHpVlRrF2DE$t8`9xinlU{o&F-WhWPJ|CGJf4cS+&Va{18)urjty}m`l_zPo;srsJ!Q46PlKzE-!xZnN3O%V;s`mejCMzh5Fr{Zq}"
    "f9j>p{hM<T^Vy(VdeCUHos*q$i#6f(4|E5%SZexUayYf~601ytMCK#NOV&0kl?srT91_CA=B<53-(_S|%-K@K5y{vJS3{_DGcI(}>cEu|N@@woPXnnm(zI$f"
    "wpimvt-k9w>J0wF|8KB*wb80IS`YXahK0<}Onlz0?e4I~POaJUci4`uyH{)NJ>ahymZ0AlenYg)Mr-&Ngzqs5F+586a5e^FMo~Js1zQ{gHV!mWW>~TLeePYJ"
    "_D(UfQ8=YD!Q_c()}>6`a)VqceM(~Sm(_u*rG*Vg0+o%8%$R|kn1GBkx=H6+B9c4Fa|)=}ij6VDfd|7AD66#vF2vmX;^=7PN1#Imb6*-I$X)Zc{B9-_u?i^N"
    "$0+kj*@C`H-jLQSs|2AZP+$ObDW`<Arp{0u<ZP}h>u_wA@95f-qD1YFPS3~(E%4EI8MKfVL8c5OnZ8naV`bJN@662b0E3;tLOux><KRf!@Eo8LNwnhN7!xfA"
    "19>$NEIV_)b~Rzh#A?+NK#u~StF$l@%W@@(@-OG-rL!|BvOJ%{AGTVRv_q7L(bkcAnYouPyZUC`86nRc-<pOKj+R`HFmHM;k}%PvLsmvacS8v#7fXq}hIXdX"
    "q3Y%h4Ll*5BpLQ}FavDr!%~i)%*tl~*mk|r3r7A)IE&h)cru$UMx_j=J>TFDg6LS?NNUo~-U4;Q(j+N>8ePa!_|vza!z=i*5XvF_BD6q~wSNKR8UC@;U*M~R"
    "W{Lg7DHcGo4}j$ADoL<`gYfVl!}?mPf4@YGcPLgYQUb-{#hbrdu#<PckZHV7$~5n0pN$9HK&qyqtVbgEwEXJHzbQiFqN|6?MRB5<7k73^9`ZX*^3v!@KxAwu"
    "8r)~Z_OXo(mY=}N&gX7qOsqfaxXX#8BuSXgN`&8>tqZbRWn^l9-=ynB80}haoymz!KI>q_7OP&viE~+*nQ8ngoQ$+!GBTWT1_yG}Uji7TfW?Ti)6Y<pac8>E"
    "OX~gJY|hI0>*RE)G`|^+nPO?d>`qcn`XbCQB@R?<`|pQmg-BU%1KMgj{Xurr<<RUhV$l^h>UxRU+RES2M$J_M-*ah?6%&zR6Ly#!&rqQqqzA4{AtKW{2A_Ae"
    "Z}cFYaYYm}qZBcp@HKJA7x&<h5+djVc^pVGVa)T##^t)#!I&_|>kpIVrbB~-l0b}oRn23vd;VU(HLBh0iu_@;q<yal^;C<wi))DxiWPM;O&eRQl4NA7tmx7Q"
    "R1_=EE8MbUuH^~6*jb0FU{Hs=GG*@3Z0a9V$TbC8e=tZ(vulLe7oVJ+7|{uCd6t9W{QSkYKLhr`USGWZH5<SCpS4<z8TGYf`#GJNCggl>Qo6+lPyWprk#6yR"
    "FWsPwYLG2L2~YfS4xKb>uRCv4WGSQ3Qy<R6Vv3QD=&K&zRb88gkUcI+q>{~x*ENOc`>6mal@idIr10CH%`V=2Gi80Qt-KVTf+XYcyI+KC^e^!Jle47V2lpO+"
    "`Q*;gip@vw-YjY=0>_!NFqN{9Lz;)_iiF&r{X%3}D4Uvj?kzEC{mDze%3?coJ6hQtHSaY^!d9guHbH;1ym<4gDeGOl{r55>bFF5j)e(8ic`pZjfAT!c$nUK^"
    "Cf@3D*~jJb3(9u64C1mX@(k?t@@dq?MC}FSkr?EeCkf9eTboT~)@wXsgg18)X;U$vh2NDiBfJQ|FX{ZeWE#bJvTk}Y%5=7X9oj}me`*Q$Dx&xpN|bj!b#h{r"
    "*B*C8v`a=do=n_L!-Qr7vay=(NHVY;kE8<U&O5P7Rr2qw<}%!<nOg3eqdF-_pfqOzLM#>VmVizGk1<5y#OUbCAX{tC;2M%2izy;k3Ai<|%ZKGGLoUdIVlOt;"
    "Jy-7Aa<CcdprkF`o1$=}+Tjbdt`Qq}+FSxn<XKOM1?8)BhGzX5%^=W{V~8Dhd1)gt4&tVhhrj|rtNib67*{_ir;i>F#-A;wVipqv&Yvx&scB$Ya|%3m0{hw6"
    "V10l3WgNf*E+=34vS*fNqB|)-00BpJ1v-cQKMhL8T+_Cboa$y(&IWEx`=ePThFo!@QH3L$d1QQ*ahTh&8*NA)FJWmuo4ppm3}^9?e-iuwzO><}N}9k?Z&TA-"
    "17UW+O1<%{|FXo|X-FD~k?6DLfG)9wbH14awYRZc*~>ZtpLbE}T%0Gl2m1`EP^D4llA)cyX3q1|+j72BvYms#XrC!}@GwU>h+XQSAfZzUH>ZM_0O!5$Pe1p^"
    "V^r4h{x2_G6!7uxVlw92o!2{HFK+gt{hK};MZvIJ`s7S}Sd|#Zgl?&iUf1@CAC;?BzYmh^qVm1r(BDQ?3fWP005XD9ot9{&LE_Po2-FTaEva%8=}i3SIGk2d"
    "SKgVw?hJvERbTNSn)Xh;-YM<>@BjD>I~*-ffrEjDg+QL5%z4bF7jOS6MDgjbSo!{~haL!?$mNjN{aOTL+}!l{Umy-9_-FFE#OqypaPj7^L4>_{^Un)x`R5^s"
    "v!TH<*|$GK!S+8PKaFrae+p{y@eGAr%+f7*ESKhv!*?L`e*1GS9M?+hIstRWNSs_Zljj=hr@pN;eS+=w7Z0RlaG&SUHSQdi)?FU$ngkrW;#ZAWSP{%h?>WV9"
    "k3?LE5gFUDKMF%%_IGsr8U?7;<x-b`5f5KC&lQ+R7{+Og_4@~-Q$#5sQh>-D*U{d+n8D^?-7YYJ(PA-=+m3X&)BEA{rJ&02aK!Vw;VV-nmF2uMRilR6QE$_Z"
    "ex2ezbc2;THl4$O5jq6%oHdH1Gz=tpu*X8a6daD`1LPiX!}3@Ww-ZbCN<Q&;mI`$@R*d+}N-4R{SJ$)*n6$&hc*$*-kBlDG3)!h@+pXAvqWs0@>E5YH)JWN?"
    "6RO?~6N{`_f+S((g=l<M>VR!B&-5pI5Xyint(~z|IsI8RAez|RWYU7cCTPN7T(@E)evBrH6Xkn0Ka~Z47@TqmF6X4JS(@tb$o#0uWL--@PL1-Utubu|rZ|XZ"
    "^Gmm-p%8+Yf?^fWQTAvAyFX@SE{CvNkM4PGF3^F%tE7|x>jj`nL-7&(&!4k+2Gxgt1nedV<}t%tLxev_P!v6EDZcTx*Ug@lJ=Wc4MlKPoujC_ke$KABMK@<A"
    "-kuW3Q-i3m#p^}-n_+9@u32VAx=#I*@Yr9>q8d*4mYy$C(`#8D@uDs6DKiB?`?|1_0;sw{;~%CrSo!sci;)liy!)mPPodA^i#Km;rR?0XFi{U@OW6lfHzD}("
    "z*v4!$1zANs=1n>XG^0iqmc#Bf-{0QMle1O;^i1SNg%@Cm(LKb;`URzsLxj3`o0{&(OL7l|1bY-$pa8Hbga41A;PjPPUpdFNCz%%HWR#1*-A_1+NDZkAhLnG"
    "G(s7>fj>2&nPaoJdb3E$Q0dep3Y;@E4*Suxtn49twX~g*?_s`Yk48bTXu}4AUc63f2#ePXTYY3M&^dcKbb(Q8he)kwAYF0!?6S+kRu$H4(u3J41ST#>Gffr~"
    "i-h3ym}Qem;3j(I_Ju!L`UGMi^`hL|Lvh;yxc5iH;;{ViP)wyFHc0=G@)$CiuosW8C9F>$;ai_t!sWr`oSv{Pa`JCYgJ;E3LtQEPQxSLbii}#MaXkb98kR-m"
    "jsIO3y<X#o{t4^(5fh=?$>IV*M%iH<BTh}o>F)N7sMb_Yooj9vcwQ~(iq?9G(Bn5tjunELJPwASX^x6a#%?$YgyGL@@F?)3{*~HO12q;w%#m%d!{vhE(#g>3"
    "n8<X=8L%}ylBn$DSfSx9x_`CC#IZnwKyU+%94sqy=Lry;cfX#F7@*cykJ;ej?cbvx>>sAbp5gB2HJ|W`+8S!Tv|L5>EB9-rD=BxUd2z3B%m^6E5sR9RFT@|r"
    "%Jv|EIlJl*u42_FF2KqT9j%MQw?Y^|EQ0wlD+PUaC03fAL?%17C&ppsrJ-vx6kN^nZL*wZv-Zr>PInxM!J!@I=KQ&c>3@*f2vjW}a$GGu6%r7enoaW4b!?cB"
    "q!rmfXa$nD8?dp&kz~`Ghp-*E#4Ac7Y}m8h*AjQkk*!Rs@cH}nX4PNcp=}AfbdPoeIbA07iZd9{NBn6VA~}vJ01A*q8AMD0$8%{O7OpK1CK-^w%Z+#9%Q~Kt"
    ")dn)_Lw=~}N-Y?E{+!B5Ac$v%uW@1FvLMKa>P*+hKuBfVl_?FnsW9N{jgB35{B-)v%f5$w`m8dQ2B%eKh^b>RTpJUE$-Cjw^i2cR{6-qu+4S8nP5}$w{Vn1m"
    "KWjQbbNin!3S3#87!|o-kscmMI^j>%uE{trfgb&IY^k{9lLq2Af=A6AB~D9Xk|&1It|-MU)Je)%2Z-qVSu@XDR&?Gi-L$w`b}SO;wMCE~3#URp!bIe`@uAd?"
    "m?p?_xieuXY*<U~wiJ-e^jBd^H#g&c6wVj>__H?~obn%|#bgXW{(sTw*&Y"
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
                local_origin = request.host_url.rstrip("/")
                lan_param = f"lan={quote(local_origin, safe='')}"
                query_str = f"?{query}&{lan_param}" if query else f"?{lan_param}"
                target = f"{public_base}/replay/cam{cam_id}{query_str}"
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
