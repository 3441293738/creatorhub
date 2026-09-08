"""Offline contracts for creator reads and the API publish submit boundary."""
import asyncio
import json
import threading
from unittest.mock import AsyncMock, Mock

import pytest

from app.platforms.xhs import creator_api, creator_sign, publish
from app.platforms.xhs.client import XhsApiError


class Response:
    def __init__(self, payload=None, status=200):
        self.status_code = status
        self.payload = payload

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


@pytest.fixture
def creator(monkeypatch):
    transport = Mock()
    transport.get.return_value = Response({"success": True, "data": {}})
    transport.post.return_value = Response({
        "success": True, "data": {"id": "fixture-note"}})
    factory = Mock(return_value=transport)
    monkeypatch.setattr(creator_api, "Session", factory)
    monkeypatch.setattr(creator_sign, "generate_xsc", lambda *_args: {"x-t": "123"})
    monkeypatch.setattr(creator_sign, "generate_xs_xs_common", lambda *_args: ("xs", 123, "common"))
    monkeypatch.setattr(creator_sign, "generate_x_rap_param", lambda *_args: "rap")
    monkeypatch.setattr(creator_sign, "available", lambda: True)
    monkeypatch.setattr(creator_api, "_video_cover_and_meta", lambda *_args: (
        b"cover", {"video": {}, "audio": {}}))
    client = creator_api.XhsCreatorApi("a1=fixture")
    client.upload_media = Mock(side_effect=lambda *_args: {
        "fileIds": "fixture", "width": 10, "height": 20, "video_id": "fixture-video"})
    client.session_factory = factory
    return client


@pytest.mark.parametrize("method,args", [
    ("get_file_ids", ("image",)), ("query_transcode", ("video",)),
    ("get_topic", ("fixture",)), ("my_info", ()), ("ping", ()),
    ("published_notes", ()),
])
@pytest.mark.parametrize("response,category", [
    (Response({"success": True, "data": {}}, 401), "auth"),
    (Response({"success": True, "data": {}}, 429), "risk"),
    (Response({"success": True, "data": {}}, 503), "network"),
    (Response({"success": False, "code": "-100", "data": {}}), "auth"),
    (Response({"success": "false", "data": {}}), "risk"),
    (Response(ValueError("not JSON")), "risk"),
])
def test_creator_reads_validate_http_and_envelopes(creator, method, args, response, category):
    creator.cli.get.return_value = response
    creator.cli.post.return_value = response
    with pytest.raises(XhsApiError) as caught:
        getattr(creator, method)(*args)
    assert caught.value.category == category


def test_creator_transport_does_not_mutate_cookies_or_follow_redirects(creator):
    options = creator.session_factory.call_args.kwargs
    assert options["discard_cookies"] is True
    assert options["allow_redirects"] is False


@pytest.mark.parametrize("status", [200, 302, 429, 503])
def test_media_upload_status_is_checked_without_requiring_json(creator, monkeypatch, status):
    creator.get_file_ids = Mock(return_value=({"data": {"uploadTempPermits": [{
        "fileIds": ["spectrum/fixture"], "token": "fixture", "expireTime": 9999999999,
    }]}}, "1234567890000"))
    monkeypatch.setattr(creator_api, "_image_info", lambda _data: (10, 20, 5, "image/png"))
    monkeypatch.setattr(creator_sign, "cos_signature", lambda *_args: "fixture")
    response = Response(ValueError("upload has no JSON"), status)
    creator.cli.put.return_value = response
    if status == 200:
        result = creator_api.XhsCreatorApi.upload_media(creator, b"image", "image")
        assert result["fileIds"] == "fixture"
    else:
        with pytest.raises(XhsApiError) as caught:
            creator_api.XhsCreatorApi.upload_media(creator, b"image", "image")
        assert caught.value.status_code == status
    creator.cli.put.assert_called_once()


@pytest.mark.parametrize("data", [
    {}, None, {"hasFirstFrame": "false"}, {"status": 1},
    {"status": "failed", "hasFirstFrame": True},
])
def test_unready_transcode_never_submits(creator, monkeypatch, data):
    creator.cli.get.return_value = Response({"success": True, "data": data})
    monkeypatch.setattr(creator_api, "TRANSCODE_MAX_RETRIES", 2)
    monkeypatch.setattr(creator_api.time, "sleep", Mock())
    callback = Mock()
    with pytest.raises(XhsApiError):
        creator.post_note(media_type="video", title="title", desc="desc",
                          video_file=b"video", on_submit=callback)
    creator.cli.post.assert_not_called()
    callback.assert_not_called()


