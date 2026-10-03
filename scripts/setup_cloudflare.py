"""Trình cài đặt và quản lý Cloudflare Tunnel độc lập cho Cambida / Camera Highlight.

Được đóng gói thành setup_cloudflare.exe (onefile).
Hỗ trợ:
- Tự động nhận diện config.json và cloudflared.exe
- Tự động dò tìm cổng máy chủ (8000, 8004, hoặc cổng đang chạy thực tế của camhl.exe)
- Tự động provision Named Tunnel + ingress + DNS từ public_base_url khi chưa có tunnel_token.txt
- Lấy Tunnel Token qua Cloudflare API và tự sinh tunnel_token.txt; API Token quản trị chỉ dùng trong RAM
- Khởi động Cloudflare Named Tunnel bằng token riêng của từng quán
- Tự động cập nhật public_base_url vào config.json
- Tự động kiểm tra độ thông mạng qua /api/ping
- Quản lý tự khởi động cùng Windows (chạy ngầm, không hiện cửa sổ cmd)
"""
import argparse
import ctypes
import getpass
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# Thiết lập encoding UTF-8 cho Windows console
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import psutil
except ImportError:
    psutil = None


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    script_dir = Path(__file__).resolve().parent
    if (script_dir.parent / "cloudflared.exe").is_file():
        return script_dir.parent
    return script_dir


BASE_DIR = get_base_dir()
CONFIG_FILE = BASE_DIR / "config.json"
CLOUDFLARED_EXE = BASE_DIR / "cloudflared.exe"
LOG_DIR = BASE_DIR / "logs"
TOKEN_FILE = BASE_DIR / "tunnel_token.txt"
CF_API_BASE = "https://api.cloudflare.com/client/v4"


def set_console_title(title: str):
    if sys.platform == "win32":
        try:
            ctypes.windll.kernel32.SetConsoleTitleW(title)
        except Exception:
            pass


def hide_console():
    if sys.platform == "win32":
        try:
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
        except Exception:
            pass


def read_config() -> dict:
    if not CONFIG_FILE.is_file():
        # Thử tìm config.release.json trong _internal
        seed_cfg = BASE_DIR / "_internal" / "config.release.json"
        if seed_cfg.is_file():
            try:
                with open(seed_cfg, "r", encoding="utf-8-sig") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception as exc:
        print(f"[!] Lỗi đọc config.json: {exc}", flush=True)
        return {}


def write_config(cfg: dict) -> bool:
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")
        return True
    except Exception as exc:
        print(f"[!] Lỗi ghi config.json: {exc}", flush=True)
        return False


def read_tunnel_token(cfg: dict = None) -> str:
    """Đọc token Named Tunnel; ưu tiên cấu hình cũ rồi tới file token riêng."""
    cfg = cfg or {}
    tunnel_cfg = cfg.get("cloudflare_tunnel", {})
    if isinstance(tunnel_cfg, dict):
        token = str(tunnel_cfg.get("token") or "").strip()
        if token:
            return token
    try:
        if TOKEN_FILE.is_file():
            return TOKEN_FILE.read_text(encoding="utf-8-sig").strip()
    except Exception:
        pass
    return ""


def write_tunnel_token(token: str) -> bool:
    token = (token or "").strip()
    if not token:
        return False
    try:
        TOKEN_FILE.write_text(token + "\n", encoding="utf-8")
        return True
    except Exception as exc:
        print(f"[!] Không thể lưu tunnel_token.txt: {exc}", flush=True)
        return False


def get_fixed_public_url(cfg: dict) -> str:
    url = str(cfg.get("public_base_url") or "").strip().rstrip("/")
    if url and not re.match(r"^https://", url, re.IGNORECASE):
        url = "https://" + url.lstrip("/")
    if "trycloudflare.com" in url.lower():
        return ""
    return url



