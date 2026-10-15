"""Guest Facebook Live Profile Store with DPAPI CurrentUser encryption.

Note on DPAPI CurrentUser limitations:
Encryption and decryption are bound to the current Windows user account and machine.
Stored credentials cannot be decrypted under a different user account or after migration
to another machine. If the application service account changes, stored stream keys cannot
be decrypted and will fail closed (HTTP 503) without exposing or silently clearing data.
"""
from __future__ import annotations

import ctypes
import hashlib
import math
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from typing import Any, Callable

from werkzeug.security import check_password_hash, generate_password_hash

_PIN_RE = re.compile(r"^[0-9]{4}$")
_STREAM_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{8,256}$")
_PHONE_CLEAN_RE = re.compile(r"^[0-9]{10,15}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{40,64}$")
_TOKEN_LIFETIME_SECONDS = 90 * 86400  # 90 days
_RATE_LIMIT_WINDOW_SECONDS = 15 * 60  # 15 minutes
_RATE_LIMIT_MAX_PHONE = 5
_RATE_LIMIT_MAX_IP = 30
_DUMMY_PIN_HASH = "pbkdf2:sha256:600000$dummy$0000000000000000000000000000000000000000000000000000000000000000"


class ProfileError(Exception):
    """Domain exception with HTTP status code and non-leaking messages."""

    def __init__(self, message: str, status: int = 500):
        super().__init__(message)
        self.status = status


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_uint32),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _dpapi_protect(data: bytes) -> bytes:
    if sys.platform != "win32":
        raise ProfileError("DPAPI chỉ khả dụng trên môi trường Windows.", status=503)
    if not isinstance(data, (bytes, bytearray)):
        raise ProfileError("Dữ liệu cần mã hóa không hợp lệ.", status=500)

    raw = bytes(data)
    cb = len(raw)
    buf = (ctypes.c_ubyte * cb).from_buffer_copy(raw)
    blob_in = _DataBlob(cb, ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    blob_out = _DataBlob(0, None)

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.c_wchar_p,
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptProtectData.restype = ctypes.c_int

    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    ok = crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        0x01,  # CRYPTPROTECT_UI_FORBIDDEN
        ctypes.byref(blob_out),
    )
    if not ok:
        raise ProfileError("Không thể mã hóa dữ liệu xác thực.", status=503)

    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        if blob_out.pbData:
            kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def _dpapi_unprotect(data: bytes) -> bytes:
    if sys.platform != "win32":
        raise ProfileError("DPAPI chỉ khả dụng trên môi trường Windows.", status=503)
    if not isinstance(data, (bytes, bytearray)):
        raise ProfileError("Dữ liệu mã hóa không hợp lệ.", status=503)

    raw = bytes(data)
    cb = len(raw)
    buf = (ctypes.c_ubyte * cb).from_buffer_copy(raw)
    blob_in = _DataBlob(cb, ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    blob_out = _DataBlob(0, None)

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(ctypes.c_wchar_p),
        ctypes.POINTER(_DataBlob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(_DataBlob),
    ]
    crypt32.CryptUnprotectData.restype = ctypes.c_int

    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    ok = crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        0x01,  # CRYPTPROTECT_UI_FORBIDDEN
        ctypes.byref(blob_out),
    )
    if not ok:
        raise ProfileError("Không thể giải mã dữ liệu xác thực.", status=503)

    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        if blob_out.pbData:
            kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


