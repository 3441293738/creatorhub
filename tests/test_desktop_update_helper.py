"""Updater process protocol, exclusive package/instance locks and installation results."""
import json
import ctypes
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from desktop import update_helper as helper
from desktop.launcher import InstanceLock
from test_desktop_update_install import installation_fixture


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home, self.app, self.stage, self.info, self.artifact = installation_fixture(self.root)
        self.manifest = {**self.artifact, "schema": 1, "home": str(self.home.resolve()),
                         "install_dir": str(self.app.resolve()), "parent_pid": os.getpid()}
        self.request = self.stage / "request.json"
        helper.atomic_json(self.request, self.manifest)

    def tearDown(self):
        self.temp.cleanup()

    def result(self):
        return json.loads((self.home / "runtime" / "update-result.json").read_text(encoding="utf-8"))

    def test_request_paths_versions_and_installer_identity(self):
        data = helper.read_request(self.request)
        self.assertEqual(data["stage"], self.stage.resolve())
        helper.verify_installer(data)
        for field, invalid in (("schema", 2), ("parent_pid", 0), ("size", True),
                               ("sha256", "bad"), ("home", "."), ("version", "../0.3.0"),
                               ("installer", str(self.app / "CreatorHub.exe")),
                               ("version", "0.1.0")):
            with self.subTest(field=field, invalid=invalid):
                helper.atomic_json(self.request, {**self.manifest, field: invalid})
                with self.assertRaises((ValueError, OSError)):
                    helper.read_request(self.request)

    def test_package_and_user_home_cannot_be_inside_installation(self):
        misplaced = self.root / "request.json"
        helper.atomic_json(misplaced, self.manifest)
        with self.assertRaises(ValueError):
            helper.read_request(misplaced)
        helper.atomic_json(self.request, {**self.manifest, "install_dir": str(self.root)})
        with self.assertRaises(ValueError):
            helper.read_request(self.request)

    def test_install_arguments_are_literal_and_never_close_other_apps_or_reboot(self):
        data = helper.read_request(self.request)
        command = helper.installer_command(data)
        self.assertIn(f"/DIR={self.app.resolve()}", command)
        self.assertIn("/NORESTART", command)
        self.assertIn("/NOCLOSEAPPLICATIONS", command)
        self.assertIn("/NORESTARTAPPLICATIONS", command)
        self.assertNotIn("/CLOSEAPPLICATIONS", command)
        self.assertEqual(command[0], self.artifact["installer"])
        with patch.dict(os.environ, {"_PYI_APPLICATION_HOME_DIR": "old", "PYINSTALLER_RESET_ENVIRONMENT": "0"}):
            env = helper.clean_environment()
            self.assertEqual(env["PYINSTALLER_RESET_ENVIRONMENT"], "1")
            self.assertFalse(any(key.startswith("_PYI_") for key in env))

    def test_success_waits_for_parent_and_restarts_only_after_locks_are_released(self):
        events = []
        parent = Mock()
        parent.wait.side_effect = lambda _: events.append("parent_exited")
        def install(data):
            events.append("install")
            self.assertTrue((self.stage / "ready.json").is_file())
            # Both locks are held, even with no service previously running.
            with self.assertRaises((OSError, RuntimeError)):
                InstanceLock(self.home)
            (self.app / "_internal" / "desktop-version.txt").write_text("0.3.0", encoding="utf-8")
            return 0
        def restart(data):
            events.append("restart")
            lock = InstanceLock(self.home)
            lock.close()
        with patch.object(helper, "ParentProcess", return_value=parent), \
             patch.object(helper, "run_installer", side_effect=install), \
             patch.object(helper, "restart_app", side_effect=restart):
            self.assertEqual(helper.apply_update(self.request), 0)
        self.assertEqual(events, ["parent_exited", "install", "restart"])
        self.assertEqual(self.result()["status"], "installed")
        parent.close.assert_called_once()

    def test_parent_timeout_or_cancel_never_installs_or_restarts(self):
        parent = Mock()
        parent.wait.side_effect = TimeoutError("parent still running")
        with patch.object(helper, "ParentProcess", return_value=parent), \
             patch.object(helper, "run_installer") as install, patch.object(helper, "restart_app") as restart:
            self.assertEqual(helper.apply_update(self.request), 1)
            install.assert_not_called()
            restart.assert_not_called()
        self.assertTrue((self.stage / "failed.json").is_file())
        self.assertFalse((self.home / "runtime" / "update-result.json").exists())

    def test_locked_service_or_tampered_package_prevents_installer_execution(self):
        for scenario in ("service", "package"):
            with self.subTest(scenario=scenario):
                lock = InstanceLock(self.home, "service.lock") if scenario == "service" else None
                if scenario == "package":
                    Path(self.artifact["installer"]).write_bytes(b"tampered")
                try:
                    with patch.object(helper, "ParentProcess", return_value=Mock()), \
                         patch.object(helper, "run_installer") as install, patch.object(helper, "restart_app"):
                        self.assertEqual(helper.apply_update(self.request), 1)
                        install.assert_not_called()
                finally:
                    if lock:
                        lock.close()
                self.assertEqual(self.result()["status"], "install_error")

    def test_installer_exit_code_and_version_are_both_verified(self):
        for code in (1, 5, 8, 199, 0):  # Even exit 0 is insufficient if version did not change.
            with self.subTest(code=code), patch.object(helper, "ParentProcess", return_value=Mock()), \
                 patch.object(helper, "run_installer", return_value=code), patch.object(helper, "restart_app"):
                self.assertEqual(helper.apply_update(self.request), 1)
                self.assertEqual(self.result()["status"], "install_error")

    def test_reboot_required_does_not_restart_windows_or_launch_partial_version(self):
        with patch.object(helper, "ParentProcess", return_value=Mock()), \
             patch.object(helper, "run_installer", return_value=3010), \
             patch.object(helper, "notify_user") as notify, patch.object(helper, "restart_app") as restart:
            self.assertEqual(helper.apply_update(self.request), 0)
            restart.assert_not_called()
            notify.assert_called_once()
        self.assertEqual(self.result()["status"], "restart_required")

    @unittest.skipUnless(os.name == "nt", "Windows sharing and process handles")
    def test_windows_package_lock_and_parent_handle(self):
        with helper.locked_installer(self.artifact["installer"]) as stream:
            helper.verify_installer(self.manifest, stream)
            with self.assertRaises(OSError):
                Path(self.artifact["installer"]).write_bytes(b"overwrite")
        # A Windows venv uses a redirector: its sys.executable can differ from
        # the real process image. The packaged launcher has no such redirector.
        image = ctypes.create_unicode_buffer(32768)
        ctypes.windll.kernel32.GetModuleFileNameW(None, image, len(image))
        process = helper.ParentProcess(os.getpid(), Path(image.value))
        cancel = self.stage / "cancel"
        cancel.touch()
        try:
            with self.assertRaises(RuntimeError):
                process.wait(cancel, timeout=1)
        finally:
            process.close()


if __name__ == "__main__":
    unittest.main()
