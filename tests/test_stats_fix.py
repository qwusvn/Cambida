import ast
from datetime import datetime, timezone
import functools
import ipaddress
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
from types import SimpleNamespace
from urllib.parse import unquote, urlparse

from flask import Flask, has_request_context, jsonify, redirect, render_template, request, send_from_directory, session, url_for
import pytest

from services import video_cutter as _video_cutter


class DeterministicCursor:
    def __init__(self, cursor, fixed_now):
        self._cursor = cursor
        self._fixed_now = fixed_now

    def execute(self, sql, params=()):
        if self._fixed_now:
            token = chr(39) + "now" + chr(39)
            repl = chr(39) + self._fixed_now + chr(39)
            sql = sql.replace(token, repl)
        return self._cursor.execute(sql, params)

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchone(self):
        return self._cursor.fetchone()

    def __iter__(self):
        return iter(self._cursor)


class DeterministicConnection:
    def __init__(self, conn, fixed_now):
        self._conn = conn
        self._fixed_now = fixed_now

    def cursor(self):
        return DeterministicCursor(self._conn.cursor(), self._fixed_now)

    def execute(self, sql, params=()):
        if self._fixed_now:
            token = chr(39) + "now" + chr(39)
            repl = chr(39) + self._fixed_now + chr(39)
            sql = sql.replace(token, repl)
        return self._conn.execute(sql, params)

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()


class DeterministicSqliteModule:
    def __init__(self, fixed_now=None):
        self.fixed_now = fixed_now

    def connect(self, database, *args, **kwargs):
        conn = sqlite3.connect(database, *args, **kwargs)
        if self.fixed_now:
            return DeterministicConnection(conn, self.fixed_now)
        return conn

    def __getattr__(self, name):
        return getattr(sqlite3, name)


@pytest.fixture(scope="module")
def runtime_factory():
    source_path = Path(__file__).resolve().parents[1] / "1.py"
    source = source_path.read_text(encoding="utf-8-sig")
    tree = ast.parse(source)

    target_names = {
        "stats_page",
        "stats_data",
        "admin_required",
        "log_download",
        "download_video",
        "api_log_download",
        "safe_video_path",
        "extract_cam_id_from_filename",
        "is_admin",
        "is_camera_private",
        "get_table_by_camera_id",
        "_ensure_db_schema",
        "_is_direct_lan_request",
        "is_remote_cloudflare_request",
        "_configured_public_hostname",
        "_request_public_hostname",
    }

    selected = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in target_names
    ]

    cleaned = []
    for node in selected:
        node_copy = ast.fix_missing_locations(node)
        node_copy.decorator_list = []
        cleaned.append(node_copy)

    mod = ast.Module(body=cleaned, type_ignores=[])
    ast.fix_missing_locations(mod)
    compiled_code = compile(mod, "<1.py:stats_subset>", "exec")

    def build_isolated_app(db_path: str, video_dir: str):
        app = Flask("cambida_isolated_stats")
        app.secret_key = "stats_test_secret_key"
        app.config.update(TESTING=True)

        sqlite_adapter = DeterministicSqliteModule()

        ns = {
            "app": app,
            "request": request,
            "session": session,
            "jsonify": jsonify,
            "send_from_directory": send_from_directory,
            "redirect": redirect,
            "url_for": url_for,
            "render_template": render_template,
            "has_request_context": has_request_context,
            "os": os,
            "re": re,
            "unquote": unquote,
            "urlparse": urlparse,
            "ipaddress": ipaddress,
            "datetime": datetime,
            "timezone": timezone,
            "sqlite3": sqlite_adapter,
            "logger": logging.getLogger("cambida_stats_isolated"),
            "CUT_FILE_CAM_MAP": {},
            "CUT_FILE_CAM_LOCK": threading.RLock(),
            "CONFIG": {"tables": [], "cameras": [{"id": 1, "enabled": True}, {"id": 2, "enabled": True}]},
            "CAMERA_LIST": [{"id": 1, "enabled": True}, {"id": 2, "enabled": True}],
            "is_table_privacy_engaged": None,
            "_video_cutter": _video_cutter,
            "VIDEO_DIR": video_dir,
            "DB_PATH": db_path,
            "wraps": functools.wraps,
            "get_site_config": lambda: {"site_name": "Cambida Test"},
            "__name__": "cambida_stats_isolated_module",
        }

        exec(compiled_code, ns)

        stats_page_view = ns["admin_required"](ns["stats_page"])
        stats_data_view = ns["admin_required"](ns["stats_data"])

        app.route("/stats", methods=["GET"])(stats_page_view)
        app.route("/stats/data", methods=["GET"])(stats_data_view)
        app.route("/download/<path:filename>", methods=["GET"])(ns["download_video"])
        app.route("/api/log-download", methods=["POST"])(ns["api_log_download"])

        runtime_obj = SimpleNamespace(
            app=app,
            DB_PATH=db_path,
            VIDEO_DIR=video_dir,
            datetime=datetime,
            log_download=ns["log_download"],
            _ensure_db_schema=ns["_ensure_db_schema"],
            sqlite_adapter=sqlite_adapter,
            ns=ns,
        )
        return runtime_obj

    return build_isolated_app


