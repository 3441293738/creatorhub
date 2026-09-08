"""Offline regression tests for media, credentials and bounded XHS reads."""
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.platforms.xhs import creator_api, creator_sign, publish
from app.platforms.xhs.browser_writes import publish_xhs_browser
from app.platforms.xhs.client import (
    XhsApiClient, cookie_str_from_state, has_a1, has_creator_cookies,
)
from app.platforms.xhs.comments import collect_note_comments
from app.platforms.xhs.responses import XhsApiError


def state(*cookies):
    return json.dumps({"cookies": list(cookies)})


@pytest.mark.parametrize("raw", ["null", "[]", '"fixture"', "1", "true", "invalid",
                                     '{"cookies":null}', '{"cookies":{}}'])
def test_malformed_cookie_state_is_empty_not_an_exception(raw):
    assert cookie_str_from_state(raw) == ""
    assert has_creator_cookies(raw) is False


@pytest.mark.parametrize("domain", [
    "not-xiaohongshu.example", "xiaohongshu.com.example", "evilxhscdn.com",
    "xiaohongshu", "https://xiaohongshu.com", "xiaohongshu.com:443", None, 1, [],
])
def test_cookie_domains_match_dns_boundaries_not_substrings(domain):
    raw = state({"domain": domain, "name": "a1", "value": "fixture"},
                {"domain": domain, "name": "galaxy_creator_session_id", "value": "fixture"})
    assert cookie_str_from_state(raw) == ""
    assert has_creator_cookies(raw) is False


@pytest.mark.parametrize("domain", ["xiaohongshu.com", ".xiaohongshu.com",
                                   "creator.xiaohongshu.com", ".XIAOHONGSHU.COM", ""])
def test_supported_cookie_domains_and_legacy_domainless_state_remain_usable(domain):
    raw = state({"domain": domain, "name": "a1", "value": "fixture=="},
                {"domain": domain, "name": "galaxy_creator_session_id", "value": "session"})
    assert has_a1(cookie_str_from_state(raw)) is True
    assert has_creator_cookies(raw) is True


@pytest.mark.parametrize("raw", [None, "", "a1=", "a1=  ", "not_a1=fixture",
                                  "other=a1=fixture", "a1\n=fixture", "a1=fixture\r\nx=1"])
def test_a1_requires_an_exact_nonempty_cookie(raw):
    assert has_a1(raw) is False


def test_cookie_state_skips_bad_rows_and_header_injection():
    raw = state(None, [], "fixture", {},
                {"name": "a1", "value": "fixture; injected=value"},
                {"name": "bad\r\nname", "value": "fixture"},
                {"name": "web_session", "value": "fixture"})
    assert cookie_str_from_state(raw) == "web_session=fixture"


def test_creator_identifier_alone_is_not_a_creator_session():
    assert has_creator_cookies(state({"name": "customerClientId", "value": "fixture"})) is False
    assert has_creator_cookies(state({"name": "galaxy_creator_session_id", "value": ""})) is False


@pytest.mark.parametrize("expires,valid", [
    (-1, True), (2000, True), ("2000", True), (0, False), (999, False),
    (1000, False), (True, False), ("nan", False), ("inf", False), ({}, False),
])
def test_expired_or_malformed_cookie_expiry_is_not_a_login(monkeypatch, expires, valid):
    from app.platforms.xhs import cookies
    monkeypatch.setattr(cookies, "time", SimpleNamespace(time=lambda: 1000))
    raw = state({"name": "a1", "value": "fixture", "expires": expires},
                {"name": "galaxy_creator_session_id", "value": "fixture", "expires": expires})
    assert has_a1(cookie_str_from_state(raw)) is valid
    assert has_creator_cookies(raw) is valid


def test_conflicting_login_values_do_not_depend_on_cookie_order():
    first = {"name": "a1", "value": "first", "domain": ".xiaohongshu.com"}
    second = {"name": "a1", "value": "second", "domain": "creator.xiaohongshu.com"}
    assert cookie_str_from_state(state(first, second)) == ""
    assert cookie_str_from_state(state(second, first)) == ""
    assert cookie_str_from_state(state(first, first)) == "a1=first"
    assert has_a1("a1=first; a1=second") is False
    assert has_a1("a1=first; a1=invalid value") is False


def test_cdn_cookies_are_not_added_to_api_requests():
    assert cookie_str_from_state(state({"name": "a1", "value": "fixture", "domain": ".xhscdn.com"})) == ""


@pytest.mark.parametrize("name", ["galaxy_creator_session_id", "customer-sso-sid",
                                 "access-token-creator.xiaohongshu.com"])
