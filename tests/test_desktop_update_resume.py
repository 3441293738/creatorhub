import hashlib
import io
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from desktop.update_download import download_file, DownloadCancelled

PAYLOAD = b"resumable-local-fixture" * 40000


class Response(io.BytesIO):
    def __init__(self, data, status=200, **headers):
        super().__init__(data)
        self.status = status
        self.headers = headers


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cache = Path(self.temp.name)
        self.digest = hashlib.sha256(PAYLOAD).hexdigest()
        self.info = {"download_url": "https://release.invalid/fixture", "sha256": self.digest, "size": len(PAYLOAD)}
        self.cancel = threading.Event()
        self.progress = Mock()

    def tearDown(self):
        self.temp.cleanup()

    def download(self, source, progress=None):
        return download_file(self.info, self.cache, self.cancel, progress or self.progress, source)

    def test_network_loss_retains_partial_then_resumes_after_new_client(self):
        cut = 300000
        with self.assertRaises(OSError):
            self.download(lambda _: Response(PAYLOAD[:cut]))
        self.assertEqual((self.cache / (self.digest + ".part")).stat().st_size, cut)
        def resume(url, offset=0):
            self.assertEqual(offset, cut)
            return Response(PAYLOAD[offset:], 206, **{"Content-Range": f"bytes {offset}-{len(PAYLOAD)-1}/{len(PAYLOAD)}"})
        path = self.download(resume)
        self.assertEqual(path.read_bytes(), PAYLOAD)
        self.assertFalse((self.cache / (self.digest + ".part")).exists())
        self.assertEqual(self.progress.call_args.args[2], cut)

    def test_cancel_preserves_bytes_and_full_cached_download_uses_no_network(self):
        def progress(done, total, resumed):
            if done > 0:
                self.cancel.set()
        with self.assertRaises(DownloadCancelled):
            self.download(lambda _: Response(PAYLOAD), progress)
        self.assertTrue((self.cache / (self.digest + ".part")).is_file())
        self.cancel.clear()
        # Range ignored: restart at zero, never append a second full response.
        self.download(lambda url, **kwargs: Response(PAYLOAD))
        source = Mock(side_effect=AssertionError("Cached file must not download again"))
        self.assertEqual(self.download(source).read_bytes(), PAYLOAD)
        source.assert_not_called()

    def test_invalid_ranges_and_encoding_are_discarded(self):
        for response in (Response(PAYLOAD[10:], 206, **{"Content-Range": "bytes 0-10/20"}),
                         Response(PAYLOAD, 200, **{"Content-Encoding": "gzip"}), Response(b"", 206)):
            with self.subTest(headers=response.headers):
                partial = self.cache / (self.digest + ".part")
                partial.write_bytes(PAYLOAD[:10])
                with self.assertRaises(ValueError):
                    self.download(lambda url, **kwargs: response)
                self.assertFalse(partial.exists())

    def test_corrupt_complete_or_partial_cache_is_never_installed(self):
        complete = self.cache / (self.digest + ".bin")
        complete.write_bytes(b"x" * len(PAYLOAD))
        self.assertEqual(self.download(lambda _: Response(PAYLOAD)).read_bytes(), PAYLOAD)
        complete.unlink()
        partial = self.cache / (self.digest + ".part")
        partial.write_bytes(b"x" * len(PAYLOAD))
        with self.assertRaises(ValueError):
            self.download(Mock())
        self.assertFalse(partial.exists())


if __name__ == "__main__":
    unittest.main()
