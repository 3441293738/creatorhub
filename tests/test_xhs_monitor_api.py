"""Offline regressions for -510000, access context and partial monitor reads."""
import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlmodel import select

import app.db as db
import app.engine.monitor as monitor
from app.engine.monitor import MonitorEngine
from app.models import ContentRecord, DouyinAccount, MonitorTarget
from app.platforms.xhs.client import XhsApiClient
from app.platforms.xhs.extract import parse_note_brief
from app.platforms.xhs.responses import XhsApiError, validate_payload
from test_project_optimizations import local_project, store
from test_xhs_risk_classification import _BrowserStub


def unavailable():
    try:
        validate_payload({"success": False, "code": -510000, "msg": "笔记不存在", "data": {}})
    except XhsApiError as error:
        return error
    pytest.fail("missing expected API error")


def card(note_id):
    return {"note_id": note_id, "type": "normal", "title": "fixture", "time": 1700000000,
            "image_list": [{"url_default": "https://media.example.invalid/fixture.jpg"}]}


def test_nested_card_keeps_actual_note_id_and_access_parameters():
    brief = parse_note_brief({"id": "wrapper-id", "model_type": "note", "note_card": {
        "note_id": "actual-note", "xsec_token": "token+/=", "xsec_source": "pc_user", "title": "fixture"}})
    assert brief["note_id"] == "actual-note"
    assert brief["xsec_token"] == "token+/=" and brief["xsec_source"] == "pc_user"


def test_conflicting_token_owners_do_not_mix_sources():
    brief = parse_note_brief({"id": "note", "xsec_token": "outer", "note_card": {
        "note_id": "note", "xsec_token": "inner", "xsec_source": "pc_search"}})
    assert brief["xsec_token"] == "outer" and brief["xsec_source"] == ""


@pytest.mark.parametrize("row", [None, [], {}, {"id": "not-a-note", "model_type": "user"},
    {"id": "query", "model_type": "hot_query"}, {"id": "bad", "model_type": [1]},
    {"id": "bad", "note_card": [1]}, {"id": "bad", "note_card": "text"}])
def test_non_note_search_rows_are_not_sent_to_feed(row):
    assert parse_note_brief(row) is None


@pytest.mark.parametrize("row,token,source", [
    ({"id": "note", "xsec_token": "token", "xsec_source": "pc_feed"}, "token", "pc_feed"),
    ({"id": "note", "xsec_source": "pc_search", "note_card": {"xsec_token": "token"}}, "token", "pc_search"),
    ({"id": "note", "xsec_token": 42, "xsec_source": {}}, "", ""),
    ({"id": "note", "cover": {"info_list": [None, {"url": "https://fixture.invalid/cover"}]}}, "", ""),
])
def test_list_shapes_preserve_only_text_access_parameters(row, token, source):
    brief = parse_note_brief(row)
    assert brief["xsec_token"] == token and brief["xsec_source"] == source


def test_feed_contract_and_matching_note_selection():
    client = object.__new__(XhsApiClient)
    wanted = {"id": "wanted", "note_card": card("wanted")}
    client._post = AsyncMock(return_value={"items": [{"id": "other", "note_card": card("other")}, wanted]})
    assert asyncio.run(client.note_detail("wanted", "token+/=", "pc_user")) == wanted["note_card"]
    uri, body = client._post.call_args.args
    assert uri == "/api/sns/web/v1/feed"
    assert body == {"source_note_id": "wanted", "image_formats": ["jpg", "webp", "avif"],
                    "extra": {"need_body_topic": "1"}, "xsec_source": "pc_user", "xsec_token": "token+/="}


@pytest.mark.parametrize("items,signal", [
    ([{"note_card": card("other")}], "note_id_mismatch"),
    ([None], "ambiguous_response"), ("not-list", "ambiguous_response"),
    ([{"note_card": "bad"}], "ambiguous_response"),
])
def test_feed_never_returns_an_unrelated_or_malformed_note(items, signal):
    client = object.__new__(XhsApiClient)
    client._post = AsyncMock(return_value={"items": items})
    with pytest.raises(XhsApiError) as caught:
        asyncio.run(client.note_detail_raw("wanted", "fixture-token"))
    assert caught.value.signal == signal
    client._post.assert_awaited_once()


def test_unavailable_error_is_actionable_without_guessing_deletion_or_echoing_tokens():
    client = object.__new__(XhsApiClient)
    client._post = AsyncMock(side_effect=unavailable())
    with pytest.raises(XhsApiError) as caught:
        asyncio.run(client.note_detail("fixture-note", "PRIVATE-TOKEN"))
    assert caught.value.code == -510000 and caught.value.category == "business"
    assert caught.value.signal == "note_unavailable"
    assert "未确认作品已删除" in str(caught.value) and "PRIVATE-TOKEN" not in str(caught.value)
    client._post.assert_awaited_once()  # No implicit browser fallback or same-note retry.