def test_creator_session_names_remain_supported(name):
    assert has_creator_cookies(state({"name": name, "value": "fixture"})) is True


@pytest.mark.parametrize("mode", ["browser", "api", "browser_direct", "api_direct"])
@pytest.mark.parametrize("bad", ["missing", "empty", "directory", "blank"])
def test_one_bad_media_file_stops_the_entire_publish(tmp_path, monkeypatch, mode, bad):
    good = tmp_path / "good.jpg"
    good.write_bytes(b"fixture")
    invalid = tmp_path / "invalid.jpg"
    if bad == "empty":
        invalid.touch()
    elif bad == "directory":
        invalid.mkdir()
    files = [str(good), "" if bad == "blank" else str(invalid)]
    browser = AsyncMock(side_effect=AssertionError("no browser operation"))
    factory = Mock(side_effect=AssertionError("no upload operation"))
    monkeypatch.setattr(publish, "publish_xhs_browser", browser)
    monkeypatch.setattr(creator_api, "XhsCreatorApi", factory)
    monkeypatch.setattr(creator_sign, "available", lambda: True)
    if mode == "browser_direct":
        result = asyncio.run(publish_xhs_browser(None, None, "images", "title", "desc", [], files))
        assert result.status == "failed"
    elif mode == "api_direct":
        result = publish._publish_api_sync("a1=fixture", "images", "title", "desc", files, [])
        assert result[0] is False
    else:
        result = asyncio.run(publish.publish_xhs(
            None, None, state({"name": "a1", "value": "fixture"}),
            "images", "title", "desc", files, mode=mode))
        assert result[0] is False
    browser.assert_not_awaited()
    factory.assert_not_called()


@pytest.mark.parametrize("media_type,count", [("video", 2), ("images", 19), ("unknown", 1)])
def test_media_count_and_type_are_validated_instead_of_truncated(tmp_path, monkeypatch, media_type, count):
    files = []
    for index in range(count):
        path = tmp_path / f"{index}.bin"
        path.write_bytes(b"fixture")
        files.append(str(path))
    factory = Mock(side_effect=AssertionError("validation must precede upload"))
    monkeypatch.setattr(creator_api, "XhsCreatorApi", factory)
    assert publish._publish_api_sync("a1=fixture", media_type, "title", "desc", files, [])[0] is False
    factory.assert_not_called()


def test_unreadable_media_is_detected_before_browser_or_api(tmp_path, monkeypatch):
    good = tmp_path / "good.jpg"
    blocked = tmp_path / "blocked.jpg"
    good.write_bytes(b"good")
    blocked.write_bytes(b"blocked")
    real_open = Path.open

    def open_file(path, *args, **kwargs):
        if path == blocked:
            raise PermissionError("fixture")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    browser = AsyncMock(side_effect=AssertionError("no browser"))
    factory = Mock(side_effect=AssertionError("no API"))
    monkeypatch.setattr(publish, "publish_xhs_browser", browser)
    monkeypatch.setattr(creator_api, "XhsCreatorApi", factory)
    result = asyncio.run(publish.publish_xhs(
        None, None, "{}", "images", "title", "desc", [str(good), str(blocked)]))
    assert result[0] is False
    browser.assert_not_awaited()
    factory.assert_not_called()


def test_file_lost_after_preflight_stops_before_constructing_api(tmp_path, monkeypatch):
    media = tmp_path / "fixture.jpg"
    media.write_bytes(b"fixture")
    factory = Mock(side_effect=AssertionError("no API"))
    monkeypatch.setattr(creator_api, "XhsCreatorApi", factory)
    real_open = Path.open
    opens = 0

    def open_file(path, *args, **kwargs):
        nonlocal opens
        if path == media:
            opens += 1
            if opens > 1:  # readable at preflight, lost before snapshotting
                raise FileNotFoundError("fixture")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    result = publish._publish_api_sync("a1=fixture", "images", "title", "desc", [str(media)], [])
    assert result[0] is False
    factory.assert_not_called()


