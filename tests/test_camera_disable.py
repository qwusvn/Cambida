"""Focused tests for camera enable/disable and recording-worker sync contracts.

Importing ``1.py`` executes Flask, logging, filesystem and hardware side effects at
module level, so these tests do not import it. Instead the functions under test are
selected from the module AST and executed in an isolated ``SimpleNamespace`` global
mapping. Only those function bodies run; no top-level statement of ``1.py`` executes
and no real camera worker, FFmpeg process or app server is started.
"""
import ast
import glob as glob_module
import os
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "1.py"


class _LoopStop(Exception):
    """Raised by the fake clock to bound one health_check_loop iteration."""


class _FakeClock:
    def __init__(self, now=10_000.0):
        self.now = now
        self.sleep_calls = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleep_calls.append(seconds)
        raise _LoopStop


class _FakeWorker:
    """Thread stand-in whose is_alive() models whether join() let it exit."""

    def __init__(self, alive=True, exit_on_join=True):
        self.alive = alive
        self.exit_on_join = exit_on_join
        self.join_calls = []

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        self.join_calls.append(timeout)
        if self.exit_on_join:
            self.alive = False


def _compile_runtime():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    module = ast.Module(body=_selected_functions(tree), type_ignores=[])
    return compile(module, str(SOURCE), "exec")


class _CapturedThread:
    """Thread stand-in: records the target, never starts a real recorder."""

    def __init__(self, target=None, daemon=None, name=None, **kwargs):
        self.target = target
        self.daemon = daemon
        self.name = name
        self.started = False

    def start(self):
        self.started = True

    def join(self, timeout=None):
        pass

    def is_alive(self):
        return self.started


class _ThreadingShim:
    """Delegates real locks/events but captures Thread objects for explicit running."""

    def __init__(self):
        self.threads = []

    def Thread(self, *args, **kwargs):
        thread = _CapturedThread(*args, **kwargs)
        self.threads.append(thread)
        return thread

    def __getattr__(self, name):
        return getattr(threading, name)


# Exact final allowlist of real 1.py functions executed by this suite. New helpers
# or dependencies require an explicit, reviewed addition here; there is no dynamic
# transitive growth and no statement-count/size fallback.
RUNTIME_FUNCTIONS = {
    "get_table_by_camera_id",
    "is_camera_active",
    "should_record_locally",
    "_stop_process",
    "ensure_camera_worker",
    "stop_camera_worker",
    "sync_camera_workers",
    "_sync_camera_workers_async",
    "health_check_loop",
    "_update_table_access_config",
}

# App/recording entry points explicitly mocked in the fixture and never extracted.
MOCKED_DEPENDENCIES = {
    "record_camera",
    "send_telegram_alert",
    "restart_camera_worker",
}


def _selected_functions(tree):
    """Return only the exact allowlisted functions; fail on drift in either direction."""
    definitions = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }
    missing = RUNTIME_FUNCTIONS - definitions.keys()
    assert not missing, f"1.py missing allowlisted runtime functions: {sorted(missing)}"

    selected = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in RUNTIME_FUNCTIONS
    ]
    referenced = {
        child.id
        for node in selected
        for child in ast.walk(node)
        if isinstance(child, ast.Name)
        and isinstance(child.ctx, ast.Load)
        and child.id in definitions
    }
    unexpected = referenced - RUNTIME_FUNCTIONS - MOCKED_DEPENDENCIES
    assert not unexpected, (
        "1.py runtime functions reference non-allowlisted top-level helpers: "
        f"{sorted(unexpected)}; review and add them explicitly"
    )
    return selected


