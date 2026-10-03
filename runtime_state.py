"""Durable small runtime-state vault for Cambida.

Release ZIPs intentionally never contain shop/runtime state.  This module keeps
an additional copy of the small, identity-critical files outside the
application directory so an in-place manual copy or failed update cannot turn a
configured shop into a fresh install.

Large operational data (database, video, logs, caches) remains in-place and is
protected by the release/updater allowlists; it is not duplicated here.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import time

CONFIG_FILE = "config.json"
TOKEN_FILE = "tunnel_token.txt"
MACHINE_ID_FILE = "controlhub_machine_id.txt"
CLIENT_SECRET_FILE = "controlhub_client_secret.txt"
BOOTSTRAP_FILE = "controlhub_bootstrap.json"
MANIFEST_FILE = "state.json"
SCHEMA = 1


def vault_dir(environ=None) -> Path:
    env = environ if environ is not None else os.environ
    local = str(env.get("LOCALAPPDATA") or "").strip()
    if local:
        return Path(local) / "Cambida" / "runtime-state"
    return Path.home() / "AppData" / "Local" / "Cambida" / "runtime-state"


def _resolve_vault(base: Path, vault_path=None) -> Path:
    if vault_path:
        return Path(vault_path).resolve()
    override = str(os.environ.get("CAMBIDA_RUNTIME_STATE_DIR") or "").strip()
    if override:
        return Path(override).resolve()
    try:
        temp_root = Path(tempfile.gettempdir()).resolve()
        if base.resolve().is_relative_to(temp_root):
            return base.resolve() / ".runtime-state-test-vault"
    except (OSError, ValueError):
        pass
    return vault_dir()


def _read_json(path: Path):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=destination.name + ".", suffix=".tmp", dir=str(destination.parent)
    )
    os.close(fd)
    temp = Path(temp_name)
    try:
        shutil.copy2(source, temp)
        os.replace(temp, destination)
    finally:
        try:
            temp.unlink()
        except OSError:
            pass


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        except OSError:
            pass


def _valid_port(value):
    try:
        port = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return port if 1 <= port <= 65535 else None


def _subdomain(config: dict | None) -> str:
    if not isinstance(config, dict):
        return ""
    return str(config.get("cloudflare_subdomain") or "").strip().lower().rstrip(".")


def snapshot_runtime_state(
    base_dir,
    *,
    vault_path: str | os.PathLike | None = None,
) -> dict:
    """Snapshot small critical runtime files without ever touching large data.

    Missing config/identity files never erase a previously good copy.  A token
    is associated with the subdomain from the config that existed when the
    token was captured, preventing an old token from being revived after a
    deliberate subdomain change.
    """
    base = Path(base_dir).resolve()
    vault = _resolve_vault(base, vault_path)
    vault.mkdir(parents=True, exist_ok=True)
    manifest = _read_json(vault / MANIFEST_FILE) or {"schema": SCHEMA}

    config = _read_json(base / CONFIG_FILE)
    if config is not None:
        _atomic_copy(base / CONFIG_FILE, vault / CONFIG_FILE)
        manifest["config"] = {
            "present": True,
            "sha256": _sha256(base / CONFIG_FILE),
            "server_port": _valid_port(config.get("server_port")),
            "subdomain": _subdomain(config),
        }

    for name, key in (
        (MACHINE_ID_FILE, "machine_id"),
        (CLIENT_SECRET_FILE, "client_secret"),
    ):
        text = _read_text(base / name)
        if text:
            _atomic_copy(base / name, vault / name)
            manifest[key] = {
                "present": True,
                "sha256": _sha256(base / name),
            }

    token = _read_text(base / TOKEN_FILE)
    if token:
        _atomic_copy(base / TOKEN_FILE, vault / TOKEN_FILE)
        manifest["tunnel_token"] = {
            "present": True,
            "sha256": _sha256(base / TOKEN_FILE),
            "subdomain": _subdomain(config),
        }

    bootstrap = _read_json(base / BOOTSTRAP_FILE)
    if bootstrap is not None:
        _atomic_copy(base / BOOTSTRAP_FILE, vault / BOOTSTRAP_FILE)
        manifest["bootstrap"] = {
            "present": True,
            "sha256": _sha256(base / BOOTSTRAP_FILE),
        }
    elif config is not None:
        # Bootstrap is intentionally one-time.  Once a valid runtime config is
        # present and bootstrap has been consumed, never resurrect an old one.
        manifest["bootstrap"] = {"present": False}

    manifest["schema"] = SCHEMA
    manifest["source_dir"] = str(base)
    manifest["updated_at"] = int(time.time())
    _atomic_json(vault / MANIFEST_FILE, manifest)
    return manifest


def _restore_file(vault: Path, base: Path, name: str) -> bool:
    source = vault / name
    destination = base / name
    if not source.is_file() or destination.exists():
        return False
    _atomic_copy(source, destination)
    return True


def restore_runtime_state(
    base_dir,
    *,
    vault_path: str | os.PathLike | None = None,
) -> list[str]:
    """Restore only missing critical files; existing runtime data always wins."""
    base = Path(base_dir).resolve()
    vault = _resolve_vault(base, vault_path)
    manifest = _read_json(vault / MANIFEST_FILE)
    if not manifest or manifest.get("schema") != SCHEMA:
        return []

    restored: list[str] = []

    # Restore config first because token validity is tied to its subdomain.
    if not (base / CONFIG_FILE).exists():
        if _read_json(vault / CONFIG_FILE) is not None and _restore_file(
            vault, base, CONFIG_FILE
        ):
            restored.append(CONFIG_FILE)

    for name, key in (
        (MACHINE_ID_FILE, "machine_id"),
        (CLIENT_SECRET_FILE, "client_secret"),
    ):
        meta = manifest.get(key)
        if isinstance(meta, dict) and meta.get("present"):
            if not _read_text(base / name) and _restore_file(vault, base, name):
                restored.append(name)

    current_config = _read_json(base / CONFIG_FILE)
    token_meta = manifest.get("tunnel_token")
    if (
        isinstance(token_meta, dict)
        and token_meta.get("present")
        and _subdomain(current_config)
        and _subdomain(current_config) == str(token_meta.get("subdomain") or "").strip().lower()
        and not _read_text(base / TOKEN_FILE)
        and _restore_file(vault, base, TOKEN_FILE)
    ):
        restored.append(TOKEN_FILE)

    bootstrap_meta = manifest.get("bootstrap")
    if (
        isinstance(bootstrap_meta, dict)
        and bootstrap_meta.get("present")
        and not (base / BOOTSTRAP_FILE).exists()
        and _read_json(vault / BOOTSTRAP_FILE) is not None
        and _restore_file(vault, base, BOOTSTRAP_FILE)
    ):
        restored.append(BOOTSTRAP_FILE)

    return restored
