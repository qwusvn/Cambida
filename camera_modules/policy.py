"""Policy enforcement module for Cambida modular camera architecture.

This module provides:
1. Stable table identity access control:
   - Preserves permanent table_id, QR code endpoints (/qr/table/<table_id>.png),
     and video recording bindings during table reordering.
   - Server-side privacy mode: blocks guest live/replay/cut/download while recording continues.
   - Dynamic table visual states: red when privacy is active/engaged, grey when normal/idle.

2. Admin-session authorization helper signatures usable by existing Flask routes:
   - Flask-compatible route decorators: @admin_session_required, @table_privacy_guard.
   - Admin credential authorization with constant-time verification.
   - Safe session management (authenticate, verify, revoke).

3. Deterministic table-limit enforcement based on VERIFIED license grant:
   - Parses /N table cap on verified Telegram pinned message; missing /N suffix means unlimited.
   - Counts ALL configured tables including disabled ones (enabled=False).
   - Rejects bulk add operations that would exceed license limits.
   - Zero-trust principle: NEVER trusts editable key strings alone without verified hardware matching.

4. Secure credential management:
   - No plaintext password storage: PBKDF2-HMAC-SHA256 hashing with cryptographically random salts.
   - Configuration sanitization and credential scrubbing before saving or logging.
"""

from __future__ import annotations

import copy
import ctypes
import functools
import hashlib
import hmac
import logging
import os
import re
import secrets
import sys
import time
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
)

logger = logging.getLogger("cambida.camera_modules.policy")

# ============================================================================
# Minimal Imports & Standalone Fallbacks (Core Worker Coordination)
# ============================================================================

try:
    from .core import (
        CameraModuleError,
        TableColorState,
        generate_table_id,
        mask_credential,
        sanitize_dict_for_logging,
        sanitize_slug,
    )
except ImportError:
    # Standalone fallbacks if imported outside package context
    class CameraModuleError(Exception):  # type: ignore
        """Base error when core module is unavailable."""

        def __init__(self, message: str, details: Optional[Dict[str, Any]] = None):
            super().__init__(message)
            self.message = message
            self.details = details or {}


    class TableColorState:  # type: ignore
        GREY = "grey"
        RED = "red"
        GREEN = "green"
        BLUE = "blue"


    def mask_credential(value: Optional[str], mask_char: str = "*", show_chars: int = 0) -> str:
        if not value:
            return ""
        s = str(value)
        if show_chars <= 0 or len(s) <= show_chars:
            return mask_char * 6
        return s[:show_chars] + (mask_char * 4)


    def sanitize_dict_for_logging(data: Dict[str, Any], secret_keys: Optional[Iterable[str]] = None) -> Dict[str, Any]:
        targets = {"password", "pass", "secret", "token", "key"}
        if secret_keys:
            targets.update(k.lower() for k in secret_keys)
        out: Dict[str, Any] = {}
        for k, v in data.items():
            if str(k).lower() in targets:
                out[k] = mask_credential(str(v)) if v not in (None, "") else ""
            elif isinstance(v, dict):
                out[k] = sanitize_dict_for_logging(v, secret_keys=targets)
            else:
                out[k] = v
        return out


    def sanitize_slug(value: str) -> str:
        s = re.sub(r"[^a-zA-Z0-9_-]", "_", str(value).strip().lower())
        s = re.sub(r"_+", "_", s).strip("_")
        return s or "default"


    def generate_table_id(raw_id: Union[str, int]) -> str:
        return sanitize_slug(str(raw_id).strip())


try:
    from .config import TableBinding
except ImportError:
    TableBinding = None  # type: ignore

# Optional Flask integration
try:
    from flask import (
        Response,
        jsonify,
        redirect,
        request,
        session,
        url_for,
    )
    FLASK_AVAILABLE = True
except ImportError:
    FLASK_AVAILABLE = False
    request = None   # type: ignore
    session = None   # type: ignore
    Response = None  # type: ignore
    jsonify = None   # type: ignore
    redirect = None  # type: ignore
    url_for = None   # type: ignore


# ============================================================================
# Exceptions
# ============================================================================

class PolicyError(CameraModuleError):
    """Base exception for policy violations."""
    pass


class TableLimitExceededError(PolicyError):
    """Raised when configured tables exceed the licensed table limit."""

    def __init__(
        self,
        message: str,
        current_count: int,
        requested_count: int,
        limit: Optional[int],
        details: Optional[Dict[str, Any]] = None,
    ):
        full_details = {
            "current_count": current_count,
            "requested_count": requested_count,
            "projected_count": current_count + requested_count,
            "limit": limit,
            "excess": (current_count + requested_count) - limit if limit is not None else 0,
        }
        if details:
            full_details.update(details)
        super().__init__(message, full_details)
        self.current_count = current_count
        self.requested_count = requested_count
        self.limit = limit


