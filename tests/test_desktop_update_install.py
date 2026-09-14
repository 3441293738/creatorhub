"""Controller handoff contract. Fixtures are text files, never executed."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from desktop.controller import Controller
from desktop.launcher import prepare_home
from desktop.update_helper import atomic_json
from test_desktop_update_download import PACKAGE, verified_release


def installation_fixture(root):
    home = root / "用户 data [fixture]"
    prepare_home(home)
    app = root / "应用 folder [fixture]"
    internal = app / "_internal"
    (internal / "desktop").mkdir(parents=True)
    (internal / "desktop-version.txt").write_text("0.2.0", encoding="utf-8")
    (internal / "desktop" / "CreatorHubUpdater.exe").write_bytes(b"helper-fixture")
    (app / "CreatorHub.exe").write_bytes(b"old-app-fixture")
    info = verified_release()
    stage = home / "runtime" / "updates" / ("update-" + "a" * 32)
    stage.mkdir(parents=True)
    installer = stage / info["asset_name"]
    installer.write_bytes(PACKAGE)
    artifact = {"installer": str(installer), "sha256": hashlib.sha256(PACKAGE).hexdigest(),
                "size": len(PACKAGE), "version": info["version"], "tag": info["tag"]}
    return home, app, stage, info, artifact


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home, self.app, self.stage, self.info, self.artifact = installation_fixture(Path(self.temp.name))
        self.controller = Controller(self.home, install_browser=False)
        self.controller.updates.result = {**self.info, "status": "downloaded", "progress": 100}
        self.controller.updates.artifact = dict(self.artifact)
        self.controller.exit_window = Mock()
        self.patches = [patch.object(self.controller, "update_install_supported", return_value=True),
                        patch("desktop.controller.sys.executable", str(self.app / "CreatorHub.exe")),
                        patch("desktop.controller.resources", return_value=self.app / "_internal")]
        for item in self.patches:
            item.start()

    def tearDown(self):
        if self.controller.install_worker:
            self.controller.install_worker.join(3)
            self.assertFalse(self.controller.install_worker.is_alive())
        for item in reversed(self.patches):
            item.stop()
        self.assertEqual((self.app / "CreatorHub.exe").read_bytes(), b"old-app-fixture")
        self.temp.cleanup()

    def install(self):
        result = self.controller.action("install_update", {"confirmed": True, "tag": self.info["tag"]})
        self.assertTrue(result["ok"])
        self.controller.install_worker.join(5)
        self.assertFalse(self.controller.install_worker.is_alive())

    def test_confirmation_current_tag_and_native_context_are_required(self):
        for data in ({}, {"tag": self.info["tag"]}, {"confirmed": True, "tag": "v0.1.0"}):
            with self.assertRaises(ValueError):
                self.controller.action("install_update", data)
        with patch.object(self.controller, "update_install_supported", return_value=False), self.assertRaises(ValueError):
            self.controller.action("install_update", {"confirmed": True, "tag": self.info["tag"]})
        self.controller.exit_window.assert_not_called()

    def test_handoff_order_stop_backup_helper_ack_then_exit(self):
        order = []
        def start_helper(args, **kwargs):
            order.append("helper")
            self.assertEqual(Path(args[0]).parent, self.stage)
            self.assertEqual(args[1], "--request")
            self.assertEqual(kwargs["cwd"], self.stage)
            self.assertEqual(kwargs["env"]["PYINSTALLER_RESET_ENVIRONMENT"], "1")
            atomic_json(self.stage / "ready.json", {"attempt": self.stage.name})
            return Mock(poll=Mock(return_value=None))
        self.controller.exit_window.side_effect = lambda: order.append("exit")
        with patch.object(self.controller, "stop", side_effect=lambda: order.append("stop")), \
             patch("desktop.controller.snapshot", side_effect=lambda _: order.append("backup")), \
             patch("desktop.controller.subprocess.Popen", side_effect=start_helper):
            self.install()
        self.assertEqual(order, ["stop", "backup", "helper", "exit"])
        self.assertEqual(self.controller.updates.state()["status"], "installing")
        self.assertTrue(self.controller.installing_update)
        for name in ("start", "exit", "prepare_update", "install_update", "check_updates"):
            with self.assertRaises(ValueError):
                self.controller.action(name, {"confirmed": True})

    def test_modified_package_does_not_stop_service_or_launch_anything(self):
        Path(self.artifact["installer"]).write_bytes(b"modified")
        with patch.object(self.controller, "stop") as stop, patch("desktop.controller.subprocess.Popen") as spawn:
            self.install()
            stop.assert_not_called()
            spawn.assert_not_called()
        self.assertEqual(self.controller.updates.state()["status"], "install_error")
        self.assertIsNone(self.controller.updates.artifact)
        self.assertFalse(self.controller.installing_update)
        self.controller.exit_window.assert_not_called()

    def test_backup_failure_keeps_old_application_and_allows_service_restart(self):
        with patch("desktop.controller.snapshot", side_effect=OSError("backup failed")), \
             patch("desktop.controller.subprocess.Popen") as spawn:
            self.install()
            spawn.assert_not_called()
        self.assertFalse(self.controller.installing_update)
        self.assertEqual(self.controller.phase, "stopped")
        self.controller.exit_window.assert_not_called()
        self.assertIsNotNone(self.controller.updates.artifact)

    def test_active_service_or_failed_helper_prevents_exit(self):
        self.controller.process = Mock(poll=Mock(return_value=None))
        with patch.object(self.controller, "stop"), patch("desktop.controller.snapshot") as backup:
            self.install()
            backup.assert_not_called()
        self.controller.process = None
        with patch("desktop.controller.snapshot"), \
             patch("desktop.controller.subprocess.Popen", return_value=Mock(poll=Mock(return_value=1))):
            self.install()
        self.controller.exit_window.assert_not_called()
        self.assertFalse(self.controller.installing_update)

    def test_mutual_exclusion_while_backup_is_running(self):
        entered, gate = threading.Event(), threading.Event()
        def backup(_):
            entered.set()
            gate.wait(3)
            raise OSError("fixture ends before install")
        with patch("desktop.controller.snapshot", side_effect=backup):
            self.controller.action("install_update", {"confirmed": True, "tag": self.info["tag"]})
            self.assertTrue(entered.wait(2))
            with self.assertRaises(ValueError):
                self.controller.start()
            with self.assertRaises(ValueError):
                self.controller.action("stop", {"confirmed": True})
            gate.set()
            self.controller.install_worker.join(3)
        self.assertFalse(self.controller.installing_update)

    def test_result_is_reported_once_and_never_restarts_tasks(self):
        atomic_json(self.home / "runtime" / "update-result.json", {"status": "installed", "version": "0.3.0"})
        with patch("desktop.controller.version", return_value="0.3.0"):
            next_controller = Controller(self.home)
        self.assertEqual(next_controller.updates.state()["status"], "installed")
        self.assertEqual(next_controller.phase, "stopped")
        self.assertIsNone(next_controller.process)
        self.assertFalse((self.home / "runtime" / "update-result.json").exists())

    def test_interrupted_handoff_is_reported_on_next_launch(self):
        atomic_json(self.home / "runtime" / "update-pending.json", {"version": "0.3.0"})
        next_controller = Controller(self.home)
        self.assertEqual(next_controller.updates.state()["status"], "install_error")
        self.assertIn("未确认完成", next_controller.updates.state()["message"])
        self.assertFalse((self.home / "runtime" / "update-pending.json").exists())


if __name__ == "__main__":
    unittest.main()