@pytest.fixture()
def stats_env(runtime_factory, tmp_path):
    test_db = tmp_path / "test_analytics.db"
    test_videos = tmp_path / "videos"
    test_videos.mkdir(parents=True, exist_ok=True)

    runtime = runtime_factory(str(test_db), str(test_videos))

    with sqlite3.connect(runtime.DB_PATH) as conn:
        runtime._ensure_db_schema(conn)
        conn.commit()

    sample_video = test_videos / "cam1_10-00-00_to_10-05-00_(11-10-2026).mp4"
    sample_video.write_bytes(b"dummy mp4 content for testing range and download")
    sample_merge = test_videos / "merge_cam1_test123.mp4"
    sample_merge.write_bytes(b"dummy merged mp4 content")

    with runtime.app.test_client() as client:
        yield {"client": client, "runtime": runtime, "db": test_db, "video_dir": test_videos}


def test_stats_data_unauthorized_returns_401(stats_env):
    client = stats_env["client"]
    resp = client.get("/stats/data")
    assert resp.status_code == 401
    data = resp.get_json()
    assert data["ok"] is False
    assert "Cần đăng nhập quản trị" in data["error"]


def test_stats_data_authorized_returns_stats(stats_env):
    client = stats_env["client"]
    runtime = stats_env["runtime"]
    # Seed a download
    runtime.log_download("cam1_10-00-00_to_10-05-00_(11-10-2026).mp4")

    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True

    resp = client.get("/stats/data?days=7")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "by_day" in data
    assert "top_files" in data
    assert "by_cam" in data
    assert len(data["by_day"]) >= 1
    assert data["by_day"][0]["count"] >= 1


def test_api_log_download_beacon(stats_env):
    client = stats_env["client"]
    headers = {
        "Origin": "http://localhost",
        "Host": "localhost",
    }
    # Invalid file
    resp_invalid = client.post("/api/log-download", json={"filename": "non_existent.mp4"}, headers=headers)
    assert resp_invalid.status_code == 400

    # Cross-origin request rejected: verify 403 and immediately assert zero DB records for this filename
    filename = "merge_cam1_test123.mp4"
    resp_cross = client.post(
        "/api/log-download",
        json={"filename": filename},
        headers={"Origin": "http://evil.invalid", "Host": "localhost"},
    )
    assert resp_cross.status_code == 403
    with sqlite3.connect(stats_env["runtime"].DB_PATH) as conn:
        count_after_rejected = conn.execute("SELECT COUNT(*) FROM downloads WHERE filename = ?", (filename,)).fetchone()[0]
        assert count_after_rejected == 0

    # Valid same-origin request accepted: verify 200 and exact 1 DB record
    resp_valid = client.post("/api/log-download", json={"filename": filename}, headers=headers)
    assert resp_valid.status_code == 200
    assert resp_valid.get_json()["ok"] is True

    with sqlite3.connect(stats_env["runtime"].DB_PATH) as conn:
        count_after_accepted = conn.execute("SELECT COUNT(*) FROM downloads WHERE filename = ?", (filename,)).fetchone()[0]
        assert count_after_accepted == 1


def test_download_video_with_range_headers(stats_env):
    client = stats_env["client"]
    runtime = stats_env["runtime"]
    filename = "cam1_10-00-00_to_10-05-00_(11-10-2026).mp4"

    # Count before
    with sqlite3.connect(runtime.DB_PATH) as conn:
        before = conn.execute("SELECT COUNT(*) FROM downloads WHERE filename = ?", (filename,)).fetchone()[0]

    # Full request (no Range)
    r1 = client.get(f"/download/{filename}")
    assert r1.status_code in (200, 206)
    r1.close()

    with sqlite3.connect(runtime.DB_PATH) as conn:
        after_full = conn.execute("SELECT COUNT(*) FROM downloads WHERE filename = ?", (filename,)).fetchone()[0]
    assert after_full == before + 1

    # Initial range request (Range: bytes=0-10) -> Should log download
    r2 = client.get(f"/download/{filename}", headers={"Range": "bytes=0-10"})
    assert r2.status_code == 206
    r2.close()

    with sqlite3.connect(runtime.DB_PATH) as conn:
        after_initial_range = conn.execute("SELECT COUNT(*) FROM downloads WHERE filename = ?", (filename,)).fetchone()[0]
    assert after_initial_range == after_full + 1

    # Subsequent range chunk (Range: bytes=10-20) -> Should NOT log duplicate
    r3 = client.get(f"/download/{filename}", headers={"Range": "bytes=10-20"})
    assert r3.status_code == 206
    r3.close()

    with sqlite3.connect(runtime.DB_PATH) as conn:
        after_subsequent_range = conn.execute("SELECT COUNT(*) FROM downloads WHERE filename = ?", (filename,)).fetchone()[0]
    assert after_subsequent_range == after_initial_range


