"""多平台分享链接识别、无水印媒体下载及下载历史管理接口。"""
from __future__ import annotations

from pathlib import Path
import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlmodel import select

from app import runtime as app_runtime
from app.db import get_session
from app.engine.share_downloader import ShareDownloadError, ShareDownloadFallback, ShareDownloader, ShareLinkError, extract_share_urls, normalize_share_text, require_share_urls
from app.models import DouyinAccount, ShareDownloadRecord
from app.platforms.channels import inspect_or_download_channels_share
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


@router.post("/api/share-download")
async def share_download(body: ShareDownloadIn):
    """解析分享文案，并下载媒体/封面/字幕/元数据，或只读取作品信息。"""
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
                fallback_error = ""
                try:
                    item = None
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
                            # Only inconclusive metadata reads may fall back.
                            # Auth/risk denials and partial media downloads stop.
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
                            # 非视频号的 qq.com 链接(如 v.qq.com)直接交由通用提取器处理
                            item = None
                    # 9 大核心平台严禁把账号登录态 Cookie 交给 yt-dlp(Python OpenSSL TLS 栈)，避免同会话 TLS 指纹突变触发风控
                    safe_ytdlp_cookie = (
                        "" if link.platform in {
                            "douyin", "xhs", "rednote", "kuaishou", "wechat",
                            "shipinhao", "tiktok", "bilibili", "instagram", "twitter"
                        } else cookie_file
                    )
                    if item is not None:
                        pass
                    elif body.download:
                        item = await downloader.download(
                            link.url,
                            quality=body.quality,
                            save_metadata=body.save_metadata,
                            save_thumbnail=body.save_thumbnail,
                            save_subtitles=body.save_subtitles,
                            proxy=proxy,
                            cookie_file=safe_ytdlp_cookie,
                            max_filesize_mb=body.max_filesize_mb,
                        )
                    else:
                        item = await downloader.inspect(
                            link.url, proxy=proxy, cookie_file=safe_ytdlp_cookie
                        )
                    item["input_platform"] = link.platform
                    item_warnings = list(item.get("warnings") or [])
                    if proxy_override_warning and proxy_override_warning not in item_warnings:
                        item_warnings.append(proxy_override_warning)
                    if not proxy and link.platform in {"tiktok", "rednote", "instagram", "twitter"}:
                        direct_warn = "提醒：当前未配置代理（走本机直连），海外平台若遇到连接超时建议配置代理"
                        if direct_warn not in item_warnings:
                            item_warnings.append(direct_warn)
                    item["warnings"] = item_warnings
                    if fallback_error:
                        item.setdefault("warnings", []).append(
                            fallback_error + "；已改用通用提取器完成处理")
                except ShareDownloadError as exc:
                    item = {
                        "ok": False,
                        "url": link.url,
                        "input_platform": link.platform,
                        "error": ((fallback_error + "；通用提取器：") if fallback_error else "") + str(exc),
                    }
                    if getattr(exc, "diagnostics", None):
                        item["diagnostics"] = dict(exc.diagnostics)
                if body.download:
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
