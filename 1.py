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
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import winreg
from contextlib import contextmanager
from datetime import datetime, timedelta
from functools import wraps
from io import BytesIO
from logging.handlers import RotatingFileHandler
from urllib.parse import quote, unquote

import cv2
import pystray
import requests
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

try:
    import qrcode
except ImportError:
    qrcode = None


if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.abspath(os.path.dirname(__file__))

def _show_error_box(title, message):
    try:
        ctypes.windll.user32.MessageBoxW(None, message, title, 0x00000010)
    except Exception:
        pass


FFMPEG_PATH = os.path.join(BASE_DIR, "ffmpeg.exe")
INSTANCE_MUTEX_NAME = "Local\\CambiDa_CCTV_Recorder_SingleInstance"
# Lưu trên ổ D: nếu có, hoặc thư mục Temp của máy để bản mở sau biết PID
INSTANCE_STATE_FILE = (
    r"D:\\cctv_recorder_instance.json"
    if os.path.exists(r"D:\\")
    else os.path.join(tempfile.gettempdir(), "cctv_recorder_instance.json")
)
INSTANCE_MUTEX_HANDLE = None

try:
    with open(os.path.join(BASE_DIR, "config.json"), "r", encoding="utf-8-sig") as f:
        CONFIG = json.load(f)
