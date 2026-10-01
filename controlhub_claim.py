"""One-time ControlHub bootstrap client for Cambida.

The shop-specific update contains only controlhub_bootstrap.json. On first start,
Cambida exchanges that one-time claim for its fixed public URL and its own
Cloudflare Tunnel token. Cloudflare administrator credentials never reach shops.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid


BOOTSTRAP_FILE = "controlhub_bootstrap.json"
MACHINE_ID_FILE = "controlhub_machine_id.txt"
TOKEN_FILE = "tunnel_token.txt"
CONFIG_FILE = "config.json"
VERSION_FILES = ("RELEASE_VERSION.txt", "VERSION.txt")


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

    # Stable per-installation ID; deliberately not hardware fingerprinting.
    value = str(uuid.uuid4())
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


def _already_provisioned(base_dir: Path) -> str:
    token_path = base_dir / TOKEN_FILE
    if not token_path.is_file():
        return ""
    try:
        if not token_path.read_text(encoding="utf-8-sig").strip():
            return ""
    except OSError:
        return ""
    cfg = _read_json(base_dir / CONFIG_FILE)
    url = str(cfg.get("public_base_url") or "").strip().rstrip("/")
    if url.lower().startswith("https://") and "trycloudflare.com" not in url.lower():
        return url
    return ""


def claim_once(base_dir: str | os.PathLike | None = None, timeout: float = 12.0, opener=None) -> dict:
    base = Path(base_dir or Path(__file__).resolve().parent).resolve()
    bootstrap_path = base / BOOTSTRAP_FILE
    if not bootstrap_path.is_file():
        return {"status": "no-bootstrap", "claimed": False}

    existing_url = _already_provisioned(base)
    if existing_url:
        try:
            bootstrap_path.unlink()
        except OSError:
            pass
        return {"status": "already-provisioned", "claimed": False, "public_base_url": existing_url}

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
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        controlhub_url + "/api/claim",
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "Cambida-ControlHub/1"},
        method="POST",
    )

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
        result = json.loads(raw.decode("utf-8"))
    except Exception:
        raise RuntimeError("ControlHub trả dữ liệu không hợp lệ.") from None

    public_url = str(result.get("public_base_url") or "").strip().rstrip("/")
    tunnel_token = str(result.get("tunnel_token") or "").strip()
    parsed_public = urllib.parse.urlparse(public_url)
    if parsed_public.scheme != "https" or not parsed_public.hostname or "trycloudflare.com" in public_url.lower():
        raise RuntimeError("ControlHub trả public_base_url không hợp lệ.")
    if not tunnel_token:
        raise RuntimeError("ControlHub không trả Tunnel Token.")

    # Prepare both durable files before removing the one-time bootstrap.
    cfg_path = base / CONFIG_FILE
    cfg = _read_json(cfg_path)
    cfg["public_base_url"] = public_url
    _atomic_write_text(base / TOKEN_FILE, tunnel_token + "\n")
    _atomic_write_text(cfg_path, json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")

    try:
        bootstrap_path.unlink()
    except OSError:
        pass

    return {
        "status": "claimed",
        "claimed": True,
        "public_base_url": public_url,
        "project_id": project_id,
        "site_id": site_id,
    }
