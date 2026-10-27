"""Isolated audit/safety tests for ``1.py``.

These tests never import ``1.py`` or the Flask app. They parse the module with
``ast``, extract selected function definitions and execute them in an explicit
namespace made only of real module dependencies (extracted helpers or explicit
stubs such as ``session``/``request``/``safe_video_path``). There is no
unknown-global auto-mock: a missing dependency raises ``NameError`` so drift in
``1.py`` is visible instead of silently mocked away.

Run only this file:
    python -m pytest tests/test_audit_safety.py -q
"""

from __future__ import annotations

import ast
import copy
import glob as glob_module
import ipaddress
import json
import os
import re
import threading
import time
import types
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from unittest import mock
from urllib.parse import quote, unquote, urlparse

SOURCE_PATH = Path(__file__).resolve().parents[1] / "1.py"

PUBLIC_HOST = "cam.example.com"
LAN_HOST = "192.168.1.10:8000"
REMOTE_IP = "203.0.113.10"
LAN_IP = "192.168.1.50"


# ---------------------------------------------------------------------------
# AST extraction harness (read-only, no import/exec of the whole module)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _source_text() -> str:
    return SOURCE_PATH.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _tree() -> ast.Module:
    return ast.parse(_source_text())


def _top_level_defs() -> dict:
    return {
        node.name: node
        for node in _tree().body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _top_level_names() -> set:
    return {
        node.name
        for node in _tree().body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _exec_function(name: str, namespace: dict):
    node = _top_level_defs().get(name)
    assert node is not None, f"{name} is no longer defined at module level in 1.py"
    node = copy.deepcopy(node)
    node.decorator_list = []  # drop app.route without needing Flask
    module = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, f"<1.py:{name}>", "exec"), namespace)  # noqa: S102
    return namespace[name]


def _call(fn, *args, **kwargs):
    result = fn(*args, **kwargs)
    if hasattr(result, "__await__"):
        import asyncio

        result = asyncio.run(result)
    return result


def _status(result) -> int:
    if isinstance(result, tuple):
        tail = result[-1]
        return int(tail) if isinstance(tail, int) else 0
    return int(getattr(result, "status_code", 0) or 0)


def _payload(result):
    if isinstance(result, tuple):
        result = result[0]
    if hasattr(result, "get_json"):
        return result.get_json()
    return result


class _PayloadResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def get_json(self):
        return self.payload


def _jsonify(payload=None, *args, **kwargs):
    status = kwargs.get("status") or kwargs.get("status_code") or 200
    if args and isinstance(args[0], int):
        status = args[0]
    return _PayloadResponse(payload, int(status))


# ---------------------------------------------------------------------------
# Real dependency context for api_log_download
# ---------------------------------------------------------------------------


class _FakeRequest:
    def __init__(self, *, payload=None, malformed=False, remote_addr=LAN_IP,
                 host=LAN_HOST, headers=None, path="/api/log-download",
                 scheme="http"):
        self._payload = payload
        self._malformed = malformed
        self.remote_addr = remote_addr
        self.host = host
        self.headers = dict(headers or {})
        self.path = path
        self.method = "POST"
        self.args = {}
        self.cookies = {}
        self.is_json = True
        self.scheme = scheme
        self.host_url = f"{scheme}://{host}/"
        self.url_root = self.host_url

    def get_json(self, *args, **kwargs):
        silent = bool(kwargs.get("silent")) or bool(args and args[0])
        if self._malformed:
            if not silent:
                raise ValueError("malformed json")
            return None
        return self._payload


def _config(tmp_path):
    return {
        "cloudflare_subdomain": PUBLIC_HOST,
        "public_base_url": "",
        "video_dir": str(tmp_path),
        "tables": [{"id": "table-2", "camera_id": 2, "enabled": False}],
        "cameras": [],
    }


def _camera_list():
    return [
        {"id": 1, "enabled": True, "privacy_off": False},
        {"id": 2, "enabled": True, "privacy_off": False},
        {"id": 3, "enabled": True, "privacy_off": False},
    ]


