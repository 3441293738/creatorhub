"""Offline contracts for the explicit XHS API compatibility path."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.platforms.xhs.client import XhsApiClient, XhsApiError
from app.platforms.xhs import creator_api, creator_sign, publish


class Response:
    def __init__(self, payload, status=200):
        self.status_code = status
        self.payload = payload
        self.text = "fixture response"

    def json(self):
        return self.payload


@pytest.mark.parametrize("data", [{}, None, {"items": []}])
@pytest.mark.parametrize("code,category", [
    (-100, "auth"), ("-100", "auth"), (429, "risk"),
    ("461", "risk"), (-9059, "business"),
])
def test_failed_envelope_with_data_never_becomes_empty_success(data, code, category):
    with pytest.raises(XhsApiError) as caught:
        XhsApiClient._unwrap(Response({
            "success": False, "code": code, "msg": "fixture", "data": data,
        }))
    assert caught.value.category == category


@pytest.mark.parametrize("payload", [
    None, [], "not an envelope", {}, {"data": {}},
    {"success": "false", "data": {}},
    {"success": True, "data": []},
    {"success": True, "data": "unexpected"},
])
def test_malformed_success_envelope_is_structured(payload):
    with pytest.raises(XhsApiError) as caught:
        XhsApiClient._unwrap(Response(payload))
    assert caught.value.category == "risk"
    assert caught.value.signal == "ambiguous_response"


@pytest.mark.parametrize("payload", [
    {"success": True, "data": {}},
    {"success": True, "code": 0, "data": None},
    {"code": "0", "data": {}},
    {"result": 0, "data": {}},
])
def test_valid_empty_success_is_preserved(payload):
    assert XhsApiClient._unwrap(Response(payload)) == {}


def test_explicit_error_code_is_respected_even_with_success_flag():
    with pytest.raises(XhsApiError) as caught:
        XhsApiClient._unwrap(Response({"success": True, "code": -100, "data": {}}))
    assert caught.value.category == "auth"


@pytest.mark.parametrize("status", [302, 400, 404])
def test_http_failure_is_not_overridden_by_body(status):
    with pytest.raises(XhsApiError) as caught:
        XhsApiClient._unwrap(Response({"success": True, "data": {}}, status))
    assert caught.value.status_code == status


class Session:
    instances = None

    def __init__(self, **options):
        self.options = options
        self.closed = False
        self.requests = []
        self.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True

    async def get(self, url, **kwargs):
        self.requests.append(("GET", url, kwargs))
        return Response({"success": True, "data": {}})

    async def post(self, url, **kwargs):
        self.requests.append(("POST", url, kwargs))
        return Response({"success": True, "data": {}})


@pytest.fixture
def api_client(monkeypatch):
    Session.instances = []
    client = XhsApiClient("a1=fixture; web_session=fixture", "Chrome/130.0.0.0")
    monkeypatch.setattr(client, "_session_cls", Session)
    monkeypatch.setattr(client, "_signer", SimpleNamespace(
        sign_headers_get=lambda *_args, **_kwargs: {"x-s": "fixture"},
        sign_headers_post=lambda *_args, **_kwargs: {"x-s": "fixture"},
    ))
    return client


def test_scoped_requests_share_transport_and_close_on_exit(api_client):
    async def scenario():
        async with api_client.session_scope():
            await api_client._get("/fixture", {})
            async with api_client.session_scope():
                await api_client._post("/fixture", {"text": "fixture"})
            assert not Session.instances[0].closed
        assert len(Session.instances) == 1
        session = Session.instances[0]
        assert session.closed
        assert session.options["discard_cookies"] is True
        assert len(session.requests) == 2
        assert all(r[2]["headers"]["Cookie"] == api_client.cookie_str for r in session.requests)
    asyncio.run(scenario())


def test_unscoped_calls_remain_auto_closing(api_client):
    async def scenario():
        await api_client._get("/fixture", {})
        await api_client._post("/fixture", {})
    asyncio.run(scenario())
    assert len(Session.instances) == 2
    assert all(s.closed for s in Session.instances)


@pytest.mark.parametrize("error", [RuntimeError("fixture"), asyncio.CancelledError()])
def test_scope_closes_on_error_and_cancellation(api_client, error):
    async def scenario():
        with pytest.raises(type(error)):
            async with api_client.session_scope():
                await api_client._get("/fixture", {})
                raise error
        assert Session.instances[0].closed
        async with api_client.session_scope():
            await api_client._get("/fixture", {})
        assert len(Session.instances) == 2
        assert Session.instances[1].closed
    asyncio.run(scenario())


def test_accounts_never_share_transport_or_cookies(api_client, monkeypatch):
    other = XhsApiClient("a1=other", "Chrome/130.0.0.0", proxy="http://127.0.0.1:9999")
    monkeypatch.setattr(other, "_session_cls", Session)
    monkeypatch.setattr(other, "_signer", api_client._signer)

    async def scenario():
        async with api_client.session_scope(), other.session_scope():
            await api_client._get("/fixture", {})
            await other._get("/fixture", {})
    asyncio.run(scenario())
    assert len(Session.instances) == 2
    first, second = [s.requests[0][2] for s in Session.instances]
    assert first["headers"]["Cookie"] != second["headers"]["Cookie"]
    assert first["proxy"] is None
    assert second["proxy"] == "http://127.0.0.1:9999"


@pytest.mark.parametrize("media_type", ["image", "video"])
@pytest.mark.parametrize("visibility,expected", [("public", 0), ("private", 1)])
def test_api_publish_forwards_visibility_end_to_end(tmp_path, monkeypatch, media_type, visibility, expected):
    media = tmp_path / "fixture.bin"
    media.write_bytes(b"fixture")
    api = Mock()
    api.post_note.return_value = (True, "ok", {"data": {"id": "fixture-note"}})
    factory = Mock(return_value=api)
    monkeypatch.setattr(creator_api, "XhsCreatorApi", factory)
    monkeypatch.setattr(creator_sign, "available", lambda: True)
    browser = AsyncMock(side_effect=AssertionError("no browser fallback"))
    monkeypatch.setattr(publish, "publish_xhs_browser", browser)

    result = asyncio.run(publish.publish_xhs(
        None, None, '{"cookies":[{"name":"a1","value":"fixture"}]}',
        media_type, "fixture", "fixture", [str(media)], mode="api", visibility=visibility))
    assert result[0] is True
    assert api.post_note.call_args.kwargs["privacy_type"] == expected
    api.post_note.assert_called_once()
    api.close.assert_called_once()
    browser.assert_not_awaited()


@pytest.mark.parametrize("visibility", ["friends", "unexpected"])
def test_unsupported_api_visibility_stops_before_upload(tmp_path, monkeypatch, visibility):
    media = tmp_path / "fixture.jpg"
    media.write_bytes(b"fixture")
    factory = Mock(side_effect=AssertionError("validation must precede upload"))
    monkeypatch.setattr(creator_api, "XhsCreatorApi", factory)
    monkeypatch.setattr(creator_sign, "available", lambda: True)
    result = asyncio.run(publish.publish_xhs(
        None, None, '{"cookies":[{"name":"a1","value":"fixture"}]}',
        "image", "fixture", "fixture", [str(media)], mode="api", visibility=visibility))
    assert result[0] is False
    assert "可见" in result[2]
    factory.assert_not_called()


def test_sync_publish_entry_also_validates_visibility(monkeypatch):
    factory = Mock(side_effect=AssertionError("no upload"))
    monkeypatch.setattr(creator_api, "XhsCreatorApi", factory)
    result = publish._publish_api_sync("a1=fixture", "image", "fixture", "fixture", [], [], visibility="friends")
    assert result[0] is False
    factory.assert_not_called()


def test_parallel_unscoped_calls_have_independent_lifetimes(api_client, monkeypatch):
    class SlowSession(Session):
        async def get(self, url, **kwargs):
            await asyncio.sleep(0)
            assert not self.closed
            return await super().get(url, **kwargs)
    monkeypatch.setattr(api_client, "_session_cls", SlowSession)

    async def scenario():
        await asyncio.gather(api_client._get("/one", {}), api_client._get("/two", {}))
    asyncio.run(scenario())
    assert len(Session.instances) == 2
    assert all(s.closed for s in Session.instances)


def test_overlapping_scopes_from_different_tasks_are_rejected(api_client):
    async def other_scope():
        with pytest.raises(RuntimeError, match="另一个任务"):
            async with api_client.session_scope():
                pytest.fail("overlapping owner")
    async def scenario():
        async with api_client.session_scope():
            await asyncio.create_task(other_scope())
            assert not Session.instances[0].closed
    asyncio.run(scenario())
    assert Session.instances[0].closed


def test_signer_node_modules_points_to_project_root():
    from pathlib import Path
    assert creator_sign._NODE_MODULES == Path(__file__).resolve().parents[1] / "node_modules"


def test_reply_page_keeps_required_endpoint_fields(api_client, monkeypatch):
    get = AsyncMock(return_value={"comments": [], "has_more": False})
    monkeypatch.setattr(api_client, "_get", get)
    asyncio.run(api_client.note_sub_comments("note", "root", "token", "cursor", 100))
    uri, params = get.call_args.args
    assert uri == "/api/sns/web/v2/comment/sub/page"
    assert params["note_id"] == "note"
    assert params["root_comment_id"] == "root"
    assert params["xsec_token"] == "token"
    assert params["cursor"] == "cursor"
    assert params["num"] == 30


def thread(api_client, **kwargs):
    return asyncio.run(api_client.collect_note_comments("note", "token", request_interval=0, **kwargs))


def test_both_comment_levels_paginate_and_deduplicate(api_client, monkeypatch):
    from copy import deepcopy
    pages = [{"comments": [{
        "id": "root", "sub_comments": [{"id": "reply-1"}],
        "sub_comment_has_more": True, "sub_comment_cursor": "reply-cursor",
    }], "has_more": True, "cursor": "root-cursor"}, {
        "comments": [{"id": "root"}, {"id": "root-2"}], "has_more": False,
    }]
    original = deepcopy(pages)
    roots = AsyncMock(side_effect=pages)
    replies = AsyncMock(return_value={
        "comments": [{"id": "reply-1"}, {"id": "reply-2"}], "has_more": False,
    })
    monkeypatch.setattr(api_client, "note_comments", roots)
    monkeypatch.setattr(api_client, "note_sub_comments", replies)
    result = thread(api_client)
    assert [c["id"] for c in result["comments"]] == ["root", "reply-1", "reply-2", "root-2"]
    assert result["comments"][2]["target_comment"]["id"] == "root"
    assert result["has_more"] is False
    assert roots.await_args_list[1].kwargs["cursor"] == "root-cursor"
    assert replies.await_args.kwargs["cursor"] == "reply-cursor"
    assert pages == original
    assert len(Session.instances) == 1
    assert Session.instances[0].closed


@pytest.mark.parametrize("scope", ["root", "reply"])
def test_repeated_cursor_stops_and_marks_partial(api_client, monkeypatch, scope):
    if scope == "root":
        roots = AsyncMock(side_effect=[
            {"comments": [{"id": "one"}], "has_more": True, "cursor": "same"},
            {"comments": [{"id": "two"}], "has_more": True, "cursor": "same"},
        ])
        replies = AsyncMock(side_effect=AssertionError("no replies"))
    else:
        roots = AsyncMock(return_value={"comments": [{
            "id": "one", "sub_comment_has_more": True, "sub_comment_cursor": "same",
        }], "has_more": False})
        replies = AsyncMock(return_value={
            "comments": [{"id": "two"}], "has_more": True, "cursor": "same",
        })
    monkeypatch.setattr(api_client, "note_comments", roots)
    monkeypatch.setattr(api_client, "note_sub_comments", replies)
    result = thread(api_client)
    assert result["has_more"] is True
    assert roots.await_count + replies.await_count == 2


def test_request_budget_is_shared_by_root_and_reply_pages(api_client, monkeypatch):
    roots = AsyncMock(return_value={"comments": [{
        "id": "root", "sub_comment_has_more": True,
    }], "has_more": True, "cursor": "root-next"})
    replies = AsyncMock(return_value={
        "comments": [{"id": "reply"}], "has_more": True, "cursor": "reply-next",
    })
    monkeypatch.setattr(api_client, "note_comments", roots)
    monkeypatch.setattr(api_client, "note_sub_comments", replies)
    result = thread(api_client, max_requests=2)
    assert result["has_more"] is True
    assert roots.await_count == replies.await_count == 1


def test_reply_pages_with_no_new_ids_stop_even_if_cursor_changes(api_client, monkeypatch):
    roots = AsyncMock(return_value={"comments": [{
        "id": "root", "sub_comments": [{"id": "reply"}], "sub_comment_has_more": True,
    }], "has_more": False})
    replies = AsyncMock(return_value={
        "comments": [{"id": "reply"}], "has_more": True, "cursor": "next",
    })
    monkeypatch.setattr(api_client, "note_comments", roots)
    monkeypatch.setattr(api_client, "note_sub_comments", replies)
    assert thread(api_client)["has_more"] is True
    replies.assert_awaited_once()


@pytest.mark.parametrize("limit,more", [(1, True), (2, True), (3, False)])
def test_comment_limit_counts_embedded_replies(api_client, monkeypatch, limit, more):
    roots = AsyncMock(return_value={"comments": [{
        "id": "root", "sub_comments": [{"id": "reply-1"}, {"id": "reply-2"}],
        "sub_comment_count": 2,
    }], "has_more": False})
    monkeypatch.setattr(api_client, "note_comments", roots)
    result = thread(api_client, max_comments=limit)
    assert len(result["comments"]) == limit
    assert result["has_more"] is more


def test_replies_can_be_disabled_without_losing_roots(api_client, monkeypatch):
    roots = AsyncMock(return_value={"comments": [{
        "id": "root", "sub_comments": [{"id": "reply"}], "sub_comment_has_more": True,
    }], "has_more": False})
    replies = AsyncMock(side_effect=AssertionError("replies disabled"))
    monkeypatch.setattr(api_client, "note_comments", roots)
    monkeypatch.setattr(api_client, "note_sub_comments", replies)
    result = thread(api_client, include_replies=False)
    assert [c["id"] for c in result["comments"]] == ["root"]
    assert result["has_more"] is False
    replies.assert_not_awaited()


@pytest.mark.parametrize("error", [
    XhsApiError("fixture", category="auth"),
    XhsApiError("fixture", category="risk"),
    TimeoutError("fixture"), asyncio.CancelledError(),
])
def test_pagination_propagates_failure_without_retry_or_partial_success(api_client, monkeypatch, error):
    roots = AsyncMock(side_effect=error)
    monkeypatch.setattr(api_client, "note_comments", roots)
    with pytest.raises(type(error)):
        thread(api_client)
    roots.assert_awaited_once()
    assert Session.instances[0].closed


def test_zero_budget_never_reads_platform(api_client, monkeypatch):
    roots = AsyncMock(side_effect=AssertionError("budget exhausted"))
    monkeypatch.setattr(api_client, "note_comments", roots)
    assert thread(api_client, max_requests=0) == {"comments": [], "has_more": True}
    roots.assert_not_awaited()


def test_flattening_preserves_parent_and_deduplicates_without_mutation():
    from app.platforms.xhs.extract import flatten_comments, parse_comment
    reply = {"id": "reply"}
    root = {"id": "root", "sub_comments": [reply]}
    rows = flatten_comments([root, reply, {"id": "root"}])
    assert [r["id"] for r in rows] == ["root", "reply"]
    assert parse_comment(rows[1])["reply_to"] == "root"
    assert "target_comment" not in reply


def test_image_payload_preserves_private_scope_and_omits_empty_location():
    payload = creator_api._image_note_data("title", "desc", None, {}, 1, [])
    assert payload["common"]["privacy_info"]["type"] == 1
    assert "post_loc" not in payload["common"]


def test_video_payload_preserves_private_scope_and_omits_empty_location():
    payload = creator_api._video_note_data("title", "desc", None, {}, 1,
                                          {"fileIds": "video"}, {"fileIds": "cover"},
                                          {"video": {}, "audio": {}})
    assert payload["common"]["privacy_info"]["type"] == 1
    assert "post_loc" not in payload["common"]


def test_missing_pagination_metadata_is_not_reported_as_complete(api_client, monkeypatch):
    roots = AsyncMock(return_value={"comments": [{"id": "root"}]})
    monkeypatch.setattr(api_client, "note_comments", roots)
    assert thread(api_client)["has_more"] is True
    roots.assert_awaited_once()


def test_pagination_paces_requests_and_propagates_the_same_token(api_client, monkeypatch):
    roots = AsyncMock(return_value={"comments": [{
        "id": "root", "sub_comment_has_more": True,
    }], "has_more": False})
    replies = AsyncMock(return_value={"comments": [], "has_more": False})
    monkeypatch.setattr(api_client, "note_comments", roots)
    monkeypatch.setattr(api_client, "note_sub_comments", replies)
    pause = AsyncMock()
    monkeypatch.setattr("app.platforms.xhs.comments.asyncio.sleep", pause)
    asyncio.run(api_client.collect_note_comments(
        "note", "token", xsec_source="pc_search", request_interval=2.5))
    pause.assert_awaited_once_with(2.5)
    assert roots.await_args.kwargs["xsec_token"] == "token"
    assert roots.await_args.kwargs["xsec_source"] == "pc_search"
    assert replies.await_args.kwargs["xsec_token"] == "token"


def test_valid_location_is_preserved_in_payload():
    location = {"poi_id": "fixture", "name": "fixture"}
    payload = creator_api._image_note_data("title", "desc", None, location, 0, [])
    assert payload["common"]["post_loc"] == location


def test_failed_api_publish_is_not_retried_or_sent_through_browser(tmp_path, monkeypatch):
    media = tmp_path / "fixture.jpg"
    media.write_bytes(b"fixture")
    api = Mock()
    api.post_note.return_value = (False, "fixture rejected", {"success": False})
    monkeypatch.setattr(creator_api, "XhsCreatorApi", Mock(return_value=api))
    monkeypatch.setattr(creator_sign, "available", lambda: True)
    browser = AsyncMock(side_effect=AssertionError("no fallback"))
    monkeypatch.setattr(publish, "publish_xhs_browser", browser)
    result = asyncio.run(publish.publish_xhs(
        None, None, '{"cookies":[{"name":"a1","value":"fixture"}]}',
        "image", "fixture", "fixture", [str(media)], mode="api", visibility="private"))
    assert result[0] is False
    api.post_note.assert_called_once()
    api.close.assert_called_once()
    browser.assert_not_awaited()
