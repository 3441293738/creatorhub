"""Offline note fixtures: codec-key changes must not erase media or authors."""
import asyncio
import copy
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

import app.db as db
from app.browser.xhs_fetcher import _decamel, fetch_xhs_note_detail
from app.engine.collection import KeywordCollector, _author_id
from app.models import DouyinAccount, KeywordCollectionContent, KeywordCollectionJob
from app.platforms.xhs.client import XhsApiClient
from app.platforms.xhs.extract import parse_note_brief, parse_note_detail
from test_project_optimizations import local_project, store  # noqa: F401


VIDEO_URL = "https://media.example.invalid/video.mp4?sign=a%2Bb%3D&t=123"
COVER_URL = "https://media.example.invalid/cover.jpg"


def note(stream=None):
    return {
        "note_id": "TARGET", "type": "video", "title": "fixture title",
        "desc": "fixture description", "time": 1700000000000,
        "user": {"user_id": "AUTHOR", "nickname": "fixture author", "avatar": COVER_URL},
        "interact_info": {"liked_count": "1.2万", "comment_count": "15"},
        "image_list": [{"url_default": COVER_URL}],
        "video": {"media": {"stream": stream if stream is not None else {
            "EF4": [{"master_url": VIDEO_URL, "video_codec": "h264"}],
        }}},
    }


@pytest.mark.parametrize("codec", ["h264", "h265", "av1", "h266", "EF4", "EF5", "EF6", "EF7", "future_codec"])
@pytest.mark.parametrize("ssr", [False, True])
def test_video_stream_keys_are_not_a_fixed_allowlist(codec, ssr):
    card = note({codec: [{"master_url": VIDEO_URL}]})
    if ssr:
        card = _decamel(card)  # EF4 currently becomes e_f4 in the browser path.
    before = copy.deepcopy(card)
    work = parse_note_detail(card)
    assert work is not None
    assert work.medias[0].url == VIDEO_URL
    assert work.medias[0].kind == "video" and work.medias[0].ext == "mp4"
    assert work.author_name == "fixture author"
    assert card == before  # Parsing must not rewrite the response snapshot.


@pytest.mark.parametrize("field", ["media_v2", "mediaV2"])
@pytest.mark.parametrize("encoded", [False, True])
@pytest.mark.parametrize("ssr", [False, True])
def test_media_v2_accepts_json_or_objects_and_camelcase_urls(field, encoded, ssr):
    card = note({})
    media = {"stream": {"EF4": [{"masterUrl": VIDEO_URL, "videoCodec": "h264"}]}}
    card["video"][field] = json.dumps(media) if encoded else media
    if ssr:
        card = _decamel(card)
    work = parse_note_detail(card)
    assert work is not None and work.medias[0].url == VIDEO_URL


def test_codec_metadata_preserves_h264_preference_for_renamed_groups():
    card = note({
        "future_hevc": [{"video_codec": "hevc", "master_url": VIDEO_URL + "&hevc=1"}],
        "unknown": [{"master_url": VIDEO_URL + "&unknown=1"}],
        "future_avc": [{"videoCodec": "avc1.640028", "masterUrl": VIDEO_URL}],
    })
    assert parse_note_detail(card).medias[0].url == VIDEO_URL


def test_classic_codec_priority_is_preserved():
    card = note({"h265": [{"master_url": VIDEO_URL + "&hevc=1"}],
                 "h264": [{"master_url": VIDEO_URL}]})
    assert parse_note_detail(card).medias[0].url == VIDEO_URL


@pytest.mark.parametrize("entry", [
    {"master_url": "", "backup_urls": [None, "", VIDEO_URL]},
    {"master_url": {}, "backup_urls": [VIDEO_URL]},
    {"master_url": "javascript:bad()", "backup_urls": [VIDEO_URL]},
    {"masterUrl": " ", "backupUrls": [42, VIDEO_URL]},
    {"url": VIDEO_URL},
])
def test_stream_url_fallbacks_skip_empty_or_non_http_values(entry):
    work = parse_note_detail(note({"EF4": [None, 42, {}, entry]}))
    assert work is not None and work.medias[0].url == VIDEO_URL