def _api_environment(tmp_path, request, session, media_paths):
    """Build a namespace from real 1.py helpers plus explicit leaf stubs."""
    config = _config(tmp_path)
    log_download = mock.Mock(name="log_download")

    def _safe_video_path(value):
        return str(media_paths[value]) if value in media_paths else None

    namespace = {
        "os": os,
        "re": re,
        "json": json,
        "time": time,
        "request": request,
        "session": session,
        "CONFIG": config,
        "CAMERA_LIST": _camera_list(),
        "VIDEO_DIR": str(tmp_path),
        "logger": mock.Mock(name="logger"),
        "jsonify": _jsonify,
        "unquote": unquote,
        "quote": quote,
        "urlparse": urlparse,
        "ipaddress": ipaddress,
        "is_table_privacy_engaged": lambda table: (
            table.get("enabled") is False or table.get("privacy_off") is True
        ),
        "CUT_FILE_CAM_MAP": {},
        "CUT_FILE_CAM_LOCK": threading.RLock(),
        "log_download": log_download,
        "safe_video_path": mock.Mock(side_effect=_safe_video_path),
    }

    for helper in (
        "extract_cam_id_from_filename",
        "is_admin",
        "get_table_by_camera_id",
        "is_camera_private",
        "_request_public_hostname",
        "_configured_public_hostname",
        "is_remote_cloudflare_request",
        "_is_direct_lan_request",
    ):
        _exec_function(helper, namespace)

    api = _exec_function("api_log_download", namespace)
    return api, log_download, namespace


def _media(tmp_path, name):
    path = tmp_path / name
    path.write_bytes(b"audit")
    return path


def _run_api(tmp_path, *, filename, session, remote_addr=LAN_IP, host=LAN_HOST,
             headers=None, malformed=False, scheme="http"):
    media = _media(tmp_path, filename)
    request = _FakeRequest(
        payload={"filename": filename},
        malformed=malformed,
        remote_addr=remote_addr,
        host=host,
        headers=headers,
        scheme=scheme,
    )
    api, log_download, _ = _api_environment(
        tmp_path, request, dict(session), {filename: media}
    )
    return _call(api), log_download


def _assert_allowed(result, log_download, filename):
    assert _status(result) == 200
    payload = _payload(result)
    if isinstance(payload, dict):
        assert payload.get("ok") is True
    assert log_download.call_args_list == [mock.call(filename)]


def _assert_denied(result, log_download, expected=403):
    assert _status(result) == expected
    payload = _payload(result)
    if isinstance(payload, dict):
        assert payload.get("ok") is not True
    log_download.assert_not_called()


# ---------------------------------------------------------------------------
# Auth matrix: LAN guest / remote_authenticated / admin / anonymous / origin /
# private camera, with exact log_download calls.
# ---------------------------------------------------------------------------


def test_auth_matrix_admin_remote_canonical_allowed(tmp_path):
    result, log_download = _run_api(
        tmp_path,
        filename="cam1_20260101_000000.mp4",
        session={"admin_authenticated": True},
        remote_addr=REMOTE_IP,
        host=PUBLIC_HOST,
        headers={"CF-Connecting-IP": REMOTE_IP},
    )
    _assert_allowed(result, log_download, "cam1_20260101_000000.mp4")


def test_auth_matrix_remote_authenticated_canonical_allowed(tmp_path):
    result, log_download = _run_api(
        tmp_path,
        filename="cam1_20260101_000000.mp4",
        session={"remote_authenticated": True},
        remote_addr=REMOTE_IP,
        host=PUBLIC_HOST,
        headers={"CF-Connecting-IP": REMOTE_IP},
    )
    _assert_allowed(result, log_download, "cam1_20260101_000000.mp4")


def test_auth_matrix_lan_guest_canonical_allowed(tmp_path):
    result, log_download = _run_api(
        tmp_path,
        filename="cam1_20260101_000000.mp4",
        session={},
        remote_addr=LAN_IP,
        host=LAN_HOST,
    )
    _assert_allowed(result, log_download, "cam1_20260101_000000.mp4")


def test_auth_matrix_anonymous_remote_canonical_denied(tmp_path):
    result, log_download = _run_api(
        tmp_path,
        filename="cam1_20260101_000000.mp4",
        session={},
        remote_addr=REMOTE_IP,
        host=PUBLIC_HOST,
        headers={"CF-Connecting-IP": REMOTE_IP},
    )
    _assert_denied(result, log_download)


