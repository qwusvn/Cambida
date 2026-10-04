r"""Independent Cambida watchdog.

The frozen executable is installed under %LOCALAPPDATA%\CambidaWatchdog and is
therefore outside the application tree. It survives application replacement,
monitors only the configured Cambida installation, and can recover an
interrupted/failed update from the updater's durable journal.
"""
from __future__ import annotations

import argparse
from collections import deque
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

from runtime_state import snapshot_runtime_state

WATCHDOG_STATE = "watchdog.json"
TRANSACTION = ".watchdog-update.json"
LOG_NAME = "watchdog.log"
RUN_VALUE = "CambidaWatchdog"
CHECK_SECONDS = 5
PROCESS_GRACE_SECONDS = 30
UPDATE_STALE_SECONDS = 180
MONITOR_HEALTHY_SECONDS = 120
MAX_UPDATE_RESTARTS = 3
RESTART_WINDOW_SECONDS = 600
MUTEX_NAME = r"Local\CambidaWatchdog_SingleInstance"
_MUTEX_HANDLE = None
PROTECTED_NAMES = {
    "config.json", "analytics.db", "device_id.key", "tunnel_token.txt",
    "controlhub_machine_id.txt", "controlhub_client_secret.txt", "controlhub_bootstrap.json",
    "logs", "cctv_videos", "nvr_cache", ".git", ".project", ".updates",
    ".update-backups", ".pending_update_notification", ".watchdog-update.json",
    "native-update.log",
}

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def local_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "CambidaWatchdog"


def log(message: str) -> None:
    root = local_root()
    root.mkdir(parents=True, exist_ok=True)
    path = root / LOG_NAME
    try:
        if path.exists() and path.stat().st_size > 1_000_000:
            old = root / (LOG_NAME + ".1")
            old.unlink(missing_ok=True)
            path.replace(old)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"{stamp} {message}\n")
    except OSError:
        pass


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, TypeError):
        return default


def safe_child(root: Path, relative: str, *, allow_protected: bool = False) -> Path:
    rel = PurePosixPath(str(relative or ""))
    if not relative or "\\" in str(relative) or ":" in str(relative) or rel.is_absolute():
        raise ValueError(f"Unsafe relative path: {relative}")
    if any(part in {"", ".", ".."} or part.endswith((" ", ".")) for part in rel.parts):
        raise ValueError(f"Unsafe relative path: {relative}")
    if not allow_protected and any(part.casefold() in PROTECTED_NAMES for part in rel.parts):
        raise ValueError(f"Protected relative path: {relative}")
    root = root.resolve()
    result = (root / rel.as_posix()).resolve()
    if result != root and not result.is_relative_to(root):
        raise ValueError(f"Path escapes root: {relative}")
    return result


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def state_path() -> Path:
    return local_root() / WATCHDOG_STATE


def transaction_path(target: Path) -> Path:
    return target / TRANSACTION


def load_state() -> dict:
    return read_json(state_path(), {}) or {}


def save_state(data: dict) -> None:
    atomic_write_json(state_path(), data)


def cleanup_disk_artifacts(target: Path | None = None) -> None:
    """Clean up orphaned PyInstaller _MEI* dirs, stale update temp folders, and old backups."""
    temp_dir = Path(tempfile.gettempdir())
    now = time.time()

    # 1. Clean up orphaned _MEI* folders in Temp (unlocked by dead PyInstaller processes)
    try:
        for mei_dir in temp_dir.glob("_MEI*"):
            if not mei_dir.is_dir():
                continue
            try:
                shutil.rmtree(mei_dir, ignore_errors=False)
            except OSError:
                pass
    except Exception as exc:
        log(f"MEI cleanup warning: {exc}")

    # 2. Clean up stale update and updater directories older than 15 minutes
    try:
        for prefix in ("cambida-update-*", "cambida-updater-*"):
            for upd_dir in temp_dir.glob(prefix):
                if not upd_dir.is_dir():
                    continue
                try:
                    mtime = upd_dir.stat().st_mtime
                    if (now - mtime) > 900:  # 15 minutes
                        shutil.rmtree(upd_dir, ignore_errors=True)
                except OSError:
                    pass
    except Exception as exc:
        log(f"update temp cleanup warning: {exc}")

    # 3. Clean up older watchdog executables in %LOCALAPPDATA%\CambidaWatchdog
    try:
        root = local_root()
        state = load_state()
        desired_str = str(state.get("desired_exe") or "").casefold()
        for old_exe in root.glob("CambidaWatchdog-*.exe"):
            if str(old_exe.resolve()).casefold() != desired_str:
                try:
                    old_exe.unlink(missing_ok=True)
                except OSError:
                    pass
    except Exception:
        pass

    # 4. Prune old .update-backups in target: keep at most 1 recent backup
    if target and target.is_dir():
        try:
            backup_root = target / ".update-backups"
            if backup_root.is_dir():
                backups = sorted(
                    [p for p in backup_root.iterdir() if p.is_dir()],
                    key=lambda p: p.stat().st_mtime,
                    reverse=True
                )
                for stale_backup in backups[1:]:
                    try:
                        shutil.rmtree(stale_backup, ignore_errors=True)
                        log(f"pruned old backup: {stale_backup.name}")
                    except OSError:
                        pass
        except Exception as exc:
            log(f"backup pruning warning: {exc}")