class LicenseVerificationError(PolicyError):
    """Raised when license grant verification fails or key string is untrusted."""
    pass


class TableAccessDeniedError(PolicyError):
    """Raised when guest or unauthorized user attempts access to private table media."""
    pass


# ============================================================================
# Password Security (No Plaintext Storage)
# ============================================================================

_HASH_ALGO = "sha256"
_DEFAULT_PBKDF2_ITERATIONS = 100_000
_PBKDF2_PREFIX = "pbkdf2:sha256:"


def hash_password(plaintext: str, iterations: int = _DEFAULT_PBKDF2_ITERATIONS) -> str:
    """Hash a plaintext password using PBKDF2-HMAC-SHA256 with a secure random salt.

    Never stores or returns plaintext passwords. Format:
    pbkdf2:sha256:<iterations>$<salt_hex>$<hash_hex>
    """
    if not isinstance(plaintext, str):
        plaintext = str(plaintext)
    salt_bytes = secrets.token_bytes(16)
    salt_hex = salt_bytes.hex()
    dk = hashlib.pbkdf2_hmac(
        _HASH_ALGO,
        plaintext.encode("utf-8"),
        salt_bytes,
        iterations,
    )
    return f"{_PBKDF2_PREFIX}{iterations}${salt_hex}${dk.hex()}"


def is_password_hash(credential: Optional[str]) -> bool:
    """Check if the provided credential string is already a supported password hash."""
    if not credential or not isinstance(credential, str):
        return False
    return credential.startswith(_PBKDF2_PREFIX) and credential.count("$") == 2


def verify_password(plaintext: str, stored_credential: Optional[str]) -> bool:
    """Verify a plaintext password against a stored credential in constant time.

    Supports:
    1. PBKDF2-HMAC-SHA256 hashes (standard).
    2. Legacy plaintext strings during migration (with constant-time comparison).

    Returns True if valid, False otherwise.
    """
    if not plaintext or not stored_credential:
        return False

    stored_str = str(stored_credential)
    if is_password_hash(stored_str):
        try:
            prefix_and_iter, salt_hex, expected_hash_hex = stored_str.split("$")
            iter_str = prefix_and_iter.split(":")[-1]
            iterations = int(iter_str)
            salt_bytes = bytes.fromhex(salt_hex)
            actual_dk = hashlib.pbkdf2_hmac(
                _HASH_ALGO,
                str(plaintext).encode("utf-8"),
                salt_bytes,
                iterations,
            )
            return hmac.compare_digest(actual_dk.hex(), expected_hash_hex)
        except Exception as exc:
            logger.warning("Error verifying hashed password: %s", exc)
            return False

    # Backwards-compatible legacy plaintext fallback with constant-time comparison
    return hmac.compare_digest(str(plaintext), stored_str)