@pytest.mark.parametrize("mode", ["browser", "api"])
def test_valid_media_keeps_the_complete_user_selected_order(tmp_path, monkeypatch, mode):
    from app.platforms.xhs.browser_writes import XhsWriteOutcome
    paths = [tmp_path / name for name in ("z.jpg", "a.jpg", "m.jpg")]
    for index, path in enumerate(paths):
        path.write_bytes(str(index).encode())
    browser = AsyncMock(return_value=XhsWriteOutcome("success", result="fixture"))
    api = Mock()
    copied = []

    def post_note(**kwargs):
        copied.extend(item.path.read_bytes() for item in kwargs["image_files"])
        return True, "ok", {"data": {"id": "fixture"}}

    api.post_note.side_effect = post_note
    monkeypatch.setattr(publish, "publish_xhs_browser", browser)
    monkeypatch.setattr(creator_api, "XhsCreatorApi", Mock(return_value=api))
    monkeypatch.setattr(creator_sign, "available", lambda: True)
    result = asyncio.run(publish.publish_xhs(
        None, None, state({"name": "a1", "value": "fixture"}),
        "images", "title", "desc", [str(path) for path in paths], mode=mode))
    assert result[0] is True
    if mode == "browser":
        assert browser.await_args.args[6] == [str(path) for path in paths]
    else:
        assert copied == [b"0", b"1", b"2"]
        assert all(not item.path.exists() for item in api.post_note.call_args.kwargs["image_files"])


@pytest.fixture
def upload_client(monkeypatch):
    account = Mock(name="account_transport")
    media = Mock(name="media_transport")
    media.curl_options = {}
    media.put.return_value = SimpleNamespace(status_code=200, headers={"X-Ros-Video-Id": "video"})
    factory = Mock(side_effect=[account, media])
    monkeypatch.setattr(creator_api, "Session", factory)
    monkeypatch.setattr(creator_sign, "cos_signature", Mock(return_value="signature"))
    monkeypatch.setattr(creator_api, "_image_info", lambda _value: (10, 20, 7, "image/png"))
    client = creator_api.XhsCreatorApi("a1=fixture; web_session=fixture", proxy="http://127.0.0.1:9999")
    return client, account, media, factory


def permit(client, address):
    value = {"fileIds": ["spectrum/fixture"], "token": "fixture-token", "expireTime": 9999999999}
    if address is not None:
        value["uploadAddr"] = address
    client.get_file_ids = Mock(return_value=({"data": {"uploadTempPermits": [value]}}, "1234567890000"))
    return value


@pytest.mark.parametrize("address", [
    "http://ros-upload.xiaohongshu.com", "https://HOST.invalid", "https://127.0.0.1",
    "https://ros-upload.xiaohongshu.com.evil.invalid", "https://evilxiaohongshu.com",
    "https://user:password@ros-upload.xiaohongshu.com", "//ros-upload.xiaohongshu.com",
    "https://ros-upload.xiaohongshu.com:8443", "https://ros-upload.xiaohongshu.com/path",
    "https://ros-upload.xiaohongshu.com?token=secret", "https://ros-upload.xiaohongshu.com#fragment",
    "https://ros-upload.xiaohongshu.com\\@HOST.invalid", "https://ros-upload.xiaohongshu.com\n",
    [], 1, False,
])
def test_upload_destination_is_validated_before_sending_media(upload_client, address):
    client, account, media, factory = upload_client
    permit(client, address)
    with pytest.raises(XhsApiError):
        client.upload_media(b"fixture", "image")
    account.put.assert_not_called()
    media.put.assert_not_called()
    assert factory.call_count == 1
    creator_sign.cos_signature.assert_not_called()


@pytest.mark.parametrize("address", [None, "ros-upload.xiaohongshu.com",
                                    "https://ros-upload.xiaohongshu.com/",
                                    "https://ROS-UPLOAD.XIAOHONGSHU.COM:443"])
def test_uploads_use_a_separate_cookie_free_transport(upload_client, address):
    client, account, media, factory = upload_client
    permit(client, address)
    client.upload_media(b"fixture", "image")
    client.upload_media(b"fixture", "video")
    account.put.assert_not_called()
    assert factory.call_count == 2
    assert media.put.call_count == 2
    for call in media.put.call_args_list:
        assert call.args[0] == "https://ros-upload.xiaohongshu.com/spectrum/fixture"
        assert not call.kwargs.get("cookies")
        assert not any(key.lower() == "cookie" for key in call.kwargs["headers"])
        assert call.kwargs["headers"]["x-cos-security-token"] == "fixture-token"
    options = factory.call_args_list[1].kwargs
    assert options["discard_cookies"] is True and options["allow_redirects"] is False
    assert options["proxies"] == {"http": "http://127.0.0.1:9999", "https": "http://127.0.0.1:9999"}
    client.close()
    account.close.assert_called_once()
    media.close.assert_called_once()