def test_auth_matrix_public_origin_guest_denied(tmp_path):
    # Requests arriving through the configured public hostname are remote even
    # when remote_addr happens to look private; anonymous guests stay denied.
    result, log_download = _run_api(
        tmp_path,
        filename="cam1_20260101_000000.mp4",
        session={},
        remote_addr=LAN_IP,
        host=PUBLIC_HOST,
    )
    _assert_denied(result, log_download)


def test_auth_matrix_cross_origin_rejected_even_for_admin(tmp_path):
    result, log_download = _run_api(
        tmp_path,
        filename="cam1_20260101_000000.mp4",
        session={"admin_authenticated": True},
        remote_addr=REMOTE_IP,
        host=PUBLIC_HOST,
        headers={"CF-Connecting-IP": REMOTE_IP, "Origin": "https://evil.example"},
        scheme="https",
    )
    _assert_denied(result, log_download)


def test_auth_matrix_cross_site_fetch_metadata_rejected(tmp_path):
    result, log_download = _run_api(
        tmp_path,
        filename="cam1_20260101_000000.mp4",
        session={"admin_authenticated": True},
        remote_addr=REMOTE_IP,
        host=PUBLIC_HOST,
        headers={"CF-Connecting-IP": REMOTE_IP, "Sec-Fetch-Site": "cross-site"},
        scheme="https",
    )
    _assert_denied(result, log_download)


def test_auth_matrix_same_origin_admin_allowed(tmp_path):
    result, log_download = _run_api(
        tmp_path,
        filename="cam1_20260101_000000.mp4",
        session={"admin_authenticated": True},
        remote_addr=REMOTE_IP,
        host=PUBLIC_HOST,
        headers={
            "CF-Connecting-IP": REMOTE_IP,
            "Origin": f"https://{PUBLIC_HOST}",
        },
        scheme="https",
    )
    _assert_allowed(result, log_download, "cam1_20260101_000000.mp4")


def test_auth_matrix_private_camera_denied_even_when_lan_guest(tmp_path):
    result, log_download = _run_api(
        tmp_path,
        filename="cam2_20260101_000000.mp4",
        session={},
        remote_addr=LAN_IP,
        host=LAN_HOST,
    )
    _assert_denied(result, log_download)


def test_auth_matrix_private_camera_denied_for_remote_authenticated(tmp_path):
    result, log_download = _run_api(
        tmp_path,
        filename="cam2_20260101_000000.mp4",
        session={"remote_authenticated": True},
        remote_addr=REMOTE_IP,
        host=PUBLIC_HOST,
        headers={"CF-Connecting-IP": REMOTE_IP},
    )
    _assert_denied(result, log_download)


def test_auth_matrix_admin_private_camera_allowed(tmp_path):
    result, log_download = _run_api(
        tmp_path,
        filename="cam2_20260101_000000.mp4",
        session={"admin_authenticated": True},
        remote_addr=REMOTE_IP,
        host=PUBLIC_HOST,
        headers={"CF-Connecting-IP": REMOTE_IP},
    )
    _assert_allowed(result, log_download, "cam2_20260101_000000.mp4")


def test_unknown_camera_ids_apply_private_fallback_for_guests(tmp_path):
    for filename in ("cam0_removed.mp4", "cam99_removed.mp4"):
        result, logged = _run_api(
            tmp_path, filename=filename, session={}, remote_addr=LAN_IP, host=LAN_HOST,
        )
        _assert_denied(result, logged)


def test_unknown_camera_ids_preserve_admin_access(tmp_path):
    for filename in ("cam0_removed.mp4", "cam99_removed.mp4"):
        result, logged = _run_api(
            tmp_path, filename=filename, session={"admin_authenticated": True},
            remote_addr=LAN_IP, host=LAN_HOST,
        )
        _assert_allowed(result, logged, filename)


# ---------------------------------------------------------------------------
# Request payload / canonical path guards
# ---------------------------------------------------------------------------