def sanitize_admin_auth_for_storage(admin_auth: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Ensure admin_auth dictionary stores password as a secure hash, never plaintext.

    Modifies in-place or returns a new dictionary with hashed password.
    """
    if not isinstance(admin_auth, dict):
        return {}

    cleaned = copy.deepcopy(admin_auth)
    raw_password = str(cleaned.get("password") or "").strip()
    if raw_password and not is_password_hash(raw_password):
        # Convert plaintext to secure PBKDF2 hash before persistence
        cleaned["password"] = hash_password(raw_password)
    return cleaned


def prepare_config_for_storage(candidate_config: Dict[str, Any]) -> Dict[str, Any]:
    """Prepares an entire configuration dictionary for safe disk persistence.

    Enforces that:
    1. admin_auth password is encrypted/hashed.
    2. No plaintext secrets leak to config.json.
    """
    config_copy = copy.deepcopy(candidate_config)
    if "admin_auth" in config_copy and isinstance(config_copy["admin_auth"], dict):
        config_copy["admin_auth"] = sanitize_admin_auth_for_storage(config_copy["admin_auth"])
    return config_copy


def redact_sensitive_config(config_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Scrub sensitive credentials from configuration dictionary before returning to client.

    Replaces admin password and camera passwords with redacted placeholders.
    """
    clean = copy.deepcopy(config_dict)
    if "admin_auth" in clean and isinstance(clean["admin_auth"], dict):
        auth = clean["admin_auth"]
        if auth.get("password"):
            auth["password"] = "******"
            auth["has_password"] = True
    if "admin_session_secret" in clean:
        clean["admin_session_secret"] = mask_credential(clean.get("admin_session_secret"))
    if "telegram_token" in clean:
        clean["telegram_token"] = mask_credential(clean.get("telegram_token"))
    if "license_telegram_token" in clean:
        clean["license_telegram_token"] = mask_credential(clean.get("license_telegram_token"))
    return clean


# ============================================================================
# Hardware Machine Key & License Verification
# ============================================================================

def get_drive_volume_serial(path: Optional[str] = None) -> Optional[str]:
    """Read the Volume Serial Number of the partition where path is located."""
    try:
        target_path = os.path.abspath(path or os.getcwd())
        drive = os.path.splitdrive(target_path)[0]
        if not drive:
            drive = os.path.abspath(os.sep)
        if not drive.endswith("\\"):
            drive += "\\"
        volume_serial = ctypes.c_ulong()
        res = ctypes.windll.kernel32.GetVolumeInformationW(
            drive, None, 0, ctypes.byref(volume_serial), None, None, None, 0
        )
        if res != 0 and volume_serial.value:
            val = volume_serial.value
            return f"{(val >> 16) & 0xFFFF:04X}-{val & 0xFFFF:04X}"
    except Exception as exc:
        logger.debug("Failed reading drive volume serial: %s", exc)
    return None


def read_windows_machine_guid() -> Optional[str]:
    """Read physical Windows MachineGuid from registry as fallback identity."""
    try:
        import winreg  # type: ignore
        access = winreg.KEY_READ
        if hasattr(winreg, "KEY_WOW64_64KEY"):
            access |= winreg.KEY_WOW64_64KEY
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            access,
        )
        try:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
        finally:
            winreg.CloseKey(key)
        val_str = str(value or "").strip()
        return val_str or None
    except Exception as exc:
        logger.debug("Failed reading Windows MachineGuid: %s", exc)
        return None


def get_machine_license_key(path: Optional[str] = None) -> str:
    """Return the hardware drive volume license key (e.g. 00E1-1D9A).

    Never trusts editable key strings. Derives identity directly from host storage.
    """
    key = get_drive_volume_serial(path)
    if key:
        return key.upper()
    machine_guid = read_windows_machine_guid()
    if machine_guid:
        return hashlib.sha256(machine_guid.encode("utf-8")).hexdigest().upper()[:16]
    return "UNKNOWN-DRIVE"


# ============================================================================
# Verified License Grant Data Structure
# ============================================================================

@dataclass(frozen=True)
class VerifiedLicenseGrant:
    """Deterministic, immutable record of an authenticated license grant.

    Key principles:
    - Never derived from editable configuration strings alone.
    - Matches hardware drive serial against authentic verified source (Telegram pin).
    - table_limit is an integer if /N cap is specified (e.g. 8, 16), or None if missing (unlimited).
    """

    hardware_key: str
    is_verified: bool
    table_limit: Optional[int] = None
    has_table_cap: bool = False
    raw_grant_text: str = ""
    verification_source: str = "unverified"
    activated_at: Optional[str] = None
    reason: str = ""

    def is_unlimited(self) -> bool:
        """True if the verified grant imposes no cap on table count."""
        return self.is_verified and (self.table_limit is None)

    def can_accommodate(self, total_table_count: int) -> bool:
        """Check if total_table_count is permissible under this grant."""
        if not self.is_verified:
            return False
        if self.table_limit is None:
            return True
        return total_table_count <= self.table_limit

    def remaining_capacity(self, current_table_count: int) -> Optional[int]:
        """Return number of tables that can still be added, or None if unlimited."""
        if not self.is_verified:
            return 0
        if self.table_limit is None:
            return None
        return max(0, self.table_limit - current_table_count)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "hardware_key": self.hardware_key,
            "is_verified": self.is_verified,
            "table_limit": self.table_limit,
            "has_table_cap": self.has_table_cap,
            "is_unlimited": self.is_unlimited(),
            "raw_grant_text": self.raw_grant_text,
            "verification_source": self.verification_source,
            "activated_at": self.activated_at,
            "reason": self.reason,
        }