@pytest.mark.parametrize("field,value", [
    ("fileIds", []), ("fileIds", "fixture"), ("fileIds", [None]),
    ("fileIds", ["spectrum/../fixture"]), ("fileIds", ["spectrum/file?query"]),
    ("fileIds", ["spectrum/file#fragment"]), ("fileIds", ["spectrum/file%2Fpath"]),
    ("token", ""), ("token", "fixture\r\nCookie: injected"),
    ("expireTime", None), ("expireTime", True), ("expireTime", "nan"),
])
def test_malformed_upload_permit_never_sends_media(upload_client, field, value):
    client, account, media, factory = upload_client
    raw = permit(client, None)
    raw[field] = value
    with pytest.raises(XhsApiError) as caught:
        client.upload_media(b"fixture", "image")
    assert caught.value.signal == "ambiguous_response"
    assert factory.call_count == 1
    account.put.assert_not_called()
    media.put.assert_not_called()


def test_upload_failure_closes_both_transports_without_publishing(upload_client, tmp_path, monkeypatch):
    client, account, media, _ = upload_client
    permit(client, None)
    media.put.side_effect = TimeoutError("fixture")
    path = tmp_path / "fixture.jpg"
    path.write_bytes(b"fixture")
    callback = Mock()
    monkeypatch.setattr(creator_api, "XhsCreatorApi", lambda *_args, **_kwargs: client)
    result = publish._publish_api_sync(
        "a1=fixture", "images", "title", "desc", [str(path)], [], on_submit=callback)
    assert result[0] is False
    account.post.assert_not_called()
    callback.assert_not_called()
    account.close.assert_called_once()
    media.close.assert_called_once()


def test_account_transport_still_closes_if_upload_cleanup_raises(upload_client):
    client, account, media, _ = upload_client
    permit(client, None)
    client.upload_media(b"fixture", "image")
    media.close.side_effect = RuntimeError("fixture")
    client.close()
    account.close.assert_called_once()


class CommentClient:
    def __init__(self, root):
        self.note_comments = AsyncMock(return_value={"comments": [root], "has_more": False})
        self.note_sub_comments = AsyncMock(side_effect=AssertionError("no speculative reads"))

    @asynccontextmanager
    async def session_scope(self):
        yield self


@pytest.mark.parametrize("extra", [
    {"sub_comment_count": 3}, {"sub_comment_count": "3"},
    {"sub_comment_count": 3, "sub_comment_has_more": False},
    {}, {"sub_comment_count": "unknown"},
])
def test_missing_or_conflicting_reply_metadata_does_not_claim_completeness(extra):
    client = CommentClient({"id": "root", "sub_comments": [{"id": "reply"}], **extra})
    result = asyncio.run(collect_note_comments(client, "note", request_interval=0))
    assert result["has_more"] is True
    assert len(result["comments"]) == 2
    client.note_sub_comments.assert_not_awaited()


def test_comment_rows_missing_ids_are_reported_as_partial():
    client = CommentClient({"content": "fixture"})
    result = asyncio.run(collect_note_comments(client, "note", request_interval=0))
    assert result == {"comments": [], "has_more": True}


@pytest.mark.parametrize("flag", [None, False])
def test_sufficient_unique_replies_and_a_total_can_prove_completeness(flag):
    root = {"id": "root", "sub_comments": [{"id": "reply"}], "sub_comment_count": 1}
    if flag is not None:
        root["sub_comment_has_more"] = flag
    result = asyncio.run(collect_note_comments(CommentClient(root), "note", request_interval=0))
    assert result["has_more"] is False


def test_reply_totals_are_compared_against_unique_retained_ids():
    client = CommentClient({"id": "root", "sub_comment_count": 2,
                            "sub_comments": [{"id": "reply"}, {"id": "reply"}]})
    result = asyncio.run(collect_note_comments(client, "note", request_interval=0))
    assert result["has_more"] is True
    assert len(result["comments"]) == 2


def test_later_duplicate_root_pages_can_supply_the_remaining_replies():
    client = CommentClient({})
    client.note_comments.side_effect = [
        {"comments": [{"id": "root", "sub_comment_count": 2,
                       "sub_comments": [{"id": "reply-1"}]}], "has_more": True, "cursor": "next"},
        {"comments": [{"id": "root", "sub_comment_count": 2,
                       "sub_comments": [{"id": "reply-2"}]}], "has_more": False},
    ]
    result = asyncio.run(collect_note_comments(client, "note", request_interval=0))
    assert [row["id"] for row in result["comments"]] == ["root", "reply-1", "reply-2"]
    assert result["has_more"] is False


def test_reply_count_discrepancy_is_ignored_when_only_roots_were_requested():
    client = CommentClient({"id": "root", "sub_comment_count": 5,
                            "sub_comments": [{"id": "reply"}]})
    result = asyncio.run(collect_note_comments(client, "note", include_replies=False, request_interval=0))
    assert result == {"comments": [{"id": "root", "sub_comment_count": 5}], "has_more": False}