@pytest.fixture()
def runtime(tmp_path):
    cameras = [
        {"name": "Cam 1", "playback_source": "local", "enabled": True},
        {"name": "Cam 2", "playback_source": "local", "enabled": True},
    ]
    tables = [
        {"id": "ban-01", "name": "Bàn 1", "camera_id": 1, "enabled": True, "privacy_off": False},
        {"id": "ban-02", "name": "Bàn 2", "camera_id": 2, "enabled": True, "privacy_off": False},
    ]
    namespace = SimpleNamespace(
        CONFIG={
            "cameras": cameras,
            "tables": tables,
            "camera_stall_sec": 600,
            "camera_recovery_cooldown_sec": 120,
        },
        CAMERA_LIST=cameras,
        CAM_LAST_SUCCESS={},
        CAM_LAST_START={},
        CAM_LAST_ERROR={},
        CAM_LAST_RECOVERY={},
        CAM_STOP_EVENTS={},
        CAM_WORKERS={},
        CAM_PROCESSES={},
        CAMERA_LOCK=threading.RLock(),
        VIDEO_DIR=str(tmp_path),
        logger=MagicMock(),
        send_telegram_alert=MagicMock(),
        restart_camera_worker=MagicMock(return_value=True),
        record_camera=MagicMock(),
        glob=glob_module,
        os=os,
        subprocess=subprocess,
        threading=_ThreadingShim(),
        time=time,
    )
    code = _compile_runtime()
    exec(code, namespace.__dict__)
    # The extracted module defines the real functions; replace the process stopper
    # afterwards so its global lookup resolves to the mock and no FFmpeg is touched.
    namespace._stop_process = MagicMock()
    return namespace


def _single_camera(runtime):
    """Limit watchdog iteration to Cam 1 so each test asserts one decision."""
    runtime.CAMERA_LIST[:] = runtime.CAMERA_LIST[:1]
    runtime.CONFIG["cameras"] = runtime.CAMERA_LIST


def _run_one_watchdog_cycle(runtime, clock):
    runtime.time = clock
    with pytest.raises(_LoopStop):
        runtime.health_check_loop()
    assert clock.sleep_calls == [60]


def test_is_camera_active_reflects_table_and_camera_state(runtime):
    assert runtime.is_camera_active(1) is True
    assert runtime.is_camera_active(2) is True

    runtime.CONFIG["tables"][0]["enabled"] = False
    assert runtime.is_camera_active(1) is False
    assert runtime.is_camera_active(2) is True

    runtime.CONFIG["tables"][0]["enabled"] = True
    runtime.CONFIG["tables"][0]["privacy_off"] = True
    assert runtime.is_camera_active(1) is False

    runtime.CONFIG["tables"][0]["privacy_off"] = False
    assert runtime.is_camera_active(1) is True

    runtime.CAMERA_LIST[1]["enabled"] = False
    assert runtime.is_camera_active(2) is False

    assert runtime.is_camera_active(0) is False
    assert runtime.is_camera_active(3) is False
    assert runtime.is_camera_active("1") is False


def test_stop_camera_worker_joins_and_cleans_partial_file(runtime, tmp_path):
    worker = _FakeWorker(exit_on_join=True)
    stop_event = threading.Event()
    runtime.CAM_WORKERS[1] = worker
    runtime.CAM_STOP_EVENTS[1] = stop_event
    partial = tmp_path / "cam1_10-00-00_to_10-05-00.part"
    partial.write_bytes(b"partial")
    other_partial = tmp_path / "cam2_10-00-00_to_10-05-00.part"
    other_partial.write_bytes(b"partial")

    runtime.stop_camera_worker(1)

    assert stop_event.is_set() is True
    runtime._stop_process.assert_called_once_with(1)
    assert worker.join_calls == [3]
    assert 1 not in runtime.CAM_WORKERS
    assert 1 not in runtime.CAM_STOP_EVENTS
    assert partial.exists() is False
    assert other_partial.exists() is True


def test_stop_camera_worker_keeps_handle_when_thread_does_not_exit(runtime):
    worker = _FakeWorker(exit_on_join=False)
    runtime.CAM_WORKERS[1] = worker
    runtime.CAM_STOP_EVENTS[1] = threading.Event()

    runtime.stop_camera_worker(1)

    assert worker.join_calls == [3]
    # A timed-out join must not forget the still-running worker, otherwise the
    # watchdog/sync path could start a second recorder for the same camera.
    assert runtime.CAM_WORKERS.get(1) is worker


