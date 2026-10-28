"""Windows sleep-blocker and firewall provisioning regressions."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("windows_integration_tested", ROOT / "windows_integration.py")
WINDOWS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WINDOWS)

COMBINED = WINDOWS.ES_CONTINUOUS | WINDOWS.ES_SYSTEM_REQUIRED


class KeepAwakeTests(unittest.TestCase):
    def test_asserts_and_releases_system_required_state(self):
        calls = []
        keeper = WINDOWS.KeepAwake(interval=0.05, state_setter=calls.append)
        with patch.object(WINDOWS, "is_windows", return_value=True):
            self.assertTrue(keeper.start())
            time.sleep(0.3)
            keeper.stop()
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(calls[0], COMBINED)
        self.assertEqual(calls[-1], WINDOWS.ES_CONTINUOUS)
        self.assertTrue(all(value == COMBINED for value in calls[:-1]))

    def test_noop_without_windows(self):
        calls = []
        keeper = WINDOWS.KeepAwake(interval=1, state_setter=calls.append)
        with patch.object(WINDOWS, "is_windows", return_value=False):
            self.assertFalse(keeper.start())
        self.assertEqual(calls, [])

    def test_global_start_is_idempotent_and_stops(self):
        with patch.object(WINDOWS, "is_windows", return_value=True), \
                patch.object(WINDOWS, "KeepAwake") as keeper_class:
            keeper = keeper_class.return_value
            keeper.start.return_value = True
            WINDOWS._keep_awake = None
            self.assertTrue(WINDOWS.start_keep_awake(interval=5))
            self.assertTrue(WINDOWS.start_keep_awake(interval=5))
            self.assertEqual(keeper_class.call_count, 1)
            WINDOWS.stop_keep_awake()
            keeper.stop.assert_called_once()
            self.assertIsNone(WINDOWS._keep_awake)


class FirewallRuleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.exe = self.base / "Cambida.exe"
        self.exe.write_bytes(b"fake")

    def tearDown(self):
        self.temp.cleanup()

    def _result(self, returncode=0, stdout=b"", stderr=b""):
        return Mock(returncode=returncode, stdout=stdout, stderr=stderr)

    def test_rule_must_name_the_current_program(self):
        output = ("Rule Name: Cambida CCTV\r\nEnabled: Yes\r\nDirection: In\r\n"
                  "Program: " + str(self.exe.resolve()) + "\r\nAction: Allow\r\n").encode("mbcs")
        with patch.object(WINDOWS, "is_windows", return_value=True), \
                patch.object(WINDOWS, "_run_netsh", return_value=self._result(stdout=output)):
            self.assertTrue(WINDOWS.firewall_rule_exists(self.exe))
            self.assertFalse(WINDOWS.firewall_rule_exists(self.base / "Moved.exe"))

    def test_missing_rule_returns_false(self):
        with patch.object(WINDOWS, "is_windows", return_value=True), \
                patch.object(WINDOWS, "_run_netsh", return_value=self._result(1, b"No rules match.")):
            self.assertFalse(WINDOWS.firewall_rule_exists(self.exe))

    def test_add_rule_replaces_stale_entry_then_allows_inbound_program(self):
        calls = []

        def fake_netsh(arguments, timeout=20):
            calls.append(list(arguments))
            return self._result()

        with patch.object(WINDOWS, "is_windows", return_value=True), \
                patch.object(WINDOWS, "_run_netsh", side_effect=fake_netsh):
            self.assertTrue(WINDOWS.add_firewall_rule(self.exe))
        self.assertEqual(calls[0], ["delete", "rule", "name=" + WINDOWS.FIREWALL_RULE_NAME])
        added = calls[1]
        self.assertIn("add", added)
        self.assertIn("dir=in", added)
        self.assertIn("action=allow", added)
        self.assertIn("program=" + str(self.exe.resolve()), added)
        self.assertIn("enable=yes", added)
        self.assertIn("profile=any", added)

    def test_add_rule_failure_is_reported(self):
        with patch.object(WINDOWS, "is_windows", return_value=True), \
                patch.object(WINDOWS, "_run_netsh", return_value=self._result(5, b"Requires elevation")):
            self.assertFalse(WINDOWS.add_firewall_rule(self.exe))

    def test_existing_rule_short_circuits(self):
        with patch.object(WINDOWS, "is_windows", return_value=True), \
                patch.object(WINDOWS, "firewall_rule_exists", return_value=True), \
                patch.object(WINDOWS, "add_firewall_rule") as add:
            self.assertTrue(WINDOWS.ensure_firewall_exception(self.exe))
        add.assert_not_called()

    def test_elevated_process_adds_and_verifies_rule(self):
        with patch.object(WINDOWS, "is_windows", return_value=True), \
                patch.object(WINDOWS, "firewall_rule_exists", side_effect=[False, True]), \
                patch.object(WINDOWS, "is_user_admin", return_value=True), \
                patch.object(WINDOWS, "add_firewall_rule", return_value=True) as add:
            self.assertTrue(WINDOWS.ensure_firewall_exception(self.exe))
        add.assert_called_once()

    def test_elevated_add_failure_is_reported(self):
        with patch.object(WINDOWS, "is_windows", return_value=True), \
                patch.object(WINDOWS, "firewall_rule_exists", return_value=False), \
                patch.object(WINDOWS, "is_user_admin", return_value=True), \
                patch.object(WINDOWS, "add_firewall_rule", return_value=False):
            self.assertFalse(WINDOWS.ensure_firewall_exception(self.exe))

    def test_unelevated_without_permission_does_not_prompt(self):
        with patch.object(WINDOWS, "is_windows", return_value=True), \
                patch.object(WINDOWS, "firewall_rule_exists", return_value=False), \
                patch.object(WINDOWS, "is_user_admin", return_value=False), \
                patch.object(WINDOWS, "launch_elevated_firewall_rule") as launch:
            self.assertFalse(WINDOWS.ensure_firewall_exception(
                self.exe, base_dir=self.base, allow_elevation=False))
        launch.assert_not_called()

    def test_non_admin_account_does_not_prompt(self):
        with patch.object(WINDOWS, "is_windows", return_value=True), \
                patch.object(WINDOWS, "firewall_rule_exists", return_value=False), \
                patch.object(WINDOWS, "is_user_admin", return_value=False), \
                patch.object(WINDOWS, "is_admin_member", return_value=False), \
                patch.object(WINDOWS, "launch_elevated_firewall_rule") as launch:
            self.assertFalse(WINDOWS.ensure_firewall_exception(self.exe, base_dir=self.base))
        launch.assert_not_called()
        self.assertFalse((self.base / WINDOWS.FIREWALL_MARKER_NAME).exists())

    def test_declined_elevation_is_remembered_and_not_repeated(self):
        with patch.object(WINDOWS, "is_windows", return_value=True), \
                patch.object(WINDOWS, "firewall_rule_exists", return_value=False), \
                patch.object(WINDOWS, "is_user_admin", return_value=False), \
                patch.object(WINDOWS, "is_admin_member", return_value=True), \
                patch.object(WINDOWS, "launch_elevated_firewall_rule", return_value=False) as launch:
            self.assertFalse(WINDOWS.ensure_firewall_exception(self.exe, base_dir=self.base))
            self.assertTrue((self.base / WINDOWS.FIREWALL_MARKER_NAME).exists())
            self.assertFalse(WINDOWS.ensure_firewall_exception(self.exe, base_dir=self.base))
        launch.assert_called_once()

    def test_elevation_success_is_confirmed_by_polling(self):
        with patch.object(WINDOWS, "is_windows", return_value=True), \
                patch.object(WINDOWS, "firewall_rule_exists", side_effect=[False, True]), \
                patch.object(WINDOWS, "is_user_admin", return_value=False), \
                patch.object(WINDOWS, "is_admin_member", return_value=True), \
                patch.object(WINDOWS, "launch_elevated_firewall_rule", return_value=True) as launch:
            self.assertTrue(WINDOWS.ensure_firewall_exception(
                self.exe, base_dir=self.base, check_interval=0.01, elevation_wait=1.0))
        launch.assert_called_once()
        self.assertFalse((self.base / WINDOWS.FIREWALL_MARKER_NAME).exists())

    def test_elevation_that_never_creates_the_rule_marks_the_install(self):
        with patch.object(WINDOWS, "is_windows", return_value=True), \
                patch.object(WINDOWS, "firewall_rule_exists", return_value=False), \
                patch.object(WINDOWS, "is_user_admin", return_value=False), \
                patch.object(WINDOWS, "is_admin_member", return_value=True), \
                patch.object(WINDOWS, "launch_elevated_firewall_rule", return_value=True):
            self.assertFalse(WINDOWS.ensure_firewall_exception(
                self.exe, base_dir=self.base, check_interval=0.01, elevation_wait=0.05))
        self.assertTrue((self.base / WINDOWS.FIREWALL_MARKER_NAME).exists())

    def test_missing_executable_is_skipped(self):
        with patch.object(WINDOWS, "is_windows", return_value=True):
            self.assertFalse(WINDOWS.ensure_firewall_exception(self.base / "missing.exe"))


if __name__ == "__main__":
    unittest.main()