@pytest.mark.parametrize("data", [
    {"hasFirstFrame": True}, {"has_first_frame": True},
    {"firstFrameFileId": "cover"}, {"first_frame_file_id": "cover"}, {"status": 2},
])
def test_explicit_ready_transcode_submits_once(creator, data):
    creator.cli.get.return_value = Response({"success": True, "data": data})
    callback = Mock()

    def submit(*_args, **_kwargs):
        callback.assert_called_once_with()
        return Response({"success": True, "data": {"id": "fixture-note"}})

    creator.cli.post.side_effect = submit
    ok, _, _ = creator.post_note(media_type="video", title="title", desc="desc",
                                 video_file=b"video", on_submit=callback)
    assert ok is True
    creator.cli.post.assert_called_once()


def test_transcode_stops_at_budget_without_final_sleep(creator, monkeypatch):
    creator.cli.get.return_value = Response({"success": True, "data": {"status": 1}})
    monkeypatch.setattr(creator_api, "TRANSCODE_MAX_RETRIES", 3)
    pause = Mock()
    monkeypatch.setattr(creator_api.time, "sleep", pause)
    with pytest.raises(XhsApiError):
        creator.post_note(media_type="video", title="title", desc="desc", video_file=b"video")
    assert creator.cli.get.call_count == 3
    assert pause.call_count == 2
    creator.cli.post.assert_not_called()


def test_failed_submission_marker_prevents_final_post(creator):
    callback = Mock(side_effect=RuntimeError("database unavailable"))
    with pytest.raises(RuntimeError, match="database unavailable"):
        creator.post_note(media_type="image", title="title", desc="desc",
                          image_files=[b"image"], on_submit=callback)
    creator.cli.post.assert_not_called()


@pytest.mark.parametrize("response", [
    Response({"success": True, "data": {}}, 503),
    Response({"success": False, "code": 429, "data": {}}),
    Response({"success": "false", "data": {"id": "fixture-note"}}),
    Response({"success": True, "data": {}}),
    Response(ValueError("not JSON")), TimeoutError("connection timeout"),
])
def test_final_response_failure_is_uncertain_not_retryable(creator, tmp_path, monkeypatch, response):
    media = tmp_path / "fixture.jpg"
    media.write_bytes(b"image")
    if isinstance(response, Exception):
        creator.cli.post.side_effect = response
    else:
        creator.cli.post.return_value = response
    monkeypatch.setattr(creator_api, "XhsCreatorApi", lambda *_args, **_kwargs: creator)
    callback = Mock()
    ok, url, error = publish._publish_api_sync(
        "a1=fixture", "image", "title", "desc", [str(media)], [], on_submit=callback)
    assert ok is False and not url
    assert error.startswith("write_uncertain:")
    callback.assert_called_once_with()
    creator.cli.post.assert_called_once()
    creator.cli.close.assert_called_once()


def test_pre_submit_failure_stays_an_ordinary_failure(creator, tmp_path, monkeypatch):
    media = tmp_path / "fixture.jpg"
    media.write_bytes(b"image")
    creator.upload_media.side_effect = TimeoutError("connection timeout")
    monkeypatch.setattr(creator_api, "XhsCreatorApi", lambda *_args, **_kwargs: creator)
    callback = Mock()
    ok, _, error = publish._publish_api_sync(
        "a1=fixture", "image", "title", "desc", [str(media)], [], on_submit=callback)
    assert ok is False
    assert not error.startswith("write_uncertain:")
    callback.assert_not_called()
    creator.cli.post.assert_not_called()
    creator.cli.close.assert_called_once()


def test_async_submit_callback_runs_on_owner_loop_before_post(creator, tmp_path, monkeypatch):
    media = tmp_path / "fixture.jpg"
    media.write_bytes(b"image")
    monkeypatch.setattr(creator_api, "XhsCreatorApi", lambda *_args, **_kwargs: creator)
    browser = AsyncMock(side_effect=AssertionError("no fallback"))
    monkeypatch.setattr(publish, "publish_xhs_browser", browser)
    owner = threading.get_ident()

    async def mark():
        assert threading.get_ident() == owner
        creator.cli.post.assert_not_called()

    callback = AsyncMock(side_effect=mark)
    result = asyncio.run(publish.publish_xhs(
        None, None, json.dumps({"cookies": [{"name": "a1", "value": "fixture"}]}),
        "image", "title", "desc", [str(media)], mode="api", on_submit=callback))
    assert result[0] is True
    callback.assert_awaited_once_with()
    browser.assert_not_awaited()


