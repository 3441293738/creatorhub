"""Offline contracts for metadata, disk snapshots and the whole-publish budget."""
import asyncio
import hashlib
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image
from curl_cffi import CurlOpt
from curl_cffi.curl import CURL_READFUNC_ABORT

from app.platforms.xhs import creator_api, creator_sign, deadline, media, publish
from app.platforms.xhs.responses import XhsApiError
from test_xhs_creator_contract import creator, Response


STATE = '{"cookies":[{"name":"a1","value":"fixture"}]}'


@pytest.fixture
def media_path(tmp_path):
    path = tmp_path / "fixture.jpg"
    path.write_bytes(b"fixture")
    return path


def image_bytes(fmt, size=(100, 10)):
    buf = io.BytesIO()
    Image.new("RGB", size, "red").save(buf, format=fmt)
    return buf.getvalue()


@pytest.mark.parametrize("fmt,mime", [("JPEG", "image/jpeg"), ("PNG", "image/png"),
                                      ("WEBP", "image/webp")])
@pytest.mark.parametrize("disk_backed", [False, True])
def test_image_metadata_matches_actual_bytes_not_extension(tmp_path, fmt, mime, disk_backed):
    blob = image_bytes(fmt)
    path = tmp_path / "misleading.bin"
    path.write_bytes(blob)
    source = media.MediaFile(path, len(blob)) if disk_backed else blob
    assert creator_api._image_info(source) == (100, 10, len(blob), mime)
    assert path.read_bytes() == blob


def test_jpeg_upload_declares_jpeg_and_preserves_pixels(creator, monkeypatch):
    blob = image_bytes("JPEG")
    creator.get_file_ids = Mock(return_value=({"data": {"uploadTempPermits": [{
        "fileIds": ["spectrum/fixture"], "token": "fixture", "expireTime": 9999999999,
    }]}}, "1234567890000"))
    creator._upload_request = Mock(return_value=Response(status=200))
    monkeypatch.setattr(creator_sign, "cos_signature", Mock(return_value="signature"))
    info = creator_api.XhsCreatorApi.upload_media(creator, blob, "image")
    payload = creator_api._image_note_data("title", "desc", None, {}, 0, [info])
    uploaded = payload["image_info"]["images"][0]
    assert (uploaded["width"], uploaded["height"]) == (100, 10)
    assert json.loads(uploaded["extra_info_json"])["mimeType"] == "image/jpeg"
    assert creator._upload_request.call_args.kwargs["data"] == blob


@pytest.mark.parametrize("bad", [b"not an image", b"\x89PNG\r\n\x1a\n", b""])
def test_invalid_images_fail_with_structured_error(bad):
    with pytest.raises(XhsApiError):
        creator_api._image_info(bad)


def test_pixel_budget_is_checked_without_decoding(monkeypatch):
    blob = image_bytes("PNG")
    monkeypatch.setattr(creator_api, "MAX_IMAGE_PIXELS", 999)
    with pytest.raises(XhsApiError, match="像素"):
        creator_api._image_info(blob)


@pytest.mark.parametrize("stage", ["open", "metadata", "read", "encode", "success"])
def test_video_snapshot_reuses_path_and_releases_decoder(tmp_path, monkeypatch, stage):
    import cv2
    path = tmp_path / "fixture.mp4"
    path.write_bytes(b"video")
    cap = Mock()
    cap.isOpened.return_value = stage != "open"
    cap.get.side_effect = RuntimeError("metadata") if stage == "metadata" else lambda _key: 1
    cap.read.side_effect = RuntimeError("read") if stage == "read" else None
    cap.read.return_value = (True, "frame")
    capture = Mock(return_value=cap)
    monkeypatch.setattr(cv2, "VideoCapture", capture)
    monkeypatch.setattr(cv2, "imencode", Mock(return_value=(
        stage != "encode", SimpleNamespace(tobytes=lambda: b"cover"))))
    monkeypatch.setattr(creator_api.tempfile, "NamedTemporaryFile", Mock(side_effect=AssertionError("no duplicate video copy")))
    if stage == "success":
        assert creator_api._video_cover_and_meta(media.MediaFile(path, 5))[0] == b"cover"
    else:
        with pytest.raises((XhsApiError, RuntimeError)):
            creator_api._video_cover_and_meta(media.MediaFile(path, 5))
    capture.assert_called_once_with(str(path))
    cap.release.assert_called_once()
    assert path.exists()


@pytest.fixture
def snapshot_root(tmp_path, monkeypatch):
    original = media.tempfile.TemporaryDirectory
    monkeypatch.setattr(media.tempfile, "TemporaryDirectory",
                        lambda **kwargs: original(dir=tmp_path, **kwargs))
    return tmp_path