def test_api_log_download_rejects_malformed_json(tmp_path):
    result, log_download = _run_api(
        tmp_path,
        filename="cam1_20260101_000000.mp4",
        session={"admin_authenticated": True},
        malformed=True,
    )
    _assert_denied(result, log_download, expected=400)


def test_api_log_download_rejects_non_canonical_path(tmp_path):
    request = _FakeRequest(
        payload={"filename": "../../config.json"},
        remote_addr=REMOTE_IP,
        host=PUBLIC_HOST,
        headers={"CF-Connecting-IP": REMOTE_IP},
    )
    api, log_download, _ = _api_environment(
        tmp_path, request, {"admin_authenticated": True}, {}
    )
    result = _call(api)
    _assert_denied(result, log_download, expected=400)


# ---------------------------------------------------------------------------
# Retired endpoints / hooks
# ---------------------------------------------------------------------------


def test_empty_after_request_hook_removed():
    names = _top_level_names()
    assert "_log_download_after" not in names, (
        "empty _log_download_after hook must be removed from 1.py"
    )


# ---------------------------------------------------------------------------
# Worker stop / replacement races
# ---------------------------------------------------------------------------


def _fake_clock():
    class _Clock:
        def __init__(self):
            self.now = 0.0

        def _tick(self, *_args, **_kwargs):
            self.now += 0.5
            return self.now

        monotonic = _tick
        time = _tick

        def sleep(self, *_args, **_kwargs):
            return None

    return _Clock()


def _stop_namespace(workers, events, tmp_path, stop_process):
    return {
        "os": os,
        "glob": glob_module,
        "time": _fake_clock(),
        "threading": threading,
        "CAMERA_LOCK": threading.RLock(),
        "CAM_WORKERS": workers,
        "CAM_STOP_EVENTS": events,
        "_stop_process": stop_process,
        "VIDEO_DIR": str(tmp_path),
        "logger": mock.Mock(name="logger"),
    }


def test_stop_camera_worker_keeps_live_worker_and_event_on_timeout(tmp_path):
    worker = mock.MagicMock(name="live-worker")
    worker.is_alive.return_value = True
    event = threading.Event()
    workers = {2: worker}
    events = {2: event}
    stop_process = mock.Mock(name="_stop_process")
    fn = _exec_function(
        "stop_camera_worker",
        _stop_namespace(workers, events, tmp_path, stop_process),
    )

    _call(fn, 2)

    stop_process.assert_called_once_with(2)
    assert event.is_set()
    assert workers.get(2) is worker, "live worker must not be dropped on timeout"
    assert events.get(2) is event, "stop event must be retained for the live worker"


def test_stop_camera_worker_replacement_race_leaves_new_worker_and_event(tmp_path):
    state = {"old_alive": True}
    old_event = threading.Event()
    new_event = threading.Event()
    old_worker = mock.MagicMock(name="old-worker")
    old_worker.is_alive.side_effect = lambda: state["old_alive"]
    replacement = mock.MagicMock(name="replacement-worker")
    replacement.is_alive.return_value = True

    workers = {2: old_worker}
    events = {2: old_event}

    def _join(*_args, **_kwargs):
        # Old worker exits and a coordinated restart installs its replacement
        # (worker + event) while the stop path is still joining.
        state["old_alive"] = False
        workers[2] = replacement
        events[2] = new_event

    old_worker.join.side_effect = _join
    stop_process = mock.Mock(name="_stop_process")
    fn = _exec_function(
        "stop_camera_worker",
        _stop_namespace(workers, events, tmp_path, stop_process),
    )

    _call(fn, 2)

    stop_process.assert_called_once_with(2)
    assert old_event.is_set(), "the old stop event must still be signalled"
    assert workers.get(2) is replacement, "replacement worker must survive the stop"
    assert events.get(2) is new_event, "replacement event must survive the stop"
    assert not new_event.is_set(), "replacement event must not be stopped"


# ---------------------------------------------------------------------------
# Scoped sync_camera_workers: camera 2 must start exactly once
# ---------------------------------------------------------------------------


