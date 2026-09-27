"""Build clean 100% EXE release package for Camera Highlight 2.2.2.

Builds camhl.exe (onedir, noconsole) and updater.exe (onefile, noconsole).
Outputs to release/2.2.2/ and release/2.2.2.zip.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import zipfile

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
VERSION = "2.2.3"
RELEASE_DIR = ROOT / "release" / VERSION
RELEASE_ZIP = ROOT / "release" / f"{VERSION}.zip"
DIST_CAMHL = ROOT / "dist-camhl"
WORK_CAMHL = ROOT / "work-camhl"
DIST_UPDATER = ROOT / "dist-updater"
WORK_UPDATER = ROOT / "work-updater"
DIST_SETUP_CF = ROOT / "dist-setup-cf"
WORK_SETUP_CF = ROOT / "work-setup-cf"

SIDECARS = [
    "index.html",
    "admin.html",
    "admin_login.html",
    "home.html",
    "live_all.html",
    "stats.html",
    "timeline.html",
    "ffmpeg.exe",
    "cloudflared.exe",
    "RELEASE_VERSION.txt",
]

FORBIDDEN_ITEMS = {
    "config.json",
    "analytics.db",
    "device_id.key",
    "logs",
    "cctv_videos",
    "nvr_cache",
    "run.cmd",
    "Chay_CCTV.cmd",
    "updater.cmd",
    "native_updater.ps1",
    "cloudflared_setup",
    "modules",
}


def sha256(path):
    with open(path, "rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def run_cmd(cmd, desc):
    print(f"[*] {desc}...", flush=True)
    t0 = time.time()
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    res = subprocess.run(cmd, cwd=ROOT, text=True, env=env)
    if res.returncode != 0:
        raise RuntimeError(f"Command failed with code {res.returncode}: {' '.join(str(c) for c in cmd)}")
    print(f"[+] {desc} hoàn tất trong {time.time() - t0:.1f}s", flush=True)


def clean_temp_dirs():
    for p in (DIST_CAMHL, WORK_CAMHL, DIST_UPDATER, WORK_UPDATER, DIST_SETUP_CF, WORK_SETUP_CF):
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
    for spec_name in ("setup_cloudflare.spec", "updater.spec"):
        spec_path = ROOT / spec_name
        if spec_path.exists():
            try:
                spec_path.unlink()
            except Exception:
                pass


def make_zip(source_dir, output_zip):
    print(f"[*] Đang nén file {output_zip.name}...", flush=True)
    t0 = time.time()
    with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for root, dirs, files in os.walk(source_dir):
            for file in sorted(files):
                abs_path = Path(root) / file
                rel_path = abs_path.relative_to(source_dir)
                zf.write(abs_path, rel_path.as_posix())
    print(f"[+] Nén hoàn tất trong {time.time() - t0:.1f}s (Dung lượng: {output_zip.stat().st_size:,} bytes)", flush=True)


def main():
    print("=" * 60, flush=True)
    print(f"BẮT ĐẦU ĐÓNG GÓI CAMBIDA / CAMERA HIGHLIGHT v{VERSION}", flush=True)
    print("=" * 60, flush=True)

    quick_mode = "--quick" in sys.argv or "--app-only" in sys.argv
    if quick_mode and (ROOT / "camhl.exe").is_file() and (ROOT / "_internal").is_dir():
        print("[*] Chế độ Quick Build: Tái sử dụng bộ 3 EXE và _internal từ thư mục gốc để đóng gói siêu tốc...", flush=True)
        updater_exe = ROOT / "updater.exe"
        setup_cf_exe = ROOT / "setup_cloudflare.exe"
        camhl_exe = ROOT / "camhl.exe"
        camhl_internal = ROOT / "_internal"
    else:
        clean_temp_dirs()

        # 1. Build updater.exe (Onefile)
        updater_script = ROOT / "scripts" / "updater.py"
        if not updater_script.is_file():
            raise RuntimeError(f"Không tìm thấy {updater_script}")

        run_cmd(
            [
                sys.executable,
                "-m",
                "PyInstaller",
                "--noconfirm",
                "--onefile",
                "--noconsole",
                "--name",
                "updater",
                "--distpath",
                str(DIST_UPDATER),
                "--workpath",
                str(WORK_UPDATER),
                str(updater_script),
            ],
            "Biên dịch updater.exe (onefile, noconsole)",
        )

        updater_exe = DIST_UPDATER / "updater.exe"
        if not updater_exe.is_file():
            raise RuntimeError("Không tìm thấy output updater.exe!")

        # 2. Build setup_cloudflare.exe (Onefile, console)
        setup_cf_script = ROOT / "scripts" / "setup_cloudflare.py"
        if not setup_cf_script.is_file():
            raise RuntimeError(f"Không tìm thấy {setup_cf_script}")

        run_cmd(
            [
                sys.executable,
                "-m",
                "PyInstaller",
                "--noconfirm",
                "--onefile",
                "--console",
                "--name",
                "setup_cloudflare",
                "--distpath",
                str(DIST_SETUP_CF),
                "--workpath",
                str(WORK_SETUP_CF),
                str(setup_cf_script),
            ],
            "Biên dịch setup_cloudflare.exe (onefile, console)",
        )

        setup_cf_exe = DIST_SETUP_CF / "setup_cloudflare.exe"
        if not setup_cf_exe.is_file():
            raise RuntimeError("Không tìm thấy output setup_cloudflare.exe!")

        # 3. Build camhl.exe (Onedir)
        spec_file = ROOT / "camhl.spec"
        if not spec_file.is_file():
            raise RuntimeError(f"Không tìm thấy {spec_file}")

        run_cmd(
            [
                sys.executable,
                "-m",
                "PyInstaller",
                "--noconfirm",
                "--distpath",
                str(DIST_CAMHL),
                "--workpath",
                str(WORK_CAMHL),
                str(spec_file),
            ],
            "Biên dịch camhl.exe (onedir, noconsole)",
        )

        camhl_exe = DIST_CAMHL / "camhl" / "camhl.exe"
        camhl_internal = DIST_CAMHL / "camhl" / "_internal"
        if not camhl_exe.is_file() or not camhl_internal.is_dir():
            raise RuntimeError("Thiếu output camhl.exe hoặc _internal!")

    # 4. Assemble release directory
    print(f"[*] Đang lắp ráp thư mục phát hành: {RELEASE_DIR}...", flush=True)
    try:
        import psutil
        for p in psutil.process_iter(["name", "exe"]):
            try:
                exe = p.info.get("exe") or ""
                if str(RELEASE_DIR).lower() in exe.lower():
                    print(f"[*] Đang dừng tiến trình đang chạy trong release: {p.info.get('name')} (PID: {p.pid})", flush=True)
                    p.kill()
            except Exception:
                pass
        time.sleep(1)
    except Exception:
        pass

    if RELEASE_DIR.exists():
        shutil.rmtree(RELEASE_DIR, ignore_errors=True)
    RELEASE_DIR.mkdir(parents=True, exist_ok=True)

    # Copy camhl.exe
    shutil.copy2(camhl_exe, RELEASE_DIR / "camhl.exe")

    # Copy _internal
    shutil.copytree(
        camhl_internal,
        RELEASE_DIR / "_internal",
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("config.json", "*.log", "*.db", "device_id.key", "__pycache__"),
    )

    # Copy updater.exe
    shutil.copy2(updater_exe, RELEASE_DIR / "updater.exe")

    # Copy setup_cloudflare.exe
    shutil.copy2(setup_cf_exe, RELEASE_DIR / "setup_cloudflare.exe")

    # Copy sidecars
    for sidecar in SIDECARS:
        src = ROOT / sidecar
        if not src.is_file():
            raise RuntimeError(f"Thiếu file sidecar bắt buộc: {sidecar}")
        shutil.copy2(src, RELEASE_DIR / sidecar)

    # Copy VERSION.txt
    shutil.copy2(ROOT / "RELEASE_VERSION.txt", RELEASE_DIR / "VERSION.txt")

    # Ensure config.release.json is in _internal
    if not (RELEASE_DIR / "_internal" / "config.release.json").is_file():
        shutil.copy2(ROOT / "config.release.json", RELEASE_DIR / "_internal" / "config.release.json")

    # Compile 1.py -> _internal/app.pyc (Ruột backend độc lập)
    print("[*] Đang biên dịch ruột backend 1.py -> _internal/app.pyc...", flush=True)
    import py_compile
    app_pyc = RELEASE_DIR / "_internal" / "app.pyc"
    py_compile.compile(str(ROOT / "1.py"), cfile=str(app_pyc), doraise=True)
    if not app_pyc.is_file():
        raise RuntimeError("Không tìm thấy output _internal/app.pyc sau khi biên dịch!")
    print(f"[+] Biên dịch ruột backend hoàn tất: {app_pyc.stat().st_size:,} bytes", flush=True)

    # 5. Strict Zero Config Pollution & Cleanliness Audit
    print("[*] Đang đối soát bảo mật (Zero Config Pollution Audit)...", flush=True)
    found_forbidden = []
    for root, dirs, files in os.walk(RELEASE_DIR):
        for name in files + dirs:
            if name.lower() in {f.lower() for f in FORBIDDEN_ITEMS}:
                found_forbidden.append(name)
            # Không được có file .py thô của ứng dụng ở root
            if root == str(RELEASE_DIR) and name.endswith(".py"):
                found_forbidden.append(name)

    if found_forbidden:
        raise RuntimeError(f"Phát hiện file/thư mục cấm trong release: {found_forbidden}")
    print("[+] Kiểm tra đối soát bảo mật: 100% PASS!", flush=True)

    # 6. Generate manifest & update info
    print("[*] Đang tạo release_manifest.json và update.json...", flush=True)
    manifest = {
        "version": VERSION,
        "product": "Camera Highlight",
        "app_binary": "camhl.exe",
        "updater_binary": "updater.exe",
        "setup_cloudflare_binary": "setup_cloudflare.exe",
        "files": {},
    }
    for root, dirs, files in os.walk(RELEASE_DIR):
        for file in sorted(files):
            abs_p = Path(root) / file
            rel_p = abs_p.relative_to(RELEASE_DIR).as_posix()
            manifest["files"][rel_p] = sha256(abs_p)

    with open(RELEASE_DIR / "release_manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        f.write("\n")

    update_info = {
        "schema": 2,
        "version": VERSION,
        "app_binary": "camhl.exe",
        "updater_binary": "updater.exe",
        "setup_cloudflare_binary": "setup_cloudflare.exe",
        "kind": "full",
    }
    with open(RELEASE_DIR / "update.json", "w", encoding="utf-8") as f:
        json.dump(update_info, f, indent=2, ensure_ascii=False)
        f.write("\n")

    # 7. Compress into ZIP
    if RELEASE_ZIP.exists():
        RELEASE_ZIP.unlink()
    make_zip(RELEASE_DIR, RELEASE_ZIP)

    zip_hash = sha256(RELEASE_ZIP)
    camhl_hash = sha256(RELEASE_DIR / "camhl.exe")
    updater_hash = sha256(RELEASE_DIR / "updater.exe")
    setup_cf_hash = sha256(RELEASE_DIR / "setup_cloudflare.exe")

    # 8. Deploy to root workspace for testing
    if not quick_mode:
        print("[*] Đang đồng bộ cấu trúc phát hành 2.2.3 vào thư mục gốc để chạy test...", flush=True)
        protected_root_files = {"config.json", "analytics.db", "device_id.key", "tunnel_token.txt"}
        for f in RELEASE_DIR.iterdir():
            if f.is_file():
                if f.name.lower() in protected_root_files:
                    continue
                try:
                    shutil.copy2(f, ROOT / f.name)
                except Exception:
                    pass
            elif f.is_dir() and f.name == "_internal":
                try:
                    shutil.copytree(f, ROOT / "_internal", dirs_exist_ok=True)
                except Exception:
                    pass
        print("[+] Đã đồng bộ cấu trúc vào thư mục gốc thành công!", flush=True)
    else:
        try:
            shutil.copy2(RELEASE_DIR / "_internal" / "app.pyc", ROOT / "_internal" / "app.pyc")
            shutil.copy2(RELEASE_DIR / "release_manifest.json", ROOT / "release_manifest.json")
            shutil.copy2(RELEASE_DIR / "update.json", ROOT / "update.json")
        except Exception:
            pass
        print("[+] Đã cập nhật ruột backend và manifest vào thư mục gốc thành công!", flush=True)

    # Clean temporary build dirs
    clean_temp_dirs()

    print("=" * 60, flush=True)
    print("ĐÓNG GÓI VÀ TRIỂN KHAI TEST HOÀN TẤT THÀNH CÔNG!", flush=True)
    print(f"RELEASE FOLDER: {RELEASE_DIR}", flush=True)
    print(f"RELEASE ZIP:    {RELEASE_ZIP} ({RELEASE_ZIP.stat().st_size:,} bytes)", flush=True)
    print(f"ZIP SHA-256:    {zip_hash}", flush=True)
    print(f"CAMHL SHA-256:  {camhl_hash}", flush=True)
    print(f"UPDATER SHA256: {updater_hash}", flush=True)
    print(f"SETUP_CF SHA256:{setup_cf_hash}", flush=True)
    print("=" * 60, flush=True)


if __name__ == "__main__":
    main()
