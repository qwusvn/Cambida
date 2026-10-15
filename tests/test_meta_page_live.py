"""Meta Page Live Video API regressions (HTTP and FFmpeg fully mocked)."""
import pytest
from flask import Flask, jsonify

from services.meta_page_live import (
    MetaLiveError, MetaPageLiveClient, valid_page_config, validate_ingest_url,
)
from services.facebook_live import install_facebook_live


PAGE = {
    "page_id": "123456789012345",
    "page_token": "EAAmockNeverRealPageToken-1234567890",
    "graph_version": "v26.0",
}


class FakeHttpResponse:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status
    def json(self):
        return self._data


class FakeGraphHttp:
    def __init__(self):
        self.calls = []
        self.created = 0
        self.return_bad_ingest = False
        self.permission_denied = False
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.permission_denied:
            return FakeHttpResponse({"error": {"code": 200, "message": "SECRET"}}, 403)
        if url.endswith("/live_videos") and method == "POST":
            self.created += 1
            return FakeHttpResponse({
                "id": str(7000000 + self.created),
                "secure_stream_url": (
                    "rtmps://evil.example/rtmp/not-allowed" if self.return_bad_ingest
                    else f"rtmps://live-api-s.facebook.com:443/rtmp/TEST-STREAM-{self.created}"
                ),
                "permalink_url": "",
            })
        if method == "GET":
            return FakeHttpResponse({"id": url.rsplit("/", 1)[-1],
                                     "permalink_url": "https://www.facebook.com/videos/7000001",
                                     "status": "LIVE"})
        return FakeHttpResponse({"success": True})


class FakeProcess:
    def __init__(self, argv, **kwargs):
        self.argv = argv
        self.running = True
    def poll(self):
        return None if self.running else 0
    def terminate(self):
        self.running = False
    def kill(self):
        self.running = False
    def wait(self, timeout=None):
        return 0


class FakeMetaClient:
    def __init__(self):
        self.created = []
        self.ended = []
        self.end_fails = False
    def create_live(self, title):
        number = len(self.created) + 1
        live_id = str(8800000 + number)
        self.created.append((live_id, title))
        return {
            "id": live_id,
            "ingest": f"rtmps://live-api-s.facebook.com:443/rtmp/TEST-KEY-{number}",
            "permalink_url": "",
        }
    def permalink(self, live_id):
        return "https://www.facebook.com/watch/live/" + str(live_id)
    def end_live(self, live_id):
        if self.end_fails:
            raise MetaLiveError("Simulated Meta 503")
        self.ended.append(live_id)
        return True


def test_meta_http_create_link_end_and_no_secrets_in_urls():
    http = FakeGraphHttp()
    client = MetaPageLiveClient(lambda: PAGE, http=http)
    result1 = client.create_live("Bàn 1")
    result2 = client.create_live("Bàn 2")
    assert result1["id"] != result2["id"]
    assert result1["ingest"] != result2["ingest"]
    assert client.permalink(result1["id"]).startswith("https://www.facebook.com/")
    assert client.end_live(result1["id"])
    method, url, kwargs = http.calls[0]
    assert method == "POST"
    assert url == "https://graph.facebook.com/v26.0/123456789012345/live_videos"
    assert kwargs["data"]["status"] == "LIVE_NOW"
    assert "secure_stream_url" in kwargs["data"]["fields"]
    assert kwargs["headers"]["Authorization"] == "Bearer " + PAGE["page_token"]
    assert PAGE["page_token"] not in str([c[1] for c in http.calls])
    assert all(c[2]["timeout"] <= 8 and c[2]["allow_redirects"] is False for c in http.calls)
    assert http.calls[-1][2]["data"]["end_live_video"] == "true"


