"""成品索引自动流水线回归。"""
from __future__ import annotations

import json
import asyncio
from pathlib import Path

import httpx
import pytest

import app.content_api as content_api
import app.main as main
from app.content_pipeline import (
    AdsAIJSONClient,
    AdsContentSyncClient,
    PLATFORMS,
    AIContentService,
    ContentIndexStore,
    ContentPipeline,
    PipelineError,
    scan_sources,
)


def _source_tree(tmp_path: Path) -> tuple[Path, Path]:
    share = tmp_path / "share"
    inbox = tmp_path / "inbox"
    (share / "小谢修车").mkdir(parents=True)
    (inbox / "7580322665321700082").mkdir(parents=True)
    (share / "小谢修车" / "7580322665321700082_发动机异响.mp4").write_bytes(b"video")
    (inbox / "7580322665321700082" / "transcript.txt").write_text(
        "发动机怠速抖动，清洗进气压力传感器后恢复正常。",
        encoding="utf-8",
    )
    return share, inbox


def _metadata() -> dict:
    return {
        "title": "发动机怠速抖动的传感器排查方法",
        "summary": "根据口播，清洗进气压力传感器后怠速恢复正常。",
        "content_type": "故障讲解",
        "make": "",
        "model": "",
        "year_start": None,
        "year_end": None,
        "topic": "故障诊断",
        "confidence": 0.91,
        "evidence": ["发动机怠速抖动", "进气压力传感器"],
    }


def _drafts() -> dict:
    result = {}
    for index, platform in enumerate(PLATFORMS):
        result[platform] = {
            "title": f"{platform} 发动机传感器排查 {index}",
            "body": f"面向 {platform} 的独立内容 {index}，根据口播整理。",
            "hashtags": [f"tag{index}"],
            "language": "zh-CN" if platform == "douyin" else "en",
            "notes": "发布前人工审核",
        }
    result["reddit"]["subreddit"] = ""
    result["pinterest"]["board"] = ""
    result["quora"]["question"] = ""
    return result


class FakeAI:
    """按顺序返回元数据和九平台草稿。"""

    def __init__(self, *, fail_primary: bool = False):
        self.calls = []
        self.fail_primary = fail_primary

    def call_json(self, prompt: str) -> tuple[dict, str]:
        self.calls.append(prompt)
        if "九个平台" in prompt:
            return _drafts(), "deepseek"
        return _metadata(), "qwen" if self.fail_primary else "deepseek"


def test_scan_matches_video_and_transcript_without_modifying_source(tmp_path):
    share, inbox = _source_tree(tmp_path)
    video = next(share.rglob("*.mp4"))
    before = (video.stat().st_size, video.stat().st_mtime_ns, video.read_bytes())
    report = scan_sources(share, inbox)
    after = (video.stat().st_size, video.stat().st_mtime_ns, video.read_bytes())
    assert report.counts == {"video": 1, "transcript": 1, "matched": 1, "missing": 0}
    assert report.items[0].source_id == "7580322665321700082"
    assert before == after


def test_metadata_does_not_invent_make_model_or_year():
    response = {**_metadata(), "make": "Toyota", "model": "Prado", "year_start": 2018, "year_end": 2020}
    service = AIContentService(FakeAI())
    guarded = service.guard_metadata(response, "发动机怠速抖动")
    assert guarded["make"] == guarded["model"] == ""
    assert guarded["year_start"] is None and guarded["year_end"] is None
    assert guarded["review_required"] is True


def test_explicit_vehicle_evidence_is_preserved():
    response = {**_metadata(), "make": "Toyota", "model": "Prado", "year_start": 2018, "year_end": 2020}
    transcript = "2018年至2020年 Toyota Prado 的刹车异响排查。"
    guarded = AIContentService(FakeAI()).guard_metadata(response, transcript)
    assert guarded["make"] == "Toyota"
    assert guarded["model"] == "Prado"
    assert guarded["year_start"] == 2018
    assert guarded["year_end"] == 2020


def test_pipeline_saves_exactly_nine_independent_drafts(tmp_path):
    share, inbox = _source_tree(tmp_path)
    store = ContentIndexStore(tmp_path / "content.sqlite")
    pipeline = ContentPipeline(store, AIContentService(FakeAI()))
    result = pipeline.run(share, inbox)
    assert result["processed"] == 1 and result["failed"] == 0
    drafts = store.list_drafts("7580322665321700082")
    assert len(drafts) == 9
    assert set(row["platform"] for row in drafts) == set(PLATFORMS)
    assert {row["status"] for row in drafts} == {"draft"}
    assert len({row["fields"]["body"] for row in drafts}) == 9


