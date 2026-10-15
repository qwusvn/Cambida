"""Isolated guest Facebook Live regressions: no cameras, no Facebook calls."""
import os
import secrets
import sqlite3
import subprocess
import threading
import time
from typing import Optional

import pytest
from flask import Flask, jsonify
from werkzeug.security import check_password_hash

from services.facebook_live import (
    GuestFacebookLive,
    facebook_target,
    ffmpeg_command,
    install_facebook_live,
)
from services.facebook_profiles import (
    GuestLiveProfileStore,
    ProfileError,
)


class FakeProcess:
    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.active = True
        self.terminated = False

    def poll(self):
        return None if self.active else 0

    def terminate(self):
        self.terminated = True
        self.active = False

    def kill(self):
        self.terminate()

    def wait(self, timeout=None):
        return 0


class StubbornFakeProcess(FakeProcess):
    def __init__(self, argv, **kwargs):
        super().__init__(argv, **kwargs)
        self.stubborn = True

    def terminate(self):
        if self.stubborn:
            raise OSError("Stubborn process cannot terminate")
        super().terminate()

    def kill(self):
        if self.stubborn:
            raise OSError("Stubborn process cannot be killed")
        super().kill()


def fake_protect(data: bytes) -> bytes:
    """Deterministic reversible encryption wrapper for test assertions."""
    return b"ENC:" + bytes(b ^ 0x5A for b in data)


def fake_unprotect(data: bytes) -> bytes:
    if not data.startswith(b"ENC:"):
        raise ValueError("Malformed ciphertext")
    return bytes(b ^ 0x5A for b in data[4:])


def test_facebook_egress_allowlist():
    key = "FB-abcdefghijk987"
    assert facebook_target("rtmps://live-api-s.facebook.com:443/rtmp/", key).endswith("/" + key)
    assert facebook_target("rtmps://rtmp-api.facebook.com/rtmp", key).startswith("rtmps://")
    bad_urls = (
        "rtmp://live-api-s.facebook.com/rtmp/",
        "rtmps://evil-facebook.com/rtmp/",
        "rtmps://live-api-s.facebook.com.evil.com/rtmp/",
        "rtmps://live-api-s.facebook.com:444/rtmp/",
        "rtmps://user:password@live-api-s.facebook.com/rtmp/",
        "rtmps://live-api-s.facebook.com/rtmp?redirect=evil",
        "rtmps://live-api-s.facebook.com/other/",
        "file:///etc/passwd",
    )
    for url in bad_urls:
        with pytest.raises(ValueError):
            facebook_target(url, key)
    for key in ["bad", "A/B", "abc;something", "A" * 500]:
        with pytest.raises(ValueError):
            facebook_target("rtmps://live-api-s.facebook.com/rtmp/", key)


def test_ffmpeg_params_have_video_and_silent_audio():
    args = ffmpeg_command("ffmpeg.exe", "rtsp://camera/stream",
                          "rtmps://live-api-s.facebook.com/rtmp/FB-abc12345")
    assert args[args.index("-i") + 1] == "rtsp://camera/stream"
    assert "anullsrc=channel_layout=stereo:sample_rate=44100" in args
    assert "-g" in args and "60" in args
    assert args[-2] == "flv"
    assert args[-1].startswith("rtmps://")


def test_manager_limits_ownership_expiry_and_privacy():
    now = [100.0]
    available = [True]
    enabled = [True]
    created = []
    def popen(argv, **opts):
        result = FakeProcess(argv, **opts)
        created.append(result)
        return result
    manager = GuestFacebookLive(
        lambda: enabled[0], lambda cam: available[0],
        lambda cam: "rtsp://camera.local/cam", "ffmpeg.exe",
        popen=popen, clock=lambda: now[0], monitor=False,
    )
    server = "rtmps://live-api-s.facebook.com/rtmp/"
    key1 = "FB-test123456781"
    key2 = "FB-test123456782"
    key3 = "FB-test123456783"

    assert manager.start(1, "person1", server, key1)[1] == 200
    assert len(created) == 1
    assert manager.status(1, "person2")["state"] == "busy"
    assert manager.stop(1, "person2")[1] == 403
    assert manager.start(1, "person2", server, key1)[1] == 409
    # Duplicate key across cams is refused, so cam 2 and cam 3 use distinct keys
    assert manager.start(2, "person2", server, key2)[1] == 200
    assert manager.start(3, "person3", server, key3)[1] == 200
    assert manager.stop(1, "person1")[1] == 200
    assert created[0].terminated
    now[0] = 100 + 3601
    assert manager.status(2, "person2")["state"] == "idle"
    assert created[1].terminated
    assert manager.start(1, "person1", server, key1)[1] == 200
    available[0] = False
    assert manager.status(1, "person1")["state"] == "idle"
    assert created[-1].terminated
    available[0] = True
    assert manager.start(1, "person1", server, key1)[1] == 200
    enabled[0] = False
    assert manager.status(1, "person1")["state"] == "idle"
    assert created[-1].terminated


def test_unsupported_netsdk_and_no_key_in_messages():
    manager = GuestFacebookLive(lambda: True, lambda c: True,
                                lambda c: None, "ffmpeg.exe", monitor=False)
    output, code = manager.start(1, "test", "rtmps://live-api-s.facebook.com/rtmp/",
                                 "FB-test123456789")
    assert code == 422
    assert "FB-test" not in str(output)


def test_flask_session_nonce_origin_https_and_opt_in():
    app = Flask(__name__)
    app.secret_key = "isolated-test-secret"
    enabled = [False]
    allowed = [True]
    tls = [True]
    created = []
    def popen(cmd, **opts):
        created.append(FakeProcess(cmd, **opts))
        return created[-1]
    manager = install_facebook_live(
        app, lambda: enabled[0], lambda c: c == 1 and allowed[0],
        lambda c: "rtsp://internal/camera/1", "ffmpeg.exe",
        lambda: tls[0], popen=popen, monitor=False,
    )

    @app.get("/replay/cam<int:cam_id>")
    def page(cam_id):
        return jsonify(nonce=manager.issue_nonce(cam_id))

    first = app.test_client()
    second = app.test_client()
    nonce = first.get("/replay/cam1", base_url="https://shop.example").get_json()["nonce"]
    key = "FB-this-is-a-test123"
    payload = {"server": "rtmps://live-api-s.facebook.com:443/rtmp/", "key": key, "consent": True}
    def submit(client=first, cam=1, headers=None):
        h = {"X-CSRF-Token": nonce, "Origin": "https://shop.example"}
        h.update(headers or {})
        return client.post(f"/api/facebook-live/cam{cam}/start", json=payload,
                           base_url="https://shop.example", headers=h)

    assert submit().status_code == 403  # opt-in off
    enabled[0] = True
    assert submit(headers={"X-CSRF-Token": "bad"}).status_code == 403
    assert submit(headers={"Origin": "https://attacker.example"}).status_code == 403
    assert submit(client=second).status_code == 403
    tls[0] = False
    assert submit().status_code == 403
    tls[0] = True
    payload.pop("consent")
    assert submit().status_code == 400
    payload["consent"] = True
    assert submit().status_code == 200
    assert len(created) == 1
    status = first.get("/api/facebook-live/cam1/status", base_url="https://shop.example").get_json()
    assert status["state"] == "running"
    assert key not in str(status)
    assert "rtsp://" not in str(status)
    assert second.get("/api/facebook-live/cam1/status", base_url="https://shop.example").get_json()["state"] == "busy"
    allowed[0] = False
    assert first.get("/api/facebook-live/cam1/status", base_url="https://shop.example").status_code == 403
    assert manager.status(1, "anything")["state"] == "idle"
    assert created[0].terminated
    allowed[0] = True
    assert submit().status_code == 200
    wrong_nonce = second.get("/replay/cam1", base_url="https://shop.example").get_json()["nonce"]
    assert second.post("/api/facebook-live/cam1/stop",
                       base_url="https://shop.example",
                       headers={"X-CSRF-Token": wrong_nonce,
                                "Origin": "https://shop.example"}).status_code == 403
    assert first.post("/api/facebook-live/cam1/stop",
                       base_url="https://shop.example",
                       headers={"X-CSRF-Token": nonce,
                                "Origin": "https://shop.example"}).status_code == 200
    assert created[-1].terminated


