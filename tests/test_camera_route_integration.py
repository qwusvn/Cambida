"""Integration regression: modular camera routes own the live Flask URL map.

No camera hardware, recorder process, or configuration write is required.
"""
from collections import Counter
from pathlib import Path
import runpy
import pytest


@pytest.fixture(scope="module")
def live_app():
    project = Path(__file__).resolve().parents[1]
    return runpy.run_path(str(project / "1.py"))["app"]


def test_unique_modular_admin_routes(live_app):
    counts = Counter((rule.rule, frozenset(rule.methods - {"OPTIONS", "HEAD"}))
                     for rule in live_app.url_map.iter_rules())
    for path, method in (("/api/admin/tables", "GET"),
                         ("/api/admin/tables/reorder", "POST"),
                         ("/api/admin/camera/probe", "POST"),
                         ("/api/admin/camera/discover", "GET")):
        assert counts[(path, frozenset({method}))] == 1, path
    paths = {rule.rule: rule.endpoint for rule in live_app.url_map.iter_rules()}
    assert paths["/api/admin/camera/probe"] == "table_camera_probe"
    assert paths["/api/admin/camera/discover"] == "table_camera_discover"
    assert paths["/api/admin/tables/reorder"] == "table_admin_reorder"


def test_probe_requires_admin_and_rejects_cross_origin(live_app):
    with live_app.test_client() as client:
        payload = {"vendor": "hikvision", "host": "192.168.1.199", "port": 8000,
                   "username": "test", "password": "synthetic-test-only"}
        assert client.post("/api/admin/camera/probe", json=payload).status_code == 401
        with client.session_transaction() as sess:
            sess["admin_authenticated"] = True
        response = client.post("/api/admin/camera/probe", json=payload,
                               headers={"Origin": "https://another-origin.invalid"})
        assert response.status_code == 403


def test_legacy_bulk_add_cannot_write_unverified_camera(live_app):
    with live_app.test_client() as client:
        with client.session_transaction() as sess:
            sess["admin_authenticated"] = True
        response = client.post("/api/admin/camera/bulk-add", json={
            "device": {"vendor": "hikvision", "host": "192.168.1.199",
                       "username": "synthetic-test-only", "password": "synthetic-test-only"},
            "channel_ids": [1, 2]})
        assert response.status_code == 501
        assert response.json["ok"] is False