def install_watchdog(target: Path) -> Path:
    target = target.resolve()
    try:
        cleanup_disk_artifacts(target)
    except Exception as exc:
        log(f"cleanup_disk_artifacts warning during install: {exc}")
    try:
        snapshot_runtime_state(target)
    except Exception as exc:
        log(f"runtime-state snapshot before install failed: {exc}")
    source = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve()
    root = local_root()
    root.mkdir(parents=True, exist_ok=True)
    suffix = sha256(source)[:12]
    desired = root / f"CambidaWatchdog-{suffix}.exe"
    if source.suffix.lower() != ".exe":
        raise RuntimeError("Watchdog install requires the frozen executable.")
    if not desired.exists() or sha256(desired) != sha256(source):
        temp = root / (desired.name + ".new")
        shutil.copy2(source, temp)
        os.replace(temp, desired)

    desired_resolved = desired.resolve()
    for old_exe in root.glob("CambidaWatchdog-*.exe"):
        if old_exe.resolve() != desired_resolved:
            try:
                old_exe.unlink(missing_ok=True)
            except OSError:
                pass

    data = load_state()
    data.update({
        "schema": 1,
        "target": str(target),
        "desired_exe": str(desired),
        "updated_at": int(time.time()),
    })
    save_state(data)

    try:
        import winreg
        command = f'"{desired}" --run'
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, command)
    except Exception as exc:
        log(f"registry install failed: {exc}")

    desired_str = str(desired.resolve()).casefold()
    already_running = False
    for pid, path in process_paths():
        if str(path.resolve()).casefold() == desired_str:
            already_running = True
            break

    if not already_running:
        flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        try:
            cmd = f'start "" "{desired}" --run'
            subprocess.Popen(
                ["cmd.exe", "/c", cmd],
                cwd=str(root),
                creationflags=flags,
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            log(f"watchdog launch failed during install: {exc}")
    else:
        log(f"watchdog already running desired={desired}")

    log(f"installed desired={desired} target={target}")
    return desired


def _kernel32():
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    k32.Process32FirstW.restype = wintypes.BOOL
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    k32.Process32NextW.restype = wintypes.BOOL
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    ]
    k32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.TerminateProcess.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    k32.CreateMutexW.restype = wintypes.HANDLE
    return k32


def acquire_singleton(timeout: float = 12.0):
    global _MUTEX_HANDLE
    if os.name != "nt":
        return object()
    deadline = time.time() + timeout
    while True:
        k32 = _kernel32()
        ctypes.set_last_error(0)
        handle = k32.CreateMutexW(None, True, MUTEX_NAME)
        if handle:
            error = ctypes.get_last_error()
            if error != 183:  # ERROR_ALREADY_EXISTS
                _MUTEX_HANDLE = handle
                return handle
            k32.CloseHandle(handle)
        if time.time() >= deadline:
            return None
        time.sleep(0.5)


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


def process_paths() -> list[tuple[int, Path]]:
    if os.name != "nt":
        return []
    k32 = _kernel32()
    TH32CS_SNAPPROCESS = 0x00000002
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    snapshot = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        return []
    rows: list[tuple[int, Path]] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        ok = k32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            pid = int(entry.th32ProcessID)
            handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                try:
                    size = wintypes.DWORD(32768)
                    buf = ctypes.create_unicode_buffer(size.value)
                    if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                        rows.append((pid, Path(buf.value)))
                finally:
                    k32.CloseHandle(handle)
            ok = k32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snapshot)
    return rows