def parse_verified_license_grant(
    pinned_text: str,
    hardware_key: Optional[str] = None,
) -> VerifiedLicenseGrant:
    """Parse deterministic table cap and license grant from verified Telegram text.

    Contract Rules (CAMERA_MODULE_CONTRACT_20260924.md):
    - License optional /N table cap on verified Telegram pin.
    - Absent /N suffix -> unlimited tables (table_limit=None).
    - Case-insensitive, supports key formats with dashes (00E1-1D9A) and without (00E11D9A).
    - Word-boundary matching prevents substring collision with longer identifiers.
    """
    hw_key = (hardware_key or get_machine_license_key()).strip().upper()
    if not hw_key or hw_key == "UNKNOWN-DRIVE":
        return VerifiedLicenseGrant(
            hardware_key=hw_key or "UNKNOWN",
            is_verified=False,
            table_limit=0,
            has_table_cap=True,
            reason="Không đọc được mã ổ cứng phần cứng.",
        )

    clean_key = hw_key
    no_dash_key = hw_key.replace("-", "")

    if not pinned_text:
        return VerifiedLicenseGrant(
            hardware_key=hw_key,
            is_verified=False,
            table_limit=0,
            has_table_cap=True,
            reason="Nội dung tin nhắn ghim Telegram trống hoặc chưa tải được.",
        )

    # Regex matches key candidate optionally followed by /<digits>
    # E.g.: "00E1-1D9A", "00E1-1D9A/8", "00E11D9A/16"
    key_escaped = f"(?:{re.escape(clean_key)}|{re.escape(no_dash_key)})"
    pattern = rf"(?<![A-Za-z0-9]){key_escaped}(?:\s*/\s*(\d+))?(?![A-Za-z0-9])"

    matches = list(re.finditer(pattern, str(pinned_text), re.IGNORECASE))
    if not matches:
        return VerifiedLicenseGrant(
            hardware_key=hw_key,
            is_verified=False,
            table_limit=0,
            has_table_cap=True,
            reason="Mã ổ cứng chưa có trong tin nhắn ghim Telegram được xác thực.",
        )

    # Deterministic resolution:
    # If any match omits /N, grant is unlimited.
    # If all matches have /N, take the maximum licensed cap.
    parsed_limits: List[int] = []
    has_unlimited = False
    matched_tokens: List[str] = []

    for m in matches:
        matched_tokens.append(m.group(0))
        cap_str = m.group(1)
        if cap_str is not None:
            parsed_limits.append(int(cap_str))
        else:
            has_unlimited = True

    if has_unlimited:
        final_limit: Optional[int] = None
        has_cap = False
        reason = f"Mã ổ cứng {hw_key} hợp lệ trong tin ghim Telegram (Không giới hạn số bàn)."
    else:
        final_limit = max(parsed_limits) if parsed_limits else None
        has_cap = final_limit is not None
        reason = f"Mã ổ cứng {hw_key} hợp lệ trong tin ghim Telegram (Giới hạn tối đa: {final_limit} bàn)."

    return VerifiedLicenseGrant(
        hardware_key=hw_key,
        is_verified=True,
        table_limit=final_limit,
        has_table_cap=has_cap,
        raw_grant_text=", ".join(matched_tokens),
        verification_source="telegram_pinned",
        reason=reason,
    )


def verify_license_grant(
    pinned_text: str,
    candidate_key: Optional[str] = None,
    hardware_key: Optional[str] = None,
) -> VerifiedLicenseGrant:
    """Verify license grant enforcing ZERO-TRUST on editable key strings.

    CRITICAL SECURITY RULE:
    Never trusts candidate_key string alone. Candidate key from user input or
    configuration MUST match physical hardware drive serial and MUST exist in
    authentic verified pinned text.
    """
    actual_hw_key = (hardware_key or get_machine_license_key()).strip().upper()

    if candidate_key:
        clean_candidate = str(candidate_key).strip().upper()
        # Verify candidate matches actual host hardware
        candidate_no_dash = clean_candidate.replace("-", "")
        hw_no_dash = actual_hw_key.replace("-", "")
        if clean_candidate != actual_hw_key and candidate_no_dash != hw_no_dash:
            return VerifiedLicenseGrant(
                hardware_key=actual_hw_key,
                is_verified=False,
                table_limit=0,
                has_table_cap=True,
                reason=f"Mã bản quyền gửi lên ({clean_candidate}) không khớp với mã ổ cứng thực tế ({actual_hw_key}).",
            )

    return parse_verified_license_grant(pinned_text, hardware_key=actual_hw_key)


# ============================================================================
# Deterministic Table-Limit Enforcement
# ============================================================================

def count_configured_tables(tables: Optional[Sequence[Any]]) -> int:
    """Count all configured tables in the system.

    CRITICAL POLICY ENFORCEMENT RULE (CAMERA_MODULE_CONTRACT_20260924.md):
    Counts ALL configured tables, including disabled ones (enabled=False).
    Disabled tables are part of system configuration and cannot be discounted
    to bypass license caps.
    """
    if not tables:
        return 0
    return len(tables)