def test_unscoped_child_request_does_not_borrow_its_parents_session():
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        transports = []

        class Transport:
            def __init__(self, **kwargs):
                self.closed = False
                transports.append(self)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                self.closed = True

            async def get(self, *_args, **_kwargs):
                started.set()
                await release.wait()
                assert not self.closed
                return "fixture"

        client = object.__new__(XhsApiClient)
        client._session_cls = Transport
        client._session = client._session_owner = None
        client.impersonate, client.proxy, client.timeout = "fixture", None, 1
        async with client.session_scope():
            child = asyncio.create_task(client._request("get", "https://HOST.invalid"))
            await started.wait()
        release.set()
        assert await child == "fixture"
        assert len(transports) == 2
        assert all(transport.closed for transport in transports)

    asyncio.run(scenario())


def test_scope_ownership_is_reserved_while_transport_is_opening():
    async def scenario():
        opening, release = asyncio.Event(), asyncio.Event()
        created = []

        class Transport:
            def __init__(self, **kwargs):
                self.closed = False
                created.append(self)

            async def __aenter__(self):
                opening.set()
                await release.wait()
                return self

            async def __aexit__(self, *_args):
                self.closed = True

        client = object.__new__(XhsApiClient)
        client._session_cls = Transport
        client._session = client._session_owner = None

        async def hold():
            async with client.session_scope():
                assert client._session is created[0]

        holder = asyncio.create_task(hold())
        try:
            await opening.wait()
            with pytest.raises(RuntimeError, match="另一个任务"):
                async with client.session_scope():
                    pytest.fail("overlapping scope entered")
        finally:
            release.set()
            await holder
        assert len(created) == 1 and created[0].closed
        assert client._session is client._session_owner is None

    asyncio.run(scenario())


@pytest.mark.parametrize("error", [RuntimeError("fixture"), asyncio.CancelledError()])
def test_failed_scope_initialization_releases_owner(error):
    client = object.__new__(XhsApiClient)
    client._session = client._session_owner = None

    class Broken:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            raise error

        async def __aexit__(self, *_args):
            pass

    client._session_cls = Broken

    async def scenario():
        with pytest.raises(type(error)):
            async with client.session_scope():
                pytest.fail("broken transport entered")
        assert client._session is client._session_owner is None

    asyncio.run(scenario())


@pytest.fixture
def publish_db(tmp_path):
    import app.db as db
    from app.models import DouyinAccount
    previous = db._engine
    db.init_db(str(tmp_path / "publish.db"))
    try:
        with db.get_session() as session:
            account = DouyinAccount(platform="xhs", status="active", storage_state=state(
                {"name": "a1", "value": "fixture"},
                {"name": "galaxy_creator_session_id", "value": "fixture"}))
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = account.id
        yield db, account_id
    finally:
        db._engine.dispose()
        db._engine = previous


def test_publish_creation_rejects_incomplete_selection_without_saving_a_task(publish_db, tmp_path):
    from fastapi import HTTPException
    from sqlmodel import select
    from app import main
    from app.models import PublishTask, TaskSubmission
    db, account_id = publish_db
    good, missing = tmp_path / "good.jpg", tmp_path / "missing.jpg"
    good.write_bytes(b"fixture")
    request = SimpleNamespace(headers={"idempotency-key": "fixture-publish-boundary-001"})
    body = main.PublishIn(account_id=account_id, media_paths=[str(good), str(missing)])
    with pytest.raises(HTTPException) as caught:
        asyncio.run(main.add_publish(body, request))
    assert caught.value.status_code == 422
    with db.get_session() as session:
        assert session.exec(select(PublishTask)).all() == []
        assert session.exec(select(TaskSubmission)).all() == []

    missing.write_bytes(b"restored")
    result = asyncio.run(main.add_publish(body, request))
    assert result["media_count"] == 2
    with db.get_session() as session:
        task = session.get(PublishTask, result["id"])
        assert json.loads(task.media_json) == body.media_paths


def test_existing_publish_receipt_replays_without_mutating_its_selection(publish_db, tmp_path):
    from app import main
    db, account_id = publish_db
    path = tmp_path / "fixture.jpg"
    path.write_bytes(b"fixture")
    body = main.PublishIn(account_id=account_id, media_paths=[str(path)])
    request = SimpleNamespace(headers={"idempotency-key": "fixture-publish-boundary-002"})
    first = asyncio.run(main.add_publish(body, request))
    path.unlink()
    second = asyncio.run(main.add_publish(body, request))
    assert second["id"] == first["id"]
    assert second["replayed"] is True
    assert second["media_count"] == 1
