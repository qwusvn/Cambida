"""Standalone binary updater for Camera Highlight (camhl.exe).

Pure Python implementation compiled to updater.exe.
Eliminates script encoding issues, PowerShell execution policy blocks,
and file sharing violations on Windows.
"""
import datetime
import os
import shutil
import subprocess
import sys
import time

PROTECTED_FILES = {
    "config.json",
    "analytics.db",
    "device_id.key",
}

INSTALL_ONCE_FILES = {
    "tunnel_token.txt",
}

PROTECTED_DIRS = {
    "logs",
    "cctv_videos",
    "nvr_cache",
    ".git",
    ".project",
    ".update-backups",
}

OBSOLETE_ITEMS = [
    "run.cmd",
    "updater.cmd",
    "native_updater.ps1",
    "cloudflared_setup",
    "modules",
]


def log(msg, log_file=None):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] [Updater] {msg}\n"
    if sys.stdout:
        try:
            sys.stdout.write(line)
            sys.stdout.flush()
        except Exception:
            pass
    if log_file:
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass


def terminate_pid(pid, log_file=None):
    if not pid or pid <= 0:
        return
    log(f"Đang đợi tiến trình cũ PID {pid} thoát...", log_file)
    for _ in range(20):
        try:
            # Check if process is alive on Windows
            res = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
            )
            if str(pid) not in res.stdout:
                log(f"Tiến trình PID {pid} đã thoát an toàn.", log_file)
                return
        except Exception:
            pass
        time.sleep(1.0)

    log(f"Hết thời gian chờ, cưỡng bức dừng PID {pid}...", log_file)
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    except Exception as exc:
        log(f"Lỗi taskkill PID {pid}: {exc}", log_file)


def release_file_locks(log_file=None):
    log("Giải phóng khóa file (ffmpeg.exe, cloudflared.exe, camhl.exe)...", log_file)
    for proc_name in ("ffmpeg.exe", "cloudflared.exe", "camhl.exe", "Cambida.exe"):
        try:
            subprocess.run(["taskkill", "/F", "/IM", proc_name], capture_output=True)
        except Exception:
            pass
    time.sleep(2.0)


def copy_with_retry(src, dst, max_retries=5, delay=1.0):
    for attempt in range(max_retries):
        try:
            shutil.copy2(src, dst)
            return True
        except (PermissionError, OSError):
            if attempt < max_retries - 1:
                time.sleep(delay)
            else:
                raise


def update_files(src_dir, dst_dir, log_file=None):
    log(f"Bắt đầu sao chép từ {src_dir} sang {dst_dir}...", log_file)
    count = 0
    for root, dirs, files in os.walk(src_dir):
        rel_root = os.path.relpath(root, src_dir)
        parts = rel_root.split(os.sep) if rel_root != "." else []

        if any(p.lower() in PROTECTED_DIRS for p in parts):
            continue

        target_root = dst_dir if rel_root == "." else os.path.join(dst_dir, rel_root)
        os.makedirs(target_root, exist_ok=True)

        for filename in files:
            src_file = os.path.join(root, filename)
            dst_file = os.path.join(target_root, filename)
            lower_name = filename.lower()

            if lower_name in PROTECTED_FILES:
                log(f"Bảo vệ cấu hình: bỏ qua ghi đè {filename}", log_file)
                continue
            if lower_name in INSTALL_ONCE_FILES and os.path.exists(dst_file):
                log(f"Bảo vệ token hiện có: bỏ qua ghi đè {filename}", log_file)
                continue


            # Đừng tự đè lên chính binary updater.exe đang chạy
            if os.path.abspath(src_file) == os.path.abspath(sys.executable) or os.path.abspath(dst_file) == os.path.abspath(sys.executable):
                continue

            copy_with_retry(src_file, dst_file)
            count += 1

    log(f"Đã cập nhật thành công {count} file vào {dst_dir}.", log_file)


