"""Regression coverage for server-side per-table guest privacy and stable IDs."""
import pytest
from flask import Flask, jsonify, request
from camera_modules.table_access import install_table_access


@pytest.fixture
def api():
    config = {
        "admin_auth": {"username": "operator", "password": "test-only"},
        "cameras": [{"name": "One"}, {"name": "Two"}],
        "tables": [
            {"id": "ban-01", "camera_id": 1, "name": "One"},
            {"id": "ban-02", "camera_id": 2, "name": "Two"},
        ],
    }
    state = {"config": config, "saves": []}
    app = Flask(__name__)
    app.secret_key = "unit-tests-only"

    @app.get("/replay/cam<int:cam_id>")
    @app.get("/cam<int:cam_id>")
    @app.get("/list/cam<int:cam_id>")
    @app.get("/snapshot/cam<int:cam_id>")
    def camera(cam_id):
        return jsonify({"cam_id": cam_id})

    @app.get("/api/timeline")
    @app.get("/live")
    def aggregate():
        return jsonify({"sensitive": True})

    @app.get("/video/<path:filename>")
    @app.get("/download/<path:filename>")
    def media(filename):
        return jsonify({"filename": filename})

    @app.get("/nvr/video/<token>")
    def nvr(token):
        return jsonify({"token": token})

    @app.get("/merge")
    @app.get("/cut")
    def clip():
        return jsonify({"clip": True})

    def setter(value):
        state["config"] = value

    def saver(value):
        state["saves"].append(value)

    install_table_access(
        app, get_config=lambda: state["config"], set_config=setter,
        save_config=saver, get_nvr_reference=lambda token: {"one": {"cam_id": 1}, "two": {"cam_id": 2}}.get(token),
    )
    return app.test_client(), state


def login(client):
    response = client.post("/api/admin/session", json={"username": "operator", "password": "test-only"})
    assert response.status_code == 200


def test_guest_can_view_enabled_tables(api):
    client, _ = api
    for path in ("/replay/cam1", "/cam2", "/list/cam1", "/video/cam1_2026.mp4", "/nvr/video/one", "/api/timeline"):
        assert client.get(path).status_code == 200, path


def test_admin_login_is_required_and_origin_checked(api):
    client, _ = api
    assert client.get("/api/admin/tables").status_code == 401
    assert client.post("/api/admin/session", json={"username": "operator", "password": "wrong"}).status_code == 401
    assert client.post("/api/admin/session", json={"username": "operator", "password": "test-only"}, headers={"Origin": "https://other.test"}).status_code == 403
    login(client)
    assert len(client.get("/api/admin/tables").json["tables"]) == 2


def test_off_denies_guest_video_and_preserves_other_table(api):
    client, state = api
    login(client)
    before = state["config"]["tables"][0].copy()
    response = client.post("/api/admin/tables/ban-01/privacy", json={"enabled": False})
    assert response.status_code == 200
    assert response.json["enabled"] is False
    after = state["config"]["tables"][0]
    assert after["camera_id"] == before["camera_id"]
    assert after["id"] == before["id"]
    assert after["privacy_off"] is True
    assert len(state["saves"]) == 1
    client.post("/api/admin/session", json={"username": "operator", "password": "wrong"})  # does not log out
    with client.session_transaction() as sess:
        sess.clear()
    for path in ("/replay/cam1", "/cam1", "/list/cam1", "/snapshot/cam1", "/video/cam1_2026.mp4", "/download/merge_cam1_1.mp4", "/nvr/video/one", "/api/timeline", "/live", "/merge?cam_id=1", "/cut?filename=cam1_2026.mp4", "/video/cut_random.mp4"):
        assert client.get(path).status_code == 403, path
    for path in ("/replay/cam2", "/list/cam2", "/video/cam2_2026.mp4", "/nvr/video/two"):
        assert client.get(path).status_code == 200, path


def test_invalid_payload_does_not_save(api):
    client, state = api
    login(client)
    for value in ("false", 0, None):
        assert client.post("/api/admin/tables/ban-01/privacy", json={"enabled": value}).status_code == 400
    assert client.post("/api/admin/tables/no-such/privacy", json={"enabled": True}).status_code == 404
    assert not state["saves"]


def test_reorder_does_not_change_camera_identity(api):
    client, state = api
    login(client)
    assert client.post("/api/admin/tables/reorder", json={"ids": ["ban-02", "ban-01"]}).status_code == 200
    tables = state["config"]["tables"]
    assert [(t["id"], t["camera_id"], t["sort_order"]) for t in tables] == [("ban-02", 2, 0), ("ban-01", 1, 1)]
    assert client.post("/api/admin/tables/reorder", json={"ids": ["ban-01", "ban-01"]}).status_code == 400


def test_camera_probe_rejects_guest_and_reports_real_capability(api):
    from types import SimpleNamespace
    from unittest.mock import patch
    client, _ = api
    payload = {"vendor": "hikvision", "host": "192.168.1.199", "port": 8000,
               "username": "test", "password": "not-a-real-secret"}
    assert client.post("/api/admin/camera/probe", json=payload).status_code == 401
    login(client)
    assert client.post("/api/admin/camera/probe", json={**payload, "vendor": "invalid"}).status_code == 400
    assert client.post("/api/admin/camera/probe", json={**payload, "port": 70000}).status_code == 400
    with patch("camera_modules.hikvision.probe_hikvision_device", return_value=SimpleNamespace(
        ok=False, device_kind="unknown", transport="none", channels=[],
        message="HCNetSDK.dll missing")):
        result = client.post("/api/admin/camera/probe", json=payload)
    assert result.status_code == 503
    assert result.json["channels"] == []
    assert "not-a-real-secret" not in result.get_data(as_text=True)


def test_camera_discovery_requires_admin(api):
    from unittest.mock import patch
    client, _ = api
    assert client.get("/api/admin/camera/discover").status_code == 401
    login(client)
    with patch("camera_modules.discovery.discover_lan_cameras", return_value=[]):
        result = client.get("/api/admin/camera/discover")
    assert result.status_code == 200
    assert result.json["devices"] == []


def test_first_install_without_tables_can_toggle_and_reorder(api):
    client, state = api
    state["config"].pop("tables")
    login(client)
    listed = client.get("/api/admin/tables")
    assert listed.status_code == 200
    assert [(t["id"], t["color"]) for t in listed.json["tables"]] == [
        ("ban-01", "red"), ("ban-02", "red")]
    switched = client.post("/api/admin/tables/ban-01/privacy", json={"enabled": False})
    assert switched.status_code == 200
    assert state["config"]["tables"][0]["privacy_off"] is True
    assert client.get("/api/admin/tables").json["tables"][0]["color"] == "grey"
    with client.session_transaction() as sess:
        sess.clear()
    assert client.get("/replay/cam1").status_code == 403
    login(client)
    reordered = client.post("/api/admin/tables/reorder", json={"ids": ["ban-02", "ban-01"]})
    assert reordered.status_code == 200
    assert [t["camera_id"] for t in state["config"]["tables"]] == [2, 1]


def test_admin_can_view_disabled_media(api):
    client, _ = api
    login(client)
    client.post("/api/admin/tables/ban-01/privacy", json={"enabled": False})
    for path in ("/replay/cam1", "/video/cam1_2026.mp4", "/nvr/video/one", "/api/timeline"):
        assert client.get(path).status_code == 200, path
