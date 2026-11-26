"""多平台分享链接识别、无水印媒体下载及下载历史管理接口。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable
import asyncio
import json
import threading
import uuid

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlmodel import select

from app import runtime as app_runtime
from app.db import get_session
from app.engine.share_downloader import ShareDownloadError, ShareDownloadFallback, ShareDownloader, ShareLinkError, extract_share_urls, normalize_share_text, require_share_urls
from app.models import DouyinAccount, ShareDownloadRecord
from app.platforms.channels import inspect_or_download_channels_share
from app.progress_hub import progress_hub
from app.services import contents as contents_service
from app.services import share_downloads as share_downloads_service
from app.services import share_native as share_native_service
from app.services import share_native_social as share_native_social_service
from app.services import thumb_cache
from app.settings import get_setting

router = APIRouter()


# ─────────── 通用分享链接下载 ───────────
class ShareLinksIn(BaseModel):
    share_text: str
    limit: int = 10


class ShareDownloadIn(BaseModel):
    share_text: str
    download: bool = True             # False = 只请求远端并解析作品信息
    all_links: bool = False           # False = 只处理 link_index 指定的一条
    link_index: int = 0
    quality: str = "highest"
    output_dir: str | None = None
    save_metadata: bool = True
    save_thumbnail: bool = True
    save_subtitles: bool = False
    max_filesize_mb: int = 0          # 0 = 不限制
    account_id: int | None = None     # 可选：复用已登录账号 Cookie / UA / 代理
    proxy: str = ""                   # 显式填写时优先于账号代理


@router.post("/api/share-download/links")
async def parse_share_links(body: ShareLinksIn):
    """只做本地文本清洗和链接提取，不访问分享站点。"""
    text = share_downloads_service._share_input(body.share_text)
    limit = max(1, min(int(body.limit or 10), 20))
    normalized = normalize_share_text(text)
    links = extract_share_urls(normalized, limit=limit)
    return {
        "ok": bool(links),
        "normalized_text": normalized,
        "links": [item.to_dict() for item in links],
        "count": len(links),
    }


def _prepare_share_context(body: ShareDownloadIn):
    text = share_downloads_service._share_input(body.share_text)
    try:
        normalized = normalize_share_text(text)
        links = require_share_urls(normalized, limit=10)
    except ShareLinkError as exc:
        raise HTTPException(400, str(exc))

    if body.all_links:
        selected = links
    else:
        if body.link_index < 0 or body.link_index >= len(links):
            raise HTTPException(400, f"link_index 超出范围（共识别到 {len(links)} 条链接）")
        selected = [links[body.link_index]]
    if body.max_filesize_mb < 0 or body.max_filesize_mb > 1024 * 100:
        raise HTTPException(400, "max_filesize_mb 需为 0～102400")

    default_root = get_setting("download_dir", app_runtime.cfg.engine.media_dir) or app_runtime.cfg.engine.media_dir
    output_root = Path(body.output_dir.strip()).expanduser() if body.output_dir and body.output_dir.strip() \
        else Path(default_root).expanduser() / "share"
    try:
        output_root.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise HTTPException(400, f"下载目录不可用：{exc}")
    return normalized, links, selected, output_root


async def _process_one_share_link(
    link,
    *,
    body: ShareDownloadIn,
    output_root: Path,
    downloader: ShareDownloader,
    proxy: str,
    user_agent: str,
    cookie_file: str,
    proxy_override_warning: str,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    if cancel_event is not None and cancel_event.is_set():
        return {
            "ok": False,
            "canceled": True,
            "url": link.url,
            "input_platform": link.platform,
            "error": "下载已取消",
        }

    def report_stage(stage: str, label: str, **extra: Any) -> None:
        if on_progress is not None:
            try:
                on_progress({"status": stage, "phase_label": label, **extra})
            except Exception:
                pass

    fallback_error = ""
    try:
        item = None
        report_stage("resolving", "正在解析链接与作品信息")
        if link.platform == "douyin":
            try:
                item = await share_native_service._douyin_native_share(
                    link.url,
                    account_id=body.account_id,
                    output_root=output_root,
                    quality=body.quality,
                    should_download=body.download,
                    save_metadata=body.save_metadata,
                    save_thumbnail=body.save_thumbnail,
                    proxy=proxy,
                    user_agent=user_agent,
                )
            except ShareDownloadFallback as exc:
                fallback_error = str(exc)
        elif link.platform == "xhs":
            item = await share_native_social_service._xhs_native_share(
                link.url,
                account_id=body.account_id,
                output_root=output_root,
                quality=body.quality,
                should_download=body.download,
                save_metadata=body.save_metadata,
                save_thumbnail=body.save_thumbnail,
                proxy=proxy,
            )
        elif link.platform == "rednote":
            item = await share_native_social_service._rednote_native_share(
                link.url,
                account_id=body.account_id,
                output_root=output_root,
                quality=body.quality,
                should_download=body.download,
                save_metadata=body.save_metadata,
                save_thumbnail=body.save_thumbnail,
                proxy=proxy,
            )
        elif link.platform == "instagram":
            try:
                item = await share_native_social_service._instagram_native_share(
                    link.url,
                    account_id=body.account_id,
                    output_root=output_root,
                    quality=body.quality,
                    should_download=body.download,
                    save_metadata=body.save_metadata,
                    save_thumbnail=body.save_thumbnail,
                    proxy=proxy,
                    user_agent=user_agent,
                )
            except ShareDownloadFallback as exc:
                fallback_error = str(exc)
        elif link.platform == "twitter":
            try:
                item = await share_native_social_service._twitter_native_share(
                    link.url,
                    account_id=body.account_id,
                    output_root=output_root,
                    quality=body.quality,
                    should_download=body.download,
                    save_metadata=body.save_metadata,
                    save_thumbnail=body.save_thumbnail,
                    proxy=proxy,
                    user_agent=user_agent,
                )
            except ShareDownloadFallback as exc:
                fallback_error = str(exc)
        elif link.platform == "facebook":
            try:
                item = await share_native_social_service._facebook_native_share(
                    link.url,
                    account_id=body.account_id,
                    output_root=output_root,
                    quality=body.quality,
                    should_download=body.download,
                    save_metadata=body.save_metadata,
                    save_thumbnail=body.save_thumbnail,
                    proxy=proxy,
                    user_agent=user_agent,
                )
            except ShareDownloadFallback as exc:
                fallback_error = str(exc)
        elif link.platform == "youtube":
            try:
                item = await share_native_social_service._youtube_native_share(
                    link.url,
                    account_id=body.account_id,
                    output_root=output_root,
                    quality=body.quality,
                    should_download=body.download,
                    save_metadata=body.save_metadata,
                    save_thumbnail=body.save_thumbnail,
                    proxy=proxy,
                    user_agent=user_agent,
                )
            except ShareDownloadFallback as exc:
                fallback_error = str(exc)
        elif link.platform == "wechat":
            account_state = ""
            if body.account_id:
                with get_session() as s_acc:
                    acc = s_acc.get(DouyinAccount, body.account_id)
                    if acc:
                        account_state = (
                            acc.storage_state
                            or acc.creator_storage_state
                            or ""
                        )
            try:
                item = await inspect_or_download_channels_share(
                    link.url,
                    cfg=app_runtime.cfg,
                    output_root=output_root,
                    quality=body.quality,
                    should_download=body.download,
                    save_metadata=body.save_metadata,
                    save_thumbnail=body.save_thumbnail,
                    proxy=proxy,
                    user_agent=user_agent,
                    account_storage_state=account_state,
                )
            except ShareDownloadFallback:
                item = None
        safe_ytdlp_cookie = (
            "" if link.platform in {
                "douyin", "xhs", "rednote", "kuaishou", "wechat",
                "shipinhao", "tiktok", "bilibili", "instagram", "twitter", "facebook", "youtube"
            } else cookie_file
        )
        if item is not None:
            pass
        elif body.download:
            report_stage("downloading", "正在下载媒体文件")
            item = await downloader.download(
                link.url,
                quality=body.quality,
                save_metadata=body.save_metadata,
                save_thumbnail=body.save_thumbnail,
                save_subtitles=body.save_subtitles,
                proxy=proxy,
                cookie_file=safe_ytdlp_cookie,
                max_filesize_mb=body.max_filesize_mb,
                on_progress=on_progress,
                cancel_event=cancel_event,
            )
        else:
            item = await downloader.inspect(
                link.url, proxy=proxy, cookie_file=safe_ytdlp_cookie
            )
        item["input_platform"] = link.platform
        item_warnings = list(item.get("warnings") or [])
        if proxy_override_warning and proxy_override_warning not in item_warnings:
            item_warnings.append(proxy_override_warning)
        if not proxy and link.platform in {"tiktok", "rednote", "instagram", "twitter", "facebook", "youtube"}:
            direct_warn = "提醒：当前未配置代理（走本机直连），海外平台若遇到连接超时建议配置代理"
            if direct_warn not in item_warnings:
                item_warnings.append(direct_warn)
        item["warnings"] = item_warnings
        if fallback_error:
            item.setdefault("warnings", []).append(
                fallback_error + "；已改用通用提取器完成处理")
    except asyncio.CancelledError:
        return {
            "ok": False,
            "canceled": True,
            "url": link.url,
            "input_platform": link.platform,
            "error": "下载已取消",
        }
    except ShareDownloadError as exc:
        item = {
            "ok": False,
            "canceled": "下载已取消" in str(exc),
            "url": link.url,
            "input_platform": link.platform,
            "error": ((fallback_error + "；通用提取器：") if fallback_error else "") + str(exc),
        }
        if getattr(exc, "diagnostics", None):
            item["diagnostics"] = dict(exc.diagnostics)
    except Exception as exc:
        item = {
            "ok": False,
            "url": link.url,
            "input_platform": link.platform,
            "error": str(exc) or repr(exc),
        }
    if body.download and not item.get("canceled"):
        try:
            item["history_id"] = share_downloads_service._save_share_download_history(
                source_url=link.url,
                platform=link.platform,
                account_id=body.account_id,
                item=item,
            )
        except Exception as exc:
            item.setdefault("warnings", []).append(
                f"下载已处理，但历史记录写入失败：{exc}"
            )
    return item


@router.post("/api/share-download")
async def share_download(body: ShareDownloadIn):
    """解析分享文案，并下载媒体/封面/字幕/元数据，或只读取作品信息（同步兼容接口）。"""
    normalized, links, selected, output_root = _prepare_share_context(body)

    cookie_file = ""
    try:
        from app.browser.manager import normalize_proxy
        cookie_file, account_proxy, account_ua = share_downloads_service._write_account_cookie_file(body.account_id)
        req_proxy = body.proxy.strip()
        proxy_override_warning = ""
        if body.account_id is not None and req_proxy and normalize_proxy(req_proxy) != normalize_proxy(account_proxy):
            proxy_override_warning = "提醒：本次下载使用了与所选账号绑定环境不同的临时代理出口，请注意跨 IP 登录态关联风险"
            proxy = req_proxy
        else:
            proxy = account_proxy if body.account_id is not None else req_proxy
        if not proxy:
            with get_session() as s_pool:
                from app.profiles import _pool_urls
                pool_candidates = _pool_urls(s_pool, app_runtime.cfg)
                if pool_candidates and body.account_id is None:
                    proxy = pool_candidates[0]
        user_agent = account_ua or app_runtime.cfg.engine.user_agent
        downloader = ShareDownloader(
            output_root,
            user_agent=user_agent,
            timeout=app_runtime.cfg.engine.request_timeout_seconds,
        )
        results = []
        async with app_runtime._share_download_sem:
            for link in selected:
                item = await _process_one_share_link(
                    link,
                    body=body,
                    output_root=output_root,
                    downloader=downloader,
                    proxy=proxy,
                    user_agent=user_agent,
                    cookie_file=cookie_file,
                    proxy_override_warning=proxy_override_warning,
                )
                results.append(item)
    finally:
        if cookie_file:
            try:
                Path(cookie_file).unlink(missing_ok=True)
            except OSError:
                pass

    return {
        "ok": bool(results) and all(item.get("ok") for item in results),
        "normalized_text": normalized,
        "links": [item.to_dict() for item in links],
        "results": results,
    }


def _sync_share_job_to_hub(job: dict[str, Any], *, immediate: bool = False) -> None:
    items = job.get("items") or []
    total_links = max(1, len(items))
    done_count = sum(1 for it in items if it.get("status") in {"completed", "failed", "canceled"})
    ok_count = sum(1 for it in items if it.get("status") == "completed")
    fail_count = sum(1 for it in items if it.get("status") == "failed")
    total_speed = sum(float(it.get("speed_bps") or 0) for it in items if it.get("status") == "running")
    active_etas = [int(it["eta_seconds"]) for it in items if it.get("status") == "running" and it.get("eta_seconds") is not None]
    eta = max(active_etas) if active_etas else None

    # 聚合进度：单链接直接映射真实字节进度；多链接按每条链接权重平均。
    if total_links == 1:
        only = items[0]
        determinate = bool(only.get("determinate")) or only.get("status") == "completed"
        percent = 100.0 if only.get("status") == "completed" else only.get("percent")
        current = only.get("downloaded_bytes") or 0
        total = only.get("total_bytes") or 0
        unit = "bytes" if total > 0 or current > 0 else ""
        phase_label = str(only.get("phase_label") or job.get("phase_label") or "处理中")
        title = str(only.get("title") or only.get("host") or only.get("url") or "链接下载")
        detail = str(only.get("host") or "")
    else:
        frac = 0.0
        has_any_pct = done_count > 0
        for it in items:
            st = it.get("status")
            if st in {"completed", "failed", "canceled"}:
                frac += 1.0
            elif it.get("percent") is not None:
                frac += max(0.0, min(100.0, float(it["percent"]))) / 100.0
                has_any_pct = True
        determinate = has_any_pct or job.get("status") == "completed"
        percent = round(min(100.0, (frac * 100.0) / total_links), 1) if determinate else None
        current = done_count
        total = total_links
        unit = "items"
        running_items = [it for it in items if it.get("status") == "running"]
        phase_label = (
            f"并发处理 {len(running_items)} 条 · 已完成 {done_count}/{total_links}"
            if job.get("status") == "running"
            else str(job.get("phase_label") or "处理完成")
        )
        title = f"批量链接下载（{total_links} 条）"
        detail = f"成功 {ok_count}" + (f" · 失败 {fail_count}" if fail_count else "")

    public_items = [
        {
            "index": it["index"],
            "url": it["url"],
            "platform": it["platform"],
            "host": it["host"],
            "title": it.get("title") or "",
            "status": it.get("status") or "queued",
            "phase_label": it.get("phase_label") or "排队中",
            "determinate": bool(it.get("determinate")),
            "percent": it.get("percent"),
            "downloaded_bytes": int(it.get("downloaded_bytes") or 0),
            "total_bytes": int(it.get("total_bytes") or 0),
            "speed_bps": it.get("speed_bps"),
            "eta_seconds": it.get("eta_seconds"),
            "error": it.get("error") or "",
            "history_id": it.get("history_id"),
        }
        for it in items
    ]
    progress_hub.upsert(
        f"share:{job['id']}",
        immediate=immediate,
        category="share_download",
        title=title[:140],
        platform=str(items[0]["platform"] if total_links == 1 else ""),
        account_id=job.get("account_id"),
        source_tab="share-download",
        status=str(job.get("status") or "running"),
        phase=str(job.get("phase") or "running"),
        phase_label=phase_label,
        detail=detail,
        error=str(job.get("error") or ""),
        determinate=determinate,
        percent=percent,
        current=current,
        total=total,
        unit=unit,
        speed_bps=total_speed if total_speed > 0 else None,
        eta_seconds=eta,
        metrics={"total": total_links, "done": done_count, "ok": ok_count, "failed": fail_count},
        cancelable=job.get("status") in {"queued", "running"},
        extra={
            "job_id": job["id"],
            "download": bool(job.get("download", True)),
            "items": public_items,
            "results": job.get("results") if job.get("status") in {"completed", "failed", "canceled"} else None,
        },
    )


async def _run_share_download_job(
    job_id: str,
    body: ShareDownloadIn,
    selected: list,
    output_root: Path,
    cancel_event: threading.Event,
) -> None:
    job = app_runtime._share_download_jobs[job_id]
    cookie_file = ""
    try:
        from app.browser.manager import normalize_proxy
        cookie_file, account_proxy, account_ua = share_downloads_service._write_account_cookie_file(body.account_id)
        req_proxy = body.proxy.strip()
        proxy_override_warning = ""
        if body.account_id is not None and req_proxy and normalize_proxy(req_proxy) != normalize_proxy(account_proxy):
            proxy_override_warning = "提醒：本次下载使用了与所选账号绑定环境不同的临时代理出口，请注意跨 IP 登录态关联风险"
            proxy = req_proxy
        else:
            proxy = account_proxy if body.account_id is not None else req_proxy
        if not proxy:
            with get_session() as s_pool:
                from app.profiles import _pool_urls
                pool_candidates = _pool_urls(s_pool, app_runtime.cfg)
                if pool_candidates and body.account_id is None:
                    proxy = pool_candidates[0]
        user_agent = account_ua or app_runtime.cfg.engine.user_agent
        downloader = ShareDownloader(
            output_root,
            user_agent=user_agent,
            timeout=app_runtime.cfg.engine.request_timeout_seconds,
        )
        job["status"] = "running"
        job["phase"] = "running"
        job["phase_label"] = "正在处理链接"
        _sync_share_job_to_hub(job, immediate=True)

        link_sem = asyncio.Semaphore(min(3, max(1, len(selected))))

        async def run_one(idx: int, link):
            entry = job["items"][idx]
            if cancel_event.is_set():
                entry["status"] = "canceled"
                entry["phase_label"] = "已取消"
                _sync_share_job_to_hub(job)
                return {
                    "ok": False, "canceled": True, "url": link.url,
                    "input_platform": link.platform, "error": "下载已取消",
                }
            async with link_sem:
                async with app_runtime._share_download_sem:
                    if cancel_event.is_set():
                        entry["status"] = "canceled"
                        entry["phase_label"] = "已取消"
                        _sync_share_job_to_hub(job)
                        return {
                            "ok": False, "canceled": True, "url": link.url,
                            "input_platform": link.platform, "error": "下载已取消",
                        }
                    entry["status"] = "running"
                    entry["phase_label"] = "正在解析作品信息"
                    _sync_share_job_to_hub(job, immediate=True)

                    def on_item_progress(data: dict[str, Any]) -> None:
                        st = str(data.get("status") or "")
                        if data.get("title"):
                            entry["title"] = str(data["title"])[:160]
                        dl = data.get("downloaded_bytes")
                        tot = data.get("total_bytes") or data.get("total_bytes_estimate")
                        if dl is not None:
                            entry["downloaded_bytes"] = int(dl)
                        if tot is not None and float(tot) > 0:
                            entry["total_bytes"] = int(tot)
                            entry["determinate"] = True
                            entry["percent"] = round(min(99.5, (float(dl or 0) * 100.0) / float(tot)), 1)
                        if data.get("speed") is not None:
                            entry["speed_bps"] = float(data["speed"])
                        if data.get("eta") is not None:
                            entry["eta_seconds"] = int(data["eta"])
                        if st == "postprocessing":
                            entry["phase_label"] = "正在合并/封装音视频"
                            entry["speed_bps"] = None
                            entry["eta_seconds"] = None
                        elif st == "finished":
                            entry["phase_label"] = "下载完成，正在收尾"
                            entry["percent"] = 99.0
                            entry["determinate"] = True
                        elif st == "downloading":
                            entry["phase_label"] = "正在下载媒体"
                        elif data.get("phase_label"):
                            entry["phase_label"] = str(data["phase_label"])
                        _sync_share_job_to_hub(job)

                    result = await _process_one_share_link(
                        link,
                        body=body,
                        output_root=output_root,
                        downloader=downloader,
                        proxy=proxy,
                        user_agent=user_agent,
                        cookie_file=cookie_file,
                        proxy_override_warning=proxy_override_warning,
                        on_progress=on_item_progress,
                        cancel_event=cancel_event,
                    )
                    meta = result.get("metadata") or {}
                    if meta.get("title") and not entry.get("title"):
                        entry["title"] = str(meta["title"])[:160]
                    entry["history_id"] = result.get("history_id")
                    entry["speed_bps"] = None
                    entry["eta_seconds"] = None
                    if result.get("canceled"):
                        entry["status"] = "canceled"
                        entry["phase_label"] = "已取消"
                        entry["error"] = ""
                    elif result.get("ok"):
                        entry["status"] = "completed"
                        entry["phase_label"] = "下载完成" if body.download else "读取完成"
                        entry["determinate"] = True
                        entry["percent"] = 100.0
                        entry["error"] = ""
                    else:
                        entry["status"] = "failed"
                        entry["phase_label"] = "下载失败"
                        entry["error"] = str(result.get("error") or "未知错误")[:500]
                    _sync_share_job_to_hub(job, immediate=True)
                    return result

        results = await asyncio.gather(*(run_one(i, link) for i, link in enumerate(selected)))
        job["results"] = list(results)
        job["completed_at"] = datetime.utcnow().isoformat() + "Z"
        if cancel_event.is_set() and not any(r.get("ok") for r in results):
            job["status"] = "canceled"
            job["phase"] = "canceled"
            job["phase_label"] = "已取消"
        elif all(r.get("ok") for r in results):
            job["status"] = "completed"
            job["phase"] = "completed"
            job["phase_label"] = "下载完成" if body.download else "读取完成"
        elif any(r.get("ok") for r in results):
            job["status"] = "completed"
            job["phase"] = "partial"
            ok_n = sum(1 for r in results if r.get("ok"))
            job["phase_label"] = f"部分完成（{ok_n}/{len(results)} 成功）"
            first_err = next((r.get("error") for r in results if not r.get("ok")), "")
            job["error"] = str(first_err or "")[:400]
        else:
            job["status"] = "failed"
            job["phase"] = "failed"
            job["phase_label"] = "下载失败"
            job["error"] = str((results[0].get("error") if results else "") or "未知错误")[:500]
        _sync_share_job_to_hub(job, immediate=True)
    except asyncio.CancelledError:
        cancel_event.set()
        job["status"] = "canceled"
        job["phase"] = "canceled"
        job["phase_label"] = "已取消"
        job["completed_at"] = datetime.utcnow().isoformat() + "Z"
        for entry in job.get("items") or []:
            if entry.get("status") in {"queued", "running"}:
                entry["status"] = "canceled"
                entry["phase_label"] = "已取消"
        _sync_share_job_to_hub(job, immediate=True)
        raise
    except Exception as exc:
        job["status"] = "failed"
        job["phase"] = "failed"
        job["phase_label"] = "下载异常"
        job["error"] = str(exc) or repr(exc)
        job["completed_at"] = datetime.utcnow().isoformat() + "Z"
        _sync_share_job_to_hub(job, immediate=True)
    finally:
        if cookie_file:
            try:
                Path(cookie_file).unlink(missing_ok=True)
            except OSError:
                pass


@router.post("/api/share-download/jobs", status_code=202)
async def create_share_download_job(body: ShareDownloadIn):
    """创建后台异步链接下载任务，立即返回 job_id，通过 /api/progress/stream 实时推送字节/速率/阶段进度。"""
    normalized, links, selected, output_root = _prepare_share_context(body)
    finished_keys = [
        key for key, value in app_runtime._share_download_jobs.items()
        if value.get("status") in {"completed", "failed", "canceled"}
    ]
    for old_key in finished_keys[:-50]:
        app_runtime._share_download_jobs.pop(old_key, None)
        app_runtime._share_download_tasks.pop(old_key, None)

    job_id = uuid.uuid4().hex[:12]
    cancel_event = threading.Event()
    job: dict[str, Any] = {
        "id": job_id,
        "download": bool(body.download),
        "account_id": body.account_id,
        "status": "queued",
        "phase": "queued",
        "phase_label": "排队中",
        "error": "",
        "normalized_text": normalized,
        "links": [item.to_dict() for item in links],
        "items": [
            {
                "index": idx,
                "url": link.url,
                "platform": link.platform,
                "host": link.host,
                "title": "",
                "status": "queued",
                "phase_label": "等待下载",
                "determinate": False,
                "percent": None,
                "downloaded_bytes": 0,
                "total_bytes": 0,
                "speed_bps": None,
                "eta_seconds": None,
                "error": "",
                "history_id": None,
            }
            for idx, link in enumerate(selected)
        ],
        "results": [],
        "created_at": datetime.utcnow().isoformat() + "Z",
        "completed_at": None,
        "_cancel_event": cancel_event,
    }
    app_runtime._share_download_jobs[job_id] = job
    _sync_share_job_to_hub(job, immediate=True)

    def _cancel_job():
        cancel_event.set()
        t = app_runtime._share_download_tasks.get(job_id)
        if t is not None and not t.done():
            t.cancel()
        return {"ok": True, "job_id": job_id}

    progress_hub.register_cancel(f"share:{job_id}", _cancel_job)
    task = asyncio.create_task(
        _run_share_download_job(job_id, body, selected, output_root, cancel_event)
    )
    app_runtime._share_download_tasks[job_id] = task
    task.add_done_callback(lambda _t, k=job_id: app_runtime._share_download_tasks.pop(k, None))
    return {k: v for k, v in job.items() if not k.startswith("_")}


@router.get("/api/share-download/jobs")
async def list_share_download_jobs():
    jobs = [
        {k: v for k, v in job.items() if not k.startswith("_")}
        for job in reversed(list(app_runtime._share_download_jobs.values()))
    ]
    return {"jobs": jobs[:30]}


@router.get("/api/share-download/jobs/{job_id}")
async def get_share_download_job(job_id: str):
    job = app_runtime._share_download_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "下载任务不存在")
    return {k: v for k, v in job.items() if not k.startswith("_")}


@router.post("/api/share-download/jobs/{job_id}/cancel")
async def cancel_share_download_job(job_id: str):
    job = app_runtime._share_download_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "下载任务不存在")
    ev = job.get("_cancel_event")
    if isinstance(ev, threading.Event):
        ev.set()
    task = app_runtime._share_download_tasks.get(job_id)
    if task is not None and not task.done():
        task.cancel()
    return {"ok": True, "job_id": job_id}


@router.get("/api/share-download/history")
async def get_share_download_history(limit: int = 100, platform: str = ""):
    limit = max(1, min(int(limit or 100), 500))
    with get_session() as s:
        query = select(ShareDownloadRecord)
        if platform.strip():
            query = query.where(ShareDownloadRecord.platform == platform.strip())
        rows = s.exec(
            query.order_by(ShareDownloadRecord.created_at.desc()).limit(limit)
        ).all()
        return [share_downloads_service._share_history_dict(row) for row in rows]


class ShareHistoryBatchDeleteIn(BaseModel):
    ids: list[int]


@router.post("/api/share-download/history/batch-delete")
async def delete_share_download_history_batch(body: ShareHistoryBatchDeleteIn):
    """批量删除链接下载历史记录，不清理本地媒体文件。"""
    ids = {int(value) for value in (body.ids or []) if int(value) > 0}
    if len(ids) > 200:
        raise HTTPException(400, "单次最多删除 200 条历史记录")
    if not ids:
        return {"ok": True, "deleted": 0}
    with get_session() as s:
        rows = s.exec(
            select(ShareDownloadRecord).where(ShareDownloadRecord.id.in_(ids))
        ).all()
        for record in rows:
            s.delete(record)
        s.commit()
    return {"ok": True, "deleted": len(rows)}


def _share_history_files(record: ShareDownloadRecord) -> list[dict]:
    try:
        files = json.loads(record.files_json or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        files = []
    return [item for item in files if isinstance(item, dict)] if isinstance(files, list) else []


def _share_history_local_path(
    record: ShareDownloadRecord,
    media_index: int | None = None,
) -> Path | None:
    files = _share_history_files(record)
    candidates = [item for item in files if item.get("role") == "media"]
    if media_index is not None:
        if media_index < 0 or media_index >= len(candidates):
            return None
        candidates = [candidates[media_index]]
    if not candidates:
        candidates = files
    for item in candidates:
        raw = str(item.get("path") or "").strip()
        if not raw:
            continue
        try:
            path = Path(raw).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if path.is_file() or path.is_dir():
            return path
    if media_index is None and record.output_dir:
        try:
            path = Path(record.output_dir).expanduser().resolve(strict=True)
            if path.is_dir():
                return path
        except (OSError, RuntimeError):
            pass
    return None


@router.api_route("/api/share-download/history/{record_id}/cover", methods=["GET", "HEAD"])
async def share_download_history_cover(record_id: int):
    """优先返回链接下载历史已落盘的本地封面/图集首图；无本地缩略图时自动持久化缓存。"""
    with get_session() as s:
        record = s.get(ShareDownloadRecord, record_id)
        if not record:
            raise HTTPException(404, "下载历史不存在")
        local_cover = share_downloads_service._find_share_local_cover(record)
        raw_cover = record.cover_url
        platform = record.platform
    if local_cover is not None:
        return thumb_cache.file_thumb_response(local_cover)
    if raw_cover:
        return await thumb_cache.serve_cached_thumb(raw_cover, platform=platform)
    raise HTTPException(404, "封面不存在")


@router.api_route("/api/share-download/history/{record_id}/media/{media_index}", methods=["GET", "HEAD"])
async def share_download_history_media(record_id: int, media_index: int):
    """返回链接下载历史中的本地媒体，供作品预览复用。"""
    with get_session() as s:
        record = s.get(ShareDownloadRecord, record_id)
        if not record:
            raise HTTPException(404, "下载历史不存在")
        path = _share_history_local_path(record, media_index)
    if not path or not path.is_file():
        raise HTTPException(404, "本地媒体不存在")
    return FileResponse(
        path,
        filename=path.name,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@router.get("/api/share-download/history/{record_id}/preview")
async def share_download_history_preview(record_id: int):
    """返回链接下载历史的本地媒体地址，供前端复用作品预览弹窗。"""
    with get_session() as s:
        record = s.get(ShareDownloadRecord, record_id)
        if not record:
            raise HTTPException(404, "下载历史不存在")
        files = _share_history_files(record)
        media_files = [item for item in files if item.get("role") == "media"]
        medias = []
        for index, item in enumerate(media_files):
            raw = str(item.get("path") or "").strip()
            if not raw:
                continue
            try:
                path = Path(raw).expanduser().resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if not path.is_file():
                continue
            kind = "image" if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".avif"} else "video"
            medias.append({
                "kind": kind,
                "url": f"/api/share-download/history/{record_id}/media/{index}",
            })
        media_type = record.media_type or ("images" if medias and medias[0]["kind"] == "image" else "video")
        video = next((item for item in medias if item["kind"] == "video"), None)
        has_cover = bool(share_downloads_service._find_share_local_cover(record) or record.cover_url)
        return {
            "id": record.id,
            "desc": record.title or record.item_id,
            "media_type": media_type,
            "cover_url": f"/api/share-download/history/{record_id}/cover" if has_cover else "",
            "medias": medias,
            "local_url": video["url"] if video else "",
        }


@router.post("/api/share-download/history/{record_id}/reveal")
async def reveal_share_download_history(record_id: int, request: Request):
    """在服务所在电脑的文件管理器中打开链接下载目录或定位文件。"""
    share_downloads_service._require_local_action(request)
    with get_session() as s:
        record = s.get(ShareDownloadRecord, record_id)
        if not record:
            raise HTTPException(404, "下载历史不存在")
        path = _share_history_local_path(record)
    if not path:
        raise HTTPException(404, "本地文件不存在")
    try:
        await asyncio.to_thread(contents_service._reveal_in_file_manager, path)
    except OSError as e:
        raise HTTPException(500, f"打开文件夹失败:{e}") from e
    return {"ok": True}


@router.delete("/api/share-download/history/{record_id}")
async def delete_share_download_history(record_id: int):
    """只删除历史行，不删除磁盘里的媒体文件。"""
    with get_session() as s:
        record = s.get(ShareDownloadRecord, record_id)
        if not record:
            raise HTTPException(404, "下载历史不存在")
        s.delete(record)
        s.commit()
    return {"ok": True}