def test_fanpage_1touch_mode_and_single_stream_limit():
    app = Flask(__name__)
    app.secret_key = "test-fanpage-secret"
    page_cfg = {
        "stream_key": "FB-persistent-page-key-999",
        "page_url": "https://facebook.com/athenabilliards",
        "server_url": "rtmps://live-api-s.facebook.com:443/rtmp/",
        "max_duration_minutes": 60,
    }
    created = []
    def popen(cmd, **opts):
        created.append(FakeProcess(cmd, **opts))
        return created[-1]

    manager = install_facebook_live(
        app,
        enabled=lambda: True,
        allowed=lambda c: True,
        get_source=lambda c: f"rtsp://internal/cam/{c}",
        ffmpeg_path="ffmpeg.exe",
        secure=lambda: True,
        get_page_config=lambda: page_cfg,
        popen=popen,
        monitor=False,
    )

    @app.get("/replay/cam<int:cam_id>")
    def page(cam_id):
        return jsonify(nonce=manager.issue_nonce(cam_id))

    c1 = app.test_client()
    c2 = app.test_client()
    nonce1 = c1.get("/replay/cam1", base_url="https://shop.example").get_json()["nonce"]
    nonce2 = c2.get("/replay/cam2", base_url="https://shop.example").get_json()["nonce"]

    res1 = c1.post(
        "/api/facebook-live/cam1/start",
        json={"consent": True},
        base_url="https://shop.example",
        headers={"X-CSRF-Token": nonce1, "Origin": "https://shop.example"},
    )
    assert res1.status_code == 200
    data1 = res1.get_json()
    assert data1["ok"] is True
    assert data1["page_url"] == "https://facebook.com/athenabilliards"
    assert "FB-persistent" not in str(data1)

    res2 = c2.post(
        "/api/facebook-live/cam2/start",
        json={"consent": True},
        base_url="https://shop.example",
        headers={"X-CSRF-Token": nonce2, "Origin": "https://shop.example"},
    )
    assert res2.status_code == 409
    assert "đang có bàn sử dụng" in res2.get_json()["error"]

    s2 = c2.get("/api/facebook-live/cam2/status", base_url="https://shop.example").get_json()
    assert s2["state"] == "busy"
    assert "đang có bàn sử dụng" in s2.get("busy_message", "")

    stop_res = c1.post(
        "/api/facebook-live/cam1/stop",
        json={},
        base_url="https://shop.example",
        headers={"X-CSRF-Token": nonce1, "Origin": "https://shop.example"},
    )
    assert stop_res.status_code == 200
    assert created[0].terminated

    res2_after = c2.post(
        "/api/facebook-live/cam2/start",
        json={"consent": True},
        base_url="https://shop.example",
        headers={"X-CSRF-Token": nonce2, "Origin": "https://shop.example"},
    )
    assert res2_after.status_code == 200


def test_fanpage_stream_pool_allocation_and_release():
    app = Flask(__name__)
    app.secret_key = "test-pool-secret"
    pool_cfg = {
        "slots": [
            {"id": "slot1", "name": "Fanpage A", "stream_key": "FB-key-page-A", "page_url": "https://fb.com/pageA"},
            {"id": "slot2", "name": "Fanpage B", "stream_key": "FB-key-page-B", "page_url": "https://fb.com/pageB"},
        ],
        "server_url": "rtmps://live-api-s.facebook.com:443/rtmp/",
        "max_duration_minutes": 60,
    }
    created = []
    def popen(cmd, **opts):
        created.append(FakeProcess(cmd, **opts))
        return created[-1]

    manager = install_facebook_live(
        app,
        enabled=lambda: True,
        allowed=lambda c: True,
        get_source=lambda c: f"rtsp://internal/cam/{c}",
        ffmpeg_path="ffmpeg.exe",
        secure=lambda: True,
        get_page_config=lambda: pool_cfg,
        popen=popen,
        monitor=False,
    )

    @app.get("/replay/cam<int:cam_id>")
    def page(cam_id):
        return jsonify(nonce=manager.issue_nonce(cam_id))

    c1 = app.test_client()
    c2 = app.test_client()
    c3 = app.test_client()
    nonce1 = c1.get("/replay/cam1", base_url="https://shop.example").get_json()["nonce"]
    nonce2 = c2.get("/replay/cam2", base_url="https://shop.example").get_json()["nonce"]
    nonce3 = c3.get("/replay/cam3", base_url="https://shop.example").get_json()["nonce"]

    r1 = c1.post("/api/facebook-live/cam1/start", json={"consent": True},
                 base_url="https://shop.example", headers={"X-CSRF-Token": nonce1, "Origin": "https://shop.example"})
    assert r1.status_code == 200
    d1 = r1.get_json()
    assert d1["page_url"] == "https://fb.com/pageA"
    assert d1["slot_name"] == "Fanpage A"

    r2 = c2.post("/api/facebook-live/cam2/start", json={"consent": True},
                 base_url="https://shop.example", headers={"X-CSRF-Token": nonce2, "Origin": "https://shop.example"})
    assert r2.status_code == 200
    d2 = r2.get_json()
    assert d2["page_url"] == "https://fb.com/pageB"
    assert d2["slot_name"] == "Fanpage B"

    assert len(created) == 2
    assert created[0].active and created[1].active

    r3 = c3.post("/api/facebook-live/cam3/start", json={"consent": True},
                 base_url="https://shop.example", headers={"X-CSRF-Token": nonce3, "Origin": "https://shop.example"})
    assert r3.status_code == 409
    assert "đang có bàn sử dụng" in r3.get_json()["error"]

    s3 = c3.get("/api/facebook-live/cam3/status", base_url="https://shop.example").get_json()
    assert s3["state"] == "busy"

    c1.post("/api/facebook-live/cam1/stop", json={},
            base_url="https://shop.example", headers={"X-CSRF-Token": nonce1, "Origin": "https://shop.example"})
    assert created[0].terminated

    r3_after = c3.post("/api/facebook-live/cam3/start", json={"consent": True},
                       base_url="https://shop.example", headers={"X-CSRF-Token": nonce3, "Origin": "https://shop.example"})
    assert r3_after.status_code == 200
    d3 = r3_after.get_json()
    assert d3["page_url"] == "https://fb.com/pageA"
    assert d3["slot_name"] == "Fanpage A"


def test_fanpage_missing_key_returns_422():
    app = Flask(__name__)
    app.secret_key = "test-missing-key"
    manager = install_facebook_live(
        app,
        enabled=lambda: True,
        allowed=lambda c: True,
        get_source=lambda c: f"rtsp://internal/cam/{c}",
        ffmpeg_path="ffmpeg.exe",
        secure=lambda: True,
        get_page_config=lambda: {"slots": []},
        monitor=False,
    )

    @app.get("/replay/cam<int:cam_id>")
    def page(cam_id):
        return jsonify(nonce=manager.issue_nonce(cam_id))

    c = app.test_client()
    nonce = c.get("/replay/cam1", base_url="https://shop.example").get_json()["nonce"]
    res = c.post(
        "/api/facebook-live/cam1/start",
        json={"consent": True},
        base_url="https://shop.example",
        headers={"X-CSRF-Token": nonce, "Origin": "https://shop.example"},
    )
    assert res.status_code == 422
    assert "chưa cấu hình Stream Key" in res.get_json()["error"]


# ============================================================================
# GuestLiveProfileStore Unit & Resilience Tests
# ============================================================================


