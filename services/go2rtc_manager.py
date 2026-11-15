"""go2rtc process lifecycle and stream configuration manager for Cambida CCTV.

Provides zero-latency WebRTC/MSE streaming by embedding go2rtc as a background service.
"""

import atexit
import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

_go2rtc_proc = None
DEFAULT_API_PORT = 1984
DEFAULT_RTSP_PORT = 8554
DEFAULT_WEBRTC_PORT = 8555


def find_go2rtc_binary(base_dir=None, bundle_dir=None):
    """Find the go2rtc executable in base_dir, bundle_dir, or system PATH."""
    candidates = []
    if base_dir:
        candidates.append(os.path.join(base_dir, "go2rtc.exe"))
        candidates.append(os.path.join(base_dir, "go2rtc"))
    if bundle_dir:
        candidates.append(os.path.join(bundle_dir, "go2rtc.exe"))
        candidates.append(os.path.join(bundle_dir, "go2rtc"))

    cwd = os.getcwd()
    candidates.append(os.path.join(cwd, "go2rtc.exe"))
    candidates.append(os.path.join(cwd, "go2rtc"))

    for path in candidates:
        if path and os.path.isfile(path):
            return os.path.abspath(path)

    # Search PATH
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        for name in ("go2rtc.exe", "go2rtc"):
            p = os.path.join(folder, name)
            if os.path.isfile(p):
                return os.path.abspath(p)

    return None


def generate_go2rtc_yaml(camera_list, rtsp_url_builder=None, api_port=DEFAULT_API_PORT,
                        rtsp_port=DEFAULT_RTSP_PORT, webrtc_port=DEFAULT_WEBRTC_PORT):
    """Generate valid go2rtc YAML configuration for cameras."""
    lines = [
        "# Cambida CCTV go2rtc configuration",
        "# Auto-generated - do not edit manually",
        "",
        "log:",
        "  level: info",
        "",
        "api:",
        f'  listen: "127.0.0.1:{api_port}"',
        "",
        "rtsp:",
        f'  listen: "127.0.0.1:{rtsp_port}"',
        "",
        "webrtc:",
        f'  listen: ":{webrtc_port}"',
        "",
        "streams:",
    ]

    has_streams = False
    for cam_idx, camera in enumerate(camera_list, start=1):
        sub_url = None
        main_url = None

        if rtsp_url_builder and callable(rtsp_url_builder):
            try:
                sub_url = rtsp_url_builder(camera, "sub")
            except Exception as exc:
                logger.debug("Failed building sub RTSP URL for cam %s: %s", cam_idx, exc)
            try:
                main_url = rtsp_url_builder(camera, "main")
            except Exception as exc:
                logger.debug("Failed building main RTSP URL for cam %s: %s", cam_idx, exc)

        # Fallback to direct camera dict properties if builder didn't return URLs
        if not sub_url:
            sub_url = camera.get("sub_rtsp_url") or camera.get("rtsp_url")
        if not main_url:
            main_url = camera.get("main_rtsp_url") or camera.get("record_rtsp_url") or sub_url

        if sub_url:
            clean_sub = str(sub_url).strip()
            lines.append(f'  cam{cam_idx}_sub: {json.dumps(clean_sub)}')
            lines.append(f'  cam{cam_idx}: {json.dumps(clean_sub)}')
            has_streams = True
        if main_url:
            clean_main = str(main_url).strip()
            lines.append(f'  cam{cam_idx}_main: {json.dumps(clean_main)}')
            if not sub_url:
                lines.append(f'  cam{cam_idx}: {json.dumps(clean_main)}')
            has_streams = True

    if not has_streams:
        lines.append("  {}")

    lines.append("")
    return "\n".join(lines)


def is_go2rtc_healthy(api_port=DEFAULT_API_PORT, timeout=1.0):
    """Check if go2rtc API responds on localhost."""
    url = f"http://127.0.0.1:{api_port}/api"
    req = urllib.request.Request(url, headers={"User-Agent": "Cambida/2.3"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status in (200, 204)
    except Exception:
        return False


def start_go2rtc(camera_list, rtsp_url_builder=None, base_dir=None, bundle_dir=None,
                 api_port=DEFAULT_API_PORT, rtsp_port=DEFAULT_RTSP_PORT,
                 webrtc_port=DEFAULT_WEBRTC_PORT):
    """Start go2rtc background process with current camera streams."""
    global _go2rtc_proc

    if _go2rtc_proc and _go2rtc_proc.poll() is None:
        logger.info("[go2rtc] Tiến trình đang chạy (PID: %s)", _go2rtc_proc.pid)
        return True

    exe_path = find_go2rtc_binary(base_dir=base_dir, bundle_dir=bundle_dir)
    if not exe_path:
        logger.warning("[go2rtc] Không tìm thấy binary go2rtc.exe, sẽ dùng MJPEG fallback.")
        return False

    config_dir = base_dir or os.getcwd()
    config_path = os.path.join(config_dir, "go2rtc.yaml")

    yaml_content = generate_go2rtc_yaml(
        camera_list=camera_list,
        rtsp_url_builder=rtsp_url_builder,
        api_port=api_port,
        rtsp_port=rtsp_port,
        webrtc_port=webrtc_port,
    )

    try:
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(yaml_content)
    except Exception as exc:
        logger.error("[go2rtc] Không thể ghi file cấu hình %s: %s", config_path, exc)
        return False

    creationflags = 0
    if sys.platform == "win32":
        creationflags = 0x08000000  # CREATE_NO_WINDOW

    cmd = [exe_path, "-config", config_path]
    try:
        _go2rtc_proc = subprocess.Popen(
            cmd,
            cwd=config_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        logger.info("[go2rtc] Đã khởi chạy tiến trình ngầm (PID: %s, API: :%s)", _go2rtc_proc.pid, api_port)
        return True
    except Exception as exc:
        logger.error("[go2rtc] Lỗi khởi chạy go2rtc: %s", exc)
        _go2rtc_proc = None
        return False


def stop_go2rtc():
    """Stop the running go2rtc background process."""
    global _go2rtc_proc
    if _go2rtc_proc:
        try:
            logger.info("[go2rtc] Đang dừng tiến trình go2rtc (PID: %s)...", _go2rtc_proc.pid)
            _go2rtc_proc.terminate()
            try:
                _go2rtc_proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                _go2rtc_proc.kill()
                _go2rtc_proc.wait(timeout=1.0)
        except Exception as exc:
            logger.warning("[go2rtc] Lỗi khi dừng tiến trình: %s", exc)
        finally:
            _go2rtc_proc = None


def get_go2rtc_status(api_port=DEFAULT_API_PORT):
    """Return status snapshot of go2rtc service."""
    running = False
    pid = None
    if _go2rtc_proc and _go2rtc_proc.poll() is None:
        running = True
        pid = _go2rtc_proc.pid
    healthy = is_go2rtc_healthy(api_port=api_port) if running else False
    return {
        "running": running,
        "healthy": healthy,
        "pid": pid,
        "api_port": api_port,
    }


atexit.register(stop_go2rtc)
