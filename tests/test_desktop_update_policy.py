"""Reminder/scheduling tests: fake clock and release responses, never real downloads."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from desktop.controller import Controller
from desktop.launcher import prepare_home
from desktop.update_policy import CHECK_INTERVAL, UpdatePolicy
from desktop.updates import UpdateChecker, release_info
from test_desktop_updates import release


class UpdatePolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def test_defaults_persist_and_validate_saved_values(self):
        path = self.root / "preferences.json"
        policy = UpdatePolicy(path)
        self.assertTrue(policy.due(1000000))
        policy.save(auto_check=False, last_check=1000000)
        self.assertFalse(UpdatePolicy(path).due(2000000))
        for raw in ('[]', 'null', 'broken', 'x' * 9000,
                    '{"auto_check": "false", "last_check": NaN, "deferred_until": true}'):
            path.write_text(raw)
            policy = UpdatePolicy(path)
            self.assertTrue(policy.data["auto_check"])
            self.assertTrue(policy.due(1000000))

    def test_one_check_per_day_and_clock_rollback(self):
        policy = UpdatePolicy()
        policy.save(last_check=1000000)
        self.assertFalse(policy.due(1000000 + CHECK_INTERVAL - 1))
        self.assertTrue(policy.due(1000000 + CHECK_INTERVAL))
        self.assertTrue(policy.due(999999))

    def test_reminders_are_scoped_to_version_and_expire(self):
        path = self.root / "preferences.json"
        policy = UpdatePolicy(path)
        policy.dismiss("v0.3.0", 1000000)
        policy = UpdatePolicy(path)
        self.assertTrue(policy.suppressed("v0.3.0", 1000001))
        self.assertFalse(policy.suppressed("v0.4.0", 1000001))
        self.assertFalse(policy.suppressed("v0.3.0", 1000000 + CHECK_INTERVAL))
        self.assertFalse(policy.suppressed("v0.3.0", 999999))
        policy.dismiss("v0.3.0", 1000000, skip=True)
        self.assertTrue(UpdatePolicy(path).suppressed("v0.3.0", 3000000))
        self.assertFalse(UpdatePolicy(path).suppressed("v0.4.0", 3000000))

    def test_write_failures_do_not_claim_preference_was_saved(self):
        policy = UpdatePolicy(self.root / "preferences.json")
        with patch.object(Path, "write_text", side_effect=PermissionError()):
            with self.assertRaises(ValueError):
                policy.save(auto_check=False)
            self.assertTrue(policy.data["auto_check"])
            policy.save(last_check=1000000, best_effort=True)
            self.assertFalse(policy.due(1000001))

    def checker(self):
        checker = UpdateChecker("0.2.0", self.root / "updates", self.root / "app")
        self.addCleanup(checker.stop_monitor)
        return checker

    def test_automatic_checks_do_not_download_and_survive_restart_throttling(self):
        checker = self.checker()
        info = release_info(release(), "0.2.0")
        with patch("desktop.updates.fetch_release", return_value=info) as fetch, \
             patch("desktop.updates.open_asset") as asset, patch.object(checker, "download") as download:
            self.assertTrue(checker.check(automatic=True))
            checker.worker.join(3)
            self.assertTrue(checker.state()["notification"])
            self.assertFalse(checker.check(automatic=True))
            restarted = self.checker()
            self.assertFalse(restarted.check(automatic=True))
            fetch.assert_called_once()
            asset.assert_not_called()
            download.assert_not_called()

    def test_source_preview_never_starts_automatic_networking(self):
        for checker in (UpdateChecker("source", self.root, self.root / "app"),
                        UpdateChecker("0.2.0", self.root)):
            with patch("desktop.updates.fetch_release") as fetch:
                checker.start_monitor()
                self.assertIsNone(checker.monitor)
                self.assertFalse(checker.check(automatic=True))
                fetch.assert_not_called()

    def test_manual_check_works_when_disabled_or_version_was_skipped(self):
        checker = self.checker()
        info = release_info(release(), "0.2.0")
        checker.result = info
        checker.dismiss(info["tag"], skip=True)
        checker.configure(False)
        self.assertFalse(checker.state()["notification"])
        with patch("desktop.updates.fetch_release", return_value=info) as fetch:
            self.assertFalse(checker.check(automatic=True))
            self.assertTrue(checker.check())
            checker.worker.join(3)
            fetch.assert_called_once()
            self.assertTrue(checker.state()["notification"])
        checker.dismiss(info["tag"])
        self.assertFalse(checker.state()["notification"])
        checker.result = release_info(release("v0.4.0"), "0.2.0")
        self.assertTrue(checker.state()["notification"])

    def test_errors_are_quiet_and_throttled(self):
        checker = self.checker()
        with patch("desktop.updates.fetch_release", side_effect=TimeoutError()) as fetch:
            checker.check(automatic=True)
            checker.worker.join(3)
            self.assertEqual(checker.state()["status"], "error")
            self.assertFalse(checker.state()["notification"])
            self.assertFalse(checker.check(automatic=True))
            fetch.assert_called_once()

    def test_background_checks_preserve_download_install_and_recovery_states(self):
        checker = self.checker()
        for status in ("downloading", "verifying", "downloaded", "preparing", "installing",
                       "download_error", "cancelled", "install_error", "restart_required", "installed"):
            checker.result = {"status": status, "tag": "v0.3.0", "progress": 75}
            with patch("desktop.updates.fetch_release") as fetch:
                self.assertFalse(checker.check(automatic=True))
                self.assertEqual(checker.result["progress"], 75)
                fetch.assert_not_called()
        checker.artifact = {"tag": "v0.3.0"}
        checker.result["status"] = "downloaded"
        with self.assertRaises(ValueError):
            checker.check()
        self.assertEqual(checker.artifact, {"tag": "v0.3.0"})

    def test_monitor_is_singleton_delayed_and_stops_promptly(self):
        checker = self.checker()
        with patch("desktop.updates.fetch_release") as fetch:
            checker.start_monitor()
            monitor = checker.monitor
            checker.start_monitor()
            self.assertIs(checker.monitor, monitor)
            checker.stop_monitor()
            self.assertFalse(monitor.is_alive())
            self.assertFalse(checker.check(automatic=True))
            fetch.assert_not_called()

    def test_controller_validation_and_snooze_do_not_touch_tasks_or_packages(self):
        prepare_home(self.root)
        controller = Controller(self.root, install_browser=False)
        for invalid in (None, 1, "false", [], {}):
            with self.assertRaises(ValueError):
                controller.action("update_preferences", {"auto_check": invalid})
        controller.action("update_preferences", {"auto_check": False, "download": True})
        self.assertFalse(Controller(self.root).updates.state()["auto_check"])
        controller.updates.result = release_info(release(), "0.2.0")
        with self.assertRaises(ValueError):
            controller.action("skip_update", {"tag": "v0.4.0"})
        controller.action("skip_update", {"tag": "v0.3.0"})
        self.assertFalse(controller.updates.state()["notification"])
        self.assertIsNone(controller.process)
        self.assertIsNone(controller.worker)
        self.assertIsNone(controller.updates.worker)
        self.assertIsNone(controller.updates.artifact)


if __name__ == "__main__":
    unittest.main()