@pytest.mark.parametrize("stage", ["upload", "submit"])
@pytest.mark.parametrize("cancel_all_tasks", [False, True])
def test_cancellation_drains_worker_before_releasing_caller(
        creator, tmp_path, monkeypatch, stage, cancel_all_tasks):
    media = tmp_path / "fixture.jpg"
    media.write_bytes(b"image")
    entered, release = threading.Event(), threading.Event()

    def factory(*_args, **kwargs):
        creator._cancel_event = kwargs.get("cancel_event")
        return creator

    def blocked(*_args, **_kwargs):
        entered.set()
        assert release.wait(5), "fixture worker not released"
        if stage == "upload":
            return {"fileIds": "fixture", "width": 10, "height": 20}
        return Response({"success": True, "data": {"id": "fixture-note"}})

    monkeypatch.setattr(creator_api, "XhsCreatorApi", factory)
    if stage == "upload":
        creator.upload_media.side_effect = blocked
    else:
        creator.cli.post.side_effect = blocked
    callback = AsyncMock()

    async def scenario():
        task = asyncio.create_task(publish.publish_xhs(
            None, None, '{"cookies":[{"name":"a1","value":"fixture"}]}',
            "image", "title", "desc", [str(media)], mode="api", on_submit=callback))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            for _ in range(2):
                if cancel_all_tasks:
                    for pending in asyncio.all_tasks():
                        if pending is not asyncio.current_task():
                            pending.cancel()
                else:
                    task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
                creator.cli.close.assert_not_called()
        finally:
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert creator._cancel_event.is_set()
        creator.cli.close.assert_called_once()

    asyncio.run(scenario())
    if stage == "upload":
        callback.assert_not_awaited()
        creator.cli.post.assert_not_called()
    else:
        callback.assert_awaited_once_with()
        creator.cli.post.assert_called_once()


def test_already_cancelled_sync_publish_never_constructs_transport(monkeypatch):
    factory = Mock(side_effect=AssertionError("no network"))
    monkeypatch.setattr(creator_api, "XhsCreatorApi", factory)
    cancelled = threading.Event()
    cancelled.set()
    result = publish._publish_api_sync(
        "a1=fixture", "image", "title", "desc", [], [], cancel_event=cancelled)
    assert result[0] is False
    factory.assert_not_called()


@pytest.mark.parametrize("payload,expected", [
    ({"success": False, "code": "401", "msg": ""}, "auth"),
    ({"success": False, "code": "407", "msg": ""}, "network"),
    ({"success": False, "code": "429", "msg": "登录验证码已过期"}, "risk"),
    ({"success": False, "msg": "上传凭证已过期"}, "business"),
])
def test_creator_error_codes_are_not_overridden_by_misleading_message(payload, expected):
    error = publish._creator_response_error(payload)
    assert isinstance(error, XhsApiError)
    assert error.category == expected
    assert error.payload is payload


@pytest.mark.parametrize("status,valid", [(401, False), (429, None), (503, None)])
def test_creator_check_preserves_tristate_on_http_failure(creator, monkeypatch, status, valid):
    creator.cli.get.return_value = Response({"success": True, "data": {}}, status)
    monkeypatch.setattr(creator_api, "XhsCreatorApi", lambda *_args, **_kwargs: creator)
    state = '{"cookies":[{"name":"a1","value":"fixture"}]}'
    assert asyncio.run(publish.creator_check(state)) is valid
    detailed = asyncio.run(publish.creator_check(state, preserve_error=True))
    assert detailed[0] is valid
    assert isinstance(detailed[1], XhsApiError)


def test_cancel_during_async_submit_notification_never_posts(creator, tmp_path, monkeypatch):
    media = tmp_path / "fixture.jpg"
    media.write_bytes(b"image")
    monkeypatch.setattr(creator_api, "XhsCreatorApi", lambda *_args, **_kwargs: creator)

    async def scenario():
        entered, finished = asyncio.Event(), asyncio.Event()

        async def mark():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

        task = asyncio.create_task(publish.publish_xhs(
            None, None, '{"cookies":[{"name":"a1","value":"fixture"}]}',
            "image", "title", "desc", [str(media)], mode="api", on_submit=mark))
        try:
            await asyncio.wait_for(entered.wait(), 5)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert finished.is_set()

    asyncio.run(scenario())
    creator.cli.post.assert_not_called()
    creator.cli.close.assert_called_once()


@pytest.mark.parametrize("preserve_error", [False, True])
def test_pre_submit_error_can_retain_category_without_changing_legacy_default(
        creator, tmp_path, monkeypatch, preserve_error):
    media = tmp_path / "fixture.jpg"
    media.write_bytes(b"image")
    error = XhsApiError("fixture", category="network", status_code=503)
    creator.upload_media.side_effect = error
    monkeypatch.setattr(creator_api, "XhsCreatorApi", lambda *_args, **_kwargs: creator)
    result = asyncio.run(publish.publish_xhs(
        None, None, '{"cookies":[{"name":"a1","value":"fixture"}]}',
        "image", "title", "desc", [str(media)], mode="api", preserve_error=preserve_error))
    assert result[0] is False
    if preserve_error:
        assert result[2] is error
    else:
        assert result[2] == "fixture"
    creator.cli.post.assert_not_called()