@pytest.mark.parametrize("fail", [False, True])
def test_snapshots_freeze_full_selection_and_cleanup(snapshot_root, monkeypatch, fail):
    paths = [snapshot_root / f"{i}.jpg" for i in range(3)]
    for i, path in enumerate(paths):
        path.write_bytes(bytes([i]) * 25)
    monkeypatch.setattr(media, "COPY_CHUNK_BYTES", 7)
    monkeypatch.setattr(Path, "read_bytes", Mock(side_effect=AssertionError("no full-file buffering")))
    saved = []
    def scenario():
        with media.snapshot_publish_files("images", paths, check_active=Mock()) as snapshots:
            saved.extend(snapshots)
            for path in paths:
                path.unlink()
            for i, item in enumerate(snapshots):
                with item.path.open("rb") as stream:
                    assert stream.read() == bytes([i]) * 25
                assert item.size == 25
            if fail:
                raise RuntimeError("upload failed")
    if fail:
        with pytest.raises(RuntimeError):
            scenario()
    else:
        scenario()
    assert len(saved) == 3 and all(not item.path.exists() for item in saved)
    assert not list(snapshot_root.glob("creatorhub-xhs-media-*"))


@pytest.mark.parametrize("sizes", [[11], [6, 5]])
def test_individual_and_batch_size_budgets_stop_before_transport(snapshot_root, monkeypatch, sizes):
    monkeypatch.setattr(media, "MAX_IMAGE_BYTES", 10)
    monkeypatch.setattr(media, "MAX_IMAGE_BATCH_BYTES", 10)
    paths = []
    for index, size in enumerate(sizes):
        path = snapshot_root / f"{index}.jpg"
        path.write_bytes(b"x" * size)
        paths.append(str(path))
    factory = Mock(side_effect=AssertionError("no API"))
    monkeypatch.setattr(creator_api, "XhsCreatorApi", factory)
    result = publish._publish_api_sync("a1=fixture", "images", "", "", paths, [])
    assert result[0] is False and "资源上限" in str(result[2])
    factory.assert_not_called()
    assert not list(snapshot_root.glob("creatorhub-xhs-media-*"))


def test_video_size_budget_and_exact_boundary(media_path, monkeypatch):
    monkeypatch.setattr(media, "MAX_VIDEO_BYTES", 7)
    assert media.validate_publish_files("video", [media_path]) == [str(media_path)]
    media_path.write_bytes(b"x" * 8)
    with pytest.raises(ValueError, match="资源上限"):
        media.validate_publish_files("video", [media_path])


@pytest.mark.parametrize("stage", ["changed", "cancelled", "disk_full"])
def test_snapshot_preparation_failures_remove_partial_files(snapshot_root, monkeypatch, stage):
    path = snapshot_root / "fixture.jpg"
    path.write_bytes(b"fixture")
    calls = 0
    def check():
        nonlocal calls
        calls += 1
        if calls == 3:
            if stage == "changed":
                path.write_bytes(b"changed content")
            elif stage == "cancelled":
                raise XhsApiError("cancelled")
            else:
                raise OSError("disk full")
    with pytest.raises((ValueError, XhsApiError, OSError)):
        with media.snapshot_publish_files("images", [path], check_active=check):
            pytest.fail("partial snapshot must not reach the caller")
    assert not list(snapshot_root.glob("creatorhub-xhs-media-*"))


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True, "1", None])
def test_invalid_publish_timeout_never_starts_work(media_path, monkeypatch, value):
    factory = Mock(side_effect=AssertionError("no API"))
    monkeypatch.setattr(creator_api, "XhsCreatorApi", factory)
    result = asyncio.run(publish.publish_xhs(None, None, STATE, "images", "", "",
        [str(media_path)], mode="api", timeout_seconds=value))
    assert result[0] is False
    factory.assert_not_called()


