import importlib.util
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest


@pytest.fixture(scope="module")
def runtime():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("cambida_remote_auth_runtime", root / "1.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.app.config.update(TESTING=True)
    return module


@pytest.fixture()
def client(runtime):
    old_config = dict(runtime.CONFIG)
    try:
        runtime.CONFIG["cloudflare_subdomain"] = "shop.example.com"
        runtime.CONFIG["public_base_url"] = "https://shop.example.com"
        runtime.CONFIG["admin_auth"] = {"username": "viewer", "password": "secret-pass"}
        with runtime.app.test_client() as c:
            yield c
    finally:
        runtime.CONFIG.clear()
        runtime.CONFIG.update(old_config)


def remote_headers():
    return {"CF-Connecting-IP": "203.0.113.25"}


def test_direct_lan_access_remains_open(client):
    response = client.get("/", base_url="http://192.168.1.10:8000")
    assert response.status_code == 200


def test_cloudflare_browser_access_redirects_to_remote_login(client):
    response = client.get(
        "/",
        base_url="https://shop.example.com",
        headers=remote_headers(),
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert "/remote/login" in response.headers["Location"]


def test_public_hostname_fallback_requires_login_even_without_cf_header(client):
    response = client.get("/", base_url="https://shop.example.com", follow_redirects=False)
    assert response.status_code == 302
    assert "/remote/login" in response.headers["Location"]


def test_remote_api_requires_auth_but_ping_stays_public(client):
    denied = client.get(
        "/api/license/status",
        base_url="https://shop.example.com",
        headers=remote_headers(),
    )
    assert denied.status_code == 401
    assert "ngoài mạng quán" in denied.get_json()["error"]

    ping = client.get(
        "/api/ping",
        base_url="https://shop.example.com",
        headers=remote_headers(),
    )
    assert ping.status_code == 200
    assert ping.get_json()["status"] == "ok"


def test_remote_login_grants_viewer_session_not_admin(client):
    response = client.post(
        "/remote/login?next=/",
        base_url="https://shop.example.com",
        headers=remote_headers(),
        data={"username": "viewer", "password": "secret-pass"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/")

    allowed = client.get(
        "/",
        base_url="https://shop.example.com",
        headers=remote_headers(),
    )
    assert allowed.status_code == 200

    admin = client.get(
        "/admin",
        base_url="https://shop.example.com",
        headers=remote_headers(),
        follow_redirects=False,
    )
    assert admin.status_code == 302
    assert "/admin/login" in admin.headers["Location"]


def test_ios_lan_redirect_uses_one_time_handoff(runtime, client, monkeypatch):
    monkeypatch.setattr(runtime, "CAMERA_LIST", [{"name": "One"}])
    monkeypatch.setattr(runtime, "is_tunnel_online", lambda: True)
    monkeypatch.setattr(runtime, "get_public_base_url", lambda: "https://shop.example.com")
    monkeypatch.setattr(runtime, "is_camera_private", lambda cam_id: False)

    local = client.get(
        "/replay/cam1?source=qr",
        base_url="http://192.168.1.10:8000",
        headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X)"},
        environ_base={"REMOTE_ADDR": "192.168.1.50"},
        follow_redirects=False,
    )
    assert local.status_code == 302
    redirect_url = urlparse(local.headers["Location"])
    assert redirect_url.scheme == "https"
    assert redirect_url.netloc == "shop.example.com"
    query = parse_qs(redirect_url.query)
    assert query["source"] == ["qr"]
    assert len(query["lan_ticket"]) == 1

    public = client.get(
        redirect_url.path + "?" + redirect_url.query,
        base_url="https://shop.example.com",
        headers=remote_headers(),
        follow_redirects=False,
    )
    assert public.status_code == 302
    assert "lan_ticket" not in public.headers["Location"]
    assert public.headers["Location"].startswith("/replay/cam1")

    allowed = client.get(
        "/",
        base_url="https://shop.example.com",
        headers=remote_headers(),
    )
    assert allowed.status_code == 200

    with runtime.app.test_client() as second:
        reused = second.get(
            redirect_url.path + "?" + redirect_url.query,
            base_url="https://shop.example.com",
            headers=remote_headers(),
            follow_redirects=False,
        )
        assert reused.status_code == 302
        assert "/remote/login" in reused.headers["Location"]


def test_tampered_lan_handoff_does_not_bypass_login(runtime):
    with runtime.app.test_request_context(
        "/",
        base_url="http://192.168.1.10:8000",
        environ_base={"REMOTE_ADDR": "192.168.1.50"},
    ):
        ticket = runtime._mint_lan_handoff_ticket("/")
    tampered = ticket[:-1] + ("A" if ticket[-1] != "A" else "B")

    with runtime.app.test_client() as c:
        response = c.get(
            "/?lan_ticket=" + tampered,
            base_url="https://shop.example.com",
            headers=remote_headers(),
            follow_redirects=False,
        )
    assert response.status_code == 302
    assert "/remote/login" in response.headers["Location"]


def test_remote_login_rejects_wrong_password_and_open_redirect(client):
    wrong = client.post(
        "/remote/login",
        base_url="https://shop.example.com",
        headers=remote_headers(),
        data={"username": "viewer", "password": "wrong"},
    )
    assert wrong.status_code == 200
    assert "Sai tên đăng nhập" in wrong.get_data(as_text=True)

    ok = client.post(
        "/remote/login?next=//evil.example",
        base_url="https://shop.example.com",
        headers=remote_headers(),
        data={"username": "viewer", "password": "secret-pass"},
        follow_redirects=False,
    )
    assert ok.status_code == 302
    assert ok.headers["Location"].endswith("/")