def test_profile_store_roundtrip_reopen_and_token_auth(tmp_path):
    db_file = str(tmp_path / "profiles.sqlite3")
    store = GuestLiveProfileStore(
        db_file,
        protect=fake_protect,
        unprotect=fake_unprotect,
    )
    token = store.register("0912345678", "1234", "FB-profile-streamkey-1")
    assert isinstance(token, str) and len(token) > 20

    auth = store.authenticate(token)
    assert auth is not None
    assert auth.get("phone") == "0912345678"
    assert auth.get("stream_key") == "FB-profile-streamkey-1"

    # Reopen database with fresh instance
    store2 = GuestLiveProfileStore(
        db_file,
        protect=fake_protect,
        unprotect=fake_unprotect,
    )
    auth2 = store2.authenticate(token)
    assert auth2 is not None
    assert auth2.get("phone") == "0912345678"
    assert auth2.get("stream_key") == "FB-profile-streamkey-1"


def test_profile_store_no_plaintext_and_version(tmp_path):
    db_file = str(tmp_path / "profiles.sqlite3")
    raw_key = "FB-secret-plaintext-key-xyz"
    raw_pin = "4321"
    store = GuestLiveProfileStore(
        db_file,
        protect=fake_protect,
        unprotect=fake_unprotect,
    )
    store.register("0987654321", raw_pin, raw_key)

    with open(db_file, "rb") as f:
        data = f.read()
    assert raw_key.encode("utf-8") not in data

    conn = sqlite3.connect(db_file)
    row = conn.execute("SELECT encrypted_key, pin_hash, token_hash FROM profiles WHERE phone = '0987654321'").fetchone()
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    conn.close()
    assert row is not None
    assert row[0] != raw_key.encode("utf-8")
    assert row[1] != raw_pin
    assert check_password_hash(row[1], raw_pin)
    assert version >= 1


def test_profile_store_phone_normalization_and_validation(tmp_path):
    db_file = str(tmp_path / "norm.sqlite3")
    store = GuestLiveProfileStore(
        db_file,
        protect=fake_protect,
        unprotect=fake_unprotect,
    )
    # +84 and 84 prefix normalized to 0
    tok1 = store.register("+84912345678", "1234", "FB-key-norm-1", ip="10.0.0.1")
    auth1 = store.authenticate(tok1)
    assert auth1 is not None and auth1["phone"] == "0912345678"

    # Registering 84912345678 should collide with 0912345678 (409 Conflict)
    with pytest.raises(ProfileError) as exc_info:
        store.register("84912345678", "1234", "FB-key-norm-2", ip="10.0.0.2")
    assert exc_info.value.status == 409

    # Phone digits < 10 or > 15
    with pytest.raises(ProfileError) as exc_short:
        store.register("12345", "1234", "FB-key-valid-1", ip="10.0.0.3")
    assert exc_short.value.status == 400

    # Non 4-digit PIN
    with pytest.raises(ProfileError) as exc_pin:
        store.register("0933333333", "12", "FB-key-valid-1", ip="10.0.0.4")
    assert exc_pin.value.status == 400


def test_profile_store_duplicate_register_and_login_invalid(tmp_path):
    db_file = str(tmp_path / "auth.sqlite3")
    store = GuestLiveProfileStore(
        db_file,
        protect=fake_protect,
        unprotect=fake_unprotect,
    )
    store.register("0900000001", "1234", "FB-key-auth-1", ip="10.1.1.1")

    # Duplicate registration -> 409
    with pytest.raises(ProfileError) as exc_dup:
        store.register("0900000001", "1234", "FB-key-auth-2", ip="10.1.1.2")
    assert exc_dup.value.status == 409

    # Wrong PIN -> 401
    with pytest.raises(ProfileError) as exc_pin:
        store.login("0900000001", "9999", ip="10.1.1.3")
    assert exc_pin.value.status == 401

    # Non-existent phone -> 401
    with pytest.raises(ProfileError) as exc_none:
        store.login("0999999999", "1234", ip="10.1.1.4")
    assert exc_none.value.status == 401

    # Invalid token -> None
    assert store.authenticate("random-non-existent-token") is None


def test_profile_store_token_rotation_update_revoke_delete(tmp_path):
    db_file = str(tmp_path / "lifecycle.sqlite3")
    store = GuestLiveProfileStore(
        db_file,
        protect=fake_protect,
        unprotect=fake_unprotect,
    )
    tok1 = store.register("0911223344", "1234", "FB-key-lifecycle-1", ip="10.2.1.1")
    assert store.authenticate(tok1) is not None

    # Login rotates token and invalidates old token
    tok2 = store.login("0911223344", "1234", ip="10.2.1.2")
    assert tok2 != tok1
    assert store.authenticate(tok1) is None
    assert store.authenticate(tok2) is not None

    # Update key
    store.update(tok2, "FB-key-lifecycle-updated")
    auth_updated = store.authenticate(tok2)
    assert auth_updated["stream_key"] == "FB-key-lifecycle-updated"

    # Revoke (logout)
    store.revoke(tok2)
    assert store.authenticate(tok2) is None

    # Login again to test delete
    tok3 = store.login("0911223344", "1234", ip="10.2.1.3")
    assert store.authenticate(tok3) is not None
    store.delete(tok3)
    assert store.authenticate(tok3) is None

    # Phone is now completely deleted; registration succeeds again
    tok4 = store.register("0911223344", "5678", "FB-key-lifecycle-new", ip="10.2.1.4")
    assert tok4 is not None


def test_profile_store_expiry_90days(tmp_path):
    db_file = str(tmp_path / "expiry.sqlite3")
    now = [1_000_000.0]
    store = GuestLiveProfileStore(
        db_file,
        protect=fake_protect,
        unprotect=fake_unprotect,
        clock=lambda: now[0],
    )
    tok = store.register("0955555555", "1234", "FB-key-expiry", ip="10.3.1.1")
    assert store.authenticate(tok) is not None

    # 89 days: still active
    now[0] += 89 * 86400
    assert store.authenticate(tok) is not None

    # > 90 days: expired -> returns None
    now[0] += 2 * 86400
    assert store.authenticate(tok) is None


def test_profile_store_corrupt_failclosed_and_rollback(tmp_path):
    db_file = str(tmp_path / "corrupt.sqlite3")
    with open(db_file, "wb") as f:
        f.write(b"NOT A VALID SQLITE DATABASE HEADER CORRUPTED FILE")

    store = GuestLiveProfileStore(
        db_file,
        protect=fake_protect,
        unprotect=fake_unprotect,
    )
    with pytest.raises(ProfileError) as exc_info:
        store.register("0944444444", "1234", "FB-key-corrupt")
    assert exc_info.value.status == 503

    # Test encryption failure rollback safety
    db_clean = str(tmp_path / "clean.sqlite3")
    def failing_protect(data: bytes) -> bytes:
        raise RuntimeError("Crypto provider failure")

    store_fail = GuestLiveProfileStore(
        db_clean,
        protect=failing_protect,
        unprotect=fake_unprotect,
    )
    with pytest.raises(ProfileError) as exc_fail:
        store_fail.register("0944444444", "1234", "FB-key-crypto-fail")
    assert exc_fail.value.status == 503


def test_profile_store_throttling_phone_and_ip(tmp_path):
    db_file = str(tmp_path / "throttle.sqlite3")
    now = [1000.0]
    store = GuestLiveProfileStore(
        db_file,
        protect=fake_protect,
        unprotect=fake_unprotect,
        clock=lambda: now[0],
    )
    phone = "0977777777"
    ip = "192.168.10.50"

    # Initial register (attempt 1)
    store.register(phone, "1234", "FB-key-throttle", ip=ip)

    # 4 invalid login attempts (attempts 2, 3, 4, 5)
    for _ in range(4):
        with pytest.raises(ProfileError) as exc:
            store.login(phone, "0000", ip=ip)
        assert exc.value.status == 401

    # 6th attempt within 15 minutes exceeds 5 attempts per phone -> 429
    with pytest.raises(ProfileError) as exc_throttled:
        store.login(phone, "1234", ip=ip)
    assert exc_throttled.value.status == 429

    # Advance clock past 15 minutes (901 seconds) -> rate window resets
    now[0] += 901
    tok = store.login(phone, "1234", ip=ip)
    assert tok is not None