def test_meta_rejects_unsafe_ingest_and_closes_created_post():
    for url in (
        "rtmp://live-api-s.facebook.com/rtmp/KEY",
        "rtmps://evil.example/rtmp/KEY",
        "rtmps://live-api-s.facebook.com.evil/rtmp/KEY",
        "rtmps://live-api-s.facebook.com:444/rtmp/KEY",
        "https://graph.facebook.com",
        "rtmps://user:pass@live-api-s.facebook.com/rtmp/KEY",
        "rtmps://live-api-s.facebook.com/rtmp/KEY?redir=https://evil.example",
    ):
        with pytest.raises(MetaLiveError):
            validate_ingest_url(url)
    http = FakeGraphHttp()
    http.return_bad_ingest = True
    with pytest.raises(MetaLiveError):
        MetaPageLiveClient(PAGE, http=http).create_live("Test")
    assert http.calls[0][0] == "POST"
    assert http.calls[-1][2]["data"]["end_live_video"] == "true"


def test_meta_permission_error_redacted():
    http = FakeGraphHttp()
    http.permission_denied = True
    with pytest.raises(MetaLiveError) as err:
        MetaPageLiveClient(PAGE, http=http).create_live("Page")
    assert "SECRET" not in str(err.value)
    assert PAGE["page_token"] not in str(err.value)
    assert not valid_page_config({"page_id": "9999", "page_token": ""})


def create_app():
    app = Flask(__name__)
    app.secret_key = "only-for-isolated-test"
    meta = FakeMetaClient()
    procs = []
    def popen(args, **kwargs):
        obj = FakeProcess(args, **kwargs)
        procs.append(obj)
        return obj
    live_cfg = {**PAGE, "max_duration_minutes": 60}
    manager = install_facebook_live(
        app, enabled=lambda: True, allowed=lambda c: c in (1, 2, 3),
        get_source=lambda c: f"rtsp://test-camera/{c}",
        ffmpeg_path="ffmpeg.exe", secure=lambda: True,
        get_page_config=lambda: live_cfg, meta_client=meta,
        popen=popen, monitor=False,
    )
    @app.get("/replay/cam<int:cam_id>")
    def page(cam_id):
        return jsonify(nonce=manager.issue_nonce(cam_id))
    return app, manager, meta, procs


def post(client, cam, op, data=None, nonce=None, origin="https://shop.example"):
    headers = {"Origin": origin}
    if nonce is not None:
        headers["X-CSRF-Token"] = nonce
    prefix = "/api/admin/facebook-live" if op in ("approve", "revoke") else "/api/facebook-live"
    return client.post(f"{prefix}/cam{cam}/{op}", json=data or {},
                       headers=headers, base_url="https://shop.example")


def test_page_live_guest_needs_staff_one_use_approval_and_stops_meta():
    app, manager, meta, procs = create_app()
    staff = app.test_client()
    guest1 = app.test_client()
    guest2 = app.test_client()
    n1 = guest1.get("/replay/cam1", base_url="https://shop.example").get_json()["nonce"]
    n2 = guest2.get("/replay/cam2", base_url="https://shop.example").get_json()["nonce"]

    status = guest1.get("/api/facebook-live/cam1/status", base_url="https://shop.example").get_json()
    assert status["page_api_enabled"] is True
    assert status["fanpage_available"] is False
    assert status["approval_seconds"] == 0
    assert post(guest1, 1, "start", {"consent": True}, n1).status_code == 403
    assert not meta.created
    assert post(staff, 1, "approve").status_code == 401
    with staff.session_transaction(base_url="https://shop.example") as s:
        s["admin_authenticated"] = True
    assert post(staff, 1, "approve", origin="https://evil.test").status_code == 403
    assert post(staff, 4, "approve").status_code == 403
    assert post(staff, 1, "approve").status_code == 200
    assert post(staff, 2, "approve").status_code == 200

    g1 = post(guest1, 1, "start", {"consent": True}, n1)
    g2 = post(guest2, 2, "start", {"consent": True}, n2)
    assert g1.status_code == 200
    assert g2.status_code == 200
    assert len(meta.created) == 2
    assert len(procs) == 2
    assert procs[0].argv[-1] != procs[1].argv[-1]
    assert "Bearer" not in str(g1.get_json())
    assert PAGE["page_token"] not in str(g1.get_json())

    # A one-time approval is consumed upon successful broadcast creation.
    assert manager._approval_remaining(1) == 0
    status = guest1.get("/api/facebook-live/cam1/status", base_url="https://shop.example").get_json()
    assert status["state"] == "running"
    assert status["page_url"].startswith("https://www.facebook.com/watch/live/")

    other = post(guest2, 1, "stop", {}, n2)
    assert other.status_code == 403
    stopped = post(guest1, 1, "stop", {}, n1)
    assert stopped.status_code == 200
    assert procs[0].poll() is not None
    assert meta.ended == [meta.created[0][0]]
    assert manager.status(1, "no-owner")["state"] == "idle"
    assert post(guest1, 1, "start", {"consent": True}, n1).status_code == 403


