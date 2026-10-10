"""Independent updater: stop Cambida, download a ZIP, overlay it, restart."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

import psutil

EXCLUDE_FILE = "update_exclude.txt"
APP_NAMES = {"cambida.exe", "camhl.exe", "ffmpeg.exe", "cloudflared.exe", "cambidawatchdog.exe"}


def log(message, target):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + message
    if sys.stdout:
        print(line, flush=True)
    folder = Path(target) / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "updater.log").open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")


def normalize_relative(relative):
    relative = str(relative).replace("\\", "/").rstrip("/")
    parts = relative.split("/")
    if not relative or ":" in relative or any(p in ("", ".", "..") or p.endswith((" ", ".")) for p in parts):
        raise ValueError("Unsafe update path: " + relative)
    return relative


def safe_path(root, relative):
    relative = normalize_relative(relative)
    parts = relative.split("/")
    root = Path(root).resolve()
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root):
        raise ValueError("Update path escapes its directory: " + relative)
    walk = path
    while walk != root:
        if walk.is_symlink() or (hasattr(walk, "is_junction") and walk.is_junction()):
            raise ValueError("Update path uses a link: " + relative)
        walk = walk.parent
    return path


def read_exclusions(target, payload):
    policy = Path(target) / EXCLUDE_FILE
    if not policy.is_file():
        policy = Path(payload) / EXCLUDE_FILE
    rules = []
    for line in policy.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            normalized = normalize_relative(line)
            rules.append(normalized.casefold())
    return rules, policy


def excluded(relative, rules):
    name = relative.replace("\\", "/").casefold()
    return any(name == rule or name.startswith(rule + "/") for rule in rules)


def copy_with_retry(source, destination, attempts=20, delay=0.25):
    destination = Path(destination)
    for attempt in range(attempts):
        try:
            if destination.exists():
                destination.chmod(stat.S_IWRITE | stat.S_IREAD)
            shutil.copy2(source, destination)
            return
        except OSError as exc:
            if attempt == attempts - 1:
                raise OSError(f"Cannot overwrite {destination}: {exc}") from exc
            time.sleep(delay)


def overlay(payload, target):
    rules, policy = read_exclusions(target, payload)
    if not (target / EXCLUDE_FILE).is_file():
        copy_with_retry(policy, target / EXCLUDE_FILE)
    count = 0
    for source in sorted(payload.rglob("*")):
        relative = source.relative_to(payload).as_posix()
        if excluded(relative, rules):
            continue
        destination = safe_path(target, relative)
        if source.is_symlink() or (hasattr(source, "is_junction") and source.is_junction()):
            raise ValueError("Payload contains a link: " + relative)
        if source.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            copy_with_retry(source, destination)
            count += 1
    return count


def legacy_watchdog_root(target):
    root = (Path(os.environ.get("LOCALAPPDATA", "")) / "CambidaWatchdog").resolve()
    try:
        state = json.loads((root / "watchdog.json").read_text(encoding="utf-8-sig"))
        watched = Path(state["target"]).resolve()
        if watched == target or watched.is_relative_to(target):
            return root
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def stop_processes(target, old_pid=0, *, watchdog_only=False):
    target = Path(target).resolve()
    watchdog_root = legacy_watchdog_root(target)
    own = psutil.Process()
    keep = {own.pid, *[p.pid for p in own.parents()]}
    # A detached updater can still have Cambida's onefile bootloader as an
    # ancestor. It must stop that bootloader too, without killing its own.
    for parent in own.parents():
        try:
            exe = Path(parent.exe()).resolve()
            if exe.is_relative_to(target) and exe.name.casefold() in APP_NAMES:
                keep.discard(parent.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    if old_pid != own.pid:
        keep.discard(old_pid)
    selected = {}
    for process in psutil.process_iter(["pid", "exe", "name"]):
        try:
            if process.pid in keep or not process.info["exe"]:
                continue
            exe = Path(process.info["exe"]).resolve()
            name = exe.name.casefold()
            watchdog = name.startswith("cambidawatchdog") and exe.parent in (target, watchdog_root)
            app = exe.is_relative_to(target) and (name in APP_NAMES or name.startswith("cctv_"))
            if watchdog or (not watchdog_only and (app or process.pid == old_pid)):
                selected[process.pid] = process
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    for process in list(selected.values()):
        try:
            for child in process.children(recursive=True):
                if child.pid not in keep:
                    selected[child.pid] = child
        except psutil.NoSuchProcess:
            pass
    for process in selected.values():
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(list(selected.values()), timeout=10)
    if alive:
        raise RuntimeError("Processes did not stop: " + ", ".join(str(p.pid) for p in alive))


def remove_watchdog(target):
    if sys.platform != "win32":
        return
    import winreg
    target = Path(target).resolve()
    root = legacy_watchdog_root(target)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
            value, _ = winreg.QueryValueEx(key, "CambidaWatchdog")
            if "cambidawatchdog" in value.casefold() and (root is not None or str(target).casefold() in value.casefold()):
                winreg.DeleteValue(key, "CambidaWatchdog")
    except FileNotFoundError:
        pass
    stop_processes(target, watchdog_only=True)


def validate_payload(payload, version):
    manifest = json.loads((payload / "release_manifest.json").read_text(encoding="utf-8-sig"))
    update = json.loads((payload / "update.json").read_text(encoding="utf-8-sig"))
    if manifest.get("version") != version or update.get("version") != version:
        raise ValueError("Release version mismatch")
    if not isinstance(manifest.get("files"), dict) or not manifest["files"]:
        raise ValueError("Empty release manifest")
    for relative, expected in manifest["files"].items():
        with safe_path(payload, relative).open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() != expected.lower():
                raise ValueError("Release checksum mismatch: " + relative)
    allowed = set(manifest["files"]) | {"release_manifest.json", "update.json"}
    for path in payload.rglob("*"):
        if path.is_file() and path.relative_to(payload).as_posix() not in allowed:
            raise ValueError("Unlisted release file: " + path.relative_to(payload).as_posix())
    entry = update.get("start", {}).get("path")
    if entry not in manifest["files"]:
        raise ValueError("Missing release entrypoint")
    safe_path(payload, entry)


def extract_archive(archive, destination, version):
    with zipfile.ZipFile(archive) as bundle:
        seen = set()
        for entry in bundle.infolist():
            safe_path(destination, entry.filename)
            folded = entry.filename.replace("\\", "/").rstrip("/").casefold()
            if folded in seen or stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError("Duplicate path or link in ZIP: " + entry.filename)
            seen.add(folded)
        bundle.extractall(destination)
    payload = destination
    if not (payload / "update.json").is_file():
        children = list(payload.iterdir())
        if len(children) == 1 and children[0].is_dir():
            payload = children[0]
    validate_payload(payload, version)
    return payload


def download(url, archive):
    headers = {"User-Agent": "Cambida-Updater"}
    token = os.environ.get("CAMBIDA_UPDATE_TOKEN")
    if token:
        headers.update(Authorization="Bearer " + token, Accept="application/octet-stream")
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=180) as response, archive.open("wb") as stream:
        shutil.copyfileobj(response, stream)


def launch_application(target):
    update_file = target / "update.json"
    update = json.loads(update_file.read_text(encoding="utf-8-sig")) if update_file.is_file() else {}
    start = update.get("start") or {}
    relative = start.get("path") or next((name for name in ("Cambida.exe", "camhl.exe") if (target / name).is_file()), "Cambida.exe")
    entry = safe_path(target, relative)
    if not entry.is_file():
        raise FileNotFoundError("Missing application: " + str(entry))
    args = start.get("args", [])
    if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
        raise ValueError("Invalid application arguments")
    flags = 0x00000008 | 0x00000200 | 0x08000000 if sys.platform == "win32" else 0
    env = os.environ.copy()
    env.pop("CAMBIDA_UPDATE_TOKEN", None)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    process = subprocess.Popen([str(entry), *args], cwd=target, env=env, creationflags=flags, close_fds=True,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1)
    if process.poll() is not None:
        raise RuntimeError(f"Application exited immediately (code {process.returncode})")
    return process.pid


def run_update(target, version, *, url=None, payload=None, old_pid=0):
    target = Path(target).resolve()
    work = Path(tempfile.mkdtemp(prefix="cambida-update-", dir=target.parent))
    stopped = False
    copying = False
    try:
        remove_watchdog(target)
        stop_processes(target, old_pid)
        stopped = True
        log("Old processes stopped; downloading release " + version, target)
        if payload is None:
            archive = work / "release.zip"
            download(url, archive)
            payload = extract_archive(archive, work / "payload", version)
        else:
            payload = Path(payload).resolve()
            if payload == target or payload.is_relative_to(target):
                raise ValueError("Payload must be outside the installation")
            validate_payload(payload, version)
        read_exclusions(target, payload)
        copying = True
        count = overlay(payload, target)
        pid = launch_application(target)
        log(f"Update {version} complete: overwritten {count} files; application PID {pid}", target)
        return 0
    except Exception as exc:
        log("Update failed: " + str(exc), target)
        if stopped and not copying:
            try:
                pid = launch_application(target)
                log(f"Original application restarted: PID {pid}", target)
            except Exception as restart_error:
                log("Original application restart failed: " + str(restart_error), target)
        return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, required=True)
    parser.add_argument("--version")
    parser.add_argument("--old-pid", type=int, default=0)
    parser.add_argument("--remove-watchdog", action="store_true")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--url")
    source.add_argument("--payload", type=Path)
    args = parser.parse_args(argv)
    if args.remove_watchdog:
        remove_watchdog(args.target)
        return 0
    if not args.target.is_dir() or not args.version or not re.fullmatch(r"\d+\.\d+\.\d+", args.version) or not (args.url or args.payload):
        parser.error("An existing target, X.Y.Z version and --url or --payload are required")
    return run_update(args.target, args.version, url=args.url, payload=args.payload, old_pid=args.old_pid)


if __name__ == "__main__":
    sys.exit(main())