def test_profile_store_concurrent_writes(tmp_path):
    db_file = str(tmp_path / "concurrent.sqlite3")
    store = GuestLiveProfileStore(
        db_file,
        protect=fake_protect,
        unprotect=fake_unprotect,
    )
    errors = []

    def worker(idx):
        try:
            phone = f"09800000{idx:02d}"
            ip = f"10.0.1.{idx}"
            tok = store.register(phone, "1234", f"FB-concurrent-key-{idx}", ip=ip)
            auth = store.authenticate(tok)
            if not auth or auth["phone"] != phone:
                errors.append(f"Mismatch for worker {idx}")
        except Exception as e:
            errors.append(str(e))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors


# ============================================================================
# Manager Duplicate Key Refusal & Global Session Cap
# ============================================================================


def test_manager_duplicate_key_refusal_across_modes_and_cams():
    manager = GuestFacebookLive(
        lambda: True, lambda cam: True,
        lambda cam: "rtsp://camera.local/cam", "ffmpeg.exe",
        popen=lambda argv, **opts: FakeProcess(argv, **opts),
        monitor=False,
    )
    server = "rtmps://live-api-s.facebook.com:443/rtmp/"
    key = "FB-duplicate-test-key-1"

    # Cam 1 starts with key
    res1, code1 = manager.start(1, "person1", server, key)
    assert code1 == 200

    # Cam 2 attempts to start with identical key -> 409 duplicate refusal
    res2, code2 = manager.start(2, "person2", server, key)
    assert code2 == 409
    assert "error" in res2


def test_manager_global_session_cap_all_modes():
    manager = GuestFacebookLive(
        lambda: True, lambda cam: True,
        lambda cam: "rtsp://camera.local/cam", "ffmpeg.exe",
        popen=lambda argv, **opts: FakeProcess(argv, **opts),
        monitor=False,
    )
    server = "rtmps://live-api-s.facebook.com:443/rtmp/"

    # Start 6 active streams with distinct keys (max capacity)
    for cam in range(1, 7):
        res, code = manager.start(cam, f"owner{cam}", server, f"FB-cap-key-{cam}")
        assert code == 200

    # 7th stream exceeds global 6 cap -> 429
    res7, code7 = manager.start(7, "owner7", server, "FB-cap-key-7")
    assert code7 == 429


# ============================================================================
# Profile Endpoints, Cookie Security, Personal Mode & Interop
# ============================================================================


def test_profile_api_endpoints_status_register_and_cookie_security(tmp_path):
    app = Flask(__name__)
    app.secret_key = "test-profile-api-secret"
    store = GuestLiveProfileStore(
        str(tmp_path / "api_profiles.sqlite3"),
        protect=fake_protect,
        unprotect=fake_unprotect,
    )
    manager = install_facebook_live(
        app,
        enabled=lambda: True,
        allowed=lambda c: True,
        get_source=lambda c: f"rtsp://internal/cam/{c}",
        ffmpeg_path="ffmpeg.exe",
        secure=lambda: True,
        profile_store=store,
        monitor=False,
    )

    @app.get("/replay/cam<int:cam_id>")
    def page(cam_id):
        return jsonify(nonce=manager.issue_nonce(cam_id))

    client = app.test_client()
    nonce = client.get("/replay/cam1", base_url="https://shop.example").get_json()["nonce"]
    headers = {"X-CSRF-Token": nonce, "Origin": "https://shop.example"}

    # Initial status: not authenticated
    st_res = client.post("/api/facebook-live/cam1/profile/status", json={},
                         base_url="https://shop.example", headers=headers)
    assert st_res.status_code == 200
    st_data = st_res.get_json()
    assert st_data.get("authenticated") is False
    assert "token" not in str(st_data)

    # Register profile
    reg_res = client.post(
        "/api/facebook-live/cam1/profile/register",
        json={"phone": "0912345678", "pin": "1234", "key": "FB-personal-key-1"},
        base_url="https://shop.example",
        headers=headers,
    )
    assert reg_res.status_code == 200
    reg_data = reg_res.get_json()
    assert reg_data.get("ok") is True
    assert reg_data.get("authenticated") is True
    # Secrets must NEVER be in response body
    assert "FB-personal" not in str(reg_data)
    assert "token" not in reg_data

    # Verify cookie attributes
    cookie_header = reg_res.headers.get("Set-Cookie", "")
    assert "__Secure-cambida_live_device=" in cookie_header
    assert "HttpOnly" in cookie_header
    assert "Secure" in cookie_header
    assert "SameSite=Strict" in cookie_header
    assert "path=/api/facebook-live" in cookie_header.lower()

    # Cache-Control no-store
    assert "no-store" in reg_res.headers.get("Cache-Control", "").lower()

    # Post-register status shows authenticated and has_key
    st_after = client.post("/api/facebook-live/cam1/profile/status", json={},
                           base_url="https://shop.example", headers=headers).get_json()
    assert st_after.get("authenticated") is True
    assert st_after.get("has_key") is True
    assert st_after.get("phone") == "0912345678"
    assert "FB-personal" not in str(st_after)


def test_profile_api_remembered_cookie_on_second_cam(tmp_path):
    app = Flask(__name__)
    app.secret_key = "test-cookie-remember"
    store = GuestLiveProfileStore(
        str(tmp_path / "cookie_profiles.sqlite3"),
        protect=fake_protect,
        unprotect=fake_unprotect,
    )
    manager = install_facebook_live(
        app,
        enabled=lambda: True,
        allowed=lambda c: True,
        get_source=lambda c: f"rtsp://internal/cam/{c}",
        ffmpeg_path="ffmpeg.exe",
        secure=lambda: True,
        profile_store=store,
        monitor=False,
    )

    @app.get("/replay/cam<int:cam_id>")
    def page(cam_id):
        return jsonify(nonce=manager.issue_nonce(cam_id))

    client = app.test_client()
    n1 = client.get("/replay/cam1", base_url="https://shop.example").get_json()["nonce"]
    r_reg = client.post(
        "/api/facebook-live/cam1/profile/register",
        json={"phone": "0988776655", "pin": "9999", "key": "FB-device-remember-key"},
        base_url="https://shop.example",
        headers={"X-CSRF-Token": n1, "Origin": "https://shop.example"},
    )
    assert r_reg.status_code == 200

    # Later replay on cam 2: cookie automatically remembers profile
    n2 = client.get("/replay/cam2", base_url="https://shop.example").get_json()["nonce"]
    st2 = client.post(
        "/api/facebook-live/cam2/profile/status",
        json={},
        base_url="https://shop.example",
        headers={"X-CSRF-Token": n2, "Origin": "https://shop.example"},
    ).get_json()
    assert st2.get("authenticated") is True
    assert st2.get("phone") == "0988776655"


