"""CreatorHub 本机成品索引 API。"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import httpx
from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

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
from .content_video_pipeline import (
    AdsMediaGatewayClient,
    LocalVideoRenderer,
    VideoPlanService,
    estimate_plan_cost,
)
from .engine.share_downloader import _find_ffmpeg

router = APIRouter(prefix="/api/content-index", tags=["content-index"])
ROOT = Path(__file__).resolve().parent.parent
VIDEO_OUTPUT_ROOT = ROOT / "data" / "content_output"
_VIDEO_BACKGROUND_TASKS: set[asyncio.Task] = set()


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
        VideoPlanService(caller),
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


def _ads_media_client() -> AdsMediaGatewayClient:
    """从系统凭证库构造媒体网关客户端。"""
    return AdsMediaGatewayClient(
        os.environ.get("CREATORHUB_ADS_URL", "https://ads.slcoolco.net"),
        os.environ.get("CREATORHUB_ADS_USER", "")
        or resolve_secret("CREATORHUB_ADS_USER"),
        resolve_secret("CREATORHUB_ADS_PASSWORD"),
    )


def _download_generated_file(url: str, path: Path, *, max_bytes: int) -> None:
    """下载模型生成结果，并限制文件大小。"""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"https", "http"} or not host.endswith(
        (".aliyuncs.com", ".aliyun.com")
    ):
        raise PipelineError("模型返回的文件地址无效")
    if parsed.scheme == "http":
        parsed = parsed._replace(scheme="https")
        url = urlunparse(parsed)
    path.parent.mkdir(parents=True, exist_ok=True)
    received = 0
    try:
        with httpx.stream("GET", url, follow_redirects=True, timeout=120) as response:
            response.raise_for_status()
            with path.open("wb") as handle:
                for chunk in response.iter_bytes():
                    received += len(chunk)
                    if received > max_bytes:
                        raise PipelineError("模型生成文件超过本机安全上限")
                    handle.write(chunk)
    except httpx.HTTPError as exc:
        raise PipelineError("模型生成文件下载失败") from exc
    if received == 0:
        raise PipelineError("模型生成文件为空")


def _wait_video_scene(client: AdsMediaGatewayClient, task_id: str) -> str:
    """轮询一个 Wan 场景，最长等待十分钟。"""
    for _ in range(120):
        result = client.get_task(task_id)
        status = result.get("status")
        if status == "SUCCEEDED":
            url = str(result.get("video_url") or "")
            if not url:
                raise PipelineError("视频任务完成但没有结果地址")
            return url
        if status in {"FAILED", "CANCELED", "UNKNOWN"}:
            raise PipelineError(result.get("error") or f"视频任务状态：{status}")
        time.sleep(5)
    raise PipelineError("视频模型等待超过十分钟")


def _run_video_job(source_id: str, quality: str) -> None:
    """后台完成四段生成、配音下载和本地合成。"""
    store = content_store()
    job = store.get_video_job(source_id)
    if not job:
        return
    client = _ads_media_client()
    workspace = VIDEO_OUTPUT_ROOT / source_id / "work" / quality
    try:
        workspace.mkdir(parents=True, exist_ok=True)
        store.update_video_job(
            source_id,
            status="generating",
            progress=2,
            quality=quality,
            estimated_cost_cny=estimate_plan_cost(job["plan"], quality),
        )
        scene_paths = []
        for index, scene in enumerate(job["plan"]["scenes"], start=1):
            scene_path = workspace / f"scene-{index}.mp4"
            task_path = workspace / f"scene-{index}.task.json"
            if not scene_path.is_file():
                task_id = ""
                if task_path.is_file():
                    try:
                        task_id = str(
                            json.loads(task_path.read_text(encoding="utf-8"))["task_id"]
                        )
                    except (OSError, KeyError, TypeError, ValueError):
                        task_id = ""
                if not task_id:
                    submitted = client.submit_scene(
                        scene["visual_prompt"],
                        int(scene["duration"]),
                        quality,
                    )
                    task_id = str(submitted["task_id"])
                    task_path.write_text(
                        json.dumps({"task_id": task_id}),
                        encoding="utf-8",
                    )
                url = _wait_video_scene(client, task_id)
                _download_generated_file(
                    url,
                    scene_path,
                    max_bytes=250 * 1024 * 1024,
                )
            scene_paths.append(scene_path)
            store.update_video_job(
                source_id,
                status="generating",
                progress=5 + index * 17,
            )
        narration = "\n".join(
            scene["narration"] for scene in job["plan"]["scenes"]
        )
        voice_path = workspace / "voice.mp3"
        if not voice_path.is_file():
            voice = client.synthesize_voice(narration)
            _download_generated_file(
                str(voice["audio_url"]),
                voice_path,
                max_bytes=30 * 1024 * 1024,
            )
        store.update_video_job(source_id, status="rendering", progress=85)
        ffmpeg_path = _find_ffmpeg()
        if not ffmpeg_path:
            raise PipelineError("本机缺少 FFmpeg，无法合成视频")
        final = LocalVideoRenderer(ffmpeg_path).render(
            source_id=source_id,
            plan=job["plan"],
            scene_paths=scene_paths,
            voice_path=voice_path,
            output_root=VIDEO_OUTPUT_ROOT,
        )
        store.update_video_job(
            source_id,
            status="ready",
            progress=100,
            output_path=str(final),
        )
    except (OSError, PipelineError) as exc:
        store.update_video_job(
            source_id,
            status="failed",
            progress=0,
            error=str(exc),
        )
    finally:
        client.close()


@router.post("/video/{source_id}/generate")
async def generate_content_video(source_id: str, body: dict):
    """确认费用后启动一条本机 AI 二创任务。"""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", source_id):
        return JSONResponse({"detail": "素材编号无效"}, status_code=400)
    if body.get("confirm_cost") is not True:
        return JSONResponse(
            {"detail": "必须先确认本次模型费用"},
            status_code=400,
        )
    quality = str(body.get("quality") or "preview")
    if quality not in {"preview", "quality"}:
        return JSONResponse({"detail": "未知视频质量档位"}, status_code=400)
    store = content_store()
    job = store.get_video_job(source_id)
    if not job:
        return JSONResponse(
            {"detail": "尚未生成视频分镜，请先运行 AI 文案处理"},
            status_code=400,
        )
    if job["status"] in {"queued", "generating", "rendering"}:
        return JSONResponse({"detail": "视频正在生成，请勿重复提交"}, status_code=409)
    cost = estimate_plan_cost(job["plan"], quality)
    store.update_video_job(
        source_id,
        status="queued",
        progress=0,
        quality=quality,
        estimated_cost_cny=cost,
        output_path="",
        error="",
    )
    task = asyncio.create_task(asyncio.to_thread(_run_video_job, source_id, quality))
    _VIDEO_BACKGROUND_TASKS.add(task)
    task.add_done_callback(_VIDEO_BACKGROUND_TASKS.discard)
    return JSONResponse(
        {
            "source_id": source_id,
            "status": "queued",
            "estimated_cost_cny": cost,
        },
        status_code=202,
    )


@router.get("/video/{source_id}")
async def get_content_video_job(source_id: str):
    """查看一条本机 AI 视频进度。"""
    job = content_store().get_video_job(source_id)
    if not job:
        return JSONResponse({"detail": "视频计划不存在"}, status_code=404)
    job["output_ready"] = bool(job.pop("output_path", ""))
    return job


@router.get("/video/{source_id}/file")
async def get_content_video_file(source_id: str):
    """仅在本机返回已生成成片。"""
    job = content_store().get_video_job(source_id)
    if not job or job.get("status") != "ready":
        return JSONResponse({"detail": "成片尚未生成"}, status_code=404)
    path = Path(job.get("output_path") or "")
    if not path.is_file() or VIDEO_OUTPUT_ROOT.resolve() not in path.resolve().parents:
        return JSONResponse({"detail": "成片文件不存在"}, status_code=404)
    return FileResponse(path, media_type="video/mp4", filename=f"{source_id}.mp4")


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