def test_meta_end_fails_closed_and_retries_without_releasing_session():
    app, manager, meta, procs = create_app()
    guest = app.test_client()
    with app.test_client() as staff:
        with staff.session_transaction(base_url="https://shop.example") as s:
            s["admin_authenticated"] = True
        assert post(staff, 1, "approve").status_code == 200
    nonce = guest.get("/replay/cam1", base_url="https://shop.example").get_json()["nonce"]
    assert post(guest, 1, "start", {"consent": True}, nonce).status_code == 200
    meta.end_fails = True
    assert post(guest, 1, "stop", {}, nonce).status_code == 503
    assert procs[0].poll() is not None
    assert 1 in manager.streams
    assert manager.streams[1]["shutdown_pending"] is True
    assert post(guest, 1, "start", {"consent": True}, nonce).status_code == 409
    meta.end_fails = False
    assert manager.status(1, "owner")["state"] == "idle"
    assert meta.ended == [meta.created[0][0]]


def test_staff_revoke_interrupts_live_page_and_cancels_guest_approval():
    app, manager, meta, procs = create_app()
    with app.test_client() as staff:
        with staff.session_transaction(base_url="https://shop.example") as s:
            s["admin_authenticated"] = True
        assert post(staff, 1, "approve").status_code == 200
        assert post(staff, 1, "revoke").status_code == 200
        assert manager._approval_remaining(1) == 0
        assert post(staff, 2, "approve").status_code == 200
        guest = app.test_client()
        nonce = guest.get("/replay/cam2", base_url="https://shop.example").get_json()["nonce"]
        assert post(guest, 2, "start", {"consent": True}, nonce).status_code == 200
        assert post(staff, 2, "revoke").status_code == 200
        assert procs[0].poll() is not None
        assert len(meta.ended) == 1

def test_admin_config_get_does_not_echo_page_access_token():
    """Regression: a persisted Page credential never appears in the admin read JSON."""
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "cambida_meta_admin_read_test", Path(__file__).resolve().parents[1] / "1.py")
    runtime = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runtime)
    original = dict(runtime.CONFIG)
    secret = "EAAPrivilegedPageSecretNeverEchoed_1234567890"
    try:
        runtime.CONFIG["facebook_graph_page_id"] = PAGE["page_id"]
        runtime.CONFIG["facebook_graph_page_token"] = secret
        with runtime.app.test_client() as c:
            with c.session_transaction() as sess:
                sess["admin_authenticated"] = True
            response = c.get("/api/admin/config", base_url="http://localhost")
            assert response.status_code == 200
            body = response.get_json()
            assert body.get("facebook_graph_token_configured") is True
            assert "facebook_graph_page_token" not in body
            assert secret not in response.get_data(as_text=True)
    finally:
        runtime.CONFIG.clear()
        runtime.CONFIG.update(original)
