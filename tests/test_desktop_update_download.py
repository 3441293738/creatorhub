"""Offline update downloads: no real release or executable is fetched/run."""
import hashlib
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.request

from desktop.updates import (UpdateChecker, release_info, checksum_for_release,
                             allowed_download_url, ReleaseRedirectHandler, MAX_INSTALLER_SIZE)
from test_desktop_updates import release

PACKAGE = b"MZ-not-an-executable-local-update-fixture\n" * 18000


def verified_release(package=PACKAGE, *, checksum=True, digest=True):
    raw = release()
    asset = raw["assets"][0]
    asset["size"] = len(package)
    if digest:
        asset["digest"] = "sha256:" + hashlib.sha256(package).hexdigest()
    if checksum:
        raw["assets"].append({"name": "SHA256.txt", "state": "uploaded", "size": 130,
            "browser_download_url": asset["browser_download_url"].rsplit("/", 1)[0] + "/SHA256.txt"})
    return release_info(raw, "0.2.0")


class Response(io.BytesIO):
    def __init__(self, data, length=None):
        super().__init__(data)
        self.status = 200
        self.headers = {} if length is None else {"Content-Length": str(length)}


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cache = Path(self.temp.name) / "updates"
        self.cache.mkdir()
        self.sentinel = self.cache / "unrelated.txt"
        self.sentinel.write_text("preserve")
        self.checker = UpdateChecker("0.2.0", self.cache)
        self.info = verified_release()
        self.checker.result = dict(self.info)

    def tearDown(self):
        self.checker.cancel_download()
        if self.checker.worker:
            self.checker.worker.join(3)
            self.assertFalse(self.checker.worker.is_alive())
        self.assertEqual(self.sentinel.read_text(), "preserve")
        self.temp.cleanup()

    def source(self, url, offset=0):
        if url.endswith("SHA256.txt"):
            text = f"{hashlib.sha256(PACKAGE).hexdigest()}  {self.info['asset_name']}\n"
            return Response(text.encode())
        response = Response(PACKAGE[offset:], len(PACKAGE) - offset)
        if offset:
            response.status = 206
            response.headers["Content-Range"] = f"bytes {offset}-{len(PACKAGE) - 1}/{len(PACKAGE)}"
        return response

    def download(self, source=None):
        with patch("desktop.updates.open_asset", side_effect=source or self.source):
            self.checker.download(self.info["tag"])
            self.checker.worker.join(5)
        self.assertFalse(self.checker.worker.is_alive())

    def test_verified_stream_progress_and_artifact_are_separate_from_public_state(self):
        self.download()
        state = self.checker.state()
        self.assertEqual(state["status"], "downloaded")
        self.assertEqual(state["progress"], 100)
        self.assertEqual(state["downloaded_bytes"], len(PACKAGE))
        self.assertNotIn(str(self.cache), str(state))
        artifact = self.checker.begin_install(self.info["tag"])
        self.assertEqual(Path(artifact["installer"]).read_bytes(), PACKAGE)
        self.assertEqual(artifact["sha256"], hashlib.sha256(PACKAGE).hexdigest())
        self.assertEqual(self.checker.state()["status"], "preparing")
        with self.assertRaises(ValueError):
            self.checker.check()
        with self.assertRaises(ValueError):
            self.checker.begin_install(self.info["tag"])

    def test_checksum_and_github_digest_each_supported_but_must_agree(self):
        for checksum, digest in ((True, False), (False, True), (True, True)):
            with self.subTest(checksum=checksum, digest=digest):
                info = verified_release(checksum=checksum, digest=digest)
                with patch("desktop.updates.open_asset", side_effect=self.source):
                    self.assertEqual(checksum_for_release(info), hashlib.sha256(PACKAGE).hexdigest())
        self.info["asset_sha256"] = "0" * 64
        with patch("desktop.updates.open_asset", side_effect=self.source), self.assertRaises(ValueError):
            checksum_for_release(self.info)

    def test_missing_duplicate_wrong_filename_or_oversized_checksum_is_rejected(self):
        good = f"{'0' * 64}  {self.info['asset_name']}\n"
        for raw in (b"", b"not a checksum", good.encode() * 2,
                    f"{'0' * 64}  different.exe".encode(), b"a" * 65537):
            with self.subTest(size=len(raw)), patch("desktop.updates.open_asset", return_value=Response(raw)):
                with self.assertRaises(ValueError):
                    checksum_for_release(self.info)

    def test_bad_package_is_discarded_and_retry_works(self):
        cases = (Response(PACKAGE[:-1]), Response(PACKAGE + b"extra"),
                 Response(b"x" * len(PACKAGE)), Response(PACKAGE, 2))
        for response in cases:
            with self.subTest(length=response.headers):
                self.checker.result = dict(self.info)
                self.download(lambda url, **kwargs: self.source(url) if url.endswith("SHA256.txt") else response)
                self.assertEqual(self.checker.state()["status"], "download_error")
                self.assertIsNone(self.checker.artifact)
                self.assertEqual(list(self.cache.glob("update-*")), [])
                with self.assertRaises(ValueError):
                    self.checker.begin_install(self.info["tag"])
        self.download()
        self.assertEqual(self.checker.state()["status"], "downloaded")

    def test_cancellation_and_single_flight_leave_service_independent(self):
        entered, gate = threading.Event(), threading.Event()
        def blocking(url):
            entered.set()
            gate.wait(3)
            return self.source(url)
        with patch("desktop.updates.open_asset", side_effect=blocking):
            self.checker.download(self.info["tag"])
            self.assertTrue(entered.wait(2))
            with self.assertRaises(ValueError):
                self.checker.download(self.info["tag"])
            with self.assertRaises(ValueError):
                self.checker.check()
            self.checker.cancel_download()
            gate.set()
            self.checker.worker.join(3)
        self.assertEqual(self.checker.state()["status"], "cancelled")
        self.assertIsNone(self.checker.artifact)
        self.download()
        self.assertEqual(self.checker.state()["status"], "downloaded")

    def test_stale_version_unverified_release_disk_and_network_failures(self):
        with self.assertRaises(ValueError):
            self.checker.download("v0.1.0")
        self.checker.result = verified_release(checksum=False, digest=False)
        with self.assertRaises(ValueError):
            self.checker.download(self.info["tag"])
        self.checker.result = dict(self.info)
        with patch("desktop.updates.shutil.disk_usage") as usage:
            usage.return_value.free = 1
            self.download()
        self.assertEqual(self.checker.state()["status"], "download_error")
        self.download(lambda _: (_ for _ in ()).throw(TimeoutError("private network detail")))
        self.assertEqual(self.checker.state()["status"], "download_error")
        self.assertNotIn("private network detail", self.checker.state()["message"])

    def test_release_assets_and_redirects_are_allowlisted(self):
        for url in ("http://github.com/a", "https://example.org/file.exe",
                    "https://github.com/another/repo/releases/download/v1/a.exe",
                    "https://user:secret@objects.githubusercontent.com/a",
                    "https://objects.githubusercontent.com:444/a"):
            self.assertFalse(allowed_download_url(url), url)
        self.assertTrue(allowed_download_url(self.info["download_url"]))
        self.assertTrue(allowed_download_url("https://release-assets.githubusercontent.com/id/file?sig=fixture"))
        with self.assertRaises(ValueError):
            ReleaseRedirectHandler().redirect_request(urllib.request.Request(self.info["download_url"]),
                None, 302, "", {}, "https://example.org/file.exe")
        raw = release()
        raw["assets"][0]["size"] = MAX_INSTALLER_SIZE + 1
        self.assertIsNone(release_info(raw, "0.2.0")["download_url"])


if __name__ == "__main__":
    unittest.main()