def _sync_namespace(cameras, active, workers, ensure, stop):
    return {
        "os": os,
        "time": _fake_clock(),
        "threading": threading,
        "CAMERA_LOCK": threading.RLock(),
        "CAMERA_LIST": cameras,
        "CAM_WORKERS": workers,
        "CAM_STOP_EVENTS": {},
        "CONFIG": {"tables": [], "cameras": []},
        "is_camera_active": lambda cam_id: active[cam_id],
        "should_record_locally": lambda camera: True,
        "ensure_camera_worker": ensure,
        "stop_camera_worker": stop,
        "logger": mock.Mock(name="logger"),
    }


def test_sync_camera_workers_signature_is_scoped():
    node = _top_level_defs().get("sync_camera_workers")
    assert node is not None
    arg_names = [arg.arg for arg in node.args.args]
    assert "camera_ids" in arg_names, (
        "sync_camera_workers must accept camera_ids=None for scoped reconciliation"
    )
    defaults = node.args.defaults
    if defaults:
        assert any(
            isinstance(default, ast.Constant) and default.value is None
            for default in defaults
        )


def test_sync_camera_workers_starts_only_camera_two_exactly_once():
    cameras = _camera_list()
    active = {1: False, 2: True, 3: True}
    camera_one_worker = mock.MagicMock(name="cam1-worker")
    camera_one_worker.is_alive.return_value = True
    camera_three_worker = mock.MagicMock(name="cam3-worker")
    camera_three_worker.is_alive.return_value = True
    ensure = mock.Mock(name="ensure_camera_worker")
    stop = mock.Mock(name="stop_camera_worker")
    fn = _exec_function(
        "sync_camera_workers",
        _sync_namespace(
            cameras, active, {1: camera_one_worker, 3: camera_three_worker}, ensure, stop
        ),
    )

    _call(fn, camera_ids=[2])

    start_calls = [
        call for call in ensure.call_args_list
        if (call.args[:1] == (2,)) or call.kwargs.get("cam_id") == 2
    ]
    assert len(start_calls) == 1, "scoped sync must start camera 2 exactly once"
    call = start_calls[0]
    camera_arg = call.args[1] if len(call.args) > 1 else call.kwargs.get("camera")
    assert camera_arg is cameras[1]
    stop.assert_not_called()
    assert ensure.call_args_list == start_calls


def test_sync_camera_workers_scope_skips_unrelated_cameras():
    cameras = _camera_list()
    active = {1: False, 2: True, 3: True}
    camera_one_worker = mock.MagicMock(name="cam1-worker")
    camera_one_worker.is_alive.return_value = True
    ensure = mock.Mock(name="ensure_camera_worker")
    stop = mock.Mock(name="stop_camera_worker")
    fn = _exec_function(
        "sync_camera_workers",
        _sync_namespace(cameras, active, {1: camera_one_worker}, ensure, stop),
    )

    _call(fn, camera_ids=[3])

    touched = [
        call.args[0] if call.args else call.kwargs.get("cam_id")
        for call in list(ensure.call_args_list) + list(stop.call_args_list)
    ]
    assert 1 not in touched, "out-of-scope camera 1 must not be stopped/reconciled"
    assert 2 not in touched, "out-of-scope camera 2 must not be started/reconciled"


def test_sync_camera_workers_scope_call_sites_pass_camera_ids():
    for node in ast.walk(_tree()):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "sync_camera_workers"
        ):
            assert node.args or node.keywords, (
                "sync_camera_workers must be invoked with an explicit camera scope "
                f"(line {node.lineno})"
            )


# ---------------------------------------------------------------------------
# Async reconcile / table access acceptance
# ---------------------------------------------------------------------------


class _TrackedLock:
    def __init__(self):
        self._lock = threading.RLock()
        self.depth = 0

    def __enter__(self):
        self._lock.acquire()
        self.depth += 1
        return self

    def __exit__(self, *_exc):
        self.depth -= 1
        self._lock.release()
        return False


class _FakeThreadFactory:
    """Captures threading.Thread targets without starting OS threads."""

    def __init__(self):
        self.created = []
        self.started = []

    def Thread(self, target=None, args=(), kwargs=None, daemon=None, name=None):
        outer = self

        class _FakeThread:
            def __init__(self):
                self.target = target
                self.args = args
                self.kwargs = kwargs or {}
                self.daemon = daemon
                self.name = name

            def start(self):
                outer.started.append(self)

            def is_alive(self):
                return True

            def join(self, timeout=None):
                return None

        thread = _FakeThread()
        self.created.append(thread)
        return thread

    def recorder_names(self):
        return [t.name for t in self.started if str(t.name).startswith("Cam-")]


