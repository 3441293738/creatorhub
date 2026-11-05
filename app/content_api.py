"""CreatorHub 本机成品索引 API。"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .content_pipeline import (
    AdsAIJSONClient,
    AdsContentSyncClient,
    AIContentService,
    ContentIndexStore,
    ContentPipeline,
    OpenAICompatibleJSONClient,
    PipelineError,
    resolve_secret,
    scan_sources,
)

router = APIRouter(prefix="/api/content-index", tags=["content-index"])
ROOT = Path(__file__).resolve().parent.parent


def content_paths() -> tuple[Path, Path, Path]:
    """解析本机素材、口播和独立缓存路径。"""
    share = Path(
        os.environ.get(
            "CREATORHUB_CONTENT_SHARE",
            str(ROOT / "data" / "media" / "share"),
        )
    ).expanduser()
    inbox = Path(
        os.environ.get(
            "CREATORHUB_CONTENT_INBOX",
            str(ROOT.parent / "media-pipeline" / "inbox"),
        )
    ).expanduser()
    database = Path(
        os.environ.get(
            "CREATORHUB_CONTENT_DB",
            str(ROOT / "data" / "content_index.sqlite"),
        )
    ).expanduser()
    return share, inbox, database


def content_store() -> ContentIndexStore:
    """获取独立成品索引缓存。"""
    return ContentIndexStore(content_paths()[2])


@router.get("")
async def list_content_index():
    """列出本机素材状态和 AI 草稿数量。"""
    store = content_store()
    items = store.list_assets()
    return {"count": len(items), "items": items}


@router.post("/scan")
async def scan_content_index():
    """只扫描和关联，不调用 AI。"""
    share, inbox, _ = content_paths()
    try:
        report = await asyncio.to_thread(scan_sources, share, inbox)
        store = content_store()
        for item in report.items:
            store.upsert_asset(item.asset_record())
        return {**report.counts, "pending": report.pending}
    except (OSError, PipelineError) as exc:
        return JSONResponse(
            {"detail": f"{exc}；检查本机素材目录和口播目录后重试"},
            status_code=400,
        )


@router.post("/process")
async def process_content_index():
    """自动执行扫描、AI 元数据和九平台草稿。"""
    share, inbox, _ = content_paths()
    ads_user = os.environ.get("CREATORHUB_ADS_USER", "") or resolve_secret("CREATORHUB_ADS_USER")
    ads_password = resolve_secret("CREATORHUB_ADS_PASSWORD")
    caller = (
        AdsAIJSONClient(
            os.environ.get("CREATORHUB_ADS_URL", "https://ads.slcoolco.net"),
            ads_user,
            ads_password,
        )
        if ads_user and ads_password
        else OpenAICompatibleJSONClient()
    )
    pipeline = ContentPipeline(
        content_store(),
        AIContentService(caller),
    )
    try:
        result = await asyncio.to_thread(pipeline.run, share, inbox)
    except (OSError, PipelineError) as exc:
        return JSONResponse(
            {"detail": f"{exc}；检查 AI 凭证或网络后重试"},
            status_code=503,
        )
    finally:
        if isinstance(caller, AdsAIJSONClient):
            caller.close()
    status = 200 if result["failed"] == 0 else 207
    return JSONResponse(result, status_code=status)


@router.get("/{source_id}/drafts")
async def list_content_drafts(source_id: str):
    """查看一条素材的九平台草稿。"""
    store = content_store()
    if not store.get_asset(source_id):
        return JSONResponse({"detail": "素材不存在"}, status_code=404)
    return {"source_id": source_id, "drafts": store.list_drafts(source_id)}


@router.post("/sync")
async def sync_content_index():
    """同步全部已生成内容；外部原片和完整口播不会离开本机。"""
    store = content_store()
    assets = [
        store.export_for_sync(item["source_id"])
        for item in store.list_assets()
        if item.get("metadata") and item.get("draft_count") == 9
    ]
    if not assets:
        return JSONResponse(
            {"detail": "没有可同步内容；先运行 AI 自动处理"},
            status_code=400,
        )
    client = AdsContentSyncClient(
        os.environ.get("CREATORHUB_ADS_URL", "https://ads.slcoolco.net"),
        os.environ.get("CREATORHUB_ADS_USER", "") or resolve_secret("CREATORHUB_ADS_USER"),
        resolve_secret("CREATORHUB_ADS_PASSWORD"),
    )
    try:
        result = await asyncio.to_thread(client.sync, assets)
    except (httpx.HTTPError, PipelineError) as exc:
        return JSONResponse(
            {"detail": f"{exc}；检查公司账号或网络后重试"},
            status_code=503,
        )
    return {**result, "sent": len(assets)}
