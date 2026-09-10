"""Security and lifecycle contracts for the loopback desktop control surface."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, Mock
import urllib.request
import urllib.error

from desktop.launcher import prepare_home
from desktop.controller import Controller
from desktop.web_shell import ShellServer


class DesktopShellTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        prepare_home(self.home)
        self.controller = Controller(self.home, install_browser=False)
        self.controller.preferences["open_on_ready"] = False
        self.server = ShellServer(self.controller)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.controller.stop()
        if self.controller.worker:
            self.controller.worker.join(timeout=50)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, payload=None, **headers):
        defaults = {"X-Desktop-Token": self.server.token}
        defaults.update(headers)
        request = urllib.request.Request(self.server.origin + path,
            data=json.dumps(payload).encode() if payload is not None else None, headers=defaults)
        try:
            with self.opener.open(request) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_token_and_origin_required(self):
        self.assertEqual(self.request("/api/state", **{"X-Desktop-Token": "bad"})[0], 403)
        self.assertEqual(self.request("/api/action", {"name": "start"}, Origin="https://example.org")[0], 403)
        self.assertEqual(self.request("/api/state", Host="attacker.invalid")[0], 403)

    def test_only_approved_assets_are_served(self):
        self.assertEqual(self.request("/config.yaml")[0], 404)
        self.assertEqual(self.request("/../launcher.py")[0], 404)
        code, body = self.request("/")
        self.assertEqual(code, 200)
        self.assertIn(self.server.token.encode(), body)

    def test_preferences_persist_only_known_fields(self):
        self.controller.action("preferences", {"theme": "dark", "open_on_ready": False, "config": "ignored"})
        next_controller = Controller(self.home)
        self.assertEqual(next_controller.preferences, {"theme": "dark", "open_on_ready": False})
        with self.assertRaises(ValueError):
            self.controller.action("preferences", {"theme": "garbage"})

    def test_destructive_actions_need_confirmation(self):
        self.assertEqual(self.request("/api/action", {"name": "stop"})[0], 400)
        self.assertEqual(self.request("/api/action", {"name": "run_command", "data": {"command": "anything"}})[0], 400)

    def test_diagnostics_exclude_private_details(self):
        data = self.controller.action("export", {})["download"]
        self.assertEqual(set(data), {"app_version", "os", "os_release", "python", "service_state", "exit_code"})
        self.assertNotIn(str(self.home), json.dumps(data))

    def test_guide_destinations_are_allowlisted(self):
        with patch("desktop.controller.webbrowser.open") as opened:
            self.controller.action("open_guide", {"platform": "xhs"})
            self.assertTrue(opened.call_args.args[0].endswith("/guide/xhs/"))
            with self.assertRaises(ValueError):
                self.controller.action("open_guide", {"platform": "https://example.org"})

    def test_real_service_can_start_stop_and_start_again(self):
        for _ in range(2):
            self.controller.start()
            worker = self.controller.worker
            self.controller.start()
            self.assertIs(self.controller.worker, worker)
            deadline = time.monotonic() + 45
            while self.controller.state()["phase"] == "starting" and time.monotonic() < deadline:
                time.sleep(.1)
            self.assertEqual(self.controller.state()["phase"], "ready")
            self.assertTrue(self.controller.state()["url"].startswith("http://127.0.0.1:"))
            self.controller.action("stop", {"confirmed": True})
            worker.join(timeout=40)
            self.assertFalse(worker.is_alive())
            self.assertEqual(self.controller.state()["phase"], "stopped")
        self.assertTrue((self.home / "data" / "creatorhub.db").exists())

    def test_leftover_owned_child_prevents_duplicate_start(self):
        self.controller.phase = "error"
        self.controller.process = Mock()
        self.controller.process.poll.return_value = None
        with self.assertRaises(ValueError):
            self.controller.start()
        self.controller.process = None


if __name__ == "__main__":
    unittest.main()
