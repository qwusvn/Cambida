"""Host Loader cho Camera Highlight (camhl.exe).

Đóng vai trò là file mồi cố định (Thin Host Loader):
- Khởi tạo môi trường Python runtime, nạp DLL Dahua NetSDK
- Tự động nạp và thực thi phần ruột backend (_internal/app.pyc)
- Cho phép cập nhật logic backend siêu tốc (0.1s qua py_compile) mà không cần build lại EXE
"""
import ctypes
import os
from pathlib import Path
import runpy
import sys
import time

# Dành cho PyInstaller tĩnh quét và đóng gói đầy đủ các thư viện phụ trợ vào _internal
if False:
    import atexit
    import base64
    import concurrent.futures
    import contextlib
    import copy
    import ctypes
    import cv2
    import dahua_37777
    import dataclasses
    import datetime
    import enum
    import flask
    import functools
    import glob
    import hashlib
    import hmac
    import importlib
    import io
    import ipaddress
    import json
    import logging
    import logging.handlers
    import math
    import native_update
    import os
    import pathlib
    import psutil
    import pystray
    import qrcode
    import queue
    import re
    import requests
    import requests.auth
    import secrets
    import shutil
    import socket
    import sqlite3
    import stat
    import struct
    import subprocess
    import sys
    import tempfile
    import threading
    import time
    import typing
    import urllib.error
    import urllib.parse
    import urllib.request
    import uuid
    import waitress
    import webbrowser
    import winreg
    import xml.etree.ElementTree
    import zipfile
    import zlib
    import PIL.Image
    import PIL.ImageDraw
    import camera_modules
    import camera_modules.config
    import camera_modules.core
    import camera_modules.dahua
    import camera_modules.discovery
    import camera_modules.hikvision
    import camera_modules.onvif
    import camera_modules.policy
    import camera_modules.rtsp
    import camera_modules.table_access


def boot_log(base_dir: Path, msg: str):
    try:
        log_dir = base_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(log_dir / "camhl_boot.log", "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def main():
    if getattr(sys, "frozen", False):
        base_dir = Path(sys.executable).resolve().parent
        bundle_dir = Path(getattr(sys, "_MEIPASS", base_dir))
    else:
        base_dir = Path(__file__).resolve().parent.parent
        bundle_dir = base_dir

    boot_log(base_dir, f"Khởi động Host Loader camhl.exe. Base: {base_dir}, Bundle: {bundle_dir}, Frozen: {getattr(sys, 'frozen', False)}")

    # 1. Thiết lập thư mục Dahua NetSDK DLLs
    sdk_candidates = [
        bundle_dir / "vendor" / "dahua_netsdk",
        base_dir / "vendor" / "dahua_netsdk",
        base_dir / "_internal" / "vendor" / "dahua_netsdk",
    ]
    for sdk_path in sdk_candidates:
        if sdk_path.is_dir():
            boot_log(base_dir, f"Đã tìm thấy NetSDK DLLs tại: {sdk_path}")
            os.environ.setdefault("DAHUA_NETSDK_DIR", str(sdk_path))
            if hasattr(os, "add_dll_directory"):
                try:
                    os.add_dll_directory(str(sdk_path))
                except Exception as exc:
                    boot_log(base_dir, f"Cảnh báo add_dll_directory: {exc}")
            break

    # 2. Bổ sung base_dir và _internal vào sys.path
    internal_dir = base_dir / "_internal"
    for p in (str(base_dir), str(internal_dir)):
        if p not in sys.path:
            sys.path.insert(0, p)
    boot_log(base_dir, f"Đã cấu hình sys.path")

    # 3. Tìm kiếm file ruột backend cần nạp
    backend_candidates = [
        internal_dir / "app.pyc",
        internal_dir / "app.py",
        base_dir / "app.pyc",
        base_dir / "1.py",
    ]

    target_backend = next((c for c in backend_candidates if c.is_file()), None)

    if not target_backend:
        msg = (
            f"Không tìm thấy file ruột backend (app.pyc) để thực thi!\n\n"
            f"Thư mục ứng dụng: {base_dir}\n"
            f"Thư mục nội bộ:   {internal_dir}\n"
            f"Vui lòng kiểm tra lại gói phát hành hoặc liên hệ kỹ thuật."
        )
        boot_log(base_dir, f"LỖI: {msg}")
        try:
            ctypes.windll.user32.MessageBoxW(None, msg, "Camera Highlight - Lỗi khởi động", 0x10)
        except Exception:
            pass
        sys.exit(1)

    boot_log(base_dir, f"Bắt đầu nạp ruột backend: {target_backend}")
    try:
        runpy.run_path(str(target_backend), run_name="__main__")
        boot_log(base_dir, f"Ruột backend đã kết thúc thực thi bình thường.")
    except SystemExit as se:
        boot_log(base_dir, f"Ruột backend gọi sys.exit({se.code})")
        raise
    except BaseException as be:
        import traceback
        err = traceback.format_exc()
        boot_log(base_dir, f"Lỗi ngoại lệ trong ruột backend:\n{err}")
        try:
            ctypes.windll.user32.MessageBoxW(
                None,
                f"Lỗi thực thi backend:\n\n{be}\n\nXem chi tiết tại logs/camhl_boot.log",
                "Camera Highlight - Sự cố",
                0x10,
            )
        except Exception:
            pass
        raise


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        pass
    except BaseException as exc:
        import traceback
        err_msg = traceback.format_exc()
        try:
            p = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
            boot_log(p, f"LỖI KHỞI ĐỘNG CẤP CAO NHẤT:\n{err_msg}")
        except Exception:
            pass