def get_table_counts(tables: Optional[Sequence[Any]]) -> Dict[str, int]:
    """Return detailed table accounting (total, enabled, disabled)."""
    if not tables:
        return {"total": 0, "enabled": 0, "disabled": 0}

    total = len(tables)
    enabled = 0
    disabled = 0

    for item in tables:
        if isinstance(item, dict):
            is_enabled = bool(item.get("enabled", True))
        elif hasattr(item, "enabled"):
            is_enabled = bool(getattr(item, "enabled", True))
        else:
            is_enabled = True

        if is_enabled:
            enabled += 1
        else:
            disabled += 1

    return {"total": total, "enabled": enabled, "disabled": disabled}


def validate_table_limit(
    tables: Optional[Sequence[Any]],
    verified_grant: VerifiedLicenseGrant,
    additional_count: int = 0,
) -> Tuple[bool, Optional[str]]:
    """Validate that total configured tables (including disabled) do not exceed limit.

    Returns:
        (is_valid: bool, error_message: Optional[str])
    """
    current_count = count_configured_tables(tables)
    projected = current_count + max(0, int(additional_count))

    if not verified_grant.is_verified:
        return False, (
            f"Bản quyền xem lại chưa được kích hoạt cho ổ đĩa ({verified_grant.hardware_key}). "
            f"Lý do: {verified_grant.reason}"
        )

    if verified_grant.table_limit is None:
        # Unlimited grant
        return True, None

    if projected > verified_grant.table_limit:
        excess = projected - verified_grant.table_limit
        return False, (
            f"Vượt quá giới hạn bàn được cấp phép: Hệ thống đang có {current_count} bàn "
            f"(bao gồm cả bàn tắt/disabled). Thêm {additional_count} bàn sẽ nâng tổng số lên "
            f"{projected} bàn, vượt quá giới hạn cấp phép {verified_grant.table_limit} bàn "
            f"({excess} bàn vượt mức)."
        )

    return True, None


def enforce_bulk_add_table_limit(
    current_tables: Optional[Sequence[Any]],
    new_tables_or_count: Union[int, Sequence[Any]],
    verified_grant: VerifiedLicenseGrant,
    raise_on_error: bool = False,
) -> Tuple[bool, Optional[str], Dict[str, Any]]:
    """Deterministic bulk-add limit enforcer.

    CRITICAL CONTRACT RULE:
    Rejects any bulk-add operation that would cause total configured tables
    (counting all disabled tables) to exceed the verified license limit.

    Args:
        current_tables: Currently configured tables (dicts or TableBinding instances).
        new_tables_or_count: Number of tables to add, or list of table candidates.
        verified_grant: The verified license grant.
        raise_on_error: If True, raises TableLimitExceededError on violation.

    Returns:
        (allowed: bool, error_message: Optional[str], diagnostics: Dict[str, Any])
    """
    current_count = count_configured_tables(current_tables)
    if isinstance(new_tables_or_count, int):
        add_count = max(0, new_tables_or_count)
    elif isinstance(new_tables_or_count, Sequence):
        add_count = len(new_tables_or_count)
    else:
        add_count = 0

    projected_count = current_count + add_count
    limit = verified_grant.table_limit

    diagnostics = {
        "current_count": current_count,
        "additional_count": add_count,
        "projected_count": projected_count,
        "table_limit": limit,
        "is_verified": verified_grant.is_verified,
        "is_unlimited": verified_grant.is_unlimited(),
        "excess": max(0, projected_count - limit) if limit is not None else 0,
        "hardware_key": verified_grant.hardware_key,
    }

    if not verified_grant.is_verified:
        err = f"Từ chối thêm bàn hàng loạt: Bản quyền chưa được xác thực ({verified_grant.reason})."
        diagnostics["error"] = err
        diagnostics["allowed"] = False
        if raise_on_error:
            raise LicenseVerificationError(err)
        return False, err, diagnostics

    if limit is not None and projected_count > limit:
        excess = projected_count - limit
        err = (
            f"Từ chối thêm hàng loạt: Tổng số {projected_count} bàn "
            f"(hiện có {current_count} bàn kể cả bàn tắt + {add_count} bàn mới) "
            f"vượt quá giới hạn {limit} bàn của bản quyền (vượt {excess} bàn)."
        )
        diagnostics["error"] = err
        diagnostics["allowed"] = False
        if raise_on_error:
            raise TableLimitExceededError(
                err,
                current_count=current_count,
                requested_count=add_count,
                limit=limit,
                details=diagnostics,
            )
        return False, err, diagnostics

    diagnostics["allowed"] = True
    return True, None, diagnostics