def test_personal_mode_start_and_mutations_stop_streams(tmp_path):
    app = Flask(__name__)
    app.secret_key = "test-personal-mutations"
    created = []
    def popen(cmd, **opts):
        proc = FakeProcess(cmd, **opts)
        created.append(proc)
        return proc

    store = GuestLiveProfileStore(
        str(tmp_path / "mutations.sqlite3"),
        protect=fake_protect,
        unprotect=fake_unprotect,
    )
    manager = install_facebook_live(
        app,
        enabled=lambda: True,
        allowed=lambda c: True,
        get_source=lambda c: f"rtsp://internal/cam/{c}",
        ffmpeg_path="ffmpeg.exe",
        secure=lambda: True,
        profile_store=store,
        popen=popen,
        monitor=False,
    )

    @app.get("/replay/cam<int:cam_id>")
    def page(cam_id):
        return jsonify(nonce=manager.issue_nonce(cam_id))

    c = app.test_client()
    nonce = c.get("/replay/cam1", base_url="https://shop.example").get_json()["nonce"]
    headers = {"X-CSRF-Token": nonce, "Origin": "https://shop.example"}

    # Register
    r_reg = c.post(
        "/api/facebook-live/cam1/profile/register",
        json={"phone": "0911223344", "pin": "1234", "key": "FB-personal-mutations-key"},
        base_url="https://shop.example",
        headers=headers,
    )
    assert r_reg.status_code == 200

    # Personal mode start: {mode: 'personal', consent: True} with no key/server
    res = c.post(
        "/api/facebook-live/cam1/start",
        json={"mode": "personal", "consent": True},
        base_url="https://shop.example",
        headers=headers,
    )
    assert res.status_code == 200
    assert len(created) == 1
    assert created[0].active

    # Mutation: update key stops active personal stream
    up_res = c.post(
        "/api/facebook-live/cam1/profile/update",
        json={"key": "FB-personal-mutations-key-2"},
        base_url="https://shop.example",
        headers=headers,
    )
    assert up_res.status_code == 200
    assert created[0].terminated

    # Restart stream
    res2 = c.post(
        "/api/facebook-live/cam1/start",
        json={"mode": "personal", "consent": True},
        base_url="https://shop.example",
        headers=headers,
    )
    assert res2.status_code == 200
    assert created[1].active

    # Mutation: logout (revoke) stops stream
    logout_res = c.post(
        "/api/facebook-live/cam1/profile/logout",
        json={},
        base_url="https://shop.example",
        headers=headers,
    )
    assert logout_res.status_code == 200
    assert created[1].terminated


def test_personal_mode_unauthorized_and_ambiguity_rejection(tmp_path):
    app = Flask(__name__)
    app.secret_key = "test-personal-unauth"
    manager = install_facebook_live(
        app,
        enabled=lambda: True,
        allowed=lambda c: True,
        get_source=lambda c: f"rtsp://internal/cam/{c}",
        ffmpeg_path="ffmpeg.exe",
        secure=lambda: True,
        profile_store=GuestLiveProfileStore(
            str(tmp_path / "unauth.sqlite3"),
            protect=fake_protect,
            unprotect=fake_unprotect,
        ),
        monitor=False,
    )

    @app.get("/replay/cam<int:cam_id>")
    def page(cam_id):
        return jsonify(nonce=manager.issue_nonce(cam_id))

    c = app.test_client()
    nonce = c.get("/replay/cam1", base_url="https://shop.example").get_json()["nonce"]
    headers = {"X-CSRF-Token": nonce, "Origin": "https://shop.example"}

    # Personal mode without cookie auth -> 401 Unauthorized (no fallback)
    res_unauth = c.post(
        "/api/facebook-live/cam1/start",
        json={"mode": "personal", "consent": True},
        base_url="https://shop.example",
        headers=headers,
    )
    assert res_unauth.status_code == 401

    # Ambiguity rejection: personal mode with manual key/server supplied -> 400
    res_ambiguous = c.post(
        "/api/facebook-live/cam1/start",
        json={"mode": "personal", "consent": True, "key": "FB-ambiguous-key"},
        base_url="https://shop.example",
        headers=headers,
    )
    assert res_ambiguous.status_code == 400


def test_personal_mode_store_error_leaves_fanpage_unaffected(tmp_path):
    app = Flask(__name__)
    app.secret_key = "test-store-error-fanpage"
    corrupt_db = str(tmp_path / "corrupt_store.sqlite3")
    with open(corrupt_db, "wb") as f:
        f.write(b"CORRUPTED DB CONTENT")

    store = GuestLiveProfileStore(
        corrupt_db,
        protect=fake_protect,
        unprotect=fake_unprotect,
    )
    page_cfg = {
        "slots": [{"id": "slot1", "name": "Fanpage A", "stream_key": "FB-fanpage-ok", "page_url": "https://fb.com"}],
        "server_url": "rtmps://live-api-s.facebook.com:443/rtmp/",
    }
    manager = install_facebook_live(
        app,
        enabled=lambda: True,
        allowed=lambda c: True,
        get_source=lambda c: f"rtsp://internal/cam/{c}",
        ffmpeg_path="ffmpeg.exe",
        secure=lambda: True,
        get_page_config=lambda: page_cfg,
        profile_store=store,
        popen=lambda cmd, **opts: FakeProcess(cmd, **opts),
        monitor=False,
    )

    @app.get("/replay/cam<int:cam_id>")
    def page(cam_id):
        return jsonify(nonce=manager.issue_nonce(cam_id))

    c = app.test_client()
    nonce = c.get("/replay/cam1", base_url="https://shop.example").get_json()["nonce"]
    headers = {"X-CSRF-Token": nonce, "Origin": "https://shop.example"}

    # Personal profile endpoint fails with 503
    p_res = c.post(
        "/api/facebook-live/cam1/profile/register",
        json={"phone": "0911111111", "pin": "1234", "key": "FB-test-key"},
        base_url="https://shop.example",
        headers=headers,
    )
    assert p_res.status_code == 503

    # Fanpage 1-touch start continues to function unaffected (200)
    fp_res = c.post(
        "/api/facebook-live/cam1/start",
        json={"consent": True},
        base_url="https://shop.example",
        headers=headers,
    )
    assert fp_res.status_code == 200
    assert fp_res.get_json()["ok"] is True


def test_profile_api_csrf_origin_and_tls_guards(tmp_path):
    app = Flask(__name__)
    app.secret_key = "test-guards"
    tls = [True]
    store = GuestLiveProfileStore(
        str(tmp_path / "guards.sqlite3"),
        protect=fake_protect,
        unprotect=fake_unprotect,
    )
    manager = install_facebook_live(
        app,
        enabled=lambda: True,
        allowed=lambda c: True,
        get_source=lambda c: f"rtsp://internal/cam/{c}",
        ffmpeg_path="ffmpeg.exe",
        secure=lambda: tls[0],
        profile_store=store,
        monitor=False,
    )

    @app.get("/replay/cam<int:cam_id>")
    def page(cam_id):
        return jsonify(nonce=manager.issue_nonce(cam_id))

    c = app.test_client()
    nonce = c.get("/replay/cam1", base_url="https://shop.example").get_json()["nonce"]

    # Missing / bad CSRF nonce -> 403
    r1 = c.post(
        "/api/facebook-live/cam1/profile/status",
        json={},
        base_url="https://shop.example",
        headers={"X-CSRF-Token": "invalid-nonce", "Origin": "https://shop.example"},
    )
    assert r1.status_code == 403

    # Untrusted Origin -> 403
    r2 = c.post(
        "/api/facebook-live/cam1/profile/status",
        json={},
        base_url="https://shop.example",
        headers={"X-CSRF-Token": nonce, "Origin": "https://attacker.example"},
    )
    assert r2.status_code == 403

    # TLS disabled / non-secure -> 403
    tls[0] = False
    r3 = c.post(
        "/api/facebook-live/cam1/profile/status",
        json={},
        base_url="https://shop.example",
        headers={"X-CSRF-Token": nonce, "Origin": "https://shop.example"},
    )
    assert r3.status_code == 403


def test_profile_register_validates_key_via_facebook_target(tmp_path):
    app = Flask(__name__)
    app.secret_key = "test-validate-key"
    store = GuestLiveProfileStore(
        str(tmp_path / "validate_key.sqlite3"),
        protect=fake_protect,
        unprotect=fake_unprotect,
    )
    manager = install_facebook_live(
        app,
        enabled=lambda: True,
        allowed=lambda c: True,
        get_source=lambda c: f"rtsp://internal/cam/{c}",
        ffmpeg_path="ffmpeg.exe",
        secure=lambda: True,
        profile_store=store,
        monitor=False,
    )

    @app.get("/replay/cam<int:cam_id>")
    def page(cam_id):
        return jsonify(nonce=manager.issue_nonce(cam_id))

    c = app.test_client()
    nonce = c.get("/replay/cam1", base_url="https://shop.example").get_json()["nonce"]
    headers = {"X-CSRF-Token": nonce, "Origin": "https://shop.example"}

    # Invalid key (e.g. contains disallowed characters or too short) -> rejected with 400
    res = c.post(
        "/api/facebook-live/cam1/profile/register",
        json={"phone": "0912345678", "pin": "1234", "key": "bad;injection"},
        base_url="https://shop.example",
        headers=headers,
    )
    assert res.status_code == 400