@pytest.mark.parametrize("video", [
    None, [], "bad", {"media": []}, {"media": {"stream": []}},
    {"media": {"stream": {"EF4": "bad", "EF5": [None, 42, {}]}}},
    {"media_v2": "not-json"}, {"media_v2": "[]"}, {"media_v2": "null"},
    {"media_v2": '__import__("os").getcwd()'},
    {"media": {"stream": {"EF4": [{"backup_urls": VIDEO_URL}]}}},
])
def test_invalid_video_fields_are_not_downloadable_but_metadata_survives(video):
    card = note()
    card["video"] = video
    assert parse_note_detail(card) is None  # Keep the download-required contract.
    work = parse_note_detail(card, require_media=False)
    assert work is not None and work.medias == []
    assert work.author_name == "fixture author" and work.avatar == COVER_URL
    assert work.desc == "fixture title\nfixture description"
    assert work.like_count == 12000 and work.comment_count == 15


def test_broken_media_v2_does_not_hide_valid_legacy_streams():
    card = note({"h264": [{"master_url": VIDEO_URL}]})
    card["video"]["media_v2"] = "not-json"
    assert parse_note_detail(card).medias[0].url == VIDEO_URL


@pytest.mark.parametrize("field", ["origin_video_key", "originVideoKey"])
def test_original_video_key_fallback_remains_supported(field):
    card = note({})
    card["video"]["consumer"] = {field: "fixture/original.mp4"}
    assert parse_note_detail(card).medias[0].url == "https://sns-video-bd.xhscdn.com/fixture/original.mp4"


@pytest.mark.parametrize("camel", [False, True])
def test_images_and_author_fields_remain_compatible(camel):
    card = note()
    card["type"] = "normal"
    card.pop("video")
    if camel:
        card["noteId"] = card.pop("note_id")
        card["imageList"] = [{"infoList": [{"imageScene": "WB_DFT", "url": COVER_URL}]}]
        card.pop("image_list")
        card["user"] = {"userId": "AUTHOR", "nickName": "fixture author", "avatar": COVER_URL}
    work = parse_note_detail(card)
    assert work is not None and work.media_type == "images"
    assert work.author_name == "fixture author" and work.medias[0].url == COVER_URL
    assert _author_id(card, "xhs") == "AUTHOR"


def test_brief_preserves_camelcase_author_and_the_token_source_pair():
    brief = parse_note_brief({"id": "TARGET", "xsecToken": "TOKEN", "xsecSource": "pc_search",
        "noteCard": {"noteId": "TARGET", "type": "video", "displayTitle": "fixture",
            "user": {"userId": "AUTHOR", "nickName": "fixture author", "avatar": COVER_URL}}})
    assert brief["author_id"] == "AUTHOR" and brief["author_name"] == "fixture author"
    assert brief["xsec_token"] == "TOKEN" and brief["xsec_source"] == "pc_search"
    assert brief["type"] == "video" and brief["title"] == "fixture"


@pytest.mark.parametrize("card", [None, [], {}, {"user": {"nickname": "not a note"}}])
def test_metadata_mode_still_requires_a_note_identity(card):
    assert parse_note_detail(card, require_media=False) is None


@pytest.mark.parametrize("mode", ["api", "browser"])
@pytest.mark.parametrize("has_media", [False, True])
def test_both_collection_paths_preserve_author_and_persist_download_state(
        local_project, monkeypatch, mode, has_media):
    card = note() if has_media else note({})
    raw = {"id": "TARGET", "note_card": copy.deepcopy(card)}
    # Keep the list deliberately brief: the successful detail contains richer metadata.
    raw["note_card"].pop("video")
    client = SimpleNamespace(note_detail=AsyncMock(return_value=card))
    browser_read = AsyncMock(return_value=(_decamel(card), ""))
    monkeypatch.setattr("app.engine.collection.fetch_xhs_note_detail", browser_read)
    collector = KeywordCollector(local_project.cfg, local_project.browser, SimpleNamespace())

    async def run():
        if mode == "api":
            return await collector._materialize_xhs(client, raw)
        return await collector._materialize_xhs_browser(None, raw)

    work, parts, error = asyncio.run(run())
    assert work.author_name == "fixture author" and work.avatar == COVER_URL
    assert work.like_count == 12000 and work.comment_count == 15
    assert len(work.medias) == int(has_media) and error == ""
    job = KeywordCollectionJob(id=1, platform="xhs", download_media=True)
    saved = collector._upsert_content(job, "fixture", work,
        _author_id(parts["card"], "xhs"), "", "pc_search", error)
    assert saved.author_name == "fixture author" and saved.author_id == "AUTHOR"
    assert saved.download_status == ("pending" if has_media else "failed")
    assert saved.error == ("" if has_media else "未取得媒体地址")
    assert bool(json.loads(saved.media_json)) is has_media
    if mode == "api":
        client.note_detail.assert_awaited_once()
        browser_read.assert_not_awaited()
    else:
        browser_read.assert_awaited_once()
        client.note_detail.assert_not_awaited()