def test_sync_stops_worker_for_disabled_table_only(runtime):
    worker_one = _FakeWorker(exit_on_join=True)
    worker_two = _FakeWorker(alive=True, exit_on_join=False)
    event_one = threading.Event()
    event_two = threading.Event()
    runtime.CAM_WORKERS.update({1: worker_one, 2: worker_two})
    runtime.CAM_STOP_EVENTS.update({1: event_one, 2: event_two})
    runtime.CONFIG["tables"][0]["enabled"] = False
    runtime.ensure_camera_worker = MagicMock()

    runtime.sync_camera_workers()

    runtime._stop_process.assert_called_once_with(1)
    assert event_one.is_set() is True
    assert event_two.is_set() is False
    assert 1 not in runtime.CAM_WORKERS
    assert runtime.CAM_WORKERS.get(2) is worker_two
    runtime.ensure_camera_worker.assert_not_called()


def test_sync_skips_disabled_camera_and_starts_after_reenable(runtime):
    runtime.ensure_camera_worker = MagicMock(return_value=True)
    runtime.CONFIG["tables"][0]["enabled"] = False

    runtime.sync_camera_workers()
    assert [call.args[0] for call in runtime.ensure_camera_worker.call_args_list] == [2]

    runtime.ensure_camera_worker.reset_mock()
    runtime.CONFIG["tables"][0]["enabled"] = True

    runtime.sync_camera_workers()
    assert sorted(call.args[0] for call in runtime.ensure_camera_worker.call_args_list) == [1, 2]
    for call in runtime.ensure_camera_worker.call_args_list:
        assert call.args[2] == "bật lại sau khi kích hoạt camera/bàn"


def test_sync_does_not_spawn_duplicate_when_stop_timeout_keeps_worker(runtime):
    worker = _FakeWorker(exit_on_join=False)
    runtime.CAM_WORKERS[1] = worker
    runtime.CAM_WORKERS[2] = _FakeWorker(alive=True, exit_on_join=False)
    runtime.CAM_STOP_EVENTS[1] = threading.Event()
    runtime.CONFIG["tables"][0]["enabled"] = False
    runtime.ensure_camera_worker = MagicMock(return_value=True)

    runtime.sync_camera_workers()
    assert runtime.CAM_WORKERS.get(1) is worker

    runtime.CONFIG["tables"][0]["enabled"] = True
    runtime.sync_camera_workers()

    # The retained (still alive) worker is treated as running, so no second
    # recorder is started for the same camera.
    runtime.ensure_camera_worker.assert_not_called()


def test_table_update_schedules_async_sync_only_for_changed_camera(runtime):
    runtime._sync_camera_workers_async = MagicMock()
    updated = dict(runtime.CONFIG)
    updated["tables"] = [dict(table) for table in runtime.CONFIG["tables"]]
    updated["tables"][0]["enabled"] = False

    runtime._update_table_access_config(updated)

    runtime._sync_camera_workers_async.assert_called_once_with([1])
    assert runtime.CONFIG is updated
    assert runtime.CONFIG["tables"][0]["enabled"] is False


def test_table_update_rename_or_reorder_does_not_touch_recorders(runtime):
    runtime._sync_camera_workers_async = MagicMock()
    worker_one = _FakeWorker(alive=True, exit_on_join=False)
    worker_two = _FakeWorker(alive=True, exit_on_join=False)
    event_one = threading.Event()
    event_two = threading.Event()
    runtime.CAM_WORKERS.update({1: worker_one, 2: worker_two})
    runtime.CAM_STOP_EVENTS.update({1: event_one, 2: event_two})

    updated = dict(runtime.CONFIG)
    updated["tables"] = [dict(table) for table in runtime.CONFIG["tables"]]
    updated["tables"][0]["name"] = "Bàn A"
    updated["tables"].reverse()

    runtime._update_table_access_config(updated)

    runtime._sync_camera_workers_async.assert_called_once_with([])
    assert runtime.CONFIG is updated
    assert runtime.CAM_WORKERS == {1: worker_one, 2: worker_two}
    assert event_one.is_set() is False
    assert event_two.is_set() is False
    assert worker_one.join_calls == []
    assert worker_two.join_calls == []