def owned_pids(target: Path) -> list[int]:
    expected = str((target / "Cambida.exe").resolve()).casefold()
    return [pid for pid, path in process_paths() if str(path.resolve()).casefold() == expected]


def terminate_owned(target: Path) -> None:
    expected = str((target / "Cambida.exe").resolve()).casefold()
    k32 = _kernel32() if os.name == "nt" else None
    for pid, path in process_paths():
        if str(path.resolve()).casefold() != expected:
            continue
        if not k32:
            continue
        PROCESS_TERMINATE = 0x0001
        handle = k32.OpenProcess(PROCESS_TERMINATE, False, pid)
        if handle:
            try:
                k32.TerminateProcess(handle, 1)
                log(f"terminated owned Cambida pid={pid}")
            finally:
                k32.CloseHandle(handle)


def runtime_port(target: Path) -> int:
    cfg = read_json(target / "config.json", {}) or {}
    try:
        port = int(cfg.get("server_port") or 8000)
    except (TypeError, ValueError):
        port = 8000
    return port if 1 <= port <= 65535 else 8000


def http_healthy(target: Path) -> bool:
    url = f"http://127.0.0.1:{runtime_port(target)}/api/ping"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "CambidaWatchdog/1"})
        with urllib.request.urlopen(request, timeout=2) as response:
            return 200 <= int(response.status) < 400
    except Exception:
        return False


def release_start(target: Path) -> tuple[Path, list[str]]:
    update = read_json(target / "update.json", {}) or {}
    start = update.get("start") if isinstance(update, dict) else {}
    if not isinstance(start, dict):
        start = {}
    relative = str(start.get("path") or "Cambida.exe")
    args = start.get("args") if isinstance(start.get("args"), list) else []
    entry = safe_child(target, relative)
    return entry, [str(arg) for arg in args if isinstance(arg, str)]


def launch_release(target: Path) -> bool:
    try:
        entry, args = release_start(target)
        if not entry.is_file():
            log(f"cannot launch missing entrypoint: {entry}")
            return False
        flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        subprocess.Popen(
            [str(entry), *args],
            cwd=str(target),
            creationflags=flags,
            close_fds=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        log(f"launched {entry.name}")
        return True
    except Exception as exc:
        log(f"launch failed: {exc}")
        return False


def rollback_transaction(target: Path, tx: dict, *, launch: bool = True) -> bool:
    journal = tx.get("journal")
    backup_rel = str(tx.get("backup_rel") or "")
    if not isinstance(journal, list) or not journal or not backup_rel:
        log("rollback refused: transaction journal/backup missing")
        return False
    try:
        backup_root = safe_child(target, backup_rel, allow_protected=True)
    except ValueError as exc:
        log(f"rollback refused: {exc}")
        return False

    errors = []
    for item in reversed(journal):
        if not isinstance(item, dict):
            errors.append("invalid journal item")
            continue
        relative = str(item.get("relative") or "")
        existed = bool(item.get("existed"))
        try:
            dest = safe_child(target, relative)
            backup = safe_child(backup_root, relative)
            if existed:
                if not backup.is_file():
                    raise FileNotFoundError(str(backup))
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup, dest)
            elif dest.exists() and dest.is_file():
                dest.unlink()
        except Exception as exc:
            errors.append(f"{relative}: {exc}")

    if errors:
        tx["stage"] = "rollback_failed"
        tx["rollback_errors"] = errors
        tx["updated_at"] = int(time.time())
        atomic_write_json(transaction_path(target), tx)
        log("rollback failed: " + "; ".join(errors))
        return False

    tx["stage"] = "rolled_back"
    tx["updated_at"] = int(time.time())
    atomic_write_json(transaction_path(target), tx)
    log(f"rollback completed from {backup_rel}")
    if launch:
        launch_release(target)
    return True


def transaction_age(tx: dict) -> float:
    try:
        stamp = float(tx.get("updated_at") or tx.get("started_at") or 0)
    except (TypeError, ValueError):
        stamp = 0
    return max(0.0, time.time() - stamp) if stamp else float("inf")