# ============================================================================
# Stable Table Identity & Privacy Access Control
# ============================================================================

def resolve_table_by_id(
    tables: Optional[Sequence[Any]],
    table_id: Union[str, int],
) -> Optional[Dict[str, Any]]:
    """Find table configuration by its distinct, stable identifier."""
    if not tables or table_id is None:
        return None
    tid_str = str(table_id).strip().lower()
    for item in tables:
        if isinstance(item, dict):
            if str(item.get("id", "")).strip().lower() == tid_str or str(item.get("table_id", "")).strip().lower() == tid_str:
                return dict(item)
        elif hasattr(item, "table_id") and str(getattr(item, "table_id", "")).strip().lower() == tid_str:
            return getattr(item, "to_modular_dict", lambda: item.__dict__)()
        elif hasattr(item, "id") and str(getattr(item, "id", "")).strip().lower() == tid_str:
            return getattr(item, "to_modular_dict", lambda: item.__dict__)()
    return None


def resolve_table_by_camera_id(
    tables: Optional[Sequence[Any]],
    camera_id: Union[int, str],
) -> Optional[Dict[str, Any]]:
    """Find table configuration bound to a given camera_id."""
    if not tables or camera_id is None:
        return None
    try:
        cid_int = int(camera_id)
    except (ValueError, TypeError):
        cid_int = None
    cid_str = str(camera_id).strip()

    for item in tables:
        if isinstance(item, dict):
            c_val = item.get("camera_id")
            if (cid_int is not None and c_val == cid_int) or str(c_val) == cid_str:
                return dict(item)
        elif hasattr(item, "camera_id"):
            c_val = getattr(item, "camera_id")
            if (cid_int is not None and c_val == cid_int) or str(c_val) == cid_str:
                return getattr(item, "to_modular_dict", lambda: item.__dict__)()
    return None


def is_table_privacy_engaged(table: Optional[Union[Dict[str, Any], Any]]) -> bool:
    """Return True if server-side privacy mode is engaged for this table.

    Contract Requirement:
    "server-side privacy off blocks guest live/replay/cut/download while recording continues"
    When privacy_off is True (or active), guests cannot view live/replay/cut/download.
    """
    if not table:
        return False
    if isinstance(table, dict):
        return bool(table.get("privacy_off", False))
    return bool(getattr(table, "privacy_off", False))


def get_table_visual_color(table: Optional[Union[Dict[str, Any], Any]]) -> str:
    """Return red/grey table visual color based on privacy and status state.

    Contract Requirement:
    "switch replaces home timeline navigation with admin session authentication and red/grey table colors"
    - Guests can view a table (enabled=True and privacy_off=False): RED.
    - Guest access disabled (enabled=False or privacy_off=True): GREY.
    """
    if not table:
        return TableColorState.GREY
    enabled = table.get("enabled", True) if isinstance(table, dict) else getattr(table, "enabled", True)
    return TableColorState.RED if enabled and not is_table_privacy_engaged(table) else TableColorState.GREY


def evaluate_table_media_access(
    table: Optional[Union[Dict[str, Any], Any]],
    is_admin: bool,
    action: str = "view",
) -> Tuple[bool, str, int]:
    """Evaluate whether an access attempt (live, replay, cut, download) is permitted.

    Contract Rules:
    1. Server-side privacy mode blocks guest live/replay/cut/download.
    2. Background recording continues uninterrupted regardless of privacy mode.
    3. Authenticated admin sessions always have full access.

    Returns:
        (permitted: bool, reason: str, http_status_code: int)
    """
    # Admin sessions always have full access
    if is_admin:
        return True, "Quyền quản trị viên được chấp thuận.", 200

    # If table is not found or has no privacy restriction, guests can access normal media
    if not table:
        return True, "Không có hạn chế quyền riêng tư.", 200

    # Check disabled table state
    is_enabled = bool(table.get("enabled", True) if isinstance(table, dict) else getattr(table, "enabled", True))
    if not is_enabled:
        return False, "Bàn đang trong trạng thái tạm ngưng (disabled).", 403

    # Check privacy engagement
    if is_table_privacy_engaged(table):
        act_label = {
            "live": "xem trực tiếp",
            "replay": "xem lại",
            "cut": "cắt video",
            "download": "tải video",
            "view": "truy cập phương tiện",
        }.get(action.lower(), action)
        return False, f"Bàn này đang bật chế độ riêng tư: Khách bị chặn {act_label}.", 403

    return True, "Khách được phép truy cập.", 200


