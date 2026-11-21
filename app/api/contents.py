"""监控作品库查询、媒体代理预览、本地文件定位与一键跨平台转发接口。"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, StrictInt
from sqlalchemy import func, or_
from sqlmodel import select

from app import runtime as app_runtime
from app.db import get_session
from app.models import ContentRecord, DouyinAccount, MonitorTarget
from app.platforms.xhs import has_creator_cookies
from app.services import contents as contents_service
from app.services import metadata as metadata_service
from app.services import share_downloads as share_downloads_service
from app.submissions import submit_once

router = APIRouter()


@router.get("/api/ig-media-thumb")
async def instagram_media_thumb_proxy(url: str):
    """本地只读代理 Instagram CDN 头像与封面，规避 Cross-Origin-Resource-Policy: same-origin 拦截并自动复用代理。"""
    import httpx
    from app.browser.manager import normalize_proxy

    raw = str(url or "").strip()
    if not raw or len(raw) > 4096:
        raise HTTPException(400, "无效的媒体地址")
    parsed = urlsplit(raw)
    host = (parsed.hostname or "").lower().strip(".")
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or not host
        or not (host.endswith(".cdninstagram.com") or host.endswith(".fbcdn.net"))
    ):
        raise HTTPException(400, "仅允许 Instagram 官方图片 CDN 域名")

    proxy_url = ""
    ua_str = app_runtime.cfg.engine.user_agent
    with get_session() as s:
        ig_acc = s.exec(
            select(DouyinAccount)
            .where(DouyinAccount.platform == "instagram")
            .order_by(DouyinAccount.id.desc())
        ).first()
        if ig_acc:
            if ig_acc.proxy:
                proxy_url = normalize_proxy(ig_acc.proxy)
            if ig_acc.ua:
                ua_str = ig_acc.ua
    if not proxy_url and app_runtime.cfg.engine.route_download_via_proxy and app_runtime.cfg.proxies:
        proxy_url = normalize_proxy(app_runtime.cfg.proxies[0])

    headers = {
        "User-Agent": ua_str,
        "Referer": "https://www.instagram.com/",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    try:
        async with httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=False,
            headers=headers,
            proxy=proxy_url or None,
        ) as http:
            async with http.stream("GET", raw) as resp:
                if resp.status_code != 200:
                    raise HTTPException(resp.status_code if 400 <= resp.status_code < 600 else 502, "上游图片不可用")
                ctype = (resp.headers.get("content-type") or "image/jpeg").split(";", 1)[0].strip().lower()
                if not ctype.startswith("image/"):
                    raise HTTPException(415, "上游响应不是图片")
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes(65536):
                    total += len(chunk)
                    if total > 8 * 1024 * 1024:
                        raise HTTPException(413, "图片体积超出预览上限")
                    chunks.append(chunk)
                return Response(
                    content=b"".join(chunks),
                    media_type=ctype,
                    headers={"Cache-Control": "private, max-age=3600"},
                )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Instagram 图片代理拉取失败: {type(exc).__name__}") from exc


@router.get("/api/tw-media-thumb")
async def twitter_media_thumb_proxy(url: str):
    """本地只读代理 X (Twitter) 官方图片 CDN (`pbs.twimg.com` / `abs.twimg.com`)，自动复用海外代理。"""
    import httpx
    from app.browser.manager import normalize_proxy

    raw = str(url or "").strip()
    if not raw or len(raw) > 4096:
        raise HTTPException(400, "无效的媒体地址")
    parsed = urlsplit(raw)
    host = (parsed.hostname or "").lower().strip(".")
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or not host
        or not (host == "twimg.com" or host.endswith(".twimg.com"))
    ):
        raise HTTPException(400, "仅允许 X (Twitter) 官方图片 CDN 域名(twimg.com)")

    proxy_url = ""
    ua_str = app_runtime.cfg.engine.user_agent
    with get_session() as s:
        tw_acc = s.exec(
            select(DouyinAccount)
            .where(DouyinAccount.platform == "twitter")
            .order_by(DouyinAccount.id.desc())
        ).first()
        if tw_acc:
            if tw_acc.proxy:
                proxy_url = normalize_proxy(tw_acc.proxy)
            if tw_acc.ua:
                ua_str = tw_acc.ua
    if not proxy_url and app_runtime.cfg.engine.route_download_via_proxy and app_runtime.cfg.proxies:
        proxy_url = normalize_proxy(app_runtime.cfg.proxies[0])

    headers = {
        "User-Agent": ua_str,
        "Referer": "https://x.com/",
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    try:
        async with httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=False,
            headers=headers,
            proxy=proxy_url or None,
        ) as http:
            async with http.stream("GET", raw) as resp:
                if resp.status_code != 200:
                    raise HTTPException(resp.status_code if 400 <= resp.status_code < 600 else 502, "上游图片不可用")
                ctype = (resp.headers.get("content-type") or "image/jpeg").split(";", 1)[0].strip().lower()
                if not ctype.startswith("image/"):
                    raise HTTPException(415, "上游响应不是图片")
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes(65536):
                    total += len(chunk)
                    if total > 8 * 1024 * 1024:
                        raise HTTPException(413, "图片体积超出预览上限")
                    chunks.append(chunk)
                return Response(
                    content=b"".join(chunks),
                    media_type=ctype,
                    headers={"Cache-Control": "private, max-age=3600"},
                )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, f"Twitter 图片代理拉取失败: {type(exc).__name__}") from exc


@router.get("/api/contents")
async def all_contents(limit: int = 100, platform: str | None = None,
                       target_id: int | None = None, group_name: str = "",
                       tag: str = "", q: str = "", media_type: str = "",
                       download_status: str = "", min_like_count: int = 0,
                       min_comment_count: int = 0, sort: str = "create_desc",
                       page: int = 1, page_size: int = 10,
                       paginate: bool = False,
                       captured_from: datetime | None = None,
                       captured_before: datetime | None = None):
    """Return monitored works, optionally as a filtered paginated result.

    The legacy list response remains the default for older callers.  The web
    UI opts into the object response with ``paginate=true`` so it can show a
    stable total while filters are applied in SQL rather than in the browser.
    """
    limit = max(1, min(limit, 1000))
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    start, end = contents_service._content_capture_bounds(captured_from, captured_before)
    with get_session() as s:
        selected_target = s.get(MonitorTarget, target_id) if target_id is not None else None
        selected_source = (contents_service._content_source(target_id, platform or (selected_target.platform if selected_target else ""), selected_target)
                           if target_id is not None else None)
        stmt = select(ContentRecord)
        if platform:
            stmt = stmt.where(ContentRecord.platform == platform)
        if target_id is not None:
            stmt = stmt.where(ContentRecord.target_id == target_id)
        stmt = metadata_service._report_window(stmt, ContentRecord, start, end)
        text_query = q.strip()
        if text_query:
            stmt = stmt.where(or_(ContentRecord.desc.contains(text_query),
                                  ContentRecord.aweme_id.contains(text_query)))
        if media_type in ("video", "images"):
            stmt = stmt.where(ContentRecord.media_type == media_type)
        if download_status:
            stmt = stmt.where(ContentRecord.download_status == download_status)
        if min_like_count > 0:
            stmt = stmt.where(ContentRecord.like_count >= min_like_count)
        if min_comment_count > 0:
            stmt = stmt.where(ContentRecord.comment_count >= min_comment_count)
        group_name, tag = metadata_service._meta_text(group_name, 40), metadata_service._meta_text(tag, 24)
        if group_name or tag:
            target_query = select(MonitorTarget)
            if platform:
                target_query = target_query.where(MonitorTarget.platform == platform)
            targets = s.exec(target_query).all()
            eligible_ids = [t.id for t in targets if t.id is not None
                            and metadata_service._meta_matches(t, group_name, tag)]
            if not eligible_ids:
                if not paginate:
                    return []
                return {"items": [], "total": 0, "page": page,
                        "page_size": page_size, "pages": 1,
                        "has_prev": page > 1, "has_next": False, "source": selected_source}
            stmt = stmt.where(ContentRecord.target_id.in_(eligible_ids))

        ordering = contents_service._content_ordering(sort)
        if not paginate:
            rows = s.exec(stmt.order_by(*ordering).limit(limit)).all()
            return contents_service._content_records(s, rows)

        total = int(s.exec(select(func.count()).select_from(stmt.subquery())).one())
        pages = max(1, (total + page_size - 1) // page_size)
        rows = s.exec(stmt.order_by(*ordering)
                      .offset((page - 1) * page_size)
                      .limit(page_size)).all()
        return {
            "items": contents_service._content_records(s, rows),
            "source": selected_source,
            "total": total, "page": page, "page_size": page_size,
            "pages": pages, "has_prev": page > 1, "has_next": page < pages,
        }


def _content_local_path(rec: ContentRecord) -> Path | None:
    if not rec.local_path:
        return None
    try:
        path = Path(rec.local_path).expanduser().resolve(strict=True)
        return path if path.is_file() or path.is_dir() else None
    except (OSError, RuntimeError):
        return None


def _content_local_media_path(rec: ContentRecord) -> Path | None:
    """Return the downloaded single-file media for a content record, if usable."""
    path = _content_local_path(rec)
    try:
        return path if path and path.is_file() and path.stat().st_size > 0 else None
    except OSError:
        return None


@router.get("/api/contents/{cid}/media")
async def content_media(cid: int):
    """返回一条作品/笔记的媒体直链列表,供前端预览(图集/视频)。"""
    with get_session() as s:
        rec = s.get(ContentRecord, cid)
        if not rec:
            raise HTTPException(404, "记录不存在")
        try:
            medias = json.loads(rec.media_json or "[]")
        except Exception:
            medias = []
        local_media = _content_local_media_path(rec)
        return {
            "id": rec.id, "platform": rec.platform, "desc": rec.desc,
            "media_type": rec.media_type, "cover_url": rec.cover_url,
            "local_path": rec.local_path, "medias": medias,
            "local_url": f"/api/contents/{rec.id}/local-media" if local_media else "",
        }


@router.api_route("/api/contents/{cid}/local-media", methods=["GET", "HEAD"])
async def content_local_media(cid: int):
    """Stream downloaded media from the recorded path, including HTTP Range support."""
    with get_session() as s:
        rec = s.get(ContentRecord, cid)
        if not rec:
            raise HTTPException(404, "记录不存在")
        path = _content_local_media_path(rec)
    if not path:
        raise HTTPException(404, "本地媒体不存在")
    return FileResponse(
        path,
        filename=path.name,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, no-cache"},
    )


@router.post("/api/contents/{cid}/reveal")
async def reveal_content_file(cid: int, request: Request):
    """在服务所在电脑的文件管理器中打开本地目录或定位文件。"""
    share_downloads_service._require_local_action(request)
    with get_session() as s:
        rec = s.get(ContentRecord, cid)
        if not rec:
            raise HTTPException(404, "记录不存在")
        path = _content_local_path(rec)
    if not path:
        raise HTTPException(404, "本地文件不存在")
    try:
        await asyncio.to_thread(contents_service._reveal_in_file_manager, path)
    except OSError as e:
        raise HTTPException(500, f"打开文件夹失败:{e}") from e
    return {"ok": True}


@router.post("/api/contents/{cid}/retry-download")
async def retry_download(cid: int):
    if not app_runtime.engine:
        raise HTTPException(503, "引擎未就绪")
    return await app_runtime.engine.retry_download(cid)


def _delete_content_files(rec: ContentRecord):
    """只删除该作品自己的文件(按 aweme_id 前缀),不动作者文件夹其它内容。"""
    if not rec.local_path:
        return 0
    p = Path(rec.local_path)
    folder = p if p.is_dir() else p.parent
    if not folder.exists():
        return 0
    n = 0
    for f in folder.glob(f"{rec.aweme_id}_*"):
        try:
            f.unlink(); n += 1
        except Exception:
            pass
    return n


@router.delete("/api/contents/{cid}")
async def del_content(cid: int, with_file: bool = True):
    removed = 0
    with get_session() as s:
        rec = s.get(ContentRecord, cid)
        if not rec:
            raise HTTPException(404, "记录不存在")
        if with_file:
            removed = _delete_content_files(rec)
        s.delete(rec); s.commit()
    return {"ok": True, "files_removed": removed}


@router.post("/api/contents/batch-delete")
async def batch_del_contents(body: contents_service.IdsIn):
    deleted = removed = 0
    with get_session() as s:
        for cid in body.ids:
            rec = s.get(ContentRecord, cid)
            if not rec:
                continue
            if body.with_file:
                removed += _delete_content_files(rec)
            s.delete(rec); deleted += 1
        s.commit()
    return {"ok": True, "deleted": deleted, "files_removed": removed}


class RepostIn(BaseModel):
    account_id: int
    scheduled_at: str | None = None
    # 转发前可编辑的笔记信息;为 None 时沿用作品原始内容
    title: str | None = None
    desc: str | None = None
    topics: str | None = None
    visibility: str = "public"           # 抖音:public | friends | private
    allow_save: bool = True              # 抖音:是否允许他人保存
    media_order: list[StrictInt] | None = None  # 剔除/调序后保留的图片原始序号(按新顺序);None=全部原序


async def _repost_content(cid: int, body: RepostIn, target_platform: str, request: Request = None):
    """把已下载作品转成目标平台(xhs / douyin / shipinhao / kuaishou / tiktok)的发布任务。"""
    if not app_runtime.engine:
        raise HTTPException(503, "引擎未就绪")
    with get_session() as s:
        def create():
            rec = s.get(ContentRecord, cid)
            if not rec:
                raise HTTPException(404, "作品不存在")
            if rec.download_status != "done":
                raise HTTPException(400, "该作品尚未下载完成,无法转发")
            acc = s.get(DouyinAccount, body.account_id)
            if not acc or acc.platform != target_platform:
                raise HTTPException(400, "请选择与目标平台匹配的已登录账号")
            if target_platform == "rednote":
                if not (acc.creator_storage_state or acc.storage_state):
                    raise HTTPException(400, "请先在账号页完成登录")
            elif target_platform in ("douyin", "shipinhao", "kuaishou", "tiktok", "bilibili", "instagram", "twitter"):
                if not (acc.creator_storage_state or acc.storage_state or acc.cookie):
                    raise HTTPException(400, "请先在账号页完成登录")
            elif not (acc.creator_storage_state or has_creator_cookies(acc.storage_state)):
                raise HTTPException(400, "请先完成小红书扫码登录或创作者登录")
            try:
                tid = app_runtime.engine.create_relay_publish(
                    cid, body.account_id, target_platform=target_platform,
                    title=body.title, desc=body.desc, topics=body.topics,
                    visibility=body.visibility if body.visibility in ("public", "friends", "private") else "public",
                    allow_save=bool(body.allow_save), media_order=body.media_order,
                    scheduled_at=metadata_service._parse_when(body.scheduled_at, acc.timezone_id), session=s)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            if not tid:
                raise HTTPException(400, "未找到该作品的本地文件,无法转发")
            return {"ok": True, "task_id": tid}
        payload, _ = submit_once(s, request=request, scope=f"repost:{target_platform}:{cid}",
                                 body=body, create=create)
        return payload


@router.post("/api/contents/{cid}/repost-xhs")
async def repost_to_xhs(cid: int, body: RepostIn, request: Request = None):
    """把一条已下载的抖音作品转成小红书发布任务。"""
    return await _repost_content(cid, body, "xhs", request)


@router.post("/api/contents/{cid}/repost-douyin")
async def repost_to_douyin(cid: int, body: RepostIn, request: Request = None):
    """把一条已下载的小红书作品转成抖音发布任务(反向转发)。"""
    return await _repost_content(cid, body, "douyin", request)


@router.post("/api/contents/{cid}/repost-shipinhao")
async def repost_to_channels(cid: int, body: RepostIn, request: Request = None):
    """把一条已下载的抖音作品转成视频号发布任务。"""
    return await _repost_content(cid, body, "shipinhao", request)


@router.post("/api/contents/{cid}/repost-kuaishou")
async def repost_to_kuaishou(cid: int, body: RepostIn, request: Request = None):
    """把一条已下载作品转成快手发布任务。"""
    return await _repost_content(cid, body, "kuaishou", request)


@router.post("/api/contents/{cid}/repost-tiktok")
async def repost_to_tiktok(cid: int, body: RepostIn, request: Request = None):
    """把一条已下载视频作品转成 TikTok 发布任务。"""
    return await _repost_content(cid, body, "tiktok", request)


@router.post("/api/contents/{cid}/repost-bilibili")
async def repost_to_bilibili(cid: int, body: RepostIn, request: Request = None):
    """把一条已下载作品转成哔哩哔哩发布任务。"""
    return await _repost_content(cid, body, "bilibili", request)


@router.post("/api/contents/{cid}/repost-rednote")
async def repost_to_rednote(cid: int, body: RepostIn, request: Request = None):
    """把一条已下载作品转成海外小红书（Rednote）发布任务。"""
    return await _repost_content(cid, body, "rednote", request)


@router.post("/api/contents/{cid}/repost-instagram")
async def repost_to_instagram(cid: int, body: RepostIn, request: Request = None):
    """把一条已下载作品转成 Instagram 发布任务。"""
    return await _repost_content(cid, body, "instagram", request)


@router.post("/api/contents/{cid}/repost-twitter")
async def repost_to_twitter(cid: int, body: RepostIn, request: Request = None):
    """把一条已下载作品转成 X (Twitter) 发布任务。"""
    return await _repost_content(cid, body, "twitter", request)
