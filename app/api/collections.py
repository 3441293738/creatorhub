"""关键词与创作者作品采集任务创建、进度查询、媒体预览及报表导出接口。"""
from __future__ import annotations

from datetime import datetime
import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import func
from sqlmodel import select

from app import runtime as app_runtime
from app.db import get_session
from app.models import DouyinAccount, KeywordCollectionComment, KeywordCollectionContent, KeywordCollectionJob
from app.operations import record_account_operation
from app.services import collections as collections_service
from app.services import contents as contents_service
from app.services import metadata as metadata_service
from app.services import share_downloads as share_downloads_service
from app.submissions import submit_once

router = APIRouter()


def _collection_requires_account(platform: str, source_type: str) -> bool:
    """视频号关键词/外部创作者采集走本机微信通道，无需网页创作者助手登录态。"""
    return not (platform == "shipinhao" and source_type in {"keyword", "creator"})


def _resolve_collection_account_id(
    session, *, platform: str, source_type: str, requested_id: int | None
) -> int:
    acc_id = int(requested_id or 0)
    account = session.get(DouyinAccount, acc_id) if acc_id else None
    if _collection_requires_account(platform, source_type):
        if (not account or account.platform != platform
                or account.status != "active" or not account.storage_state):
            raise HTTPException(400, "所选账号不存在、登录态失效或与平台不匹配")
        return account.id
    if account is not None and account.platform != platform:
        raise HTTPException(400, "所选账号与当前采集平台不匹配")
    if account is not None and account.status == "active" and account.storage_state:
        return account.id
    return 0


@router.post("/api/collections")
async def create_keyword_collection(body: collections_service.KeywordCollectionIn, request: Request = None):
    await collections_service._resolve_ks_creator_source(body)
    await collections_service._resolve_channels_creator_source(body)
    await collections_service._resolve_bili_creator_source(body)
    await collections_service._resolve_rednote_creator_source(body)
    await collections_service._resolve_ig_creator_source(body)
    await collections_service._resolve_twitter_creator_source(body)
    with get_session() as session:
        def create():
            platform, keywords, quality, download_dir, options = collections_service._validated_collection_input(body)
            account_id = _resolve_collection_account_id(
                session,
                platform=platform,
                source_type=options["source_type"],
                requested_id=body.account_id,
            )
            job = KeywordCollectionJob(
                platform=platform, account_id=account_id,
                keywords=json.dumps(keywords, ensure_ascii=False),
                max_contents_per_keyword=body.max_contents_per_keyword,
                **options,
                max_comments_per_content=body.max_comments_per_content,
                include_replies=body.include_replies,
                download_media=body.download_media,
                video_quality=quality, download_dir=download_dir,
            )
            session.add(job); session.flush()
            return collections_service._collection_job_dict(job)
        payload, created = submit_once(session, request=request, scope="collection", body=body, create=create)
    if app_runtime.engine and created:
        app_runtime.engine.enqueue_collection_job(payload["id"])
        record_account_operation(
            platform=payload["platform"], account_id=payload["account_id"],
            task_type="collection", task_id=payload["id"], action="queued",
            status="pending", target=payload.get("source_name") or
            "、".join(payload.get("keywords") or []), channel="queue")
    return payload