class GuestLiveProfileStore:
    """Multi-process, thread-safe guest profile store backed by SQLite and DPAPI."""

    def __init__(
        self,
        path: str | None,
        protect: Callable[[bytes], bytes] | None = None,
        unprotect: Callable[[bytes], bytes] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.path = path
        self._protect_fn = protect or _dpapi_protect
        self._unprotect_fn = unprotect or _dpapi_unprotect
        self.clock = clock or time.time
        self._lock = threading.Lock()
        self._initialized = False

    def _ensure_initialized(self) -> None:
        if self.path is None:
            raise ProfileError("Dịch vụ tài khoản chưa được định cấu hình.", status=503)

        if self._initialized:
            return

        with self._lock:
            if self._initialized:
                return

            try:
                db_dir = os.path.dirname(os.path.abspath(self.path))
                if db_dir:
                    os.makedirs(db_dir, exist_ok=True)
            except OSError:
                raise ProfileError("Không thể khởi tạo thư mục lưu trữ.", status=503) from None

            conn = self._connect_raw()
            try:
                cur = conn.cursor()
                cur.execute("BEGIN IMMEDIATE;")
                cur.execute("PRAGMA user_version;")
                row = cur.fetchone()
                version = row[0] if row else 0

                if version == 0:
                    cur.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';")
                    existing_user_tables = cur.fetchone()[0]
                    if existing_user_tables > 0:
                        cur.execute("ROLLBACK;")
                        raise ProfileError("Cơ sở dữ liệu đã tồn tại với cấu trúc không xác định.", status=503)

                    cur.execute(
                        """
                        CREATE TABLE profiles (
                            id TEXT PRIMARY KEY,
                            phone TEXT UNIQUE NOT NULL,
                            pin_hash TEXT NOT NULL,
                            encrypted_key BLOB NOT NULL,
                            token_hash TEXT,
                            token_expires REAL,
                            created_at REAL NOT NULL,
                            updated_at REAL NOT NULL
                        );
                        """
                    )
                    cur.execute(
                        """
                        CREATE TABLE attempts (
                            key TEXT NOT NULL,
                            timestamp REAL NOT NULL
                        );
                        """
                    )
                    cur.execute("CREATE INDEX idx_profiles_token_hash ON profiles(token_hash);")
                    cur.execute("CREATE INDEX idx_attempts_key_time ON attempts(key, timestamp);")
                    cur.execute("CREATE INDEX idx_attempts_time ON attempts(timestamp);")
                    cur.execute("PRAGMA user_version = 1;")
                elif version == 1:
                    pass
                else:
                    cur.execute("ROLLBACK;")
                    raise ProfileError("Phiên bản cơ sở dữ liệu không tương thích.", status=503)

                self._validate_schema_structure(cur)
                cur.execute("COMMIT;")
                self._initialized = True
            except ProfileError:
                try:
                    conn.execute("ROLLBACK;")
                except sqlite3.Error:
                    pass
                raise
            except sqlite3.Error:
                try:
                    conn.execute("ROLLBACK;")
                except sqlite3.Error:
                    pass
                raise ProfileError("Lỗi khởi tạo cơ sở dữ liệu.", status=503) from None
            finally:
                conn.close()

    def _validate_schema_structure(self, cur: sqlite3.Cursor) -> None:
        cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('profiles', 'attempts');")
        tables = {r[0] for r in cur.fetchall()}
        if {"profiles", "attempts"} - tables:
            raise ProfileError("Cấu trúc cơ sở dữ liệu không hợp lệ.", status=503)

        cur.execute('PRAGMA table_info("profiles");')
        profiles_info = {r[1]: r for r in cur.fetchall()}
        expected_profiles = {
            "id": ("TEXT", True, True),
            "phone": ("TEXT", False, True),
            "pin_hash": ("TEXT", False, True),
            "encrypted_key": ("BLOB", False, True),
            "token_hash": ("TEXT", False, False),
            "token_expires": ("REAL", False, False),
            "created_at": ("REAL", False, True),
            "updated_at": ("REAL", False, True),
        }

        if set(profiles_info.keys()) != set(expected_profiles.keys()):
            raise ProfileError("Cấu trúc cơ sở dữ liệu không hợp lệ.", status=503)

        for col_name, (exp_type, is_pk, is_notnull) in expected_profiles.items():
            info = profiles_info[col_name]
            col_type = str(info[2]).upper()
            col_notnull = bool(info[3])
            col_pk = int(info[5]) > 0

            if col_type != exp_type:
                raise ProfileError("Cấu trúc cơ sở dữ liệu không hợp lệ.", status=503)
            if is_pk and not col_pk:
                raise ProfileError("Cấu trúc cơ sở dữ liệu không hợp lệ.", status=503)
            if is_notnull:
                if not is_pk and not col_notnull:
                    raise ProfileError("Cấu trúc cơ sở dữ liệu không hợp lệ.", status=503)
            else:
                if col_notnull:
                    raise ProfileError("Cấu trúc cơ sở dữ liệu không hợp lệ.", status=503)

        cur.execute('PRAGMA index_list("profiles");')
        index_rows = cur.fetchall()
        phone_unique_found = False
        for idx_row in index_rows:
            idx_name = idx_row[1]
            idx_unique = bool(idx_row[2])
            if not idx_unique:
                continue
            safe_idx_name = str(idx_name).replace('"', '""')
            cur.execute(f'PRAGMA index_info("{safe_idx_name}");')
            idx_cols = [r[2] for r in cur.fetchall()]
            if idx_cols == ["phone"]:
                phone_unique_found = True
                break

        if not phone_unique_found:
            raise ProfileError("Cấu trúc cơ sở dữ liệu không hợp lệ.", status=503)

        cur.execute('PRAGMA table_info("attempts");')
        attempts_info = {r[1]: r for r in cur.fetchall()}
        expected_attempts = {
            "key": ("TEXT", True),
            "timestamp": ("REAL", True),
        }
        if set(attempts_info.keys()) != set(expected_attempts.keys()):
            raise ProfileError("Cấu trúc cơ sở dữ liệu không hợp lệ.", status=503)

        for col_name, (exp_type, is_notnull) in expected_attempts.items():
            info = attempts_info[col_name]
            col_type = str(info[2]).upper()
            col_notnull = bool(info[3])

            if col_type != exp_type:
                raise ProfileError("Cấu trúc cơ sở dữ liệu không hợp lệ.", status=503)
            if is_notnull and not col_notnull:
                raise ProfileError("Cấu trúc cơ sở dữ liệu không hợp lệ.", status=503)

    def _connect_raw(self) -> sqlite3.Connection:
        if self.path is None:
            raise ProfileError("Dịch vụ tài khoản chưa được định cấu hình.", status=503)
        conn = None
        try:
            conn = sqlite3.connect(self.path, timeout=15.0, isolation_level=None)
            cur = conn.cursor()
            cur.execute("PRAGMA busy_timeout = 15000;")
            cur.execute("PRAGMA synchronous = FULL;")
            return conn
        except sqlite3.Error:
            if conn is not None:
                conn.close()
            raise ProfileError("Không thể kết nối cơ sở dữ liệu.", status=503) from None

    def _connect(self) -> sqlite3.Connection:
        conn = self._connect_raw()
        if self._initialized:
            try:
                cur = conn.cursor()
                cur.execute("PRAGMA user_version;")
                row = cur.fetchone()
                if not row or row[0] != 1:
                    raise ProfileError("Phiên bản cơ sở dữ liệu không tương thích.", status=503)
                self._validate_schema_structure(cur)
            except ProfileError:
                conn.close()
                raise
            except sqlite3.Error:
                conn.close()
                raise ProfileError("Lỗi kết nối cơ sở dữ liệu.", status=503) from None
        return conn

    @staticmethod
    def _token_hash(token: Any) -> str | None:
        if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
            return None
        return hashlib.sha256(token.encode("ascii")).hexdigest()

    @staticmethod
    def _normalize_phone(phone: Any) -> str:
        if not isinstance(phone, str) or len(phone) > 32:
            raise ProfileError("Số điện thoại không hợp lệ.", status=400)
        p = phone.strip()
        if p.startswith("+84"):
            p = "0" + p[3:]
        elif p.startswith("84") and len(p) >= 11:
            p = "0" + p[2:]

        if not _PHONE_CLEAN_RE.fullmatch(p):
            raise ProfileError("Số điện thoại không hợp lệ.", status=400)
        return p

    @staticmethod
    def _validate_pin(pin: Any) -> str:
        if not isinstance(pin, str) or len(pin) != 4 or not _PIN_RE.fullmatch(pin):
            raise ProfileError("Mã PIN phải gồm đúng 4 chữ số.", status=400)
        return pin

    @staticmethod
    def _validate_stream_key(key: Any) -> str:
        if not isinstance(key, str) or not (8 <= len(key) <= 256) or not _STREAM_KEY_RE.fullmatch(key.strip()):
            raise ProfileError("Stream key không hợp lệ.", status=400)
        return key.strip()

    def _reserve_rate_limit(self, phone: str | None, ip: str) -> None:
        self._ensure_initialized()
        now = self.clock()
        cutoff = now - _RATE_LIMIT_WINDOW_SECONDS

        ip_key = f"ip:{hashlib.sha256(ip.encode('utf-8')).hexdigest()}" if ip else None
        phone_key = f"phone:{hashlib.sha256(phone.encode('utf-8')).hexdigest()}" if phone else None

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            cur = conn.cursor()
            cur.execute("DELETE FROM attempts WHERE timestamp < ?;", (cutoff,))

            if ip_key:
                cur.execute("SELECT COUNT(*) FROM attempts WHERE key = ? AND timestamp >= ?;", (ip_key, cutoff))
                ip_count = cur.fetchone()[0]
                if ip_count >= _RATE_LIMIT_MAX_IP:
                    conn.execute("COMMIT;")
                    raise ProfileError("Quá nhiều lần thử từ địa chỉ này. Vui lòng thử lại sau.", status=429)

            if phone_key:
                cur.execute("SELECT COUNT(*) FROM attempts WHERE key = ? AND timestamp >= ?;", (phone_key, cutoff))
                phone_count = cur.fetchone()[0]
                if phone_count >= _RATE_LIMIT_MAX_PHONE:
                    if ip_key:
                        cur.execute("INSERT INTO attempts (key, timestamp) VALUES (?, ?);", (ip_key, now))
                    conn.execute("COMMIT;")
                    raise ProfileError("Quá nhiều lần thử cho tài khoản này. Vui lòng thử lại sau.", status=429)

            if ip_key:
                cur.execute("INSERT INTO attempts (key, timestamp) VALUES (?, ?);", (ip_key, now))
            if phone_key:
                cur.execute("INSERT INTO attempts (key, timestamp) VALUES (?, ?);", (phone_key, now))

            conn.execute("COMMIT;")
        except sqlite3.Error:
            try:
                conn.execute("ROLLBACK;")
            except sqlite3.Error:
                pass
            raise ProfileError("Lỗi hệ thống khi kiểm tra giới hạn.", status=503) from None
        finally:
            conn.close()

    def register(self, phone: Any, pin: Any, key: Any, ip: str = "") -> str:
        ip_bounded = ip.strip()[:128] if isinstance(ip, str) and ip else ""

        try:
            clean_phone = self._normalize_phone(phone)
            clean_pin = self._validate_pin(pin)
            clean_key = self._validate_stream_key(key)
        except ProfileError:
            if ip_bounded:
                self._reserve_rate_limit(phone=None, ip=ip_bounded)
            raise

        self._reserve_rate_limit(phone=clean_phone, ip=ip_bounded)

        pin_hash = generate_password_hash(clean_pin, method="pbkdf2:sha256:600000")
        try:
            encrypted_key = self._protect_fn(clean_key.encode("utf-8"))
        except ProfileError:
            raise
        except Exception:
            raise ProfileError("Không thể mã hóa dữ liệu stream key.", status=503) from None

        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode("ascii")).hexdigest()
        profile_id = secrets.token_hex(16)
        now = self.clock()
        token_expires = now + _TOKEN_LIFETIME_SECONDS

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO profiles (
                    id, phone, pin_hash, encrypted_key, token_hash, token_expires, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    profile_id,
                    clean_phone,
                    pin_hash,
                    encrypted_key,
                    token_hash,
                    token_expires,
                    now,
                    now,
                ),
            )
            conn.execute("COMMIT;")
            return raw_token
        except sqlite3.IntegrityError as err:
            try:
                conn.execute("ROLLBACK;")
            except sqlite3.Error:
                pass

            err_text = str(err).lower()
            if "phone" in err_text or "unique" in err_text:
                check_conn = self._connect()
                try:
                    c = check_conn.cursor()
                    c.execute("SELECT 1 FROM profiles WHERE phone = ?;", (clean_phone,))
                    if c.fetchone():
                        raise ProfileError(
                            "Không thể đăng ký thông tin này. Nếu đã có tài khoản, hãy đăng nhập.",
                            status=409,
                        )
                finally:
                    check_conn.close()

            raise ProfileError("Lỗi hệ thống khi lưu tài khoản.", status=503) from None
        except sqlite3.Error:
            try:
                conn.execute("ROLLBACK;")
            except sqlite3.Error:
                pass
            raise ProfileError("Lỗi hệ thống khi lưu tài khoản.", status=503) from None
        finally:
            conn.close()

    def login(
        self,
        phone: Any,
        pin: Any,
        ip: str = "",
        before_rotate: Callable[[str], None] | None = None,
    ) -> str:
        ip_bounded = ip.strip()[:128] if isinstance(ip, str) and ip else ""

        try:
            clean_phone = self._normalize_phone(phone)
            clean_pin = self._validate_pin(pin)
        except ProfileError:
            if ip_bounded:
                self._reserve_rate_limit(phone=None, ip=ip_bounded)
            raise

        self._reserve_rate_limit(phone=clean_phone, ip=ip_bounded)

        conn = self._connect()
        row = None
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, pin_hash, encrypted_key, token_hash FROM profiles WHERE phone = ?;",
                (clean_phone,),
            )
            row = cur.fetchone()
        except sqlite3.Error:
            raise ProfileError("Lỗi hệ thống khi đọc tài khoản.", status=503) from None
        finally:
            conn.close()

        if row is None:
            check_password_hash(_DUMMY_PIN_HASH, clean_pin)
            raise ProfileError("Thông tin đăng nhập không chính xác.", status=401)

        profile_id, stored_pin_hash, stored_encrypted_key = row[0], row[1], row[2]
        stored_token_hash = row[3]
        if not check_password_hash(stored_pin_hash, clean_pin):
            raise ProfileError("Thông tin đăng nhập không chính xác.", status=401)

        if not isinstance(stored_encrypted_key, (bytes, bytearray)):
            raise ProfileError("Dữ liệu tài khoản bị lỗi.", status=503)

        try:
            decrypted_bytes = self._unprotect_fn(stored_encrypted_key)
            decrypted_key = decrypted_bytes.decode("utf-8")
        except ProfileError:
            raise
        except Exception:
            raise ProfileError("Lỗi giải mã thông tin tài khoản.", status=503) from None

        if not _STREAM_KEY_RE.fullmatch(decrypted_key):
            raise ProfileError("Dữ liệu tài khoản bị lỗi.", status=503)

        # The caller holds the manager lock; stopping streams must not hold a DB transaction.
        if before_rotate is not None:
            try:
                before_rotate(profile_id)
            except Exception:
                raise ProfileError("Không thể cập nhật phiên đăng nhập.", status=503) from None

        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode("ascii")).hexdigest()
        now = self.clock()
        token_expires = now + _TOKEN_LIFETIME_SECONDS

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE profiles
                SET token_hash = ?, token_expires = ?, updated_at = ?
                WHERE id = ? AND pin_hash = ? AND token_hash IS ?;
                """,
                (token_hash, token_expires, now, profile_id, stored_pin_hash, stored_token_hash),
            )
            if cur.rowcount != 1:
                conn.execute("ROLLBACK;")
                raise ProfileError("Thông tin đăng nhập không chính xác.", status=401)
            conn.execute("COMMIT;")
            return raw_token
        except sqlite3.Error:
            try:
                conn.execute("ROLLBACK;")
            except sqlite3.Error:
                pass
            raise ProfileError("Lỗi hệ thống khi cập nhật phiên đăng nhập.", status=503) from None
        finally:
            conn.close()

    def authenticate(self, token: Any) -> dict[str, str | float] | None:
        """Return backend-only credentials and authorization metadata, never a public payload."""
        token_hash = self._token_hash(token)
        if token_hash is None:
            return None

        self._ensure_initialized()
        now = self.clock()

        conn = self._connect()
        row = None
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, phone, encrypted_key, token_expires FROM profiles WHERE token_hash = ?;",
                (token_hash,),
            )
            row = cur.fetchone()
        except sqlite3.Error:
            raise ProfileError("Lỗi hệ thống khi xác thực phiên.", status=503) from None
        finally:
            conn.close()

        if row is None:
            return None

        pid, phone, encrypted_key, expires = row[0], row[1], row[2], row[3]
        if type(expires) not in (int, float) or not math.isfinite(expires):
            raise ProfileError("Dữ liệu tài khoản bị lỗi.", status=503)
        expires = float(expires)
        if expires <= now:
            return None

        if not isinstance(encrypted_key, (bytes, bytearray)):
            raise ProfileError("Dữ liệu tài khoản bị lỗi.", status=503)

        try:
            decrypted_bytes = self._unprotect_fn(encrypted_key)
            stream_key = decrypted_bytes.decode("utf-8")
        except ProfileError:
            raise
        except Exception:
            raise ProfileError("Lỗi giải mã thông tin tài khoản.", status=503) from None

        if not _STREAM_KEY_RE.fullmatch(stream_key):
            raise ProfileError("Dữ liệu stream key bị lỗi.", status=503)

        return {
            "id": str(pid),
            "phone": str(phone),
            "stream_key": stream_key,
            "authorization": token_hash,
            "expires": expires,
        }

    def authorized(self, profile_id: Any, authorization: Any) -> bool:
        """Check a server-only generation without decrypting the stored stream key."""
        if (
            type(profile_id) is not str
            or len(profile_id) != 32
            or not re.fullmatch(r"[0-9a-f]{32}", profile_id)
            or type(authorization) is not str
            or len(authorization) != 64
            or not re.fullmatch(r"[0-9a-fA-F]{64}", authorization)
        ):
            return False

        self._ensure_initialized()
        now = self.clock()
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT token_expires FROM profiles
                WHERE id = ? AND token_hash = ? AND token_expires > ?;
                """,
                (profile_id, authorization, now),
            ).fetchone()
        except sqlite3.Error:
            raise ProfileError("Lỗi hệ thống khi xác thực phiên.", status=503) from None
        finally:
            conn.close()

        if row is None:
            return False
        expires = row[0]
        if type(expires) not in (int, float) or not math.isfinite(expires):
            raise ProfileError("Dữ liệu tài khoản bị lỗi.", status=503)
        return expires > now

    def update(self, token: Any, key: Any) -> dict[str, str]:
        token_hash = self._token_hash(token)
        if token_hash is None:
            raise ProfileError("Chưa xác thực hoặc phiên đã hết hạn.", status=401)

        clean_key = self._validate_stream_key(key)
        self._ensure_initialized()

        current_profile = self.authenticate(token)
        if current_profile is None:
            raise ProfileError("Chưa xác thực hoặc phiên đã hết hạn.", status=401)

        try:
            encrypted_key = self._protect_fn(clean_key.encode("utf-8"))
        except ProfileError:
            raise
        except Exception:
            raise ProfileError("Không thể mã hóa stream key mới.", status=503) from None

        now = self.clock()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE profiles
                SET encrypted_key = ?, updated_at = ?
                WHERE token_hash = ? AND token_expires > ?;
                """,
                (encrypted_key, now, token_hash, now),
            )
            if cur.rowcount == 0:
                conn.execute("ROLLBACK;")
                raise ProfileError("Chưa xác thực hoặc phiên đã hết hạn.", status=401)
            conn.execute("COMMIT;")
        except sqlite3.Error:
            try:
                conn.execute("ROLLBACK;")
            except sqlite3.Error:
                pass
            raise ProfileError("Lỗi hệ thống khi cập nhật stream key.", status=503) from None
        finally:
            conn.close()

        return {
            "id": current_profile["id"],
            "phone": current_profile["phone"],
            "stream_key": clean_key,
        }

    def revoke(self, token: Any) -> None:
        token_hash = self._token_hash(token)
        if token_hash is None:
            return

        self._ensure_initialized()
        now = self.clock()

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE profiles
                SET token_hash = NULL, token_expires = NULL, updated_at = ?
                WHERE token_hash = ?;
                """,
                (now, token_hash),
            )
            conn.execute("COMMIT;")
        except sqlite3.Error:
            try:
                conn.execute("ROLLBACK;")
            except sqlite3.Error:
                pass
            raise ProfileError("Lỗi hệ thống khi thu hồi phiên.", status=503) from None
        finally:
            conn.close()

    def delete(self, token: Any) -> None:
        token_hash = self._token_hash(token)
        if token_hash is None:
            raise ProfileError("Chưa xác thực hoặc phiên đã hết hạn.", status=401)

        self._ensure_initialized()
        now = self.clock()

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            cur = conn.cursor()
            cur.execute(
                """
                DELETE FROM profiles
                WHERE token_hash = ? AND token_expires > ?;
                """,
                (token_hash, now),
            )
            if cur.rowcount == 0:
                conn.execute("ROLLBACK;")
                raise ProfileError("Chưa xác thực hoặc phiên đã hết hạn.", status=401)
            conn.execute("COMMIT;")
        except sqlite3.Error:
            try:
                conn.execute("ROLLBACK;")
            except sqlite3.Error:
                pass
            raise ProfileError("Lỗi hệ thống khi xóa tài khoản.", status=503) from None
        finally:
            conn.close()