# ============================================================================
# Focused Follow-up Regressions & Shared Helper
# ============================================================================


def make_api_env(tmp_path, page_cfg=None, store=None, popen=None):
    created = []

    def fake_popen(cmd, **opts):
        proc = FakeProcess(cmd, **opts)
        created.append(proc)
        return proc

    active_popen = popen or fake_popen

    if store is None:
        store = GuestLiveProfileStore(
            str(tmp_path / "env_profiles.sqlite3"),
            protect=fake_protect,
            unprotect=fake_unprotect,
        )
    app = Flask(__name__)
    app.secret_key = "test-shared-env-secret"
    manager = install_facebook_live(
        app,
        enabled=lambda: True,
        allowed=lambda c: True,
        get_source=lambda c: f"rtsp://internal/cam/{c}",
        ffmpeg_path="ffmpeg.exe",
        secure=lambda: True,
        get_page_config=lambda: page_cfg or {},
        profile_store=store,
        popen=active_popen,
        monitor=False,
    )

    @app.get("/replay/cam<int:cam_id>")
    def page(cam_id):
        return jsonify(nonce=manager.issue_nonce(cam_id))

    def get_nonce(client, cam_id=1):
        return client.get(f"/replay/cam{cam_id}", base_url="https://shop.example").get_json()["nonce"]

    def post(client, path, payload, cam_id=1, headers=None):
        nonce = get_nonce(client, cam_id)
        h = {"X-CSRF-Token": nonce, "Origin": "https://shop.example"}
        if headers:
            h.update(headers)
        return client.post(path, json=payload, base_url="https://shop.example", headers=h)

    return app, manager, store, created, get_nonce, post


def test_personal_start_with_no_fanpage_and_full_pool(tmp_path):
    app1, mgr1, store1, cr1, get_nonce1, post1 = make_api_env(tmp_path / "env1", page_cfg=None)
    c1 = app1.test_client()
    r_reg1 = post1(c1, "/api/facebook-live/cam1/profile/register", {"phone": "0912000001", "pin": "1234", "key": "FB-pers-key1"})
    assert r_reg1.status_code == 200
    res1 = post1(c1, "/api/facebook-live/cam1/start", {"mode": "personal", "consent": True})
    assert res1.status_code == 200

    pool_cfg = {
        "slots": [{"id": "s1", "name": "Fanpage A", "stream_key": "FB-fp-key-1"}],
        "server_url": "rtmps://live-api-s.facebook.com:443/rtmp/",
    }
    app2, mgr2, store2, cr2, get_nonce2, post2 = make_api_env(tmp_path / "env2", page_cfg=pool_cfg)
    c_fp = app2.test_client()
    assert post2(c_fp, "/api/facebook-live/cam1/start", {"consent": True}, cam_id=1).status_code == 200

    c_p2 = app2.test_client()
    r_reg2 = post2(c_p2, "/api/facebook-live/cam2/profile/register", {"phone": "0912000002", "pin": "1234", "key": "FB-pers-key2"}, cam_id=2)
    assert r_reg2.status_code == 200
    s2 = c_p2.get("/api/facebook-live/cam2/status", base_url="https://shop.example").get_json()
    assert s2["camera_busy"] is False
    assert s2["personal_available"] is True
    assert s2["fanpage_available"] is False
    assert s2["mode"] is None
    res_p2 = post2(c_p2, "/api/facebook-live/cam2/start", {"mode": "personal", "consent": True}, cam_id=2)
    assert res_p2.status_code == 200


def test_cross_mode_and_cross_profile_duplicate_key_refusal(tmp_path):
    fp_key = "FB-cross-dup-key"
    pool_cfg = {
        "slots": [{"id": "s1", "name": "FP", "stream_key": fp_key}],
        "server_url": "rtmps://live-api-s.facebook.com:443/rtmp/",
    }
    app, mgr, store, cr, get_nonce, post = make_api_env(tmp_path, page_cfg=pool_cfg)
    c1, c2, c3 = app.test_client(), app.test_client(), app.test_client()
    assert post(c1, "/api/facebook-live/cam1/start", {"consent": True}, cam_id=1).status_code == 200
    res_custom = post(c2, "/api/facebook-live/cam2/start", {"server": "rtmps://live-api-s.facebook.com:443/rtmp/", "key": fp_key, "consent": True}, cam_id=2)
    assert res_custom.status_code == 409

    same_pers_key = "FB-pers-same-key"
    r_reg2 = post(c2, "/api/facebook-live/cam2/profile/register", {"phone": "0913000001", "pin": "1234", "key": same_pers_key}, cam_id=2)
    assert r_reg2.status_code == 200
    r_reg3 = post(c3, "/api/facebook-live/cam3/profile/register", {"phone": "0913000002", "pin": "1234", "key": same_pers_key}, cam_id=3)
    assert r_reg3.status_code == 200
    assert post(c2, "/api/facebook-live/cam2/start", {"mode": "personal", "consent": True}, cam_id=2).status_code == 200
    assert post(c3, "/api/facebook-live/cam3/start", {"mode": "personal", "consent": True}, cam_id=3).status_code == 409
    assert post(c2, "/api/facebook-live/cam2/stop", {}, cam_id=2).status_code == 200
    assert post(c3, "/api/facebook-live/cam3/start", {"mode": "personal", "consent": True}, cam_id=3).status_code == 200


def test_login_rotation_and_delete_stop_streams(tmp_path):
    app, mgr, store, cr, get_nonce, post = make_api_env(tmp_path)
    c_old, c_new = app.test_client(), app.test_client()
    r_reg = post(c_old, "/api/facebook-live/cam1/profile/register", {"phone": "0914000001", "pin": "1234", "key": "FB-rot-key"}, cam_id=1)
    assert r_reg.status_code == 200
    assert post(c_old, "/api/facebook-live/cam1/start", {"mode": "personal", "consent": True}, cam_id=1).status_code == 200
    assert len(cr) == 1 and cr[0].active

    assert post(c_new, "/api/facebook-live/cam1/profile/login", {"phone": "0914000001", "pin": "1234"}, cam_id=1).status_code == 200
    assert cr[0].terminated
    assert post(c_old, "/api/facebook-live/cam1/start", {"mode": "personal", "consent": True}, cam_id=1).status_code == 401
    assert post(c_new, "/api/facebook-live/cam1/start", {"mode": "personal", "consent": True}, cam_id=1).status_code == 200
    assert cr[1].active
    assert post(c_new, "/api/facebook-live/cam1/profile/delete", {}, cam_id=1).status_code == 200
    assert cr[1].terminated


def test_profile_unprotect_and_revoke_error_503_retains_cookie(tmp_path):
    pool_cfg = {
        "slots": [{"id": "s1", "name": "FP", "stream_key": "FB-fp-ok"}],
        "server_url": "rtmps://live-api-s.facebook.com:443/rtmp/",
    }
    app, mgr, store, cr, get_nonce, post = make_api_env(tmp_path, page_cfg=pool_cfg)
    c = app.test_client()
    r_reg = post(c, "/api/facebook-live/cam1/profile/register", {"phone": "0915000001", "pin": "1234", "key": "FB-unprotect-err"}, cam_id=1)
    assert r_reg.status_code == 200

    orig_unprotect = store._unprotect_fn
    def failing_unprotect(d):
        raise ProfileError("Decryption failure", status=503)
    store._unprotect_fn = failing_unprotect
    r_status = post(c, "/api/facebook-live/cam1/profile/status", {}, cam_id=1)
    assert r_status.status_code == 503
    set_cookie = r_status.headers.get("Set-Cookie", "")
    assert "Max-Age=0" not in set_cookie and "Thu, 01 Jan 1970" not in set_cookie
    store._unprotect_fn = orig_unprotect

    orig_revoke = store.revoke
    def failing_revoke(t):
        raise ProfileError("Revoke failure", status=503)
    store.revoke = failing_revoke
    r_logout = post(c, "/api/facebook-live/cam1/profile/logout", {}, cam_id=1)
    assert r_logout.status_code == 503
    store.revoke = orig_revoke

    assert post(c, "/api/facebook-live/cam1/start", {"consent": True}, cam_id=1).status_code == 200