except FileNotFoundError:
    _show_error_box(
        "Lỗi khởi động CCTV",
        f"Không tìm thấy file 'config.json' trong thư mục:\n{BASE_DIR}\n\n"
        "LƯU Ý: Nếu bạn đang mở trực tiếp từ file ZIP, vui lòng 'Giải nén toàn bộ' (Extract All) ra một thư mục trước khi chạy.",
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
        "Thiếu file ffmpeg.exe",
        f"Không tìm thấy file 'ffmpeg.exe' trong thư mục:\n{BASE_DIR}\n\n"
        "Vui lòng đảm bảo file ffmpeg.exe nằm cùng thư mục với file chạy ứng dụng.",
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
    if not _show_restart_prompt():
        return False

    previous_pid = _read_instance_pid()
    if not previous_pid:
        try:
            ctypes.windll.user32.MessageBoxW(
                None,
                "Không xác định được bản CCTV đang chạy. Hãy đóng bản đó rồi thử lại.",
                "Không thể khởi động lại",
                0x00000010,
            )
        except Exception:
            pass
        return False
    try:
        subprocess.run(
            ["taskkill", "/PID", str(previous_pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=12,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False

    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if _try_take_instance_mutex():
            return True
        time.sleep(0.5)
    try:
        ctypes.windll.user32.MessageBoxW(
            None,
            "Bản CCTV cũ chưa dừng. Không mở thêm ứng dụng để tránh ghi trùng camera.",
            "Không thể khởi động lại",
            0x00000010,
        )
    except Exception:
        pass
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
CAM_LAST_SUCCESS = {}
CAM_LAST_START = {}
CAM_LAST_ERROR = {}
CAM_LAST_RECOVERY = {}
CAM_STOP_EVENTS = {}
CAM_WORKERS = {}
CAM_PROCESSES = {}
CAMERA_LOCK = threading.RLock()
VIDEO_VALIDITY_CACHE = {}

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

TEMPLATE_DIR = getattr(sys, "_MEIPASS", BASE_DIR) if getattr(sys, "frozen", False) else BASE_DIR
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
    for index, camera in enumerate(cameras, start=1):
        if not isinstance(camera, dict) or not all(camera.get(key) for key in ("ip", "user", "pass")):
            return f"Camera {index} cần có ip, user và pass."
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


def build_rtsp_url(camera, stream):
    """Build an RTSP URL or use a per-camera direct URL override."""
    direct_url = camera.get(f"{stream}_rtsp_url")
    if direct_url:
        return str(direct_url)
    default_paths = {
        "record": "h264/ch1/main/av_stream",
        "preview": "h264/ch1/sub/av_stream",
    }
    path = str(camera.get(f"{stream}_path", default_paths[stream])).lstrip("/")
    separator = "&" if "?" in path else "?"
    return (
        f"rtsp://{quote(str(camera['user']), safe='')}:{quote(str(camera['pass']), safe='')}"
        f"@{camera['ip']}:{camera.get('port', 554)}/{path}{separator}timeout=20000000"
    )


def test_camera_connection(camera):
    """Probe the configured recording stream without creating a video file."""
    if not os.path.exists(FFMPEG_PATH):
        return {"ok": False, "message": "Không tìm thấy ffmpeg.exe cạnh ứng dụng."}
    try:
        rtsp_url = build_rtsp_url(camera, "record")
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
            rtsp_url,
            "-t",
            "3",
            "-f",
            "null",
            "-",
        ]
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=12,
            creationflags=subprocess.CREATE_NO_WINDOW,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode == 0:
            return {"ok": True, "message": "Kết nối thành công, đã nhận được luồng video."}
        error_text = (result.stderr or "").lower()
        if any(word in error_text for word in ("401", "unauthorized", "authentication failed", "invalid credentials")):
            message = "Kết nối thất bại: sai tài khoản hoặc mật khẩu."
        elif any(word in error_text for word in ("connection refused", "failed to connect", "timed out", "timeout", "network is unreachable", "no route", "connection reset")):
            message = "Kết nối thất bại: không truy cập được IP/port camera."
        elif any(word in error_text for word in ("404", "not found", "invalid data found", "method not allowed")):
            message = "Kết nối thất bại: sai đường dẫn RTSP hoặc camera không hỗ trợ luồng này."
        else:
            message = "Kết nối thất bại: camera không trả về luồng video."
        logger.warning("[CamTest] FFmpeg kiểm tra camera thất bại (mã %s).", result.returncode)
        return {"ok": False, "message": message}
    except subprocess.TimeoutExpired:
        return {"ok": False, "message": "Kết nối thất bại: camera phản hồi quá chậm (timeout)."}
    except Exception as exc:
        logger.warning("[CamTest] Lỗi kiểm tra camera: %s", exc)
        return {"ok": False, "message": "Không thể chạy kiểm tra kết nối trên máy này."}


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


def record_camera(cam_id, camera, stop_event):
    """Record one camera. The supervisor guarantees a single worker per camera."""
    last_alert_time = 0
    retry_delay = 1
    ip = camera["ip"]
    user = camera["user"]
    password = camera["pass"]
    while not stop_event.is_set():
        start_dt = datetime.now()
        end_dt = start_dt + timedelta(seconds=DURATION)
        filename = build_filename(cam_id, start_dt, end_dt)
        filepath = os.path.join(VIDEO_DIR, filename)
        temp_filepath = f"{filepath}.part"
        # Giữ nguyên luồng ghi đã dùng trong bản cũ. Chỉ dùng RTSP tùy chỉnh
        # khi người vận hành cấu hình rõ ràng trong Admin.
        configured_record_path = camera.get("record_path")
        if camera.get("record_rtsp_url") or (
            configured_record_path
            and configured_record_path != "h264/ch1/main/av_stream"
        ):
            rtsp_url = build_rtsp_url(camera, "record")
        else:
            rtsp_url = (
                f"rtsp://{user}:{password}@{ip}:554/"
                "h264/ch1/main/av_stream?timeout=20000000"
            )
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
        # Giữ nguyên cơ chế ghi của bản 1.2 đầu tiên: copy trực tiếp luồng
        # camera vào MP4, không chuyển mã và không thay đổi tốc độ/độ phân giải.
        # Ghi vào .part trước. Sau khi FFmpeg kết thúc thành công, file mới
        # được đổi tên sang .mp4 để trang xem lại chỉ thấy video hoàn chỉnh.
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
                last_err = (stderr or "").strip().splitlines()[-1:] or [""]
                last_err = last_err[0]
            except subprocess.TimeoutExpired:
                proc.kill()
                _, stderr = proc.communicate()
                last_err = (stderr or "").strip()[-500:]
        except Exception as e:
            last_err = str(e)
            logger.exception(f"[Cam {cam_id}] Không thể chạy FFmpeg")
        finally:
            with CAMERA_LOCK:
                if CAM_PROCESSES.get(cam_id) is proc:
                    CAM_PROCESSES.pop(cam_id, None)

        rc = proc.returncode if proc else -1
        valid_file = os.path.exists(temp_filepath) and os.path.getsize(temp_filepath) > 0
        if rc == 0 and valid_file and not stop_event.is_set():
            try:
                os.replace(temp_filepath, filepath)
            except OSError as exc:
                valid_file = False
                last_err = f"Không thể hoàn tất file video: {exc}"
        if rc == 0 and valid_file and not stop_event.is_set():
            size_mb = os.path.getsize(filepath) / 1_048_576
            logger.info(f"[Cam {cam_id}] Ghi xong: {filename} ({size_mb:.1f} MB)")
            try:
                # Chỉ ghi chỉ mục từ metadata đã biết; không ffprobe hàng loạt.
                upsert_video_index(filepath, validate=False, known_duration=DURATION)
            except Exception as exc:
                logger.warning(f"[Cam {cam_id}] Không cập nhật được chỉ mục video: {exc}")
            CAM_LAST_SUCCESS[cam_id] = time.time()
            CAM_LAST_ERROR.pop(cam_id, None)
            retry_delay = 1
        else:
            try:
                if os.path.exists(temp_filepath):
                    os.remove(temp_filepath)
            except OSError as exc:
                logger.warning(f"[Cam {cam_id}] Không thể xóa file tạm: {exc}")
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


def get_rtsp_url(cam_id, stream="sub"):
    if 1 <= cam_id <= len(CAMERA_LIST):
        cam = CAMERA_LIST[cam_id - 1]
        return build_rtsp_url(cam, "record" if stream == "main" else "preview")
    return None


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
    return render_template(
        "index.html",
        cam_id=cam_id,
        camera_name=get_camera_name(cam_id),
        site=get_site_config(),
        max_merge_minutes=MAX_MERGE_MINUTES,
    )


@app.route("/live")
def live_all_cameras():
    cameras = [
        {"id": cam_id, "name": get_camera_name(cam_id)}
        for cam_id in range(1, len(CAMERA_LIST) + 1)
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
    stream = request.args.get("stream", "sub").lower()
    if stream not in {"sub", "main"}:
        return "Luồng không hợp lệ", 400
    rtsp_url = get_rtsp_url(cam_id, stream)
    if not rtsp_url:
        return "Camera không tồn tại", 404
    return Response(
        gen_frames(rtsp_url), mimetype="multipart/x-mixed-replace; boundary=frame"
    )


@app.route("/snapshot/cam<int:cam_id>")
def camera_snapshot(cam_id):
    """Return one JPEG frame so the all-camera page does not consume 12
    permanent browser connections (most desktop browsers cap this at 6)."""
    stream = request.args.get("stream", "sub").lower()
    if stream not in {"sub", "main"}:
        return "Luồng không hợp lệ", 400
    rtsp_url = get_rtsp_url(cam_id, stream)
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
    """Return every completed MP4 immediately; active recordings use .part."""
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
    videos = []
    for _, filename in candidates:
        item = {"name": filename, "url": f"/video/{quote(filename)}"}
        metadata = parse_video_metadata(filename, known_duration=DURATION)
        if metadata:
            item["format"] = metadata["format"]
            item["started_at"] = metadata["start"].isoformat(timespec="seconds")
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
    global CONFIG
    if request.method == "GET":
        return jsonify(CONFIG)
    candidate = request.get_json(silent=True)
    error = validate_config(candidate)
    if error:
        return jsonify({"ok": False, "error": error}), 400
    try:
        candidate = dict(candidate)
        for key in ("server_port", "record_duration_sec", "record_timeout_sec", "camera_stall_sec", "max_merge_minutes"):
            if key in candidate:
                candidate[key] = int(candidate[key])
        if "disk_limit_gb" in candidate:
            candidate["disk_limit_gb"] = float(candidate["disk_limit_gb"])
        save_config(candidate)
        CONFIG = candidate
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
        save_config(candidate)
        global CONFIG
        CONFIG = candidate
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
            worker = CAM_WORKERS.get(cam_id)
            proc = CAM_PROCESSES.get(cam_id)
            last_success = CAM_LAST_SUCCESS.get(cam_id)
            cameras.append(
                {
                    "id": cam_id,
                    "name": get_camera_name(cam_id),
                    "worker_alive": bool(worker and worker.is_alive()),
                    "ffmpeg_running": bool(proc and proc.poll() is None),
                    "last_success_seconds": int(now - last_success) if last_success else None,
                    "last_error": CAM_LAST_ERROR.get(cam_id),
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
    if not isinstance(camera, dict) or not all(camera.get(key) for key in ("ip", "user", "pass")):
        return jsonify({"ok": False, "message": "Cần nhập IP, tài khoản và mật khẩu trước khi kiểm tra."}), 400
    try:
        camera["port"] = int(camera.get("port", 554))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Port RTSP không hợp lệ."}), 400
    return jsonify(test_camera_connection(camera))


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
    init_db()
    index_untracked_video_files()
    with db_connection() as connection:
        segment_rows = connection.execute(
            "SELECT filename, cam_id, started_at, ended_at, duration_sec, status, locked, note "
            "FROM video_segments ORDER BY started_at ASC, cam_id ASC, id ASC"
        ).fetchall()
        event_rows = connection.execute(
            "SELECT filename, cam_id, event_time, label, note "
            "FROM video_events ORDER BY event_time ASC, id ASC"
        ).fetchall()

    segments = []
    for row in segment_rows:
        segment = _timeline_segment_from_row(row)
        if segment is None:
            continue
        if segment["status"] == "complete":
            media_path = safe_video_path(segment["filename"])
            if not media_path or not os.path.isfile(media_path):
                continue
        clip_start = _parse_local_datetime_value(segment["started_at"])
        clip_end = _parse_local_datetime_value(segment["end_at"])
        if clip_start < window_end and clip_end > window_start:
            segments.append(segment)

    events = []
    for row in event_rows:
        event = _timeline_event_from_row(row)
        if event is None:
            continue
        event_time = _parse_local_datetime_value(event["event_time"])
        if window_start <= event_time <= window_end:
            events.append(event)
    return {"segments": segments, "events": events}


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
        req_start = datetime.strptime(start_str, "%Y-%m-%dT%H:%M")
        req_end = datetime.strptime(end_str, "%Y-%m-%dT%H:%M")
        if (req_end - req_start).total_seconds() > int(MAX_MERGE_MINUTES) * 60:
            return Response(
                f"data: error:Quá {MAX_MERGE_MINUTES} phút\n\n",
                mimetype="text/event-stream",
            )

        files = glob.glob(os.path.join(VIDEO_DIR, f"cam{cam_id}_*.mp4"))
        candidates = []
        for f_path in files:
            f_start, _ = parse_video_filename(os.path.basename(f_path))
            if not f_start:
                continue
            f_end = f_start + timedelta(seconds=DURATION + 10)
            if f_start < req_end and f_end > req_start:
                candidates.append((f_start, f_path))
        candidates.sort(key=lambda x: x[0])
        if not candidates:
            return Response(
                "data: error:Không có video\n\n", mimetype="text/event-stream"
            )

        list_file_path = os.path.join(
            VIDEO_DIR, f"merge_list_{uuid.uuid4().hex}.txt"
        )
        with open(list_file_path, "w", encoding="utf-8") as f:
            for _, path in candidates:
                escaped_path = (
                    os.path.abspath(path)
                    .replace(chr(92), "/")
                    .replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))
                )
                f.write(f"file '{escaped_path}'\n")

        output_filename = f"merge_cam{cam_id}_{uuid.uuid4().hex[:8]}.mp4"
        output_path = os.path.join(VIDEO_DIR, output_filename)
        cmd = [
            FFMPEG_PATH,
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            list_file_path,
            "-c",
            "copy",
        ]
        cmd.extend(["-progress", "pipe:1", "-nostats", output_path])

        def generate_merge_process():
            total_duration_est = len(candidates) * DURATION
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            yield "data: 5\n\n"
            try:
                for line in process.stdout:
                    if "out_time_ms=" not in line:
                        continue
                    try:
                        ms = int(line.strip().split("=")[1])
                        percent = int(ms / 1_000_000 / total_duration_est * 100)
                        yield f"data: {min(percent, 99)}\n\n"
                    except:
                        pass
            except:
                yield "data: error:Lỗi xử lý\n\n"
                return

            process.wait()
            try:
                os.remove(list_file_path)
            except:
                pass
            if process.returncode == 0:
                yield f"data: done:{output_filename}\n\n"
                return
            yield "data: error:Ghép thất bại\n\n"

        return Response(
            stream_with_context(generate_merge_process()),
            mimetype="text/event-stream",
        )
    except Exception as e:
        return Response(f"data: error:{str(e)}\n\n", mimetype="text/event-stream")


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

    if run_tray:
        threading.Thread(target=run_server, daemon=True).start()
        try:
            setup_tray()
        except Exception as e:
            logger.error(f"Lỗi khởi động System Tray: {e}")
            run_server()
    else:
        run_server()