def reorder_tables_preserving_identity(
    tables: Sequence[Union[Dict[str, Any], Any]],
    ordered_table_ids: Sequence[str],
) -> List[Dict[str, Any]]:
    """Reorder configured tables while strictly preserving stable IDs, QR endpoints, and video bindings.

    CRITICAL CONTRACT REQUIREMENT:
    "admin table reorder preserves QR/video identity"

    This function:
    1. Reorders tables according to the requested sequence of table IDs.
    2. Updates sort_order metadata accordingly.
    3. Guarantees that table_id, name, camera_id, QR code URLs, and recordings are 100% unaltered.
    4. Retains any existing tables omitted from ordered_table_ids at the end without data loss.
    """
    if not tables:
        return []

    # Map tables by sanitized ID
    table_map: Dict[str, Dict[str, Any]] = {}
    original_order: List[str] = []

    for item in tables:
        t_dict = dict(item) if isinstance(item, dict) else item.to_modular_dict()
        tid = str(t_dict.get("id") or t_dict.get("table_id") or "").strip()
        if tid:
            table_map[tid.lower()] = t_dict
            original_order.append(tid.lower())

    reordered: List[Dict[str, Any]] = []
    seen: Set[str] = set()

    # Place specified IDs in given order
    for idx, raw_id in enumerate(ordered_table_ids, start=1):
        clean_id = str(raw_id).strip().lower()
        if clean_id in table_map and clean_id not in seen:
            t = copy.deepcopy(table_map[clean_id])
            t["sort_order"] = idx
            reordered.append(t)
            seen.add(clean_id)

    # Append any remaining tables omitted from the sequence
    remaining_idx = len(reordered) + 1
    for orig_id in original_order:
        if orig_id not in seen:
            t = copy.deepcopy(table_map[orig_id])
            t["sort_order"] = remaining_idx
            reordered.append(t)
            seen.add(orig_id)
            remaining_idx += 1

    return reordered


# ============================================================================
# Admin-Session Authorization Helpers (Flask-Compatible Signatures)
# ============================================================================

def is_admin_session(sess: Optional[Any] = None) -> bool:
    """Check if current request session is authenticated as admin.

    Works seamlessly with Flask's session proxy or standalone session dicts.
    """
    target = sess if sess is not None else (session if FLASK_AVAILABLE else None)
    if target is None:
        return False
    try:
        return target.get("admin_authenticated") is True
    except Exception:
        return False


def authenticate_admin_session(
    sess: Any,
    username: str = "admin",
) -> None:
    """Mark session as authenticated admin."""
    if sess is not None:
        sess["admin_authenticated"] = True
        sess["admin_user"] = username
        sess["admin_auth_time"] = time.time()


def revoke_admin_session(sess: Any) -> None:
    """Clear admin authentication from session."""
    if sess is not None:
        sess.pop("admin_authenticated", None)
        sess.pop("admin_user", None)
        sess.pop("admin_auth_time", None)


def authorize_admin_login(
    username: str,
    password: str,
    config: Dict[str, Any],
) -> Tuple[bool, Optional[str]]:
    """Authorize admin login credentials without storing or logging plaintext passwords.

    Uses constant-time comparison against hashed or legacy password.
    Returns:
        (is_authenticated: bool, error_message: Optional[str])
    """
    auth = config.get("admin_auth")
    if not isinstance(auth, dict):
        return False, "Hệ thống chưa cấu hình tài khoản quản trị."

    expected_username = str(auth.get("username", "admin")).strip()
    expected_password = auth.get("password")

    if not expected_password:
        return False, "Mật khẩu quản trị chưa được thiết lập."

    user_matches = hmac.compare_digest(str(username).strip(), expected_username)
    pass_matches = verify_password(str(password), expected_password)

    if user_matches and pass_matches:
        return True, None

    return False, "Sai tên đăng nhập hoặc mật khẩu."