def test_malformed_and_oversized_payload_rejection(tmp_path):
    app, mgr, store, cr, get_nonce, post = make_api_env(tmp_path)
    c = app.test_client()
    for ep in ["status", "register", "login", "update", "logout", "delete"]:
        path = f"/api/facebook-live/cam1/profile/{ep}"
        n = get_nonce(c, 1)
        h = {"X-CSRF-Token": n, "Origin": "https://shop.example"}
        assert c.post(path, json=[], base_url="https://shop.example", headers=h).status_code == 400
        assert c.post(path, data=b"x" * 5000, content_type="application/json", base_url="https://shop.example", headers=h).status_code == 400


def test_unknown_schema_version_failclosed_and_invalid_tokens(tmp_path):
    db_file = str(tmp_path / "schema_v2.sqlite3")
    conn = sqlite3.connect(db_file)
    conn.execute("PRAGMA user_version = 2;")
    conn.commit()
    conn.close()
    with open(db_file, "rb") as f:
        raw_before = f.read()

    store = GuestLiveProfileStore(db_file, protect=fake_protect, unprotect=fake_unprotect)
    with pytest.raises(ProfileError) as exc:
        store.register("0916000001", "1234", "FB-v2-key")
    assert exc.value.status == 503
    with open(db_file, "rb") as f:
        assert f.read() == raw_before

    store_ok = GuestLiveProfileStore(str(tmp_path / "valid.sqlite3"), protect=fake_protect, unprotect=fake_unprotect)
    tok = store_ok.register("0916000002", "1234", "FB-valid-key")
    for bt in ["bad_token", "\x00invalid_unicode", "x" * 600, "prefix_" + tok]:
        assert store_ok.authenticate(bt) is None
        with pytest.raises(ProfileError) as exc_up:
            store_ok.update(bt, "FB-up-key")
        assert exc_up.value.status == 401
        store_ok.revoke(bt)


def test_ip_limiter_30_attempts_and_rate_consumption_before_hashing(tmp_path, monkeypatch):
    store = GuestLiveProfileStore(str(tmp_path / "ip_rate.sqlite3"), protect=fake_protect, unprotect=fake_unprotect)
    ip = "192.168.1.99"
    for i in range(15):
        phone = f"09170000{i:02d}"
        store.register(phone, "1234", f"FB-rate-key-{i}", ip=ip)
        with pytest.raises(ProfileError):
            store.login(phone, "0000", ip=ip)
    with pytest.raises(ProfileError) as exc:
        store.register("0917000099", "1234", "FB-rate-key-99", ip=ip)
    assert exc.value.status == 429

    import services.facebook_profiles as sfp
    hash_calls = [0]
    orig_hash = sfp.generate_password_hash
    def counted_hash(pin, method=None):
        hash_calls[0] += 1
        return orig_hash(pin, method=method)
    monkeypatch.setattr(sfp, "generate_password_hash", counted_hash)

    phone2 = "0918000001"
    store2 = GuestLiveProfileStore(str(tmp_path / "hash_rate.sqlite3"), protect=fake_protect, unprotect=fake_unprotect)
    store2.register(phone2, "1234", "FB-hash-key-1", ip="10.5.0.1")
    for _ in range(4):
        with pytest.raises(ProfileError):
            store2.login(phone2, "0000", ip="10.5.0.1")
    hash_calls_before = hash_calls[0]
    with pytest.raises(ProfileError) as exc_throttled:
        store2.register(phone2, "1234", "FB-hash-key-2", ip="10.5.0.1")
    assert exc_throttled.value.status == 429
    assert hash_calls[0] == hash_calls_before


def test_no_plaintext_db_select_and_update_rollback(tmp_path):
    db_file = str(tmp_path / "select_plain.sqlite3")
    store = GuestLiveProfileStore(db_file, protect=fake_protect, unprotect=fake_unprotect)
    raw_phone = "0919000001"
    raw_pin = "4321"
    raw_key = "FB-secret-key-query"
    tok = store.register(raw_phone, raw_pin, raw_key)

    conn = sqlite3.connect(db_file)
    row = conn.execute("SELECT encrypted_key, pin_hash, token_hash FROM profiles WHERE phone = ?", (raw_phone,)).fetchone()
    conn.close()
    assert row is not None
    assert row[0] != raw_key.encode()
    assert row[1] != raw_pin
    assert row[2] != tok

    def failing_protect(d):
        raise RuntimeError("Crypto fail")
    store._protect_fn = failing_protect
    with pytest.raises(ProfileError) as exc:
        store.update(tok, "FB-updated-failed-key")
    assert exc.value.status == 503
    store._protect_fn = fake_protect
    assert store.authenticate(tok)["stream_key"] == raw_key
    store.update(tok, "FB-updated-proper-key")
    assert store.authenticate(tok)["stream_key"] == "FB-updated-proper-key"