@pytest.mark.parametrize("message,category", [("请完成验证码", "risk"), ("登录状态已失效", "auth")])
def test_unavailable_code_never_overrides_an_explicit_account_failure(message, category):
    client = object.__new__(XhsApiClient)
    with pytest.raises(XhsApiError) as response_error:
        validate_payload({"success": False, "code": -510000, "msg": message})
    client._post = AsyncMock(side_effect=response_error.value)
    with pytest.raises(XhsApiError) as caught:
        asyncio.run(client.note_detail("fixture-note", "fixture-token"))
    assert caught.value is response_error.value and caught.value.category == category
    assert caught.value.signal != "note_unavailable"


@pytest.fixture
def scanning(local_project, monkeypatch):
    cfg = local_project.cfg
    cfg.engine.xhs_read_mode = "api"
    cfg.risk_control.enabled = False
    account_id = store(DouyinAccount(platform="xhs", status="active",
        storage_state=json.dumps({"cookies": [{"name": "a1", "value": "fixture"}]})))
    target_id = store(MonitorTarget(platform="xhs", target_kind="creator", sec_uid="creator-fixture",
        account_id=account_id, download_enabled=False, max_items_per_scan=10))
    client = SimpleNamespace(notes_by_creator=AsyncMock(return_value={"notes": []}),
        search_notes=AsyncMock(return_value=[]), user_info=AsyncMock(return_value={}),
        note_detail=AsyncMock(side_effect=lambda note_id, **_: card(note_id)))
    engine = MonitorEngine(cfg, _BrowserStub())
    engine._xhs_gap = AsyncMock()
    engine._notify_new = AsyncMock()
    engine._download = AsyncMock()
    monkeypatch.setattr(monitor, "XhsApiClient", lambda *_args, **_kwargs: client)
    browser_detail = AsyncMock(side_effect=AssertionError("API mode must not silently open a browser"))
    monkeypatch.setattr(monitor, "fetch_xhs_note_detail", browser_detail)
    return SimpleNamespace(cfg=cfg, account_id=account_id, target_id=target_id, client=client,
                           engine=engine, browser_detail=browser_detail)


def records(fixture):
    with db.get_session() as session:
        return session.exec(select(ContentRecord).where(ContentRecord.target_id == fixture.target_id).order_by(ContentRecord.id)).all()


@pytest.mark.parametrize("kind", ["creator", "keyword"])
def test_one_unavailable_note_does_not_abort_other_notes_or_send_false_notifications(scanning, kind):
    f = scanning
    rows = [
        {"model_type": "hot_query", "id": "query-not-a-note"},
        {"id": "gone", "xsec_token": "old", "xsec_source": "pc_user", "type": "video", "time": 1700000000},
        {"id": "wrapper-id", "model_type": "note", "note_card": {"note_id": "good", "xsec_token": "nested", "xsec_source": "pc_search"}},
    ]
    f.client.notes_by_creator.return_value = {"notes": rows}
    f.client.search_notes.return_value = rows
    with db.get_session() as session:
        target = session.get(MonitorTarget, f.target_id)
        target.last_scan_at = datetime.utcnow()
        target.target_kind = kind
        target.keyword = "fixture-keyword" if kind == "keyword" else ""
        session.add(target); session.commit()
    async def detail(note_id, **_kwargs):
        if note_id == "gone":
            raise unavailable()
        return card(note_id)
    f.client.note_detail.side_effect = detail
    result = asyncio.run(f.engine.scan_target(f.target_id))
    assert not result["ok"] and result["partial"]
    assert result["captured"] == 1 and result["failed"] == 1 and result["new"] == 2
    assert result["scanned"] == 2 and "-510000" in result["error"]
    bad, good = records(f)
    assert bad.download_status == "failed" and not bad.media_json
    assert bad.create_time == 1700000000 and bad.media_type == "video"
    assert (bad.xsec_token, bad.xsec_source) == ("old", "pc_user")
    assert (good.xsec_token, good.xsec_source) == ("nested", "pc_search")
    assert f.client.note_detail.await_args_list[1].kwargs == {"xsec_token": "nested", "xsec_source": "pc_search"}
    f.engine._notify_new.assert_awaited_once()
    assert [aw.aweme_id for aw in f.engine._notify_new.call_args.args[1]] == ["good"]
    f.browser_detail.assert_not_awaited()


def test_search_failure_is_not_misreported_as_a_single_missing_note(scanning):
    f = scanning
    with db.get_session() as session:
        target = session.get(MonitorTarget, f.target_id)
        target.target_kind = "keyword"; target.keyword = "fixture"
        session.add(target); session.commit()
    f.client.search_notes.side_effect = unavailable()
    result = asyncio.run(f.engine.scan_target(f.target_id))
    assert not result["ok"] and not result["partial"]
    assert result["scanned"] == result["failed"] == 0
    f.client.note_detail.assert_not_awaited()
    assert not records(f)