@pytest.mark.parametrize("mode", ["api", "browser"])
def test_failed_detail_keeps_list_author_without_promoting_cover_to_video(monkeypatch, mode):
    raw = {"id": "TARGET", "note_card": note({})}
    client = SimpleNamespace(note_detail=AsyncMock(side_effect=RuntimeError("fixture detail error")))
    monkeypatch.setattr("app.engine.collection.fetch_xhs_note_detail",
                        AsyncMock(return_value=(None, "fixture detail error")))
    collector = KeywordCollector(SimpleNamespace(engine=SimpleNamespace(block_media_resources=True)), None, None)
    result = (collector._materialize_xhs(client, raw) if mode == "api"
              else collector._materialize_xhs_browser(None, raw))
    work, parts, error = asyncio.run(result)
    assert work.author_name == "fixture author" and work.avatar == COVER_URL
    assert work.medias == [] and work.media_type == "video"
    assert parts["brief"]["author_id"] == "AUTHOR"
    assert "fixture detail error" in error


@pytest.mark.parametrize("mode", ["api", "browser"])
@pytest.mark.parametrize("has_media", [False, True])
def test_collection_run_keeps_list_author_when_detail_omits_it_and_only_downloads_media(
        local_project, monkeypatch, mode, has_media):
    cfg = local_project.cfg
    cfg.engine.xhs_read_mode = mode
    account_id = store(DouyinAccount(platform="xhs", storage_state=json.dumps({
        "cookies": [{"name": "a1", "value": "TOKEN", "domain": ".xiaohongshu.com"}]})))
    job_id = store(KeywordCollectionJob(platform="xhs", account_id=account_id,
        keywords='["fixture"]', download_media=True, max_comments_per_content=0))
    with db.get_session() as session:
        account = session.get(DouyinAccount, account_id)
    raw = {"id": "TARGET", "note_card": note({})}
    card = note() if has_media else note({})
    card.pop("user")
    client = SimpleNamespace(note_detail=AsyncMock(return_value=card))

    @asynccontextmanager
    async def session_scope():
        yield client

    client.session_scope = session_scope
    monkeypatch.setattr("app.engine.collection.XhsApiClient", lambda *_a, **_kw: client)
    monkeypatch.setattr("app.engine.collection.fetch_xhs_note_detail",
                        AsyncMock(return_value=(_decamel(card), "")))
    browser = SimpleNamespace(identity_for=lambda _a: SimpleNamespace(ua="fixture"))
    downloader = SimpleNamespace(download_aweme=AsyncMock(return_value=(
        True, str(local_project.root / "fixture.mp4"), "")))
    collector = KeywordCollector(cfg, browser, downloader)
    collector._discover_xhs = AsyncMock(return_value=([raw], ""))
    collector._discover_xhs_browser = AsyncMock(return_value=([raw], ""))
    result = asyncio.run(collector.run(job_id, account))
    assert result["contents"] == 1
    with db.get_session() as session:
        saved = session.exec(select(KeywordCollectionContent).where(
            KeywordCollectionContent.job_id == job_id)).one()
        assert saved.author_id == "AUTHOR" and saved.author_name == "fixture author"
        assert saved.download_status == ("done" if has_media else "failed")
        assert saved.error == ("" if has_media else "未取得媒体地址")
    if has_media:
        downloader.download_aweme.assert_awaited_once()
        assert downloader.download_aweme.call_args.args[0].medias[0].url == VIDEO_URL
    else:
        downloader.download_aweme.assert_not_awaited()


def test_api_and_ssr_fetchers_feed_new_video_shape_into_shared_parser(monkeypatch):
    card = note()
    client = object.__new__(XhsApiClient)
    client._post = AsyncMock(return_value={"items": [{"id": "TARGET", "note_card": card}]})

    class Page:
        url = "https://www.xiaohongshu.com/explore/TARGET"

        def on(self, *_args):
            pass

        async def goto(self, *_args, **_kwargs):
            pass

        async def evaluate(self, _script, _note_id):
            return json.dumps(card)

    @asynccontextmanager
    async def visible_page(_identity):
        yield Page()

    monkeypatch.setattr("app.browser.xhs_fetcher._xhs_reading_pause", AsyncMock())

    async def run():
        api_card = await client.note_detail("TARGET")
        browser_card, error = await fetch_xhs_note_detail(SimpleNamespace(visible_page=visible_page), None, "TARGET")
        assert error == ""
        for value in (api_card, browser_card):
            work = parse_note_detail(value)
            assert work is not None and work.medias[0].url == VIDEO_URL
            assert work.author_name == "fixture author"

    asyncio.run(run())