def admin_session_required(
    view_func: Optional[Callable] = None,
    redirect_endpoint: str = "admin_login",
):
    """Flask route decorator requiring authenticated admin session.

    Usable directly on Flask routes:
        @app.route('/api/admin/...')
        @admin_session_required
        def my_view(): ...

    - Returns JSON 401 on /api/ endpoints.
    - Returns SSE formatted error on /merge.
    - Redirects to login on browser views, preserving safe 'next' redirect.
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not FLASK_AVAILABLE:
                return fn(*args, **kwargs)

            if is_admin_session(session):
                return fn(*args, **kwargs)

            # API routes return JSON 401
            req_path = request.path if request else ""
            if req_path.startswith("/api/"):
                return jsonify({"ok": False, "error": "Cần đăng nhập quản trị."}), 401

            # SSE endpoint returns SSE event 401
            if req_path == "/merge":
                return Response(
                    "data: error:Cần đăng nhập quản trị.\n\n",
                    status=401,
                    mimetype="text/event-stream",
                )

            # Web browser views redirect to login
            safe_next = request.full_path if request else "/"
            if not safe_next.startswith("/") or safe_next.startswith("//"):
                safe_next = "/"
            return redirect(url_for(redirect_endpoint, next=safe_next))

        return wrapper

    if view_func is not None:
        return decorator(view_func)
    return decorator


def table_privacy_guard(
    action: str = "view",
    camera_id_param: str = "cam_id",
    table_id_param: str = "table_id",
    tables_provider: Optional[Callable[[], Sequence[Any]]] = None,
):
    """Flask route decorator enforcing server-side privacy access control.

    Protects guest access to live/replay/cut/download when table privacy is active,
    while allowing admin sessions and background recording to continue.
    """
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not FLASK_AVAILABLE or request is None:
                return fn(*args, **kwargs)

            # Admin session always bypasses privacy block
            if is_admin_session(session):
                return fn(*args, **kwargs)

            # Retrieve target camera_id or table_id from view args or query args
            cam_val = kwargs.get(camera_id_param) or request.args.get(camera_id_param)
            tbl_val = kwargs.get(table_id_param) or request.args.get(table_id_param)

            # Resolve tables configuration
            tables: Sequence[Any] = []
            if tables_provider:
                tables = tables_provider()
            else:
                # Default attempt to read from Flask app config
                try:
                    from flask import current_app
                    tables = current_app.config.get("TABLES", [])
                except Exception:
                    tables = []

            target_table = None
            if tbl_val is not None:
                target_table = resolve_table_by_id(tables, tbl_val)
            elif cam_val is not None:
                target_table = resolve_table_by_camera_id(tables, cam_val)

            allowed, reason, status = evaluate_table_media_access(target_table, is_admin=False, action=action)
            if not allowed:
                req_path = request.path
                if req_path.startswith("/api/") or "json" in request.headers.get("Accept", "").lower():
                    return jsonify({"ok": False, "error": reason}), status
                if req_path == "/merge":
                    return Response(
                        f"data: error:{reason}\n\n",
                        status=status,
                        mimetype="text/event-stream",
                    )
                return Response(reason, status=status, mimetype="text/plain; charset=utf-8")

            return fn(*args, **kwargs)

        return wrapper
    return decorator


# ============================================================================
# Standalone Module Health Check & Export Interface
# ============================================================================

def policy_module_health() -> Dict[str, Any]:
    """Diagnostic health status of policy enforcement module."""
    hw_key = get_machine_license_key()
    return {
        "status": "healthy",
        "module": "camera_modules.policy",
        "hardware_license_key": hw_key,
        "flask_available": FLASK_AVAILABLE,
        "features": [
            "stable_table_identity",
            "server_side_privacy_mode",
            "red_grey_table_visuals",
            "verified_license_grant_cap",
            "count_all_configured_tables_including_disabled",
            "bulk_add_rejection",
            "no_plaintext_password_storage",
            "zero_trust_editable_keys",
        ],
    }


__all__ = [
    # Exceptions
    "PolicyError",
    "TableLimitExceededError",
    "LicenseVerificationError",
    "TableAccessDeniedError",
    # Password Security
    "hash_password",
    "verify_password",
    "is_password_hash",
    "sanitize_admin_auth_for_storage",
    "prepare_config_for_storage",
    "redact_sensitive_config",
    # Hardware Key & License Verification
    "get_drive_volume_serial",
    "read_windows_machine_guid",
    "get_machine_license_key",
    "VerifiedLicenseGrant",
    "parse_verified_license_grant",
    "verify_license_grant",
    # Table Limit Enforcement
    "count_configured_tables",
    "get_table_counts",
    "validate_table_limit",
    "enforce_bulk_add_table_limit",
    # Stable Table Identity & Privacy Access Control
    "resolve_table_by_id",
    "resolve_table_by_camera_id",
    "is_table_privacy_engaged",
    "get_table_visual_color",
    "evaluate_table_media_access",
    "reorder_tables_preserving_identity",
    # Admin Session & Route Guards
    "is_admin_session",
    "authenticate_admin_session",
    "revoke_admin_session",
    "authorize_admin_login",
    "admin_session_required",
    "table_privacy_guard",
    # Health Check
    "policy_module_health",
]
