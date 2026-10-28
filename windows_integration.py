"""Windows integration for the recorder: keep the PC awake and allow the web UI.

The recorder runs unattended, so while the application is alive it asks Windows
not to enter sleep. The web UI is opened from phones on the shop LAN, so the
executable also has to be present in the inbound firewall allow list. Adding a
firewall rule requires an elevated process; this module uses the current token
when it is already elevated and asks for elevation once (Windows administrator
accounts, UAC enabled) when the rule is missing. A marker in the install
directory stops repeated elevation prompts after the user declines.
"""
import base64
import ctypes
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

FIREWALL_RULE_NAME = "Cambida CCTV"
FIREWALL_MARKER_NAME = "firewall-elevation.json"
FIREWALL_DESCRIPTION = "Cambida CCTV web UI"

CREATE_NO_WINDOW = 0x08000000
NETSH_TIMEOUT = 20
ADMIN_GROUP_SID = "S-1-5-32-544"

LOGGER = logging.getLogger("cambida.windows")


def _log(log, level, message, *args):
    target = log or LOGGER
    try:
        target.log(level, message, *args)
    except Exception:
        pass


def is_windows():
    return sys.platform == "win32"


def _kernel32():
    return ctypes.windll.kernel32


def set_execution_state(flags):
    """Set the current thread execution state; 0 means the call failed."""
    if not is_windows():
        return 0
    try:
        return int(_kernel32().SetThreadExecutionState(ctypes.c_uint(flags)))
    except Exception:
        return 0


class KeepAwake:
    """Assert ES_SYSTEM_REQUIRED from a live thread and re-assert periodically."""

    def __init__(self, interval=60.0, state_setter=None, log=None):
        value = float(interval)
        self.interval = value if value > 0 else 60.0
        self._setter = state_setter or set_execution_state
        self._log = log
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if not is_windows():
            return False
        if self._thread and self._thread.is_alive():
            return True
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="KeepAwake")
        self._thread.start()
        return True

    def _run(self):
        confirmed = False
        warned = False
        try:
            while not self._stop.is_set():
                try:
                    value = self._setter(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
                except Exception as exc:
                    value = 0
                    if not warned:
                        _log(self._log, logging.WARNING, "Lỗi khi yêu cầu Windows không ngủ: %s", exc)
                        warned = True
                if value:
                    if not confirmed:
                        _log(self._log, logging.INFO, "Đã yêu cầu Windows không ngủ khi ứng dụng đang chạy.")
                        confirmed = True
                elif not warned:
                    _log(self._log, logging.WARNING,
                         "Windows chưa xác nhận yêu cầu chống ngủ; sẽ thử lại.")
                    warned = True
                if self._stop.wait(self.interval):
                    break
        finally:
            try:
                self._setter(ES_CONTINUOUS)
            except Exception:
                pass
            _log(self._log, logging.INFO, "Đã trả quyền ngủ cho Windows.")

    def stop(self, timeout=3.0):
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout)
        self._thread = None


_keep_awake_lock = threading.Lock()
_keep_awake = None


def start_keep_awake(interval=60.0, log=None):
    """Start the sleep blocker once; safe to call again."""
    global _keep_awake
    if not is_windows():
        return False
    with _keep_awake_lock:
        if _keep_awake is None:
            _keep_awake = KeepAwake(interval=interval, log=log)
        return _keep_awake.start()


def stop_keep_awake():
    global _keep_awake
    with _keep_awake_lock:
        keeper = _keep_awake
        _keep_awake = None
    if keeper is not None:
        keeper.stop()


def _run_netsh(arguments, timeout=NETSH_TIMEOUT):
    if not is_windows():
        raise OSError("Windows only")
    return subprocess.run(
        ["netsh", "advfirewall", "firewall", *arguments],
        capture_output=True,
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )


def _output_variants(result):
    """Decode console output with every code page Windows may have used."""
    data = (result.stdout or b"") + (result.stderr or b"")
    encodings = ["utf-8"]
    try:
        encodings.append("cp%d" % ctypes.windll.kernel32.GetOEMCP())
    except Exception:
        pass
    encodings.append("mbcs")
    variants = []
    for encoding in encodings:
        try:
            text = data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
        if text not in variants:
            variants.append(text)
    if not variants:
        variants.append(data.decode("latin-1", "replace"))
    return variants


def _output_text(result):
    return _output_variants(result)[0]


def firewall_rule_exists(executable=None, name=FIREWALL_RULE_NAME):
    """True when a rule with this name exists and lists the given program."""
    if not is_windows():
        return False
    try:
        # Only the verbose listing includes the program path of a rule.
        result = _run_netsh(["show", "rule", "name=" + name, "verbose"])
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    if executable:
        target = str(Path(executable)).casefold()
        if not any(target in text.casefold() for text in _output_variants(result)):
            return False
    return True


def add_firewall_rule(executable, name=FIREWALL_RULE_NAME, log=None):
    """Replace any stale rule with an inbound allow rule for this program."""
    if not is_windows():
        return False
    program = str(Path(executable).resolve())
    try:
        # A moved installation leaves the old path in the rule; delete by name
        # first so a single, current rule remains.
        _run_netsh(["delete", "rule", "name=" + name])
        result = _run_netsh([
            "add", "rule", "name=" + name, "dir=in", "action=allow",
            "program=" + program, "enable=yes", "profile=any",
            "description=" + FIREWALL_DESCRIPTION,
        ])
    except (OSError, subprocess.SubprocessError) as exc:
        _log(log, logging.WARNING, "Không thêm được ngoại lệ tường lửa: %s", exc)
        return False
    if result.returncode != 0:
        _log(log, logging.WARNING, "Không thêm được ngoại lệ tường lửa (mã %s): %s",
             result.returncode, _output_text(result).strip()[:300])
        return False
    _log(log, logging.INFO, "Đã thêm ngoại lệ tường lửa cho %s.", program)
    return True