@pytest.mark.parametrize("kind,source", [("creator", "pc_user"), ("keyword", "pc_search")])
def test_api_defaults_match_the_note_list_origin(scanning, kind, source):
    f = scanning
    rows = [{"id": "note", "xsec_token": "fixture-token"}]
    with db.get_session() as session:
        target = session.get(MonitorTarget, f.target_id)
        target.target_kind = kind; target.keyword = "fixture" if kind == "keyword" else ""
        session.add(target); session.commit()
    f.client.notes_by_creator.return_value = {"notes": rows}
    f.client.search_notes.return_value = rows
    result = asyncio.run(f.engine.scan_target(f.target_id))
    assert result["ok"] and not result["partial"]
    assert f.client.note_detail.call_args.kwargs["xsec_source"] == source
    assert records(f)[0].xsec_source == source


def test_repeated_unavailable_notes_stop_the_batch_without_success_or_retry_storm(scanning):
    f = scanning
    f.client.notes_by_creator.return_value = {"notes": [{"id": f"note-{i}"} for i in range(10)]}
    f.client.note_detail.side_effect = unavailable()
    result = asyncio.run(f.engine.scan_target(f.target_id))
    assert not result["ok"] and not result["partial"]
    assert result["captured"] == 0 and result["failed"] == result["scanned"] == 3
    assert "连续 3 条" in result["error"]
    assert f.client.note_detail.await_count == 3 and len(records(f)) == 3
    f.engine._notify_new.assert_not_awaited()
    f.engine.retry_download = AsyncMock()
    asyncio.run(f.engine._retry_failed())
    f.engine.retry_download.assert_not_awaited()


@pytest.mark.parametrize("category,signal", [("risk", "http_429"), ("auth", "auth_expired"), ("network", "network_failure"), ("business", "unknown")])
def test_non_note_errors_still_stop_immediately(scanning, category, signal):
    f = scanning
    f.client.notes_by_creator.return_value = {"notes": [{"id": "one"}, {"id": "two"}]}
    f.client.note_detail.side_effect = XhsApiError("fixture failure", category=category, signal=signal)
    result = asyncio.run(f.engine.scan_target(f.target_id))
    assert not result["ok"] and result["error"] == "fixture failure"
    assert f.client.note_detail.await_count == 1 and not records(f)


@pytest.mark.parametrize("snapshot", ["", "[]", "{}", "null", "broken", '[{}]', '[{"url":""}]'])
def test_legacy_empty_snapshots_never_auto_refetch_feed(scanning, snapshot):
    f = scanning
    store(ContentRecord(platform="xhs", target_id=f.target_id, aweme_id="old", download_status="failed", media_json=snapshot))
    f.engine.retry_download = AsyncMock()
    asyncio.run(f.engine._retry_failed())
    f.engine.retry_download.assert_not_awaited()


def test_manual_retry_reuses_persisted_source_and_token(scanning):
    f = scanning
    record_id = store(ContentRecord(platform="xhs", target_id=f.target_id, aweme_id="old",
        download_status="failed", xsec_token="stored-token", xsec_source="pc_note_detail"))
    f.engine._xhs_client = lambda *_args, **_kwargs: f.client
    f.engine.downloader.download_aweme = AsyncMock(return_value=(True, "fixture.jpg", ""))
    result = asyncio.run(f.engine.retry_download(record_id))
    assert result["ok"]
    assert f.client.note_detail.call_args.kwargs == {"xsec_token": "stored-token", "xsec_source": "pc_note_detail"}
    assert records(f)[0].download_status == "done"


def test_list_refresh_updates_failed_access_without_automatic_detail_requests(scanning):
    f = scanning
    retry_id = store(ContentRecord(platform="xhs", target_id=f.target_id, aweme_id="retry",
        download_status="failed", xsec_token="old", xsec_source="pc_feed", media_json="[]", error="old error"))
    store(ContentRecord(platform="xhs", target_id=f.target_id, aweme_id="done",
        download_status="done", xsec_token="keep", xsec_source="pc_user"))
    f.client.notes_by_creator.return_value = {"notes": [
        {"id": "retry", "xsec_token": "fresh", "xsec_source": "pc_search"},
        {"id": "done", "xsec_token": "not-used"},
    ]}
    result = asyncio.run(f.engine.scan_target(f.target_id))
    assert result["ok"] and result["refreshed"] == 1 and result["new"] == 0
    f.client.note_detail.assert_not_awaited()
    retry, done = records(f)
    assert retry.id == retry_id and retry.download_status == "failed" and retry.error == "old error"
    assert (retry.xsec_token, retry.xsec_source) == ("fresh", "pc_search")
    assert done.xsec_token == "keep"


def test_existing_database_gets_empty_source_without_losing_records(local_project):
    record_id = store(ContentRecord(platform="xhs", target_id=1, aweme_id="old", xsec_token="preserved"))
    with db._engine.begin() as connection:
        connection.execute(text('ALTER TABLE contentrecord DROP COLUMN xsec_source'))
    db._auto_migrate(db._engine)
    with db.get_session() as session:
        row = session.get(ContentRecord, record_id)
        assert row.xsec_source == "" and row.xsec_token == "preserved"