def test_requests_and_poll_delays_share_remaining_budget(creator, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(deadline, "time", SimpleNamespace(monotonic=lambda: now[0]))
    creator._deadline = 101.0
    creator._request("get", "https://HOST.invalid")
    assert creator.cli.get.call_args.kwargs["timeout"] == 1.0
    now[0] = 100.75
    creator._request("post", "https://HOST.invalid")
    assert creator.cli.post.call_args.kwargs["timeout"] == .25
    def sleep(seconds):
        assert seconds == .25
        now[0] += seconds
    monkeypatch.setattr(creator_api.time, "sleep", sleep)
    with pytest.raises(XhsApiError) as caught:
        creator._poll_delay()
    assert caught.value.signal == "publish_timeout"
    creator.cli.get.reset_mock()
    now[0] = 100.9999
    with pytest.raises(XhsApiError):
        creator._request("get", "https://HOST.invalid")
    creator.cli.get.assert_not_called()  # never round to curl's unlimited 0 ms


def test_transcode_wait_stops_at_total_deadline_before_submit(creator, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(deadline, "time", SimpleNamespace(monotonic=lambda: now[0]))
    creator._deadline = 101.0
    creator.cli.get.return_value = Response({"success": True, "data": {"status": 1}})
    def sleep(seconds):
        now[0] += seconds
    monkeypatch.setattr(creator_api.time, "sleep", sleep)
    callback = Mock()
    with pytest.raises(XhsApiError) as caught:
        creator.post_note(media_type="video", title="", desc="", video_file=b"video", on_submit=callback)
    assert caught.value.signal == "publish_timeout"
    creator.cli.get.assert_called_once()
    callback.assert_not_called()
    creator.cli.post.assert_not_called()


def test_direct_creator_calls_obey_media_byte_limits(creator, monkeypatch):
    monkeypatch.setattr(media, "MAX_IMAGE_BYTES", 3)
    monkeypatch.setattr(media, "MAX_IMAGE_BATCH_BYTES", 4)
    for selection in ([b"1234"], [b"123", b"12"]):
        with pytest.raises(XhsApiError, match="资源上限"):
            creator.post_note(media_type="image", title="", desc="", image_files=selection)
    creator.upload_media.assert_not_called()
    creator.cli.post.assert_not_called()


@pytest.mark.parametrize("stage", ["upload", "submit"])
def test_timeout_drains_worker_and_preserves_submit_boundary(creator, media_path, monkeypatch, stage):
    entered, release = threading.Event(), threading.Event()
    def factory(*_args, **kwargs):
        creator._deadline = kwargs["deadline"]
        creator._cancel_event = kwargs["cancel_event"]
        return creator
    def blocked(*_args, **_kwargs):
        entered.set()
        assert release.wait(5), "fixture worker not released"
        if stage == "upload":
            return {"fileIds": "fixture", "width": 100, "height": 10}
        return Response({"success": True, "data": {"id": "fixture"}})
    monkeypatch.setattr(creator_api, "XhsCreatorApi", factory)
    if stage == "upload":
        creator.upload_media.side_effect = blocked
    else:
        creator.cli.post.side_effect = blocked
    callback = AsyncMock()
    browser = AsyncMock(side_effect=AssertionError("no fallback"))
    monkeypatch.setattr(publish, "publish_xhs_browser", browser)
    async def scenario():
        task = asyncio.create_task(publish.publish_xhs(None, None, STATE, "images", "", "",
            [str(media_path)], mode="api", timeout_seconds=.3, on_submit=callback,
            preserve_error=True))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.sleep(.35)
            assert creator._cancel_event.is_set()
            assert not task.done()
            creator.cli.close.assert_not_called()
            assert creator.upload_media.call_args.args[0].path.exists()
        finally:
            release.set()
        return await asyncio.wait_for(task, 2)
    result = asyncio.run(scenario())
    assert result[0] is False
    if stage == "upload":
        assert isinstance(result[2], XhsApiError) and result[2].signal == "publish_timeout"
        callback.assert_not_awaited()
        creator.cli.post.assert_not_called()
    else:
        assert str(result[2]).startswith("write_uncertain:")
        callback.assert_awaited_once()
        creator.cli.post.assert_called_once()
    creator.cli.close.assert_called_once()
    assert not creator.upload_media.call_args.args[0].path.exists()
    browser.assert_not_awaited()


def test_timeout_cancels_stalled_submit_callback(creator, media_path, monkeypatch):
    monkeypatch.setattr(creator_api, "XhsCreatorApi", lambda *_args, **_kwargs: creator)
    async def scenario():
        finished = asyncio.Event()
        async def mark():
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()
        result = await asyncio.wait_for(publish.publish_xhs(None, None, STATE,
            "images", "", "", [str(media_path)], mode="api", timeout_seconds=.2,
            on_submit=mark, preserve_error=True), 2)
        assert finished.is_set()
        assert result[0] is False and result[2].signal == "publish_timeout"
    asyncio.run(scenario())
    creator.cli.post.assert_not_called()
    creator.cli.close.assert_called_once()


def test_callback_own_timeout_is_not_misreported_as_budget_expiration(creator, media_path, monkeypatch):
    monkeypatch.setattr(creator_api, "XhsCreatorApi", lambda *_args, **_kwargs: creator)
    callback = AsyncMock(side_effect=TimeoutError("callback timeout"))
    result = asyncio.run(publish.publish_xhs(None, None, STATE, "images", "", "",
        [str(media_path)], mode="api", on_submit=callback, preserve_error=True))
    assert result[0] is False and "callback timeout" in str(result[2])
    assert "总时限" not in str(result[2])
    creator.cli.post.assert_not_called()


def test_signing_preflight_is_off_loop_and_checks_deadline(media_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def available():
        entered.set()
        assert release.wait(5)
        return True
    monkeypatch.setattr(creator_sign, "available", available)
    factory = Mock(side_effect=AssertionError("no API after deadline"))
    monkeypatch.setattr(creator_api, "XhsCreatorApi", factory)
    async def scenario():
        task = asyncio.create_task(publish.publish_xhs(None, None, STATE,
            "images", "", "", [str(media_path)], mode="api", timeout_seconds=.2,
            preserve_error=True))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.sleep(.25)
            assert not task.done()
        finally:
            release.set()
        result = await asyncio.wait_for(task, 2)
        assert result[0] is False and result[2].signal == "publish_timeout"
    asyncio.run(scenario())
    factory.assert_not_called()


def test_stream_callback_aborts_and_restores_transport_options(creator, media_path):
    transport = Mock()
    previous = {CurlOpt.CONNECTTIMEOUT_MS: 50}
    transport.curl_options = previous
    creator._upload_cli = transport
    creator._cancel_event = threading.Event()
    def put(*_args, **kwargs):
        assert "data" not in kwargs
        read = transport.curl_options[CurlOpt.READFUNCTION]
        assert read(2) == b"fi"
        creator._cancel_event.set()
        assert read(2) == CURL_READFUNC_ABORT
        raise RuntimeError("curl aborted")
    transport.put.side_effect = put
    with pytest.raises(XhsApiError) as caught:
        creator._upload_request("https://HOST.invalid", media_file=media.MediaFile(media_path, 7))
    assert caught.value.signal == "publish_cancelled"
    assert transport.curl_options is previous
    media_path.unlink()  # upload file handle was closed even on abort (Windows)


def test_real_curl_streams_raw_body_without_account_cookies(tmp_path, monkeypatch):
    """Only loopback HTTP: test actual curl_cffi options, not a put() mock."""
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_PUT(self):
            length = int(self.headers.get("Content-Length", "0"))
            digest = hashlib.sha256()
            remaining = length
            while remaining:
                chunk = self.rfile.read(min(65536, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
            received.append((length, remaining, digest.hexdigest(), self.headers.get("Cookie"),
                             self.headers.get("Transfer-Encoding")))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.send_header("Set-Cookie", "fixture-upload-cookie=never-forward; Path=/")
            self.end_headers()
        def log_message(self, *_args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = creator_api.XhsCreatorApi("a1=fixture-secret", deadline=deadline.deadline_after(5))
    client.cli.cookies.set("account-only", "fixture-secret")
    blob = bytes(range(256)) * 8192
    path = tmp_path / "snapshot.bin"
    path.write_bytes(blob)
    reads = []
    real_open = Path.open
    class BoundedReader:
        def __enter__(self):
            self.stream = real_open(path, "rb")
            return self
        def __exit__(self, *_args):
            self.stream.close()
        def read(self, size):
            assert 0 <= size <= media.COPY_CHUNK_BYTES
            reads.append(size)
            return self.stream.read(size)
    monkeypatch.setattr(Path, "open", lambda self, *args, **kwargs:
                        BoundedReader() if self == path and args == ("rb",)
                        else real_open(self, *args, **kwargs))
    monkeypatch.setattr(Path, "read_bytes", Mock(side_effect=AssertionError("no buffered upload")))
    try:
        url = f"http://127.0.0.1:{server.server_port}/upload"
        for _ in range(2):
            response = client._upload_request(url, media_file=media.MediaFile(path, len(blob)))
            assert response.status_code == 200
        assert client._upload_cli is not client.cli
        assert client._upload_cli.curl_options == {}
        # A subsequent ordinary byte upload must not reuse a stale read callback.
        assert client._upload_request(url, data=b"last").status_code == 200
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(5)
    digest = hashlib.sha256(blob).hexdigest()
    assert received == [(len(blob), 0, digest, None, None)] * 2 + [
        (4, 0, hashlib.sha256(b"last").hexdigest(), None, None)]
    assert len(reads) > 2