def is_user_admin():
    if not is_windows():
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def is_admin_member():
    """True when the current Windows account belongs to Administrators."""
    if not is_windows():
        return False
    try:
        result = subprocess.run(
            ["whoami", "/groups"],
            capture_output=True,
            timeout=15,
            creationflags=CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return ADMIN_GROUP_SID in _output_text(result)


def _elevated_script(executable, name):
    program = str(Path(executable).resolve()).replace("'", "''")
    rule = name.replace("'", "''")
    return (
        "$ErrorActionPreference='Stop';"
        "$name='" + rule + "';"
        "$program='" + program + "';"
        "& netsh.exe advfirewall firewall delete rule name=$name 2>$null | Out-Null;"
        "& netsh.exe advfirewall firewall add rule name=$name dir=in action=allow "
        "program=$program enable=yes profile=any description='" + FIREWALL_DESCRIPTION + "' | Out-Null;"
        "exit $LASTEXITCODE"
    )


def launch_elevated_firewall_rule(executable, name=FIREWALL_RULE_NAME, log=None):
    """Ask UAC to run one netsh command.

    Returns "started" when the elevated helper was launched, "declined" when
    the user refused the prompt and "error" for any other failure.
    """
    if not is_windows():
        return "error"
    try:
        encoded = base64.b64encode(_elevated_script(executable, name).encode("utf-16-le")).decode("ascii")
        parameters = ("-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden "
                      "-EncodedCommand " + encoded)
        shell32 = ctypes.windll.shell32
        shell32.ShellExecuteW.restype = ctypes.c_void_p
        value = shell32.ShellExecuteW(None, "runas", "powershell.exe", parameters, None, 0)
    except Exception as exc:
        _log(log, logging.WARNING, "Không yêu cầu được quyền Administrator: %s", exc)
        return "error"
    if not value or int(value) <= 32:
        code = int(value or 0)
        # ShellExecute returns SE_ERR_ACCESSDENIED when the UAC prompt is
        # cancelled; every other small error means the helper could not run
        # (for example a missing PowerShell) and must not disable retries.
        if code == 5:
            _log(log, logging.WARNING, "Người dùng không cấp quyền Administrator cho ngoại lệ tường lửa.")
            return "declined"
        _log(log, logging.WARNING, "Không chạy được trình thêm ngoại lệ tường lửa (mã %s).", code)
        return "error"
    _log(log, logging.INFO, "Đã yêu cầu Windows nâng quyền để thêm ngoại lệ tường lửa.")
    return "started"


def _marker_path(base_dir):
    local = os.environ.get("LOCALAPPDATA")
    root = Path(local) / "Cambida" if local else (Path(base_dir) if base_dir else None)
    return root / FIREWALL_MARKER_NAME if root is not None else None


def _read_marker(marker):
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _declined_for(marker, program):
    data = _read_marker(marker)
    if not data or data.get("state") != "declined":
        return False
    return str(data.get("program", "")).casefold() == str(Path(program).resolve()).casefold()


def _write_marker(marker, program, state, log=None):
    if marker is None:
        return
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({
            "program": str(Path(program).resolve()),
            "state": state,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        _log(log, logging.WARNING, "Không ghi được đánh dấu bỏ qua tường lửa: %s", exc)


def ensure_firewall_exception(executable, *, name=FIREWALL_RULE_NAME, base_dir=None,
                              allow_elevation=True, check_interval=1.0,
                              elevation_wait=15.0, log=None):
    """Make sure an inbound allow rule exists for the given executable."""
    if not is_windows() or not executable:
        return False
    program = Path(executable)
    if not program.is_file():
        return False
    if firewall_rule_exists(program, name):
        _log(log, logging.INFO, "Ngoại lệ tường lửa đã có cho %s.", program)
        return True
    if is_user_admin():
        if add_firewall_rule(program, name, log=log) and firewall_rule_exists(program, name):
            return True
        _log(log, logging.WARNING, "Chưa thêm được ngoại lệ tường lửa dù đang có quyền quản trị.")
        return False
    if not allow_elevation:
        _log(log, logging.WARNING, "Chưa có ngoại lệ tường lửa; cần quyền Administrator.")
        return False
    marker = _marker_path(base_dir)
    if marker is not None and _declined_for(marker, program):
        _log(log, logging.INFO, "Bỏ qua nâng quyền tường lửa (người dùng đã từ chối): %s", marker)
        return False
    if not is_admin_member():
        _log(log, logging.WARNING,
             "Tài khoản hiện tại không thuộc nhóm Administrators; không thể tự thêm ngoại lệ tường lửa.")
        return False
    status = launch_elevated_firewall_rule(program, name, log=log)
    if status == "declined":
        _write_marker(marker, program, "declined", log=log)
        return False
    if status != "started":
        return False
    deadline = time.monotonic() + max(0.0, elevation_wait)
    while time.monotonic() < deadline:
        if firewall_rule_exists(program, name):
            _log(log, logging.INFO, "Đã thêm ngoại lệ tường lửa sau khi nâng quyền.")
            return True
        time.sleep(check_interval)
    _log(log, logging.WARNING,
         "Đã yêu cầu nâng quyền nhưng chưa thấy ngoại lệ tường lửa; lần khởi động sau sẽ kiểm tra lại.")
    return False
