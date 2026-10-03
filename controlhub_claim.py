"""Cloudflare provisioning client for Cambida.

Cambida 2.3+ normally needs only a Cloudflare subdomain entered in config.json.
On startup it asks ControlHub for the Tunnel Token assigned to that registered
subdomain, binding the site to the first installation machine. Legacy one-time
bootstrap packages remain supported for upgrades from older releases.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import socket
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid


BOOTSTRAP_FILE = "controlhub_bootstrap.json"
MACHINE_ID_FILE = "controlhub_machine_id.txt"
CLIENT_SECRET_FILE = "controlhub_client_secret.txt"
TOKEN_FILE = "tunnel_token.txt"
CONFIG_FILE = "config.json"
VERSION_FILES = ("RELEASE_VERSION.txt", "VERSION.txt")
DEFAULT_CONTROLHUB_URL = "https://server.hhan24.org"


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except OSError:
            pass


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}


def _get_machine_id(base_dir: Path) -> str:
    path = base_dir / MACHINE_ID_FILE
    try:
        value = path.read_text(encoding="utf-8-sig").strip()
        if value:
            return value
    except OSError:
        pass
    value = str(uuid.uuid4())
    _atomic_write_text(path, value + "\n")
    return value


def _get_client_secret(base_dir: Path) -> str:
    path = base_dir / CLIENT_SECRET_FILE
    try:
        value = path.read_text(encoding="utf-8-sig").strip()
        if value:
            return value
    except OSError:
        pass
    value = secrets.token_urlsafe(32)
    _atomic_write_text(path, value + "\n")
    return value


def _get_app_version(base_dir: Path) -> str:
    for name in VERSION_FILES:
        try:
            value = (base_dir / name).read_text(encoding="utf-8-sig").strip()
            if value:
                return value
        except OSError:
            pass
    return ""


def _validate_controlhub_url(value: str) -> str:
    raw = str(value or "").strip().rstrip("/")
    parsed = urllib.parse.urlparse(raw)
    if parsed.scheme == "https" and parsed.hostname:
        return raw
    if parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
        return raw
    raise ValueError("controlhub_url phải dùng HTTPS (HTTP chỉ được phép cho localhost).")


def normalize_subdomain(value: str) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if "://" in raw:
        parsed = urllib.parse.urlparse(raw)
        raw = parsed.hostname or ""
    else:
        raw = raw.split("/", 1)[0].split(":", 1)[0]
    raw = raw.rstrip(".")
    if len(raw) > 253 or "." not in raw:
        raise ValueError("Subdomain Cloudflare không hợp lệ.")
    labels = raw.split(".")
    for label in labels:
        if not label or len(label) > 63 or label[0] == "-" or label[-1] == "-":
            raise ValueError("Subdomain Cloudflare không hợp lệ.")
        if not all(ch.isascii() and (ch.isalnum() or ch == "-") for ch in label):
            raise ValueError("Subdomain Cloudflare không hợp lệ.")
    return raw


def _response_json(request, timeout: float, opener):
    open_fn = opener or urllib.request.urlopen
    try:
        response = open_fn(request, timeout=timeout)
        if hasattr(response, "__enter__"):
            with response as resp:
                raw = resp.read()
        else:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8", "replace")).get("error")
        except Exception:
            detail = None
        raise RuntimeError(detail or f"ControlHub HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Không kết nối được ControlHub: {exc.reason}") from None
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        raise RuntimeError("ControlHub trả dữ liệu không hợp lệ.") from None


def _claim_by_subdomain(base: Path, cfg: dict, timeout: float, opener) -> dict:
    subdomain = normalize_subdomain(cfg.get("cloudflare_subdomain"))
    if not subdomain:
        return {"status": "no-subdomain", "claimed": False}

    bound = normalize_subdomain(cfg.get("cloudflare_bound_subdomain")) if cfg.get("cloudflare_bound_subdomain") else ""
    try:
        server_port = int(cfg.get("server_port") or 8000)
    except (TypeError, ValueError):
        server_port = 8000
    if server_port < 1 or server_port > 65535:
        raise ValueError("server_port không hợp lệ.")
    try:
        bound_port = int(cfg.get("cloudflare_bound_port") or 0)
    except (TypeError, ValueError):
        bound_port = 0
    token_path = base / TOKEN_FILE
    if bound == subdomain and bound_port == server_port and token_path.is_file():
        try:
            if token_path.read_text(encoding="utf-8-sig").strip():
                public_url = "https://" + subdomain
                if cfg.get("public_base_url") != public_url:
                    cfg["public_base_url"] = public_url
                    _atomic_write_text(base / CONFIG_FILE, json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
                return {"status": "already-provisioned", "claimed": False, "public_base_url": public_url}
        except OSError:
            pass

    controlhub_url = _validate_controlhub_url(cfg.get("controlhub_url") or DEFAULT_CONTROLHUB_URL)
    payload = {
        "subdomain": subdomain,
        "machine_id": _get_machine_id(base),
        "app_version": _get_app_version(base),
        "hostname": socket.gethostname(),
        "server_port": server_port,
    }
    request = urllib.request.Request(
        controlhub_url + "/api/cambida/claim-by-subdomain",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "Cambida-ControlHub/2"},
        method="POST",
    )
    result = _response_json(request, timeout, opener)
    public_url = str(result.get("public_base_url") or "").strip().rstrip("/")
    tunnel_token = str(result.get("tunnel_token") or "").strip()
    parsed_public = urllib.parse.urlparse(public_url)
    if parsed_public.scheme != "https" or (parsed_public.hostname or "").lower() != subdomain:
        raise RuntimeError("ControlHub trả public_base_url không khớp subdomain đã cấu hình.")
    if not tunnel_token:
        raise RuntimeError("ControlHub không trả Tunnel Token.")

    cfg["cloudflare_subdomain"] = subdomain
    cfg["cloudflare_bound_subdomain"] = subdomain
    cfg["cloudflare_bound_port"] = server_port
    cfg["controlhub_url"] = controlhub_url
    cfg["public_base_url"] = public_url
    _atomic_write_text(token_path, tunnel_token + "\n")
    _atomic_write_text(base / CONFIG_FILE, json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    try:
        (base / BOOTSTRAP_FILE).unlink()
    except OSError:
        pass
    return {"status": "claimed", "claimed": True, "public_base_url": public_url, "subdomain": subdomain}


def _register_client(base: Path, cfg: dict, timeout: float, opener) -> dict:
    """Register this installation and wait for ControlHub admin assignment."""
    controlhub_url = _validate_controlhub_url(cfg.get("controlhub_url") or DEFAULT_CONTROLHUB_URL)
    try:
        server_port = int(cfg.get("server_port") or 8000)
    except (TypeError, ValueError):
        server_port = 8000
    if server_port < 1 or server_port > 65535:
        raise ValueError("server_port không hợp lệ.")

    payload = {
        "machine_id": _get_machine_id(base),
        "client_secret": _get_client_secret(base),
        "app_version": _get_app_version(base),
        "hostname": socket.gethostname(),
        "server_port": server_port,
    }
    request = urllib.request.Request(
        controlhub_url + "/api/cambida/register-client",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "Cambida-ControlHub/3"},
        method="POST",
    )
    result = _response_json(request, timeout, opener)
    status = str(result.get("status") or "").strip().lower()
    pairing_code = str(result.get("pairing_code") or "").strip()

    if status != "assigned":
        return {
            "status": "waiting-assignment",
            "claimed": False,
            "pairing_code": pairing_code,
            "machine_id": payload["machine_id"],
        }

    subdomain = normalize_subdomain(result.get("subdomain"))
    public_url = str(result.get("public_base_url") or "").strip().rstrip("/")
    tunnel_token = str(result.get("tunnel_token") or "").strip()
    parsed_public = urllib.parse.urlparse(public_url)
    if not subdomain:
        raise RuntimeError("ControlHub không trả subdomain đã gán.")
    if parsed_public.scheme != "https" or (parsed_public.hostname or "").lower() != subdomain:
        raise RuntimeError("ControlHub trả public_base_url không khớp subdomain đã gán.")
    if not tunnel_token:
        raise RuntimeError("ControlHub không trả Tunnel Token.")

    cfg["cloudflare_subdomain"] = subdomain
    cfg["cloudflare_bound_subdomain"] = subdomain
    cfg["cloudflare_bound_port"] = server_port
    cfg["controlhub_url"] = controlhub_url
    cfg["public_base_url"] = public_url
    _atomic_write_text(base / TOKEN_FILE, tunnel_token + "\n")
    _atomic_write_text(base / CONFIG_FILE, json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    return {
        "status": "claimed",
        "claimed": True,
        "public_base_url": public_url,
        "subdomain": subdomain,
        "pairing_code": pairing_code,
    }


def _claim_legacy_bootstrap(base: Path, timeout: float, opener) -> dict:
    bootstrap_path = base / BOOTSTRAP_FILE
    if not bootstrap_path.is_file():
        return {"status": "no-bootstrap", "claimed": False}

    bootstrap = _read_json(bootstrap_path)
    controlhub_url = _validate_controlhub_url(bootstrap.get("controlhub_url"))
    project_id = str(bootstrap.get("project_id") or "").strip()
    site_id = str(bootstrap.get("site_id") or "").strip()
    claim_token = str(bootstrap.get("claim_token") or "").strip()
    if not project_id or not site_id or not claim_token:
        raise ValueError("controlhub_bootstrap.json thiếu project_id/site_id/claim_token.")

    payload = {
        "project_id": project_id,
        "site_id": site_id,
        "claim_token": claim_token,
        "machine_id": _get_machine_id(base),
        "app_version": _get_app_version(base),
        "hostname": socket.gethostname(),
    }
    request = urllib.request.Request(
        controlhub_url + "/api/claim",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "Cambida-ControlHub/1"},
        method="POST",
    )
    result = _response_json(request, timeout, opener)
    public_url = str(result.get("public_base_url") or "").strip().rstrip("/")
    tunnel_token = str(result.get("tunnel_token") or "").strip()
    parsed_public = urllib.parse.urlparse(public_url)
    if parsed_public.scheme != "https" or not parsed_public.hostname or "trycloudflare.com" in public_url.lower():
        raise RuntimeError("ControlHub trả public_base_url không hợp lệ.")
    if not tunnel_token:
        raise RuntimeError("ControlHub không trả Tunnel Token.")

    cfg = _read_json(base / CONFIG_FILE)
    subdomain = normalize_subdomain(parsed_public.hostname)
    cfg["public_base_url"] = public_url
    cfg["cloudflare_subdomain"] = subdomain
    cfg["cloudflare_bound_subdomain"] = subdomain
    cfg["controlhub_url"] = controlhub_url
    _atomic_write_text(base / TOKEN_FILE, tunnel_token + "\n")
    _atomic_write_text(base / CONFIG_FILE, json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    try:
        bootstrap_path.unlink()
    except OSError:
        pass
    return {"status": "claimed", "claimed": True, "public_base_url": public_url, "subdomain": subdomain}


def claim_once(base_dir: str | os.PathLike | None = None, timeout: float = 12.0, opener=None) -> dict:
    base = Path(base_dir or Path(__file__).resolve().parent).resolve()
    cfg = _read_json(base / CONFIG_FILE)
    subdomain_result = _claim_by_subdomain(base, cfg, timeout, opener)
    if subdomain_result.get("status") != "no-subdomain":
        return subdomain_result
    if (base / BOOTSTRAP_FILE).is_file():
        return _claim_legacy_bootstrap(base, timeout, opener)
    return _register_client(base, cfg, timeout, opener)