def monitor(target: Path) -> int:
    unhealthy_since = None
    restart_times: deque[float] = deque()
    tx_healthy_since = None
    last_state_snapshot = 0.0
    last_cleanup = time.time()

    try:
        cleanup_disk_artifacts(target)
    except Exception as exc:
        log(f"initial disk cleanup warning: {exc}")

    while True:
        state = load_state()
        desired = Path(str(state.get("desired_exe") or "")).resolve() if state.get("desired_exe") else None
        current = Path(sys.executable).resolve()
        if desired and desired.is_file() and str(desired).casefold() != str(current).casefold():
            log(f"new watchdog desired; exiting current binary: {desired}")
            return 3

        target_text = str(state.get("target") or target)
        target = Path(target_text).resolve()
        tx = read_json(transaction_path(target), {}) or {}
        stage = str(tx.get("stage") or "")

        if stage in {"applying", "starting"}:
            if transaction_age(tx) >= UPDATE_STALE_SECONDS:
                log(f"stale update transaction stage={stage}; rolling back")
                rollback_transaction(target, tx)
            time.sleep(CHECK_SECONDS)
            continue

        healthy = http_healthy(target)
        now = time.time()

        if stage == "monitoring":
            if healthy:
                if tx_healthy_since is None:
                    tx_healthy_since = now
                if now - tx_healthy_since >= MONITOR_HEALTHY_SECONDS:
                    tx["stage"] = "healthy"
                    tx["healthy_at"] = int(now)
                    tx["updated_at"] = int(now)
                    atomic_write_json(transaction_path(target), tx)
                    log("updated release passed watchdog stabilization window")
                time.sleep(CHECK_SECONDS)
                continue

            tx_healthy_since = None

        if healthy:
            unhealthy_since = None
            if now - last_state_snapshot >= 60:
                try:
                    snapshot_runtime_state(target)
                    last_state_snapshot = now
                except Exception as exc:
                    log(f"runtime-state periodic snapshot failed: {exc}")
            if now - last_cleanup >= 3600:
                try:
                    cleanup_disk_artifacts(target)
                    last_cleanup = now
                except Exception as exc:
                    log(f"periodic disk cleanup warning: {exc}")
            time.sleep(CHECK_SECONDS)
            continue

        pids = owned_pids(target)
        if pids:
            if unhealthy_since is None:
                unhealthy_since = now
            if now - unhealthy_since < PROCESS_GRACE_SECONDS:
                time.sleep(CHECK_SECONDS)
                continue
            terminate_owned(target)
            unhealthy_since = None
        else:
            unhealthy_since = None

        if stage == "monitoring":
            failures = int(tx.get("watchdog_failures") or 0)
            if failures >= MAX_UPDATE_RESTARTS:
                log("updated release failed after repeated restart attempts; rolling back")
                terminate_owned(target)
                rollback_transaction(target, tx)
                time.sleep(CHECK_SECONDS)
                continue
            tx["watchdog_failures"] = failures + 1
            tx["updated_at"] = int(now)
            atomic_write_json(transaction_path(target), tx)

        while restart_times and now - restart_times[0] > RESTART_WINDOW_SECONDS:
            restart_times.popleft()
        if len(restart_times) >= 5:
            log("restart rate limit reached; backing off 60 seconds")
            time.sleep(60)
            continue
        if launch_release(target):
            restart_times.append(now)
            unhealthy_since = None
        time.sleep(CHECK_SECONDS)


def status(target: Path) -> dict:
    tx = read_json(transaction_path(target), {}) or {}
    return {
        "target": str(target),
        "port": runtime_port(target),
        "healthy": http_healthy(target),
        "owned_pids": owned_pids(target),
        "transaction_stage": tx.get("stage"),
        "watchdog_root": str(local_root()),
    }


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--target")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    target = Path(args.target).resolve() if args.target else None
    if args.install:
        if not target:
            raise SystemExit("--target is required for --install")
        install_watchdog(target)
        return 0

    state = load_state()
    if target is None and state.get("target"):
        target = Path(str(state["target"])).resolve()
    if target is None:
        raise SystemExit("Watchdog target is not configured.")

    if args.status:
        print(json.dumps(status(target), ensure_ascii=False))
        return 0
    if args.rollback:
        tx = read_json(transaction_path(target), {}) or {}
        return 0 if rollback_transaction(target, tx) else 2
    if args.run:
        if not acquire_singleton():
            return 0
        return monitor(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