def test_stats_data_midnight_boundary_grouping(stats_env):
    """Test actual extracted stats_data query groups UTC timestamps straddling local midnight correctly."""
    client = stats_env["client"]
    runtime = stats_env["runtime"]

    # Pin adapter clock to deterministic fixed time: 2026-10-25 12:00:00 UTC
    fixed_now_utc = "2026-10-25 12:00:00"
    runtime.sqlite_adapter.fixed_now = fixed_now_utc

    with sqlite3.connect(runtime.DB_PATH) as conn:
        # Require verified non-UTC environment via explicit assertion SQLite localtime != UTC at pinned time
        fixed_now_local = conn.execute(
            "SELECT datetime(?, 'localtime')",
            (fixed_now_utc,),
        ).fetchone()[0]
        assert fixed_now_local != fixed_now_utc, "Environment must have a non-UTC local timezone offset"

        # Compute exact UTC timestamps straddling local midnight before fixed_now_utc:
        # 1 second before local midnight (yesterday local)
        ts_before_utc = conn.execute(
            "SELECT datetime(?, 'localtime', 'start of day', '-1 second', 'utc')",
            (fixed_now_utc,),
        ).fetchone()[0]
        # 1 second after local midnight (today local)
        ts_after_utc = conn.execute(
            "SELECT datetime(?, 'localtime', 'start of day', '+1 second', 'utc')",
            (fixed_now_utc,),
        ).fetchone()[0]

        day_before_local = conn.execute(
            "SELECT strftime('%Y-%m-%d', datetime(?, 'localtime'))",
            (ts_before_utc,),
        ).fetchone()[0]
        day_after_local = conn.execute(
            "SELECT strftime('%Y-%m-%d', datetime(?, 'localtime'))",
            (ts_after_utc,),
        ).fetchone()[0]

        assert day_before_local != day_after_local

        # Assert raw UTC dates would collapse or differ from expected local day grouping
        # If SQLite localtime was removed, raw UTC dates for ts_before and ts_after would be identical
        day_before_utc_raw = conn.execute(
            "SELECT strftime('%Y-%m-%d', datetime(?))",
            (ts_before_utc,),
        ).fetchone()[0]
        day_after_utc_raw = conn.execute(
            "SELECT strftime('%Y-%m-%d', datetime(?))",
            (ts_after_utc,),
        ).fetchone()[0]
        assert (day_before_utc_raw, day_after_utc_raw) != (day_before_local, day_after_local), (
            "UTC raw dates must differ from local day grouping to guard against removing localtime"
        )
        assert day_before_utc_raw == day_after_utc_raw, (
            "Under non-UTC offset, boundary timestamps share the same raw UTC date but split across local midnight"
        )

        # Seed records with UTC timestamps
        conn.execute(
            "INSERT INTO downloads (filename, cam_id, ip, ua, ts) VALUES (?, ?, ?, ?, ?)",
            ("boundary_cam1_before.mp4", 1, "127.0.0.1", "test-ua", ts_before_utc + "Z"),
        )
        conn.execute(
            "INSERT INTO downloads (filename, cam_id, ip, ua, ts) VALUES (?, ?, ?, ?, ?)",
            ("boundary_cam2_after.mp4", 2, "127.0.0.1", "test-ua", ts_after_utc + "Z"),
        )
        conn.commit()

    with client.session_transaction() as sess:
        sess["admin_authenticated"] = True

    # Call extracted stats_data via Flask test client using actual production query
    resp = client.get("/stats/data?days=7")
    assert resp.status_code == 200
    data = resp.get_json()

    # Verify both days are correctly separated and counted around local midnight boundary
    by_day_dict = {item["day"]: item["count"] for item in data["by_day"]}
    assert by_day_dict.get(day_before_local) == 1
    assert by_day_dict.get(day_after_local) == 1
