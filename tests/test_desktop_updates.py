import json
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
from desktop.updates import UpdateChecker, numeric_version, release_info, RELEASES_URL
from desktop.controller import Controller
from desktop.launcher import prepare_home


def release(tag="v0.3.0", asset=True):
    name = f"CreatorHub-Setup-{tag.removeprefix('v')}-windows-x64.exe"
    return {"tag_name": tag, "draft": False, "prerelease": False, "body": "更新说明\n<script>alert(1)</script>",
            "assets": [{"name": name, "state": "uploaded", "size": 123456,
                        "browser_download_url": f"{RELEASES_URL}/download/{tag}/{name}"}] if asset else []}


class DesktopUpdateTests(unittest.TestCase):
    def test_numeric_comparison(self):
        self.assertGreater(numeric_version("0.10.0"), numeric_version("0.9.0"))
        self.assertEqual(numeric_version("v1.2.3"), numeric_version("1.2.3.0"))
        for value in ("source", "1.0", "1.0.0-rc1", None, "../1.0.0"):
            self.assertIsNone(numeric_version(value))

    def test_available_current_and_development(self):
        for current, status in (("0.2.0", "available"), ("0.3.0", "current"), ("0.4.0", "current"), ("source", "development")):
            self.assertEqual(release_info(release(), current)["status"], status)

    def test_missing_or_untrusted_asset(self):
        self.assertEqual(release_info(release(asset=False), "0.2.0")["status"], "unavailable")
        data = release()
        data["assets"][0]["browser_download_url"] = "https://example.com/file.exe"
        self.assertIsNone(release_info(data, "0.2.0")["download_url"])

    def test_preview_and_invalid_tags_rejected(self):
        for field in ("draft", "prerelease"):
            data = release(); data[field] = True
            with self.assertRaises(ValueError):
                release_info(data, "0.2.0")
        with self.assertRaises(ValueError):
            release_info(release("nightly"), "0.2.0")

    def test_http_and_network_failures(self):
        for error, status in ((urllib.error.HTTPError("url", 404, "", {}, None), "empty"),
                              (urllib.error.HTTPError("url", 429, "", {}, None), "error"),
                              (TimeoutError(), "error"), (ValueError(), "error")):
            checker = UpdateChecker("0.2.0")
            with patch("desktop.updates.fetch_release", side_effect=error):
                checker.run()
            self.assertEqual(checker.state()["status"], status)
            self.assertNotIn("download_url", checker.state())

    def test_checks_are_single_flight_and_retryable(self):
        checker = UpdateChecker("0.2.0")
        gate = threading.Event()
        def fetch(_):
            gate.wait(2)
            return release_info(release(), "0.2.0")
        with patch("desktop.updates.fetch_release", side_effect=fetch) as request:
            checker.check(); worker = checker.worker; checker.check()
            self.assertIs(worker, checker.worker)
            self.assertEqual(checker.state()["status"], "checking")
            gate.set(); worker.join(3)
            self.assertEqual(request.call_count, 1)
            self.assertEqual(checker.state()["status"], "available")
            with self.assertRaises(ValueError): checker.check()
            checker.last_attempt -= 6
            checker.check(); checker.worker.join(3)
            self.assertEqual(request.call_count, 2)

    def test_download_confirmation_and_stale_version(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp); prepare_home(home)
            controller = Controller(home, install_browser=False)
            controller.updates.result = release_info(release(), "0.2.0")
            with patch("desktop.controller.webbrowser.open", return_value=True) as opened:
                with self.assertRaises(ValueError): controller.action("download_update", {"tag": "v0.3.0"})
                with self.assertRaises(ValueError): controller.action("download_update", {"tag": "v0.2.0", "confirmed": True})
                opened.assert_not_called()
                controller.action("download_update", {"tag": "v0.3.0", "confirmed": True, "url": "https://example.com"})
                opened.assert_called_once_with(controller.updates.result["download_url"])
                self.assertEqual(controller.phase, "stopped")
                self.assertIsNone(controller.process)


if __name__ == "__main__": unittest.main()