def test_async_sync_schedules_daemon_reconcile_for_sorted_scope(runtime):
    runtime.sync_camera_workers = MagicMock()

    runtime._sync_camera_workers_async([2, 1, 2])

    assert len(runtime.threading.threads) == 1
    thread = runtime.threading.threads[0]
    assert thread.daemon is True
    assert thread.started is True

    thread.target()
    runtime.sync_camera_workers.assert_called_once_with(camera_ids=(1, 2))


def test_async_sync_ignores_empty_scope(runtime):
    runtime.sync_camera_workers = MagicMock()

    runtime._sync_camera_workers_async([])

    assert runtime.threading.threads == []
    runtime.sync_camera_workers.assert_not_called()


def test_watchdog_skips_inactive_camera_even_with_stale_heartbeat(runtime):
    _single_camera(runtime)
    runtime.CONFIG["tables"][0]["enabled"] = False
    runtime.CAM_LAST_SUCCESS[1] = 0
    runtime.CAM_LAST_START[1] = 0
    runtime.ensure_camera_worker = MagicMock()

    _run_one_watchdog_cycle(runtime, _FakeClock(10_000.0))

    runtime.ensure_camera_worker.assert_not_called()
    runtime.send_telegram_alert.assert_not_called()
    runtime.restart_camera_worker.assert_not_called()


def test_watchdog_recreates_missing_worker_for_active_camera(runtime):
    _single_camera(runtime)
    runtime.ensure_camera_worker = MagicMock(return_value=True)

    _run_one_watchdog_cycle(runtime, _FakeClock(10_000.0))

    runtime.ensure_camera_worker.assert_called_once()
    args = runtime.ensure_camera_worker.call_args.args
    assert args[0] == 1
    assert args[2] == "được watchdog tạo lại"
    runtime.send_telegram_alert.assert_not_called()
    runtime.restart_camera_worker.assert_not_called()


def test_watchdog_alerts_and_restarts_stalled_active_camera(runtime):
    _single_camera(runtime)
    clock = _FakeClock(10_000.0)
    runtime.CAM_WORKERS[1] = _FakeWorker(alive=True)
    runtime.CAM_LAST_SUCCESS[1] = clock.now - 601
    runtime.ensure_camera_worker = MagicMock()

    _run_one_watchdog_cycle(runtime, clock)

    runtime.ensure_camera_worker.assert_not_called()
    runtime.restart_camera_worker.assert_called_once_with(1, runtime.CAMERA_LIST[0])
    assert runtime.CAM_LAST_RECOVERY[1] == clock.now
    assert runtime.send_telegram_alert.call_count == 2
    first_message = runtime.send_telegram_alert.call_args_list[0].args[0]
    assert "TREO CAM 1" in first_message
    second_message = runtime.send_telegram_alert.call_args_list[1].args[0]
    assert "ĐÃ KHỞI ĐỘNG LẠI" in second_message


def test_watchdog_respects_recovery_cooldown_for_stalled_camera(runtime):
    _single_camera(runtime)
    clock = _FakeClock(10_000.0)
    runtime.CAM_WORKERS[1] = _FakeWorker(alive=True)
    runtime.CAM_LAST_SUCCESS[1] = clock.now - 900
    runtime.CAM_LAST_RECOVERY[1] = clock.now - 10

    _run_one_watchdog_cycle(runtime, clock)

    runtime.restart_camera_worker.assert_not_called()
    runtime.send_telegram_alert.assert_not_called()