def get_public_hostname(cfg: dict) -> str:
    public_url = get_fixed_public_url(cfg)
    if not public_url:
        return ""
    try:
        return (urllib.parse.urlparse(public_url).hostname or "").strip().lower()
    except Exception:
        return ""


def _cloudflare_api_request(method: str, path: str, api_token: str, payload=None, query=None, timeout: float = 15.0):
    """Gọi Cloudflare API mà không ghi API token quản trị xuống đĩa."""
    api_token = (api_token or "").strip()
    if not api_token:
        raise RuntimeError("Cloudflare API Token đang trống.")

    url = CF_API_BASE + path
    if query:
        url += "?" + urllib.parse.urlencode(query)

    data = None
    headers = {
        "Authorization": f"Bearer {api_token}",
        "Accept": "application/json",
        "User-Agent": "CambidaCloudflareProvisioner/1.0",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read().decode("utf-8", "replace")
            body = json.loads(raw)
            errors = body.get("errors") or []
            message = "; ".join(str(e.get("message") or e) for e in errors) or raw[:300]
        except Exception:
            message = str(exc.reason or exc)
        raise RuntimeError(f"Cloudflare API HTTP {exc.code}: {message}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Không kết nối được Cloudflare API: {exc.reason}") from None

    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError("Cloudflare API trả dữ liệu không hợp lệ.") from None

    if not body.get("success", False):
        errors = body.get("errors") or []
        message = "; ".join(str(e.get("message") or e) for e in errors) or "Cloudflare API báo thất bại."
        raise RuntimeError(message)
    return body.get("result")


def discover_cloudflare_zone(api_token: str, hostname: str) -> dict:
    """Tự dò zone và account từ hostname. API token cần Zone Read."""
    hostname = (hostname or "").strip().lower().rstrip(".")
    if not hostname:
        raise RuntimeError("Hostname Cloudflare đang trống.")

    zones = _cloudflare_api_request(
        "GET",
        "/zones",
        api_token,
        query={"per_page": 50},
    ) or []
    matches = []
    for zone in zones:
        zone_name = str(zone.get("name") or "").strip().lower().rstrip(".")
        if zone_name and (hostname == zone_name or hostname.endswith("." + zone_name)):
            matches.append(zone)
    if not matches:
        raise RuntimeError(
            f"Không tìm thấy Cloudflare Zone phù hợp với {hostname}. "
            "API Token cần quyền Zone Read cho zone này."
        )
    return max(matches, key=lambda z: len(str(z.get("name") or "")))


def _tunnel_name_for_hostname(hostname: str) -> str:
    clean = re.sub(r"[^a-z0-9-]+", "-", (hostname or "").lower()).strip("-")
    return ("cambida-" + clean)[:100]


def get_or_create_cloudflare_tunnel(api_token: str, account_id: str, hostname: str) -> dict:
    tunnel_name = _tunnel_name_for_hostname(hostname)
    existing = _cloudflare_api_request(
        "GET",
        f"/accounts/{account_id}/cfd_tunnel",
        api_token,
        query={"name": tunnel_name, "is_deleted": "false", "per_page": 10},
    ) or []
    for tunnel in existing:
        if str(tunnel.get("name") or "") == tunnel_name and not tunnel.get("deleted_at"):
            return tunnel

    return _cloudflare_api_request(
        "POST",
        f"/accounts/{account_id}/cfd_tunnel",
        api_token,
        payload={"name": tunnel_name, "config_src": "cloudflare"},
    )


def configure_cloudflare_tunnel(api_token: str, account_id: str, tunnel_id: str, hostname: str, port: int):
    payload = {
        "config": {
            "ingress": [
                {
                    "hostname": hostname,
                    "service": f"http://127.0.0.1:{int(port)}",
                    "originRequest": {},
                },
                {"service": "http_status:404"},
            ]
        }
    }
    return _cloudflare_api_request(
        "PUT",
        f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations",
        api_token,
        payload=payload,
    )


def ensure_cloudflare_dns(api_token: str, zone_id: str, hostname: str, tunnel_id: str):
    target = f"{tunnel_id}.cfargotunnel.com"
    records = _cloudflare_api_request(
        "GET",
        f"/zones/{zone_id}/dns_records",
        api_token,
        query={"name": hostname, "per_page": 100},
    ) or []

    desired = {
        "type": "CNAME",
        "name": hostname,
        "content": target,
        "ttl": 1,
        "proxied": True,
    }

    if records:
        cname = next((r for r in records if str(r.get("type") or "").upper() == "CNAME"), None)
        if cname is None:
            kinds = ", ".join(sorted({str(r.get("type") or "?") for r in records}))
            raise RuntimeError(
                f"DNS {hostname} đang có record loại {kinds}; không tự xóa record hiện có."
            )
        if (
            str(cname.get("content") or "").rstrip(".").lower() == target.lower()
            and bool(cname.get("proxied", False))
        ):
            return cname
        return _cloudflare_api_request(
            "PATCH",
            f"/zones/{zone_id}/dns_records/{cname['id']}",
            api_token,
            payload=desired,
        )

    return _cloudflare_api_request(
        "POST",
        f"/zones/{zone_id}/dns_records",
        api_token,
        payload=desired,
    )


def provision_cloudflare_named_tunnel(api_token: str, cfg: dict, port: int) -> dict:
    """
    Tự tạo/reuse Named Tunnel, cấu hình ingress, DNS và lấy Tunnel Token.
    API Token quản trị chỉ tồn tại trong RAM trong lần provisioning này.
    """
    hostname = get_public_hostname(cfg)
    if not hostname:
        raise RuntimeError("Hãy điền public_base_url bằng hostname HTTPS cố định trong config.json.")

    zone = discover_cloudflare_zone(api_token, hostname)
    zone_id = str(zone.get("id") or "").strip()
    account_id = str((zone.get("account") or {}).get("id") or "").strip()
    if not zone_id or not account_id:
        raise RuntimeError("Cloudflare Zone không trả về zone_id/account_id hợp lệ.")

    tunnel = get_or_create_cloudflare_tunnel(api_token, account_id, hostname)
    tunnel_id = str((tunnel or {}).get("id") or "").strip()
    if not tunnel_id:
        raise RuntimeError("Cloudflare không trả về Tunnel ID.")

    configure_cloudflare_tunnel(api_token, account_id, tunnel_id, hostname, port)
    ensure_cloudflare_dns(api_token, zone_id, hostname, tunnel_id)

    tunnel_token = _cloudflare_api_request(
        "GET",
        f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/token",
        api_token,
    )
    tunnel_token = str(tunnel_token or "").strip()
    if not tunnel_token:
        raise RuntimeError("Cloudflare không trả về Tunnel Token.")

    return {
        "hostname": hostname,
        "zone_id": zone_id,
        "account_id": account_id,
        "tunnel_id": tunnel_id,
        "tunnel_name": str((tunnel or {}).get("name") or _tunnel_name_for_hostname(hostname)),
        "tunnel_token": tunnel_token,
    }

def ping_port(port: int, timeout: float = 1.2) -> bool:
    """Kiểm tra xem cổng có đang chạy máy chủ Cambida (phản hồi /api/ping) không."""
    try:
        url = f"http://127.0.0.1:{port}/api/ping"
        req = urllib.request.Request(url, headers={"User-Agent": "SetupCloudflare"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                raw = resp.read().decode("utf-8", "ignore")
                return "ok" in raw.lower()
    except Exception:
        pass
    return False


def detect_server_port(cfg: dict) -> int:
    """Tự động phát hiện cổng máy chủ đang chạy hoặc từ cấu hình."""
    cfg_port = cfg.get("server_port")
    try:
        if cfg_port:
            cfg_port = int(cfg_port)
    except (ValueError, TypeError):
        cfg_port = None

    # 1. Thử ping cổng trong config
    if cfg_port and ping_port(cfg_port):
        return cfg_port

    # 2. Quét cổng TCP đang lắng nghe của camhl.exe qua psutil
    if psutil:
        try:
            for p in psutil.process_iter(["name", "pid"]):
                name = (p.info["name"] or "").lower()
                if "camhl" in name or "cambida" in name:
                    try:
                        for conn in p.net_connections(kind="tcp"):
                            if conn.status == psutil.CONN_LISTEN and conn.laddr:
                                port = conn.laddr.port
                                if ping_port(port):
                                    return port
                    except Exception:
                        pass
        except Exception:
            pass

    # 3. Thử các cổng ứng viên thông dụng
    candidates = [8000, 8004, 8080, 8888]
    if cfg_port and cfg_port not in candidates:
        candidates.insert(0, cfg_port)
    for c_port in candidates:
        if ping_port(c_port):
            return c_port

    # 4. Fallback về cổng cấu hình hoặc 8000
    return cfg_port or 8000


def stop_cloudflared_processes():
    """Dừng triệt để tất cả tiến trình cloudflared cũ."""
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", "cloudflared.exe"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3,
            )
        except Exception:
            pass
    if psutil:
        try:
            for p in psutil.process_iter(["name"]):
                if "cloudflared" in (p.info["name"] or "").lower():
                    try:
                        p.kill()
                    except Exception:
                        pass
        except Exception:
            pass
    time.sleep(1)


def is_cloudflared_running() -> bool:
    if psutil:
        try:
            for p in psutil.process_iter(["name"]):
                if "cloudflared" in (p.info["name"] or "").lower():
                    return True
        except Exception:
            pass
    return False


def get_startup_shortcut_path() -> Path:
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Cambida_Cloudflared_Tunnel.lnk"
    return Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Cambida_Cloudflared_Tunnel.lnk"


def check_autostart_status() -> bool:
    shortcut = get_startup_shortcut_path()
    return shortcut.is_file()


def toggle_autostart(enable: bool, port: int) -> bool:
    shortcut_path = get_startup_shortcut_path()
    if not enable:
        try:
            if shortcut_path.is_file():
                shortcut_path.unlink()
            return True
        except Exception as exc:
            print(f"[!] Không thể xóa shortcut tự khởi động: {exc}", flush=True)
            return False

    # Tạo shortcut qua PowerShell WScript.Shell
    try:
        exe_path = sys.executable if getattr(sys, "frozen", False) else CLOUDFLARED_EXE
        if getattr(sys, "frozen", False):
            target = str(Path(sys.executable).resolve())
            args = f"--daemon --port {port}"
            work_dir = str(BASE_DIR)
        else:
            target = sys.executable
            args = f'"{str(Path(__file__).resolve())}" --daemon --port {port}'
            work_dir = str(BASE_DIR)

        ps_script = f"""
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut('{str(shortcut_path)}')
$Shortcut.TargetPath = '{target}'
$Shortcut.Arguments = '{args}'
$Shortcut.WorkingDirectory = '{work_dir}'
$Shortcut.Description = 'Tự động chạy Cloudflare Tunnel cho Cambida'
$Shortcut.WindowStyle = 7
$Shortcut.Save()
"""
        res = subprocess.run(["powershell", "-NoProfile", "-Command", ps_script], capture_output=True, text=True)
        return res.returncode == 0
    except Exception as exc:
        print(f"[!] Lỗi khi đăng ký khởi động cùng Windows: {exc}", flush=True)
        return False


def run_named_tunnel(token: str, wait_timeout: int = 5) -> bool:
    """Khởi động Named Tunnel bằng token riêng của quán; không tạo Quick Tunnel."""
    token = (token or "").strip()
    if not token:
        print("[!] Chưa có tunnel_token.txt cho quán này.", flush=True)
        return False
    if not CLOUDFLARED_EXE.is_file():
        print(f"[!] Không tìm thấy file {CLOUDFLARED_EXE.name} trong thư mục:", flush=True)
        print(f"    {BASE_DIR}", flush=True)
        return False

    stop_cloudflared_processes()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / "cloudflared.log"

    flags = 0
    if sys.platform == "win32":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    cmd = [str(CLOUDFLARED_EXE), "tunnel", "run", "--token", token]
    try:
        with open(log_file, "a", encoding="utf-8") as lf:
            proc = subprocess.Popen(
                cmd,
                stdout=lf,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=flags,
            )
        deadline = time.time() + max(1, wait_timeout)
        while time.time() < deadline:
            if proc.poll() is not None:
                print(f"[!] Named Tunnel dừng sớm. Xem nhật ký: {log_file}", flush=True)
                return False
            time.sleep(0.25)
        return proc.poll() is None
    except Exception as exc:
        print(f"[!] Không thể khởi động Named Tunnel: {exc}", flush=True)
        return False


def test_public_url(url: str, timeout: float = 3.5) -> bool:
    """Kiểm tra đường truyền internet qua URL công khai."""
    try:
        ping_url = f"{url.rstrip('/')}/api/ping"
        req = urllib.request.Request(ping_url, headers={"User-Agent": "SetupCloudflareTest"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def action_setup(port_override: int = None, non_interactive: bool = False):
    cfg = read_config()
    detected_port = port_override or detect_server_port(cfg)

    print("\n" + "=" * 60, flush=True)
    print("  THIẾT LẬP CLOUDFLARE NAMED TUNNEL - CAMBIDA CCTV", flush=True)
    print("=" * 60, flush=True)

    is_running = ping_port(detected_port)
    if is_running:
        print(f"[+] Web Server Cambida đang HOẠT ĐỘNG tại cổng: {detected_port}", flush=True)
    else:
        print(f"[*] Chưa phát hiện Web Server phản hồi tại cổng: {detected_port}", flush=True)
        print("    (Tunnel vẫn có thể cài trước; Cambida có thể bật sau.)", flush=True)

    target_port = detected_port
    if not non_interactive:
        prompt_val = input(f"\nCổng Cambida [Mặc định: {detected_port}]: ").strip()
        if prompt_val:
            try:
                target_port = int(prompt_val)
            except ValueError:
                print(f"[!] Cổng không hợp lệ, giữ mặc định {detected_port}", flush=True)
                target_port = detected_port

    if cfg.get("server_port") != target_port:
        cfg["server_port"] = target_port

    public_url = get_fixed_public_url(cfg)
    if not public_url:
        if non_interactive:
            print("[!] Hãy điền public_base_url bằng hostname HTTPS cố định trong config.json.", flush=True)
            return False
        domain = input("\nTên miền cố định (ví dụ https://cam1.example.com): ").strip()
        if domain:
            tmp_cfg = dict(cfg)
            tmp_cfg["public_base_url"] = domain
            public_url = get_fixed_public_url(tmp_cfg)
        if not public_url:
            print("[!] Tên miền không hợp lệ hoặc vẫn là trycloudflare.com.", flush=True)
            return False

    if cfg.get("public_base_url") != public_url:
        cfg["public_base_url"] = public_url
        if not write_config(cfg):
            print("[!] Không thể lưu public_base_url vào config.json.", flush=True)
            return False

    token = read_tunnel_token(cfg)
    if not token:
        api_token = (os.environ.get("CLOUDFLARE_API_TOKEN") or "").strip()
        if not api_token and non_interactive:
            print(
                "[!] Chưa có tunnel_token.txt. Đặt CLOUDFLARE_API_TOKEN cho lần provisioning đầu tiên.",
                flush=True,
            )
            return False
        if not api_token:
            print(
                "\nLần đầu Cambida sẽ tự tạo/reuse Tunnel + DNS + Tunnel Token.",
                flush=True,
            )
            print(
                "API Token cần: Account/Cloudflare Tunnel Edit + Zone/DNS Edit + Zone/Zone Read.",
                flush=True,
            )
            api_token = getpass.getpass(
                "Dán Cloudflare API Token quản trị (chỉ dùng trong RAM, không lưu): "
            ).strip()
        if not api_token:
            print("[!] Cloudflare API Token đang trống.", flush=True)
            return False
        try:
            provisioned = provision_cloudflare_named_tunnel(api_token, cfg, target_port)
        except RuntimeError as exc:
            print(f"[!] Provisioning Cloudflare thất bại: {exc}", flush=True)
            return False
        finally:
            api_token = ""

        token = provisioned["tunnel_token"]
        if not write_tunnel_token(token):
            print("[!] Không thể sinh tunnel_token.txt.", flush=True)
            return False
        print(
            f"[+] Đã provision Tunnel {provisioned['tunnel_name']} cho {provisioned['hostname']}.",
            flush=True,
        )
        print("[+] Đã tự sinh tunnel_token.txt cho quán này.", flush=True)

    if not run_named_tunnel(token):
        print("[!] Quá trình khởi động Named Tunnel thất bại.", flush=True)
        return False

    print("\n" + "=" * 60, flush=True)
    print("  [THÀNH CÔNG] CLOUDFLARE NAMED TUNNEL ĐÃ SẴN SÀNG!", flush=True)
    print(f"  Link cố định: {public_url}", flush=True)
    print("=" * 60, flush=True)

    if is_running:
        print("[*] Đang xác thực đường truyền từ Internet...", end="", flush=True)
        time.sleep(1.5)
        if test_public_url(public_url):
            print(" [OK - Đã thông mạng]", flush=True)
        else:
            print(" [Chưa phản hồi - kiểm tra Public Hostname/Service trên Cloudflare]", flush=True)

    if not non_interactive:
        autostart_current = check_autostart_status()
        status_str = "ĐANG BẬT" if autostart_current else "ĐANG TẮT"
        ans = input(f"\nBạn có muốn Tunnel tự chạy ngầm cùng Windows không? (Y/N) [Hiện tại: {status_str}, Mặc định: Y]: ").strip().lower()
        if ans in ("", "y", "yes"):
            if toggle_autostart(True, target_port):
                print("[+] Đã tạo shortcut khởi động cùng Windows thành công!", flush=True)
            else:
                print("[!] Chưa thể đăng ký tự khởi động.", flush=True)
        elif ans in ("n", "no"):
            toggle_autostart(False, target_port)
            print("[+] Đã tắt tự khởi động cùng Windows.", flush=True)

    print("\nCambida sẽ dùng public_base_url cố định; không còn tự tạo tên miền trycloudflare.com.")
    return True


def action_status():
    print("\n" + "=" * 60, flush=True)
    print("  TRẠNG THÁI CLOUDFLARE TUNNEL", flush=True)
    print("=" * 60, flush=True)
    running = is_cloudflared_running()
    print(f"- Tiến trình cloudflared.exe: {'🟢 ĐANG CHẠY' if running else '🔴 CHƯA CHẠY'}", flush=True)

    cfg = read_config()
    port = cfg.get("server_port", 8000)
    server_running = ping_port(port)
    print(f"- Web Server nội bộ (port {port}): {'🟢 HOẠT ĐỘNG' if server_running else '🔴 CHƯA PHẢN HỒI'}", flush=True)

    token_present = bool(read_tunnel_token(cfg))
    print(f"- Named Tunnel Token: {'🟢 ĐÃ CÓ' if token_present else '🔴 CHƯA CÓ'}", flush=True)

    pub_url = get_fixed_public_url(cfg)
    if pub_url:
        print(f"- URL công khai: {pub_url}", flush=True)
        if running and server_running:
            online = test_public_url(pub_url)
            print(f"- Tình trạng truy cập từ xa: {'🟢 ONLINE (Thông mạng)' if online else '🟡 ĐANG KẾT NỐI'}", flush=True)
        else:
            print("- Tình trạng truy cập từ xa: 🟡 Đang chờ máy chủ hoặc tunnel", flush=True)
    else:
        print("- URL công khai: (Chưa cấu hình)", flush=True)

    autostart = check_autostart_status()
    print(f"- Tự chạy cùng Windows: {'🟢 BẬT' if autostart else '⚪ TẮT'}", flush=True)
    print("=" * 60 + "\n", flush=True)


def action_stop():
    print("\n[*] Đang dừng tất cả tiến trình cloudflared...", flush=True)
    stop_cloudflared_processes()
    print("[+] Đã dừng toàn bộ Cloudflare Tunnel.", flush=True)


def daemon_mode(port: int):
    """Chạy ngầm Named Tunnel và tự khởi động lại nếu connector dừng."""
    hide_console()
    while True:
        try:
            cfg = read_config()
            token = read_tunnel_token(cfg)
            if not token:
                time.sleep(10)
                continue
            if not get_fixed_public_url(cfg):
                time.sleep(10)
                continue
            run_named_tunnel(token, wait_timeout=2)
            while is_cloudflared_running():
                time.sleep(10)
        except Exception:
            pass
        time.sleep(5)


def main():
    set_console_title("Cài đặt Cloudflare Tunnel - Cambida CCTV")

    parser = argparse.ArgumentParser(description="Trình cài đặt và quản lý Cloudflare Tunnel cho Cambida CCTV")
    parser.add_argument("--setup", action="store_true", help="Chạy cài đặt tự động không cần hỏi")
    parser.add_argument("--status", action="store_true", help="Kiểm tra trạng thái tunnel")
    parser.add_argument("--stop", action="store_true", help="Dừng tiến trình tunnel")
    parser.add_argument("--port", type=int, default=None, help="Chỉ định cổng máy chủ")
    parser.add_argument("--daemon", action="store_true", help="Chế độ chạy ngầm tự phục hồi")
    args = parser.parse_args()

    if args.daemon:
        daemon_mode(args.port or 8000)
        return

    if args.status:
        action_status()
        return

    if args.stop:
        action_stop()
        return

    if args.setup:
        action_setup(port_override=args.port, non_interactive=True)
        return

    # Giao diện menu tương tác nếu chạy trực tiếp
    while True:
        print("\n" + "=" * 60, flush=True)
        print("   QUẢN LÝ CLOUDFLARE TUNNEL - CAMBIDA CCTV", flush=True)
        print("=" * 60, flush=True)
        print("  [1] Cài đặt / Kết nối Cloudflare Named Tunnel", flush=True)
        print("  [2] Kiểm tra trạng thái kết nối Cloudflare", flush=True)
        print("  [3] Dừng Cloudflare Tunnel", flush=True)
        print("  [4] Bật / Tắt tự khởi động cùng Windows", flush=True)
        print("  [5] Thoát", flush=True)
        print("=" * 60, flush=True)

        choice = input("Vui lòng chọn thao tác (1-5) [Mặc định: 1]: ").strip()
        if choice in ("", "1"):
            action_setup(port_override=args.port)
            input("\nNhấn Enter để quay lại menu...")
        elif choice == "2":
            action_status()
            input("Nhấn Enter để quay lại menu...")
        elif choice == "3":
            action_stop()
            input("Nhấn Enter để quay lại menu...")
        elif choice == "4":
            cfg = read_config()
            port = args.port or detect_server_port(cfg)
            current = check_autostart_status()
            target = not current
            toggle_autostart(target, port)
            print(f"[+] Đã {'BẬT' if target else 'TẮT'} tự khởi động cùng Windows.", flush=True)
            input("Nhấn Enter để quay lại menu...")
        elif choice in ("5", "q", "exit"):
            print("Đang thoát...")
            break
        else:
            print("[!] Lựa chọn không hợp lệ, vui lòng thử lại.", flush=True)


if __name__ == "__main__":
    main()