@router.put("/api/collections/{job_id}")
async def update_keyword_collection(job_id: int, body: collections_service.KeywordCollectionIn):
    """修改已结束任务的配置；历史作品/评论保留，续跑时按任务去重。"""
    await collections_service._resolve_ks_creator_source(body)
    await collections_service._resolve_channels_creator_source(body)
    await collections_service._resolve_bili_creator_source(body)
    await collections_service._resolve_rednote_creator_source(body)
    await collections_service._resolve_ig_creator_source(body)
    await collections_service._resolve_twitter_creator_source(body)
    platform, keywords, quality, download_dir, options = collections_service._validated_collection_input(body)
    with get_session() as session:
        job = session.get(KeywordCollectionJob, job_id)
        if not job:
            raise HTTPException(404, "采集任务不存在")
        if platform != job.platform:
            raise HTTPException(400, "采集任务创建后不能变更平台")
        if job.status in {"pending", "running"}:
            raise HTTPException(409, "等待或执行中的任务请先取消，再编辑配置")
        account_id = _resolve_collection_account_id(
            session,
            platform=platform,
            source_type=options["source_type"],
            requested_id=body.account_id,
        )

        job.account_id = account_id
        job.keywords = json.dumps(keywords, ensure_ascii=False)
        job.max_contents_per_keyword = body.max_contents_per_keyword
        for field, value in options.items():
            setattr(job, field, value)
        job.max_comments_per_content = body.max_comments_per_content
        job.include_replies = body.include_replies
        job.download_media = body.download_media
        job.video_quality = quality
        job.download_dir = download_dir
        job.current_keyword = ""
        job.current_step = "配置已更新，可点击续跑"
        job.processed_keyword_count = 0
        job.current_item_index = 0
        job.current_item_total = 0
        job.cancel_requested = False
        session.add(job); session.commit(); session.refresh(job)
        payload = collections_service._collection_job_dict(job)
    record_account_operation(
        platform=payload["platform"], account_id=payload["account_id"],
        task_type="collection", task_id=payload["id"], action="configured",
        status=payload["status"], target=payload.get("source_name") or
        "、".join(payload.get("keywords") or []), channel="api")
    return payload


@router.get("/api/collections")
async def list_keyword_collections(platform: str | None = None, limit: int = 100):
    limit = max(1, min(limit, 300))
    with get_session() as session:
        stmt = select(KeywordCollectionJob)
        if platform in {"douyin", "xhs", "rednote", "kuaishou", "shipinhao", "tiktok", "bilibili", "instagram", "twitter"}:
            stmt = stmt.where(KeywordCollectionJob.platform == platform)
        rows = session.exec(
            stmt.order_by(KeywordCollectionJob.created_at.desc()).limit(limit)).all()
        return [collections_service._collection_job_dict(row) for row in rows]


@router.get("/api/collections/{job_id}")
async def get_keyword_collection(job_id: int):
    with get_session() as session:
        job = session.get(KeywordCollectionJob, job_id)
        if not job:
            raise HTTPException(404, "采集任务不存在")
        return collections_service._collection_job_dict(job)