def test_profile_store_concurrent_init_multiple_instances(tmp_path):
    db_file = str(tmp_path / "shared_init.sqlite3")
    errors = []

    def init_worker(idx):
        try:
            store = GuestLiveProfileStore(db_file, protect=fake_protect, unprotect=fake_unprotect)
            tok = store.register(f"09200000{idx:02d}", "1234", f"FB-init-key-{idx}", ip=f"10.9.0.{idx}")
            auth = store.authenticate(tok)
            if not auth:
                errors.append(f"Auth failed for worker {idx}")
        except Exception as e:
            errors.append(str(e))

    threads = [threading.Thread(target=init_worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors


def test_profile_store_corrupted_schema_missing_columns_fails_503(tmp_path):
    db_file = str(tmp_path / "bad_schema.sqlite3")
    conn = sqlite3.connect(db_file)
    # Schema version 1 but missing UNIQUE phone and missing required columns
    conn.execute("CREATE TABLE profiles (id TEXT PRIMARY KEY, phone TEXT);")
    conn.execute("CREATE TABLE attempts (key TEXT, timestamp REAL);")
    conn.execute("PRAGMA user_version = 1;")
    conn.commit()
    conn.close()

    store = GuestLiveProfileStore(db_file, protect=fake_protect, unprotect=fake_unprotect)
    with pytest.raises(ProfileError) as exc:
        store.register("0921000001", "1234", "FB-bad-schema-key")
    assert exc.value.status == 503


def test_manager_failed_stop_retains_fingerprint_stopping_state_and_unstuck_frees():
    now = [100.0]
    created = []
    def stubborn_popen(cmd, **opts):
        proc = StubbornFakeProcess(cmd, **opts)
        created.append(proc)
        return proc

    manager = GuestFacebookLive(
        lambda: True, lambda c: True,
        lambda c: "rtsp://camera.local/cam", "ffmpeg.exe",
        popen=stubborn_popen, clock=lambda: now[0], monitor=False,
    )
    server = "rtmps://live-api-s.facebook.com:443/rtmp/"
    key1 = "FB-stubborn-key-1"
    key2 = "FB-other-stream-key"

    # Start stream 1 (legacy custom, no mode) and stream 2 on cam 2
    res1, c1 = manager.start(1, "owner1", server, key1)
    assert c1 == 200
    res2, c2 = manager.start(2, "owner2", server, key2)
    assert c2 == 200

    proc1 = created[0]
    with pytest.raises(RuntimeError):
        manager.stop(1, "owner1")

    # Status of cam 1 shows state: "stopping", camera_busy: True, is_owner: True, secret key not exposed
    st1 = manager.status(1, "owner1")
    assert st1["state"] == "stopping"
    assert st1["camera_busy"] is True
    assert st1["is_owner"] is True
    assert key1 not in str(st1)

    # Stream 2 is completely unaffected
    st2 = manager.status(2, "owner2")
    assert st2["state"] == "running"
    assert st2["camera_busy"] is True

    # Another camera attempting to use key1 gets 409 (fingerprint retained while stopping)
    res3, c3 = manager.start(3, "owner3", server, key1)
    assert c3 == 409

    # Unstuck proc1 and trigger _reap: frees cam 1
    proc1.stubborn = False
    proc1.active = False
    manager._reap()
    assert manager.status(1, "owner1")["state"] == "idle"

    # Now another camera can start with key1
    res4, c4 = manager.start(3, "owner3", server, key1)
    assert c4 == 200


def test_api_login_callback_stop_failure_retains_token_and_cookie_until_unstuck(tmp_path):
    proc_ref = []
    def stubborn_popen(cmd, **opts):
        proc = StubbornFakeProcess(cmd, **opts)
        proc_ref.append(proc)
        return proc

    app, mgr, store, created, get_nonce, post = make_api_env(
        tmp_path, popen=stubborn_popen
    )
    c_old = app.test_client()
    phone = "0931000001"
    pin = "1234"
    r_reg = post(c_old, "/api/facebook-live/cam1/profile/register", {"phone": phone, "pin": pin, "key": "FB-login-cb-key"}, cam_id=1)
    assert r_reg.status_code == 200

    # Start personal stream on cam 1
    assert post(c_old, "/api/facebook-live/cam1/start", {"mode": "personal", "consent": True}, cam_id=1).status_code == 200
    assert len(proc_ref) == 1 and proc_ref[0].active

    # Get current token hash from DB before login attempt
    conn = sqlite3.connect(store.path)
    old_token_hash = conn.execute("SELECT token_hash FROM profiles WHERE phone = ?", (phone,)).fetchone()[0]
    conn.close()

    # Client 2 logs in: stop_profile_streams fails because proc is stubborn -> returns 503
    c_new = app.test_client()
    r_login_fail = post(c_new, "/api/facebook-live/cam1/profile/login", {"phone": phone, "pin": pin}, cam_id=1)
    assert r_login_fail.status_code == 503
    # No hash or secrets in response
    assert old_token_hash not in str(r_login_fail.get_json())

    # Token in DB UNCHANGED
    conn = sqlite3.connect(store.path)
    current_token_hash = conn.execute("SELECT token_hash FROM profiles WHERE phone = ?", (phone,)).fetchone()[0]
    conn.close()
    assert current_token_hash == old_token_hash

    # Old stream is in stopping state
    st = mgr.status(1, "dummy")
    assert st["state"] == "busy"

    # Unstuck proc and reap
    proc_ref[0].stubborn = False
    proc_ref[0].active = False
    mgr._reap()

    # Now login succeeds and rotates token
    r_login_ok = post(c_new, "/api/facebook-live/cam1/profile/login", {"phone": phone, "pin": pin}, cam_id=1)
    assert r_login_ok.status_code == 200
    conn = sqlite3.connect(store.path)
    new_token_hash = conn.execute("SELECT token_hash FROM profiles WHERE phone = ?", (phone,)).fetchone()[0]
    conn.close()
    assert new_token_hash != old_token_hash


def test_personal_revocation_expiry_db_outage_reap_ends_stream_safe(tmp_path):
    now = [1000.0]
    db_file = str(tmp_path / "rev_exp.sqlite3")
    store = GuestLiveProfileStore(db_file, protect=fake_protect, unprotect=fake_unprotect, clock=lambda: now[0])
    created = []
    def popen(cmd, **opts):
        proc = FakeProcess(cmd, **opts)
        created.append(proc)
        return proc

    manager = GuestFacebookLive(
        lambda: True, lambda c: True,
        lambda c: "rtsp://camera.local/cam", "ffmpeg.exe",
        popen=popen, clock=lambda: now[0], monitor=False, profile_store=store,
    )
    tok1 = store.register("0932000001", "1234", "FB-safe-reap-1", ip="10.8.0.1")
    auth1 = store.authenticate(tok1)
    assert auth1 is not None

    res1, code1 = manager.start(
        1, "owner1", key=auth1["stream_key"], mode="personal",
        profile_id=auth1["id"], authorization=auth1["authorization"], expires=auth1["expires"],
    )
    assert code1 == 200
    assert len(created) == 1 and created[0].active

    # Case A: Revocation ends stream via reap
    store.revoke(tok1)
    manager._reap()
    assert created[0].terminated

    # Case B: Expiry ends stream via reap
    tok2 = store.login("0932000001", "1234", ip="10.8.0.2")
    auth2 = store.authenticate(tok2)
    res2, code2 = manager.start(
        2, "owner2", key=auth2["stream_key"], mode="personal",
        profile_id=auth2["id"], authorization=auth2["authorization"], expires=auth2["expires"],
    )
    assert code2 == 200
    assert created[1].active
    # Advance clock past expiration
    now[0] += 91 * 86400
    manager._reap()
    assert created[1].terminated

    # Case C: DB outage fail-closed ends stream via reap
    tok3 = store.login("0932000001", "1234", ip="10.8.0.3")
    auth3 = store.authenticate(tok3)
    res3, code3 = manager.start(
        3, "owner3", key=auth3["stream_key"], mode="personal",
        profile_id=auth3["id"], authorization=auth3["authorization"], expires=auth3["expires"],
    )
    assert code3 == 200
    orig_authorized = store.authorized
    store.authorized = lambda pid, auth: (_ for _ in ()).throw(ProfileError("DB outage", 503))
    manager._reap()
    assert created[2].terminated
    store.authorized = orig_authorized


def test_store_concurrent_login_callback_barrier_only_one_winner(tmp_path):
    db_file = str(tmp_path / "barrier.sqlite3")
    store = GuestLiveProfileStore(db_file, protect=fake_protect, unprotect=fake_unprotect)
    phone = "0933000001"
    pin = "1234"
    tok = store.register(phone, pin, "FB-barrier-key", ip="10.7.0.1")
    assert store.authenticate(tok) is not None

    barrier = threading.Barrier(2)
    results = []
    errors = []

    def before_rot(pid):
        barrier.wait(timeout=5)

    def login_task(idx):
        try:
            new_tok = store.login(phone, pin, ip=f"10.7.0.{idx + 2}", before_rotate=before_rot)
            results.append(new_tok)
        except ProfileError as pe:
            errors.append(pe)

    t1 = threading.Thread(target=login_task, args=(1,))
    t2 = threading.Thread(target=login_task, args=(2,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert len(results) == 1
    assert len(errors) == 1
    assert errors[0].status == 401
    assert store.authenticate(results[0]) is not None


def test_stale_authorization_manager_personal_start_rejects_before_popen(tmp_path):
    created = []
    def popen(cmd, **opts):
        proc = FakeProcess(cmd, **opts)
        created.append(proc)
        return proc

    db_file = str(tmp_path / "stale_auth.sqlite3")
    store = GuestLiveProfileStore(db_file, protect=fake_protect, unprotect=fake_unprotect)
    manager = GuestFacebookLive(
        lambda: True, lambda c: True,
        lambda c: "rtsp://camera.local/cam", "ffmpeg.exe",
        popen=popen, monitor=False, profile_store=store,
    )
    phone = "0934000001"
    tok = store.register(phone, "1234", "FB-stale-key-1", ip="10.6.0.1")
    auth = store.authenticate(tok)
    assert auth is not None
    assert "authorization" in auth
    assert "expires" in auth
    assert isinstance(auth["expires"], float)

    # Rotate token so previous auth becomes stale
    tok2 = store.login(phone, "1234", ip="10.6.0.2")
    assert store.authenticate(tok) is None

    # Attempt to start with stale authorization
    res, code = manager.start(
        1, "owner1", key=auth["stream_key"], mode="personal",
        profile_id=auth["id"], authorization=auth["authorization"], expires=auth["expires"],
    )
    assert code == 401
    assert len(created) == 0
