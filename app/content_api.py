"""CreatorHub 本机成品索引 API。"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
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
    def validated_url(candidate: str) -> str:
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"https", "http"} or not host.endswith(
            (".aliyuncs.com", ".aliyun.com", ".volces.com")
        ):
            raise PipelineError("模型返回的文件地址无效")
        try:
            addresses = {
                info[4][0]
                for info in socket.getaddrinfo(host, parsed.port or 443)
            }
        except socket.gaierror as exc:
            raise PipelineError("模型结果域名无法解析") from exc
        if not addresses or any(
            not ipaddress.ip_address(address).is_global for address in addresses
        ):
            raise PipelineError("模型结果地址不是公网地址")
        if parsed.scheme == "http":
            parsed = parsed._replace(scheme="https")
        return urlunparse(parsed)

    url = validated_url(url)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".part")
    partial.unlink(missing_ok=True)
    received = 0
    client = httpx.Client(follow_redirects=False, timeout=120, trust_env=False)
    try:
        for _ in range(4):
            with client.stream("GET", url) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location", "")
                    url = validated_url(str(response.url.join(location)))
                    continue
                response.raise_for_status()
                with partial.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        received += len(chunk)
                        if received > max_bytes:
                            raise PipelineError("模型生成文件超过本机安全上限")
                        handle.write(chunk)
                break
        else:
            raise PipelineError("模型生成文件重定向过多")
    except PipelineError:
        partial.unlink(missing_ok=True)
        raise
    except (httpx.HTTPError, OSError) as exc:
        partial.unlink(missing_ok=True)
        raise PipelineError("模型生成文件下载失败") from exc
    finally:
        client.close()
    if received == 0 or not partial.is_file():
        partial.unlink(missing_ok=True)
        raise PipelineError("模型生成文件为空")
    header = partial.read_bytes()[:32]
    if path.suffix.lower() == ".mp4" and b"ftyp" not in header:
        partial.unlink(missing_ok=True)
        raise PipelineError("模型视频文件格式无效")
    if path.suffix.lower() == ".mp3" and not (
        header.startswith(b"ID3") or header[:1] == b"\xff"
    ):
        partial.unlink(missing_ok=True)
        raise PipelineError("模型配音文件格式无效")
    partial.replace(path)


def _plan_hash(plan: dict) -> str:
    """为分镜生成稳定版本号，隔离失败重试与新计划。"""
    return hashlib.sha256(
        json.dumps(plan, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:16]


def _synthesize_voice_locally(text: str, path: Path) -> None:
    """网关配音不可用时，用本机百炼 CLI 或系统语音生成旁白。"""
    narration = (text or "").strip()
    if not narration:
        raise PipelineError("旁白为空，无法生成本机配音")
    path.parent.mkdir(parents=True, exist_ok=True)
    bl = shutil.which("bl")
    if bl:
        try:
            subprocess.run(
                [
                    bl,
                    "speech",
                    "synthesize",
                    "--text",
                    narration,
                    "--out",
                    str(path),
                    "--format",
                    "mp3",
                    "--quiet",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=180,
            )
            if path.is_file() and path.stat().st_size > 0:
                return
        except (OSError, subprocess.SubprocessError) as exc:
            path.unlink(missing_ok=True)
            # 继续尝试系统语音
            _ = exc
    say = shutil.which("say")
    ffmpeg = _find_ffmpeg()
    if not say or not ffmpeg:
        raise PipelineError("本机配音兜底不可用，请配置百炼 CLI 或系统语音")
    aiff = path.with_suffix(".aiff")
    try:
        subprocess.run(
            [say, "-v", "Tingting", "-o", str(aiff), narration],
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-i",
                str(aiff),
                "-codec:a",
                "libmp3lame",
                "-qscale:a",
                "4",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        path.unlink(missing_ok=True)
        raise PipelineError("本机配音生成失败") from exc
    finally:
        aiff.unlink(missing_ok=True)
    if not path.is_file() or path.stat().st_size <= 0:
        raise PipelineError("本机配音文件为空")


def _write_json_atomic(path: Path, payload: dict) -> None:
    """原子写入小型任务清单。"""
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(path)


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
        if status in {"FAILED", "CANCELED"}:
            detail = result.get("error") or ""
            raise PipelineError(f"视频任务明确失败（{status}）：{detail}")
        if status == "UNKNOWN":
            raise PipelineError("视频任务状态未知，已停止自动重试")
        time.sleep(5)
    raise PipelineError("视频模型等待超过十分钟")


def _run_video_job(source_id: str, quality: str) -> None:
    """后台完成四段生成、配音下载和本地合成。"""
    store = content_store()
    job = store.get_video_job(source_id)
    if not job:
        return
    client = _ads_media_client()
    workspace = (
        VIDEO_OUTPUT_ROOT
        / source_id
        / "work"
        / quality
        / _plan_hash(job["plan"])
        / str(job["generation_id"])
    )
    try:
        paid_started = False
        workspace.mkdir(parents=True, exist_ok=True)
        store.update_video_job(
            source_id,
            status="generating",
            progress=2,
            quality=quality,
        )
        scene_paths = []
        scene_durations = [
            int(item.get("duration") or 0)
            for item in (job.get("plan") or {}).get("scenes") or []
        ]
        for index, scene in enumerate(job["plan"]["scenes"], start=1):
            scene_path = workspace / f"scene-{index}.mp4"
            task_path = workspace / f"scene-{index}.task.json"
            if scene_path.is_file():
                paid_started = True
            if not scene_path.is_file():
                task_id = ""
                if task_path.is_file():
                    try:
                        manifest = json.loads(task_path.read_text(encoding="utf-8"))
                        if manifest.get("generation_id") != job["generation_id"]:
                            raise PipelineError("视频场景幂等编号不一致")
                        if manifest.get("state") == "submitted":
                            task_id = str(manifest["task_id"])
                            paid_started = True
                    except (OSError, KeyError, TypeError, ValueError):
                        task_id = ""
                if not task_id:
                    if not task_path.exists():
                        try:
                            descriptor = os.open(
                                task_path,
                                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                0o600,
                            )
                        except FileExistsError as exc:
                            raise PipelineError(
                                "视频场景已被另一任务领取"
                            ) from exc
                        with os.fdopen(
                            descriptor,
                            "w",
                            encoding="utf-8",
                        ) as handle:
                            json.dump(
                                {
                                    "state": "submitting",
                                    "quote_id": job["quote_id"],
                                    "generation_id": job["generation_id"],
                                    "scene_index": index,
                                },
                                handle,
                            )
                    render_prompt = VideoPlanService.build_render_prompt(
                        job.get("plan") or {},
                        scene,
                    )
                    submitted = client.submit_scene(
                        render_prompt,
                        int(scene["duration"]),
                        quality,
                        str(job["quote_id"]),
                        str(job["generation_id"]),
                        index,
                        look_variant=str(
                            (job.get("plan") or {}).get("look_variant") or "no_hat"
                        ),
                        scene_durations=scene_durations,
                    )
                    paid_started = True
                    task_id = str(submitted["task_id"])
                    _write_json_atomic(
                        task_path,
                        {
                            "state": "submitted",
                            "task_id": task_id,
                            "quote_id": job["quote_id"],
                            "generation_id": job["generation_id"],
                            "scene_index": index,
                        },
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
            try:
                voice = client.synthesize_voice(narration)
                _download_generated_file(
                    str(voice["audio_url"]),
                    voice_path,
                    max_bytes=30 * 1024 * 1024,
                )
            except PipelineError:
                # 国际 Key 的 CosyVoice 仅支持 WebSocket；本机先用百炼 CLI 兜底。
                _synthesize_voice_locally(narration, voice_path)
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
        message = str(exc)
        status = (
            "uncertain"
            if (
                (
                    paid_started
                    and "视频任务明确失败" not in message
                )
                or "禁止重复扣费" in message
                or "提交结果确认中" in message
            )
            else "failed"
        )
        store.update_video_job(
            source_id,
            status=status,
            progress=0,
            error=message,
        )
    finally:
        client.close()


async def resume_incomplete_video_jobs() -> int:
    """应用启动时恢复任务；本地数据库暂时不可用时自动重试。"""
    jobs = None
    for attempt in range(3):
        try:
            jobs = await asyncio.to_thread(
                content_store().list_incomplete_video_jobs
            )
            break
        except (OSError, sqlite3.Error):
            if attempt == 2:
                raise
            await asyncio.sleep(1 + attempt)
    resumed = 0
    for job in jobs or []:
        task = asyncio.create_task(
            asyncio.to_thread(
                _run_video_job,
                str(job["source_id"]),
                str(job["quality"]),
            )
        )
        _VIDEO_BACKGROUND_TASKS.add(task)
        task.add_done_callback(_VIDEO_BACKGROUND_TASKS.discard)
        resumed += 1
    return resumed


@router.post("/video/{source_id}/quote")
async def quote_content_video(source_id: str, body: dict):
    """取得服务端当前模型报价，供用户确认后原样提交。"""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", source_id):
        return JSONResponse({"detail": "素材编号无效"}, status_code=400)
    quality = str(body.get("quality") or "preview")
    if quality not in {"preview", "quality"}:
        return JSONResponse({"detail": "未知视频质量档位"}, status_code=400)
    job = content_store().get_video_job(source_id)
    if not job:
        return JSONResponse({"detail": "视频计划不存在"}, status_code=404)
    client = _ads_media_client()
    scene_durations = [
        int(scene.get("duration") or 0)
        for scene in (job.get("plan") or {}).get("scenes") or []
    ]
    try:
        quote = await asyncio.to_thread(
            client.quote,
            quality,
            str(body.get("generation_id") or ""),
            scene_durations,
        )
    except PipelineError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=503)
    finally:
        client.close()
    return quote


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
    if job["status"] == "ready":
        return JSONResponse({"detail": "成片已完成，无需重复付费制作"}, status_code=409)
    client = _ads_media_client()
    scene_durations = [
        int(scene.get("duration") or 0)
        for scene in (job.get("plan") or {}).get("scenes") or []
    ]
    try:
        quote = await asyncio.to_thread(
            client.quote,
            quality,
            str(body.get("generation_id") or ""),
            scene_durations,
        )
    except PipelineError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=503)
    finally:
        client.close()
    if str(body.get("generation_id") or "") != str(quote.get("generation_id") or ""):
        return JSONResponse({"detail": "生成批次已变化，请重新确认费用"}, status_code=409)
    if str(body.get("quote_id") or "") != str(quote.get("quote_id") or ""):
        return JSONResponse({"detail": "视频报价已变化，请重新确认"}, status_code=409)
    if not store.claim_video_job(source_id, quality, quote):
        return JSONResponse({"detail": "视频正在生成，请勿重复提交"}, status_code=409)
    task = asyncio.create_task(asyncio.to_thread(_run_video_job, source_id, quality))
    _VIDEO_BACKGROUND_TASKS.add(task)
    task.add_done_callback(_VIDEO_BACKGROUND_TASKS.discard)
    return JSONResponse(
        {
            "source_id": source_id,
            "status": "queued",
            "estimated_cost_cny": quote["total_cost_cny"],
            "model": quote["model"],
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