def test_repeated_pipeline_run_is_idempotent(tmp_path):
    share, inbox = _source_tree(tmp_path)
    store = ContentIndexStore(tmp_path / "content.sqlite")
    pipeline = ContentPipeline(store, AIContentService(FakeAI()))
    pipeline.run(share, inbox)
    pipeline.run(share, inbox)
    assert store.count_assets() == 1
    assert len(store.list_drafts("7580322665321700082")) == 9


@pytest.mark.parametrize(
    ("platform", "field"),
    [("reddit", "subreddit"), ("pinterest", "board"), ("quora", "question")],
)
def test_target_fields_block_ready(platform, field, tmp_path):
    store = ContentIndexStore(tmp_path / "content.sqlite")
    store.save_drafts("asset", _drafts(), "deepseek")
    with pytest.raises(PipelineError, match=field):
        store.transition_draft("asset", platform, "ready", expected_version=1)


def test_human_confirmed_metadata_is_not_silently_overwritten(tmp_path):
    store = ContentIndexStore(tmp_path / "content.sqlite")
    store.upsert_asset({
        "source_id": "asset",
        "author": "a",
        "source_title": "source",
        "source_path": "/a.mp4",
        "transcript_path": "/a.txt",
        "sha256": "x",
        "size_bytes": 1,
        "mtime": 1,
    })
    store.save_metadata("asset", {**_metadata(), "title": "人工标题"}, "human", confirmed=True)
    store.save_metadata("asset", _metadata(), "deepseek", confirmed=False)
    assert store.get_asset("asset")["metadata"]["title"] == "人工标题"


def test_export_payload_contains_no_source_path_or_transcript(tmp_path):
    share, inbox = _source_tree(tmp_path)
    store = ContentIndexStore(tmp_path / "content.sqlite")
    ContentPipeline(store, AIContentService(FakeAI())).run(share, inbox)
    payload = store.export_for_sync("7580322665321700082")
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "source_path" not in encoded
    assert "transcript_path" not in encoded
    assert "发动机怠速抖动，清洗" not in encoded
    assert len(payload["drafts"]) == 9


def test_ads_sync_uses_authenticated_metadata_only_request():
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        if request.url.path == "/login":
            return httpx.Response(
                302,
                headers={"set-cookie": "session_token=test; Path=/; HttpOnly"},
            )
        assert request.url.path == "/api/content-index/sync"
        body = json.loads(request.content)
        assert body["assets"][0]["drafts"] == []
        return httpx.Response(200, json={"count": 1})

    client = AdsContentSyncClient(
        "https://ads.example",
        "uploader",
        "secret",
        transport=httpx.MockTransport(handler),
    )
    assert client.sync([{"source_id": "1", "drafts": []}])["count"] == 1
    assert len(requests) == 2


def test_ads_ai_client_logs_in_once_and_returns_provider():
    paths = []

    def handler(request: httpx.Request):
        paths.append(request.url.path)
        if request.url.path == "/login":
            return httpx.Response(
                302,
                headers={"set-cookie": "session_token=test; Path=/; HttpOnly; Secure"},
            )
        return httpx.Response(
            200,
            json={"result": {"title": "自动标题"}, "provider": "deepseek"},
        )

    client = AdsAIJSONClient(
        "https://ads.example",
        "uploader",
        "secret",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert client.call_json("first")[1] == "deepseek"
        assert client.call_json("second")[0]["title"] == "自动标题"
    finally:
        client.close()
    assert paths == ["/login", "/api/content-index/ai-json", "/api/content-index/ai-json"]


def test_local_scan_api_indexes_real_files_without_ai(tmp_path, monkeypatch):
    share, inbox = _source_tree(tmp_path)
    database = tmp_path / "content.sqlite"
    monkeypatch.setattr(content_api, "content_paths", lambda: (share, inbox, database))

    async def scenario():
        transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 1234))
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            response = await client.post("/api/content-index/scan")
            assert response.status_code == 200
            assert response.json()["matched"] == 1
            listing = await client.get("/api/content-index")
            assert listing.status_code == 200
            assert listing.json()["items"][0]["draft_count"] == 0

    asyncio.run(scenario())