def cleanup_obsolete(target_dir, log_file=None):
    log("Dọn dẹp các file/thư mục cũ không còn dùng...", log_file)
    for name in OBSOLETE_ITEMS:
        item_path = os.path.join(target_dir, name)
        if os.path.isfile(item_path):
            try:
                os.remove(item_path)
                log(f"Đã xóa file cũ: {name}", log_file)
            except Exception:
                pass
        elif os.path.isdir(item_path):
            try:
                shutil.rmtree(item_path, ignore_errors=True)
                log(f"Đã xóa thư mục cũ: {name}", log_file)
            except Exception:
                pass

    # Xóa các file CCTV_*.exe hoặc launcher cũ
    try:
        for f in os.listdir(target_dir):
            if (f.startswith("CCTV_") and f.endswith(".exe")) or (f.startswith("CCTV_") and f.endswith(".launcher.cmd")):
                try:
                    os.remove(os.path.join(target_dir, f))
                    log(f"Đã dọn dẹp file cũ: {f}", log_file)
                except Exception:
                    pass
    except Exception:
        pass


def launch_application(target_dir, log_file=None):
    app_exe = os.path.join(target_dir, "camhl.exe")
    if not os.path.isfile(app_exe):
        # Fallback to other exes if camhl.exe not found
        for f in ("Cambida.exe",):
            cand = os.path.join(target_dir, f)
            if os.path.isfile(cand):
                app_exe = cand
                break

    if not os.path.isfile(app_exe):
        log(f"LỖI: Không tìm thấy file thực thi chính tại {target_dir}", log_file)
        return False

    log(f"Khởi động lại ứng dụng: {app_exe}...", log_file)
    flags = 0
    if sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        if hasattr(subprocess, "DETACHED_PROCESS"):
            flags |= subprocess.DETACHED_PROCESS
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            flags |= subprocess.CREATE_NO_WINDOW

    try:
        subprocess.Popen(
            [app_exe],
            cwd=target_dir,
            creationflags=flags,
            close_fds=True,
        )
        log("Ứng dụng đã được khởi động thành công.", log_file)
        return True
    except Exception as exc:
        log(f"Lỗi khi khởi động ứng dụng: {exc}", log_file)
        return False


def main():
    old_pid = None
    if len(sys.argv) > 1 and sys.argv[1].strip():
        try:
            old_pid = int(sys.argv[1].strip())
        except ValueError:
            old_pid = None

    extract_dir = sys.argv[2] if len(sys.argv) > 2 else ""
    target_dir = sys.argv[3] if len(sys.argv) > 3 else os.path.dirname(os.path.abspath(sys.executable))

    if not target_dir or not os.path.isdir(target_dir):
        target_dir = os.path.dirname(os.path.abspath(__file__))

    log_dir = os.path.join(target_dir, "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "updater.log")

    log("=" * 60, log_file)
    log(f"Khởi chạy updater.exe (OLD_PID={old_pid}, EXTRACT={extract_dir}, TARGET={target_dir})", log_file)

    if old_pid:
        terminate_pid(old_pid, log_file)

    release_file_locks(log_file)

    # Locate actual source folder inside extract_dir
    src_folder = extract_dir
    if extract_dir and os.path.isdir(extract_dir):
        candidates = [os.path.join(extract_dir, d) for d in os.listdir(extract_dir)
                      if os.path.isdir(os.path.join(extract_dir, d))]
        for c in candidates:
            if os.path.isfile(os.path.join(c, "camhl.exe")) or os.path.isfile(os.path.join(c, "index.html")):
                src_folder = c
                break

    if src_folder and os.path.isdir(src_folder) and os.path.abspath(src_folder) != os.path.abspath(target_dir):
        try:
            update_files(src_folder, target_dir, log_file)
        except Exception as exc:
            log(f"LỖI trong quá trình sao chép file: {exc}", log_file)
            return 1

    cleanup_obsolete(target_dir, log_file)

    launch_application(target_dir, log_file)

    # Clean up extract dir
    if extract_dir and os.path.isdir(extract_dir) and os.path.abspath(extract_dir) != os.path.abspath(target_dir):
        time.sleep(2.0)
        try:
            shutil.rmtree(extract_dir, ignore_errors=True)
            log(f"Đã dọn dẹp thư mục tạm: {extract_dir}", log_file)
        except Exception:
            pass

    log("Quá trình cập nhật hoàn tất 100%.", log_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