@router.get("/api/collections/{job_id}/contents")
async def list_keyword_collection_contents(job_id: int, keyword: str = "",
                                           page: int = 1, page_size: int = 20):
    page = max(1, page)
    page_size = max(1, min(page_size, 100))
    with get_session() as session:
        if not session.get(KeywordCollectionJob, job_id):
            raise HTTPException(404, "采集任务不存在")
        filters = [KeywordCollectionContent.job_id == job_id]
        if keyword:
            filters.append(KeywordCollectionContent.keyword == keyword)
        count_stmt = select(func.count(KeywordCollectionContent.id)).where(*filters)
        total = int(session.exec(count_stmt).one() or 0)
        rows = session.exec(
            select(KeywordCollectionContent).where(*filters)
            .order_by(KeywordCollectionContent.created_at.desc())
            .offset((page - 1) * page_size).limit(page_size)
        ).all()
        return {
            "items": [collections_service._collection_content_dict(row) for row in rows],
            "page": page, "page_size": page_size, "total": total,
            "pages": max(1, (total + page_size - 1) // page_size),
        }


def _get_collection_content(session, job_id: int,
                            content_id: int) -> KeywordCollectionContent:
    row = session.get(KeywordCollectionContent, content_id)
    if not row or row.job_id != job_id:
        raise HTTPException(404, "采集作品不存在")
    return row


@router.get("/api/collections/{job_id}/contents/{content_id}/media")
async def keyword_collection_content_media(job_id: int, content_id: int):
    """Return local-first media URLs for the collection result preview."""
    with get_session() as session:
        row = _get_collection_content(session, job_id, content_id)
        local_paths = collections_service._collection_local_media_paths(row)
        remote_medias = collections_service._collection_remote_medias(row)
        local_medias = [{
            "kind": "image" if path.suffix.lower() in collections_service._COLLECTION_IMAGE_EXTS else "video",
            "url": f"/api/collections/{job_id}/contents/{content_id}/local-media/{index}",
        } for index, path in enumerate(local_paths)]
        local_video = next(
            (item for item in local_medias if item["kind"] == "video"), None)
        has_local_images = any(item["kind"] == "image" for item in local_medias)
        playable_remote = [
            {k: v for k, v in item.items() if k != "encrypted"}
            for item in remote_medias
            if not item.get("encrypted")
        ]
        medias = (
            local_medias
            if (has_local_images or (local_video and not playable_remote))
            else playable_remote
        )
        return {
            "id": row.id, "platform": row.platform, "desc": row.desc,
            "media_type": row.media_type, "cover_url": row.cover_url,
            "local_path": row.local_path, "medias": medias,
            "local_url": local_video["url"] if local_video else "",
            "source_url": (f"https://www.xiaohongshu.com/explore/{row.aweme_id}"
                           if row.platform == "xhs"
                           else (row.source_url or f"https://www.rednote.com/zh/discovery/item/{row.aweme_id}")
                           if row.platform == "rednote"
                           else row.source_url
                           if row.platform == "tiktok"
                           else (row.source_url or "https://channels.weixin.qq.com/platform/post/list")
                           if row.platform == "shipinhao"
                           else f"https://www.kuaishou.com/short-video/{row.aweme_id}"
                           if row.platform == "kuaishou"
                           else (row.source_url or (
                               f"https://www.bilibili.com/opus/{row.aweme_id}"
                               if str(row.aweme_id or "").isdigit() and len(str(row.aweme_id or "")) >= 15
                               else f"https://www.bilibili.com/video/{row.aweme_id}"
                           ))
                           if row.platform == "bilibili"
                           else (row.source_url or f"https://www.instagram.com/p/{row.aweme_id}/")
                           if row.platform == "instagram"
                           else (row.source_url or f"https://x.com/i/web/status/{row.aweme_id}")
                           if row.platform == "twitter"
                           else f"https://www.douyin.com/video/{row.aweme_id}"),
        }


@router.api_route(
    "/api/collections/{job_id}/contents/{content_id}/local-media/{media_index}",
    methods=["GET", "HEAD"],
)
async def keyword_collection_local_media(job_id: int, content_id: int,
                                         media_index: int):
    """Stream a downloaded collection media file for inline browser preview."""
    with get_session() as session:
        row = _get_collection_content(session, job_id, content_id)
        paths = collections_service._collection_local_media_paths(row)
    if media_index < 0 or media_index >= len(paths):
        raise HTTPException(404, "本地媒体不存在")
    path = paths[media_index]
    return FileResponse(
        path,
        filename=path.name,
        content_disposition_type="inline",
        headers={"Cache-Control": "private, no-cache"},
    )


@router.post("/api/collections/{job_id}/contents/{content_id}/reveal")
async def reveal_keyword_collection_content(job_id: int, content_id: int,
                                            request: Request):
    """Reveal a downloaded collection file (or its gallery directory)."""
    share_downloads_service._require_local_action(request)
    with get_session() as session:
        row = _get_collection_content(session, job_id, content_id)
        path = collections_service._collection_local_path(row)
    if not path:
        raise HTTPException(404, "本地文件不存在")
    try:
        await asyncio.to_thread(contents_service._reveal_in_file_manager, path)
    except OSError as exc:
        raise HTTPException(500, f"打开文件夹失败：{exc}") from exc
    return {"ok": True}


@router.post("/api/collections/{job_id}/contents/{content_id}/open")
async def open_keyword_collection_content(job_id: int, content_id: int,
                                          request: Request):
    """Open the downloaded media in the operating system's default application."""
    share_downloads_service._require_local_action(request, "open")
    with get_session() as session:
        row = _get_collection_content(session, job_id, content_id)
        media_paths = collections_service._collection_local_media_paths(row)
        path = media_paths[0] if media_paths else collections_service._collection_local_path(row)
    if not path:
        raise HTTPException(404, "本地文件不存在")
    try:
        await asyncio.to_thread(contents_service._open_local_path, path)
    except OSError as exc:
        raise HTTPException(500, f"打开文件失败：{exc}") from exc
    return {"ok": True}


@router.get("/api/collections/{job_id}/comments")
async def list_keyword_collection_comments(job_id: int, content_id: int | None = None,
                                           q: str = "", ip_label: str = "",
                                           user_sec_uid: str = "",
                                           dedupe_users: bool = False,
                                           paginate: bool = False, page: int = 1,
                                           page_size: int = 50, limit: int = 300):
    limit = max(1, min(limit, 1000))
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    with get_session() as session:
        job, statement = collections_service._collection_comment_query(
            session, job_id, content_id=content_id, q=q, ip_label=ip_label,
            user_sec_uid=user_sec_uid, dedupe_users=dedupe_users)
        ordering = (KeywordCollectionComment.create_time.desc(),
                    KeywordCollectionComment.id.desc())
        if not paginate:
            rows = session.exec(statement.order_by(*ordering).limit(limit)).all()
            return collections_service._collection_comment_payloads(session, rows)
        total = int(session.exec(
            select(func.count()).select_from(statement.subquery())
        ).one())
        pages = max(1, (total + page_size - 1) // page_size)
        rows = session.exec(
            statement.order_by(*ordering)
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return {
            "items": collections_service._collection_comment_payloads(session, rows),
            "total": total, "page": page, "page_size": page_size,
            "pages": pages, "has_prev": page > 1, "has_next": page < pages,
            "job_id": job_id, "content_id": content_id,
        }


@router.post("/api/collections/{job_id}/cancel")
async def cancel_keyword_collection(job_id: int):
    should_abort_worker = False
    with get_session() as session:
        job = session.get(KeywordCollectionJob, job_id)
        if not job:
            raise HTTPException(404, "采集任务不存在")
        if job.status in {"done", "partial", "failed", "canceled"}:
            return collections_service._collection_job_dict(job)
        already_requested = bool(job.cancel_requested)
        job.cancel_requested = True
        if job.status == "pending" or (app_runtime.engine is None and already_requested):
            job.status = "canceled"
            job.current_step = "已取消"
            job.finished_at = datetime.utcnow()
        else:
            job.current_step = "正在安全停止"
            should_abort_worker = True
        session.add(job); session.commit(); session.refresh(job)
        payload = collections_service._collection_job_dict(job)
    if should_abort_worker and app_runtime.engine is not None and hasattr(app_runtime.engine, "cancel_collection_job"):
        await app_runtime.engine.cancel_collection_job(job_id)
        with get_session() as session:
            refreshed = session.get(KeywordCollectionJob, job_id)
            if refreshed is not None:
                payload = collections_service._collection_job_dict(refreshed)
    record_account_operation(
        platform=payload["platform"], account_id=payload["account_id"],
        task_type="collection", task_id=payload["id"], action="cancel_requested",
        status=payload["status"], target=payload.get("source_name") or
        "、".join(payload.get("keywords") or []), channel="api")
    return payload


@router.post("/api/collections/{job_id}/retry")
async def retry_keyword_collection(job_id: int):
    with get_session() as session:
        job = session.get(KeywordCollectionJob, job_id)
        if not job:
            raise HTTPException(404, "采集任务不存在")
        if job.status in {"pending", "running"}:
            raise HTTPException(409, "任务仍在等待或执行中")
        job.status = "pending"
        job.current_keyword = ""
        job.current_step = "等待继续"
        job.processed_keyword_count = 0
        job.current_item_index = 0
        job.current_item_total = 0
        job.error_count = 0
        job.error = ""
        job.cancel_requested = False
        job.started_at = None
        job.finished_at = None
        session.add(job); session.commit(); session.refresh(job)
        payload = collections_service._collection_job_dict(job)
    if app_runtime.engine:
        app_runtime.engine.enqueue_collection_job(job_id)
    record_account_operation(
        platform=payload["platform"], account_id=payload["account_id"],
        task_type="collection", task_id=payload["id"], action="requeued",
        status="pending", target=payload.get("source_name") or
        "、".join(payload.get("keywords") or []), channel="queue")
    return payload


@router.delete("/api/collections/{job_id}")
async def delete_keyword_collection(job_id: int):
    with get_session() as session:
        job = session.get(KeywordCollectionJob, job_id)
        if not job:
            return {"ok": True, "deleted": 0}
        if job.status == "running":
            raise HTTPException(409, "请先取消正在执行的任务")
        for row in session.exec(
                select(KeywordCollectionComment)
                .where(KeywordCollectionComment.job_id == job_id)).all():
            session.delete(row)
        for row in session.exec(
                select(KeywordCollectionContent)
                .where(KeywordCollectionContent.job_id == job_id)).all():
            session.delete(row)
        session.delete(job); session.commit()
    return {"ok": True, "deleted": 1}


@router.get("/api/collections/{job_id}/export.xlsx")
async def export_keyword_collection(job_id: int, content_id: int | None = None,
                                    q: str = "", ip_label: str = "",
                                    user_sec_uid: str = "",
                                    dedupe_users: bool = False):
    with get_session() as session:
        job, statement = collections_service._collection_comment_query(
            session, job_id, content_id=content_id, q=q, ip_label=ip_label,
            user_sec_uid=user_sec_uid, dedupe_users=dedupe_users)
        comments = session.exec(statement.order_by(
            KeywordCollectionComment.aweme_id,
            KeywordCollectionComment.create_time.desc(),
            KeywordCollectionComment.id.desc())).all()
        filters = [("评论范围", "当前作品" if content_id is not None else "全部作品")]
        if (q or "").strip():
            filters.append(("评论关键词", (q or "").strip()))
        if (ip_label or "").strip():
            filters.append(("地区", (ip_label or "").strip()))
        if (user_sec_uid or "").strip():
            filters.append(("用户 sec_uid", (user_sec_uid or "").strip()))
        if dedupe_users:
            filters.append(("用户去重", "按 sec_uid 保留最新评论"))
        all_filtered_statement = collections_service._collection_comment_query(
            session, job_id, content_id=content_id, q=q, ip_label=ip_label,
            user_sec_uid=user_sec_uid, dedupe_users=False)[1]
        lead_comments = session.exec(all_filtered_statement.order_by(
            KeywordCollectionComment.create_time.desc(),
            KeywordCollectionComment.id.desc())).all()
        has_filters = bool((q or "").strip() or (ip_label or "").strip()
                           or (user_sec_uid or "").strip()
                           or content_id is not None or dedupe_users)
        if has_filters:
            referenced_ids = {row.content_id for row in lead_comments}
            if referenced_ids:
                contents = session.exec(
                    select(KeywordCollectionContent)
                    .where(KeywordCollectionContent.job_id == job_id)
                    .where(KeywordCollectionContent.id.in_(referenced_ids))
                    .order_by(KeywordCollectionContent.keyword,
                              KeywordCollectionContent.create_time.desc())
                ).all()
            else:
                contents = []
        else:
            contents = session.exec(
                select(KeywordCollectionContent)
                .where(KeywordCollectionContent.job_id == job_id)
                .order_by(KeywordCollectionContent.keyword,
                          KeywordCollectionContent.create_time.desc())).all()
        from app.reporting import build_keyword_collection_report
        payload = build_keyword_collection_report(
            job, contents, comments, filters=filters,
            lead_comments=lead_comments)
    return metadata_service._report_download(payload, f"keyword-collection-{job_id}")