def _worker_context(tmp_path, config, workers=None, events=None, lock=None,
                    factory=None):
    factory = factory or _FakeThreadFactory()
    lock = lock or _TrackedLock()
    namespace = {
        "os": os,
        "glob": glob_module,
        "time": time,
        "threading": types.SimpleNamespace(
            Thread=factory.Thread, Event=threading.Event
        ),
        "CAMERA_LOCK": lock,
        "CAMERA_LIST": _camera_list(),
        "CAM_WORKERS": {} if workers is None else workers,
        "CAM_STOP_EVENTS": {} if events is None else events,
        "CAM_LAST_START": {},
        "CONFIG": config,
        "VIDEO_DIR": str(tmp_path),
        "_stop_process": mock.Mock(name="_stop_process"),
        "logger": mock.Mock(name="logger"),
        "record_camera": lambda *_args, **_kwargs: None,
        "is_table_privacy_engaged": None,
    }
    for helper in (
        "should_record_locally",
        "get_table_by_camera_id",
        "is_camera_active",
        "ensure_camera_worker",
        "stop_camera_worker",
        "sync_camera_workers",
        "_sync_camera_workers_async",
    ):
        _exec_function(helper, namespace)
    return namespace, factory, lock


def test_async_reconcile_restarts_once_after_quick_reenable_no_lock_during_join(tmp_path):
    table = {"camera_id": 2, "enabled": False}
    config = {"tables": [table], "cameras": [], "video_dir": str(tmp_path)}
    state = {"alive": True}
    old_worker = mock.MagicMock(name="old-worker")
    old_worker.is_alive.side_effect = lambda: state["alive"]
    workers = {2: old_worker}
    namespace, factory, lock = _worker_context(
        tmp_path, config, workers=workers, events={}
    )
    join_timeouts = []

    def _join(timeout=None):
        assert lock.depth == 0, "stop path must not hold CAMERA_LOCK while joining"
        join_timeouts.append(timeout)
        # A quick re-enable lands while the old worker is exiting.
        table["enabled"] = True
        state["alive"] = False

    old_worker.join.side_effect = _join
    namespace["_sync_camera_workers_async"]([2])
    reconcile_threads = [
        thread for thread in factory.started if thread.name == "Camera-reconcile"
    ]
    assert len(reconcile_threads) == 1
    assert factory.recorder_names() == []

    # Deterministic execution of the captured reconcile target, no real thread.
    reconcile_threads[0].target()

    assert join_timeouts == [3]
    assert factory.recorder_names() == ["Cam-2"], (
        "quick re-enable must restart the worker exactly once"
    )
    assert workers[2] is factory.created[-1]
    assert lock.depth == 0


def test_async_reconcile_recovers_after_retained_timeout(tmp_path):
    table = {"camera_id": 2, "enabled": False}
    state = {"alive": True}
    worker = mock.Mock()
    worker.is_alive.side_effect = lambda: state["alive"]
    namespace, factory, lock = _worker_context(
        tmp_path, {"tables": [table]}, workers={2: worker}, events={}
    )
    timeouts = []

    def join(timeout=None):
        assert lock.depth == 0
        timeouts.append(timeout)
        if timeout == 3:
            assert namespace["CAM_WORKERS"][2] is worker
            table["enabled"] = True
            # Deliberately remain alive after the stop timeout.
        else:
            assert timeout == 0.25
            assert namespace["CAM_WORKERS"][2] is worker
            assert namespace["CAM_STOP_EVENTS"][2].is_set()
            state["alive"] = False

    worker.join.side_effect = join
    namespace["time"] = types.SimpleNamespace(time=lambda: 100, sleep=lambda _: None)
    namespace["_sync_camera_workers_async"]([2])
    factory.started[0].target()
    assert timeouts == [3, 0.25]
    assert factory.recorder_names() == ["Cam-2"]
    assert namespace["CAM_WORKERS"][2] is factory.created[-1]
    assert not namespace["CAM_STOP_EVENTS"][2].is_set()
    assert lock.depth == 0


def test_stale_async_disable_never_starts_inactive_camera(tmp_path):
    config = {
        "tables": [{"camera_id": 2, "enabled": False}],
        "cameras": [],
        "video_dir": str(tmp_path),
    }
    namespace, factory, _ = _worker_context(tmp_path, config)

    namespace["_sync_camera_workers_async"]([2])
    reconcile_threads = [
        thread for thread in factory.started if thread.name == "Camera-reconcile"
    ]
    assert len(reconcile_threads) == 1
    reconcile_threads[0].target()

    assert factory.recorder_names() == [], "disabled camera must not be started"
    assert namespace["ensure_camera_worker"](2, namespace["CAMERA_LIST"][1]) is False
    assert factory.recorder_names() == []


def _update_env(initial_config, async_mock):
    namespace = {
        "CONFIG": initial_config,
        "CAMERA_LIST": _camera_list(),
        "CAMERA_LOCK": _TrackedLock(),
        "logger": mock.Mock(name="logger"),
        "_sync_camera_workers_async": async_mock,
    }
    _exec_function("should_record_locally", namespace)
    _exec_function("_update_table_access_config", namespace)
    return namespace


def test_update_table_access_reorder_is_noop_scope(tmp_path):
    before = {
        "tables": [
            {"camera_id": 2, "enabled": False, "name": "Desk A"},
            {"camera_id": 3, "enabled": False, "name": "Desk B"},
        ],
        "cameras": [],
    }
    updated = {
        "tables": [
            {"camera_id": 3, "enabled": False, "name": "Desk B renamed"},
            {"camera_id": 2, "enabled": False, "name": "Desk A"},
        ],
        "cameras": [],
    }
    async_mock = mock.Mock(name="_sync_camera_workers_async")
    env = _update_env(before, async_mock)

    env["_update_table_access_config"](updated)

    assert async_mock.call_args_list == [mock.call([])]
    assert env["CONFIG"] is updated


def test_update_table_access_rebound_disabled_table_scopes_old_and_new_camera():
    before = {"tables": [{"camera_id": 2, "enabled": False}], "cameras": []}
    updated = {"tables": [{"camera_id": 3, "enabled": False}], "cameras": []}
    async_mock = mock.Mock(name="_sync_camera_workers_async")
    env = _update_env(before, async_mock)

    env["_update_table_access_config"](updated)

    assert async_mock.call_args_list == [mock.call([2, 3])]


def test_update_table_access_removed_disabled_table_scopes_old_camera():
    before = {"tables": [{"camera_id": 2, "enabled": False}], "cameras": []}
    updated = {"tables": [], "cameras": []}
    async_mock = mock.Mock(name="_sync_camera_workers_async")
    env = _update_env(before, async_mock)

    env["_update_table_access_config"](updated)

    assert async_mock.call_args_list == [mock.call([2])]


# ---------------------------------------------------------------------------
# Restart coordination: never start a second worker over a live one
# ---------------------------------------------------------------------------


def test_restart_camera_worker_does_not_start_second_worker_when_old_still_alive(tmp_path):
    worker = mock.MagicMock(name="restart-old-worker")
    worker.is_alive.return_value = True
    ensure = mock.Mock(name="ensure_camera_worker")
    stop = mock.Mock(name="stop_camera_worker", return_value=False)
    reconcile = mock.Mock(name="_sync_camera_workers_async")
    namespace = {
        "os": os,
        "time": _fake_clock(),
        "threading": threading,
        "CAMERA_LOCK": threading.RLock(),
        "CAM_WORKERS": {2: worker},
        "CAM_STOP_EVENTS": {},
        "stop_camera_worker": stop,
        "_sync_camera_workers_async": reconcile,
        "ensure_camera_worker": ensure,
        "logger": mock.Mock(name="logger"),
    }
    fn = _exec_function("restart_camera_worker", namespace)

    result = _call(fn, 2, {"id": 2})

    assert result is False
    stop.assert_called_once_with(2, timeout=12)
    ensure.assert_not_called()
    reconcile.assert_called_once_with([2])
