"""弹幕监控目标管理与已采集弹幕查询清理接口。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field as PydanticField
from sqlalchemy import func, or_
from sqlmodel import select

from app import runtime as app_runtime
from app.db import get_session
from app.engine.cadence import row_deadline
from app.models import DanmakuRecord, DanmakuWatch, DouyinAccount
from app.platforms.bilibili import looks_like_bili_work, resolve_bili_mid, resolve_bili_work
from app.platforms.douyin import resolve_aweme_id, resolve_sec_uid
from app.scheduling import utc_iso
from app.services import contents as contents_service
from app.services import metadata as metadata_service
from app.services import watch_records as watch_records_service

router = APIRouter()


class DanmakuWatchIn(BaseModel):
    url_or_id: str
    platform: str = "douyin"
    kind: str = "auto"              # auto | video | user
    mode: str = "public"            # public | creator
    account_id: int | None = None
    interval_seconds: int = 0       # 0=跟随全局扫描间隔
    recent_works: int = 0           # 0=跟随全局弹幕作品数
    recent_days: int = 0             # 0=跟随全局弹幕时间范围
    max_scrolls: int = 0             # 0=跟随全局弹幕加载轮次
    time_start_ms: int = 0
    time_end_ms: int = 0
    probe_step_seconds: float = 0.0  # 0=跟随全局时间轴步长
    include_keywords: list[str] = PydanticField(default_factory=list)
    exclude_keywords: list[str] = PydanticField(default_factory=list)
    min_text_length: int = 0
    max_text_length: int = 0
    min_like_count: int = 0
    max_records_per_scan: int = 0  # 0=跟随全局
    max_records_total: int = 0     # 0=跟随全局
    alias: str = ""
    group_name: str = ""
    tags: list[str] = PydanticField(default_factory=list)


class DanmakuWatchUpdate(BaseModel):
    enabled: bool | None = None
    interval_seconds: int | None = None
    mode: str | None = None
    account_id: int | None = None
    recent_works: int | None = None
    recent_days: int | None = None
    max_scrolls: int | None = None
    time_start_ms: int | None = None
    time_end_ms: int | None = None
    probe_step_seconds: float | None = None
    include_keywords: list[str] | None = None
    exclude_keywords: list[str] | None = None
    min_text_length: int | None = None
    max_text_length: int | None = None
    min_like_count: int | None = None
    max_records_per_scan: int | None = None
    max_records_total: int | None = None
    alias: str | None = None
    group_name: str | None = None
    tags: list[str] | None = None


def _danmaku_watch_dict(w: DanmakuWatch) -> dict:
    return {
        "id": w.id, "platform": w.platform, "kind": w.kind,
        "aweme_id": w.aweme_id, "sec_uid": w.sec_uid,
        "title": w.title, "avatar": w.avatar, "mode": w.mode,
        "alias": w.alias, "group_name": w.group_name,
        "tags": metadata_service._load_meta_tags(w.tags),
        "account_id": w.account_id, "interval_seconds": w.interval_seconds,
        "effective_interval_seconds": w.interval_seconds or app_runtime.cfg.engine.scan_interval_seconds,
        "uses_global_interval": w.interval_seconds == 0,
        "recent_works": w.recent_works, "recent_days": w.recent_days,
        "max_scrolls": w.max_scrolls, "enabled": w.enabled,
        "effective_recent_works": w.recent_works or app_runtime.cfg.engine.danmaku_recent_works,
        "effective_recent_days": w.recent_days or app_runtime.cfg.engine.danmaku_recent_days,
        "effective_max_scrolls": w.max_scrolls or app_runtime.cfg.engine.danmaku_max_scrolls,
        "time_start_ms": w.time_start_ms, "time_end_ms": w.time_end_ms,
        "probe_step_seconds": w.probe_step_seconds,
        "effective_probe_step_seconds": w.probe_step_seconds or app_runtime.cfg.engine.danmaku_probe_step_seconds,
        "effective_max_probe_points": app_runtime.cfg.engine.danmaku_max_probe_points,
        "include_keywords": metadata_service._load_meta_tags(w.include_keywords),
        "exclude_keywords": metadata_service._load_meta_tags(w.exclude_keywords),
        "min_text_length": w.min_text_length, "max_text_length": w.max_text_length,
        "min_like_count": w.min_like_count,
        "max_records_per_scan": w.max_records_per_scan,
        "max_records_total": w.max_records_total,
        "effective_max_records_per_scan": w.max_records_per_scan or app_runtime.cfg.engine.danmaku_max_records_per_scan,
        "effective_max_records_total": w.max_records_total or app_runtime.cfg.engine.danmaku_max_records_total,
        "danmaku_count": w.danmaku_count,
        "last_scan_at": w.last_scan_at.isoformat() if w.last_scan_at else None,
        "next_auto_run_at": utc_iso(row_deadline(w, app_runtime.cfg, kind="danmaku")),
        "last_error": w.last_error,
    }


@router.get("/api/danmaku-watches")
async def list_danmaku_watches(platform: str | None = None):
    with get_session() as s:
        q = select(DanmakuWatch).order_by(DanmakuWatch.id.desc())
        if platform:
            q = q.where(DanmakuWatch.platform == platform)
        watches = s.exec(q).all()
        counts = dict(s.exec(select(DanmakuRecord.watch_id, func.count(DanmakuRecord.id))
                             .where(DanmakuRecord.watch_id.in_([w.id for w in watches]))
                             .group_by(DanmakuRecord.watch_id)).all()) if watches else {}
        return [dict(_danmaku_watch_dict(w), danmaku_count=counts.get(w.id, 0)) for w in watches]


@router.post("/api/danmaku-watches")
async def add_danmaku_watch(body: DanmakuWatchIn):
    if body.platform not in ("douyin", "bilibili"):
        raise HTTPException(400, "弹幕监控当前仅支持抖音与哔哩哔哩")
    platform = body.platform
    target_input = metadata_service._clean_platform_target_input(body.url_or_id, platform)
    if body.interval_seconds != 0 and not 1 <= body.interval_seconds <= 86400:
        raise HTTPException(400, "监控间隔须为 1~86400 秒,或填 0 跟随全局")
    if not 0 <= body.recent_works <= 50:
        raise HTTPException(400, "近期作品数须为 0~50")
    if not 0 <= body.recent_days <= 365:
        raise HTTPException(400, "近期天数须为 0~365")
    if not 0 <= body.max_scrolls <= 50:
        raise HTTPException(400, "抓取轮次须为 0~50")
    if body.time_start_ms < 0 or body.time_start_ms > 86_400_000:
        raise HTTPException(400, "视频起始时间须为 0~86400 秒")
    if body.time_end_ms < 0 or body.time_end_ms > 86_400_000:
        raise HTTPException(400, "视频结束时间须为 0~86400 秒")
    if body.time_end_ms and body.time_end_ms < body.time_start_ms:
        raise HTTPException(400, "视频结束时间须不早于起始时间")
    if body.probe_step_seconds != 0 and not 0.25 <= body.probe_step_seconds <= 30:
        raise HTTPException(400, "时间扫描步长须为 0 或 0.25~30 秒")
    if not 0 <= body.min_text_length <= 200 or not 0 <= body.max_text_length <= 200:
        raise HTTPException(400, "文本长度过滤须为 0~200")
    if body.max_text_length and body.max_text_length < body.min_text_length:
        raise HTTPException(400, "最大文本长度须不小于最小文本长度")
    if body.min_like_count < 0:
        raise HTTPException(400, "最少点赞数不能小于 0")
    if not 0 <= body.max_records_per_scan <= 100_000:
        raise HTTPException(400, "单轮记录上限须为 0~100000")
    if not 0 <= body.max_records_total <= 1_000_000:
        raise HTTPException(400, "总记录上限须为 0~1000000")

    aweme_id = sec_uid = ""
    kind = body.kind
    mode = body.mode if body.mode in ("public", "creator") else "public"
    title = ""
    if platform == "bilibili":
        mode = "public"
        if kind == "auto":
            kind = "video" if looks_like_bili_work(target_input) else (
                "video" if await resolve_bili_work(target_input, app_runtime.cfg.engine.user_agent) else "user"
            )
        if kind not in ("video", "user"):
            raise HTTPException(400, "监控对象类型须为 video 或 user")
        if kind == "video":
            aweme_id = await resolve_bili_work(target_input, app_runtime.cfg.engine.user_agent) or ""
            if not aweme_id or not aweme_id.upper().startswith("BV1"):
                raise HTTPException(400, "无法解析 B 站视频，请粘贴 BV 号、av 号、视频链接或 b23.tv 短链")
            title = "视频 " + aweme_id
        else:
            sec_uid = await resolve_bili_mid(target_input, app_runtime.cfg.engine.user_agent) or ""
            if not sec_uid:
                raise HTTPException(400, "无法解析 B 站 UP 主 UID，请粘贴空间链接、b23.tv 短链或数字 UID")
            title = "UP主 " + sec_uid
    else:
        if kind == "auto":
            aweme_id = await resolve_aweme_id(target_input, app_runtime.cfg.engine.user_agent) or ""
            kind = "video" if aweme_id else "user"
        if kind not in ("video", "user"):
            raise HTTPException(400, "监控对象类型须为 video 或 user")
        if kind == "video":
            if not aweme_id:
                aweme_id = await resolve_aweme_id(target_input, app_runtime.cfg.engine.user_agent)
            if not aweme_id:
                raise HTTPException(400, "无法解析视频 id,请粘贴完整分享文案、作品链接、短链或数字 id")
            title = "视频 " + aweme_id
        else:
            sec_uid = await resolve_sec_uid(target_input, app_runtime.cfg.engine.user_agent)
            if not sec_uid:
                raise HTTPException(400, "无法解析 sec_uid,请粘贴完整分享文案、账号主页、短链或 sec_uid")
            title = "账号 " + sec_uid[:12]

        if mode == "creator":
            with get_session() as s:
                acc = s.get(DouyinAccount, body.account_id) if body.account_id else None
                if not acc or acc.platform != "douyin" or not acc.creator_storage_state:
                    raise HTTPException(400, "创作中心弹幕模式需要选择已完成创作者登录的抖音账号")
                if kind == "user" and acc.sec_uid and acc.sec_uid != sec_uid:
                    raise HTTPException(400, "创作中心账号与监控账号不一致")

    with get_session() as s:
        q = select(DanmakuWatch).where(
            DanmakuWatch.platform == platform,
            DanmakuWatch.kind == kind,
            DanmakuWatch.mode == mode)
        q = q.where(DanmakuWatch.aweme_id == aweme_id if kind == "video"
                    else DanmakuWatch.sec_uid == sec_uid)
        if s.exec(q).first():
            raise HTTPException(409, "已存在相同的弹幕监控")
        watch = DanmakuWatch(
            platform=platform, kind=kind, aweme_id=aweme_id, sec_uid=sec_uid,
            title=title, mode=mode, account_id=body.account_id,
            interval_seconds=body.interval_seconds,
            recent_works=body.recent_works, recent_days=body.recent_days,
            max_scrolls=body.max_scrolls,
            time_start_ms=body.time_start_ms, time_end_ms=body.time_end_ms,
            probe_step_seconds=body.probe_step_seconds,
            include_keywords=metadata_service._dump_meta_tags(metadata_service._meta_tags(body.include_keywords)),
            exclude_keywords=metadata_service._dump_meta_tags(metadata_service._meta_tags(body.exclude_keywords)),
            min_text_length=body.min_text_length, max_text_length=body.max_text_length,
            min_like_count=body.min_like_count,
            max_records_per_scan=body.max_records_per_scan,
            max_records_total=body.max_records_total,
            alias=metadata_service._meta_text(body.alias, 60),
            group_name=metadata_service._meta_text(body.group_name, 40),
            tags=metadata_service._dump_meta_tags(metadata_service._meta_tags(body.tags)))
        s.add(watch)
        s.commit()
        s.refresh(watch)
        return _danmaku_watch_dict(watch)


@router.put("/api/danmaku-watches/{wid}")
async def update_danmaku_watch(wid: int, body: DanmakuWatchUpdate):
    with get_session() as s:
        watch = s.get(DanmakuWatch, wid)
        if not watch:
            raise HTTPException(404, "弹幕监控不存在")
        if body.interval_seconds is not None and body.interval_seconds != 0 \
                and not 1 <= body.interval_seconds <= 86400:
            raise HTTPException(400, "监控间隔须为 1~86400 秒,或填 0 跟随全局")
        if body.recent_works is not None and not 0 <= body.recent_works <= 50:
            raise HTTPException(400, "近期作品数须为 0~50")
        if body.recent_days is not None and not 0 <= body.recent_days <= 365:
            raise HTTPException(400, "近期天数须为 0~365")
        if body.max_scrolls is not None and not 0 <= body.max_scrolls <= 50:
            raise HTTPException(400, "抓取轮次须为 0~50")
        current_start = body.time_start_ms if body.time_start_ms is not None else watch.time_start_ms
        current_end = body.time_end_ms if body.time_end_ms is not None else watch.time_end_ms
        current_step = body.probe_step_seconds if body.probe_step_seconds is not None else watch.probe_step_seconds
        current_min_len = body.min_text_length if body.min_text_length is not None else watch.min_text_length
        current_max_len = body.max_text_length if body.max_text_length is not None else watch.max_text_length
        current_min_like = body.min_like_count if body.min_like_count is not None else watch.min_like_count
        current_scan_cap = body.max_records_per_scan if body.max_records_per_scan is not None else watch.max_records_per_scan
        current_total_cap = body.max_records_total if body.max_records_total is not None else watch.max_records_total
        if current_start < 0 or current_start > 86_400_000 or current_end < 0 or current_end > 86_400_000:
            raise HTTPException(400, "视频时间范围须为 0~86400 秒")
        if current_end and current_end < current_start:
            raise HTTPException(400, "视频结束时间须不早于起始时间")
        if current_step != 0 and not 0.25 <= current_step <= 30:
            raise HTTPException(400, "时间扫描步长须为 0 或 0.25~30 秒")
        if not 0 <= current_min_len <= 200 or not 0 <= current_max_len <= 200:
            raise HTTPException(400, "文本长度过滤须为 0~200")
        if current_max_len and current_max_len < current_min_len:
            raise HTTPException(400, "最大文本长度须不小于最小文本长度")
        if current_min_like < 0:
            raise HTTPException(400, "最少点赞数不能小于 0")
        if not 0 <= current_scan_cap <= 100_000 or not 0 <= current_total_cap <= 1_000_000:
            raise HTTPException(400, "记录上限超出范围")
        new_mode = body.mode if body.mode is not None else watch.mode
        if new_mode not in ("public", "creator"):
            raise HTTPException(400, "弹幕来源须为 public 或 creator")
        new_account_id = body.account_id if body.account_id is not None else watch.account_id
        if new_mode == "creator":
            acc = s.get(DouyinAccount, new_account_id) if new_account_id else None
            if not acc or acc.platform != "douyin" or not acc.creator_storage_state:
                raise HTTPException(400, "创作中心弹幕模式需要绑定已完成创作者登录的抖音账号")
            if watch.kind == "user" and acc.sec_uid and watch.sec_uid \
                    and acc.sec_uid != watch.sec_uid:
                raise HTTPException(400, "创作中心账号与监控账号不一致")
        if body.enabled is not None:
            watch.enabled = body.enabled
        if body.interval_seconds is not None:
            watch.interval_seconds = body.interval_seconds
        if body.mode is not None:
            watch.mode = new_mode
        if body.account_id is not None:
            watch.account_id = body.account_id
        if body.recent_works is not None:
            watch.recent_works = body.recent_works
        if body.recent_days is not None:
            watch.recent_days = body.recent_days
        if body.max_scrolls is not None:
            watch.max_scrolls = body.max_scrolls
        if body.time_start_ms is not None:
            watch.time_start_ms = body.time_start_ms
        if body.time_end_ms is not None:
            watch.time_end_ms = body.time_end_ms
        if body.probe_step_seconds is not None:
            watch.probe_step_seconds = body.probe_step_seconds
        if body.include_keywords is not None:
            watch.include_keywords = metadata_service._dump_meta_tags(metadata_service._meta_tags(body.include_keywords))
        if body.exclude_keywords is not None:
            watch.exclude_keywords = metadata_service._dump_meta_tags(metadata_service._meta_tags(body.exclude_keywords))
        if body.min_text_length is not None:
            watch.min_text_length = body.min_text_length
        if body.max_text_length is not None:
            watch.max_text_length = body.max_text_length
        if body.min_like_count is not None:
            watch.min_like_count = body.min_like_count
        if body.max_records_per_scan is not None:
            watch.max_records_per_scan = body.max_records_per_scan
        if body.max_records_total is not None:
            watch.max_records_total = body.max_records_total
        if body.alias is not None:
            watch.alias = metadata_service._meta_text(body.alias, 60)
        if body.group_name is not None:
            watch.group_name = metadata_service._meta_text(body.group_name, 40)
        if body.tags is not None:
            watch.tags = metadata_service._dump_meta_tags(metadata_service._meta_tags(body.tags))
        s.add(watch)
        s.commit()
        s.refresh(watch)
        return _danmaku_watch_dict(watch)


@router.delete("/api/danmaku-watches/{wid}")
async def delete_danmaku_watch(wid: int, with_records: bool = False):
    with get_session() as s:
        watch = s.get(DanmakuWatch, wid)
        if not watch:
            return {"ok": True}
        deleted = 0
        if with_records:
            rows = s.exec(select(DanmakuRecord).where(
                DanmakuRecord.watch_id == wid)).all()
            for row in rows:
                s.delete(row)
            deleted = len(rows)
        s.delete(watch)
        s.commit()
        return {"ok": True, "records_deleted": deleted}


@router.post("/api/danmaku-watches/{wid}/scan-now")
async def scan_danmaku_watch_now(wid: int):
    if app_runtime.engine is None:
        raise HTTPException(503, "引擎未就绪")
    result = await app_runtime.engine.scan_danmaku_watch(wid)
    if not result.get("ok") and not result.get("new_danmaku"):
        raise HTTPException(400, result.get("error") or "弹幕抓取失败")
    return result


@router.get("/api/danmaku")
async def list_danmaku(limit: int = 100, watch_id: int | None = None,
                       aweme_id: str | None = None, platform: str | None = None,
                       group_name: str = "", tag: str = "", q: str = "",
                       min_video_time_ms: int = 0, max_video_time_ms: int = 0,
                       min_like_count: int = 0, sort: str = "video_asc",
                       page: int = 1, page_size: int = 10,
                       paginate: bool = False,
                       captured_from: datetime | None = None,
                       captured_before: datetime | None = None):
    limit = max(1, min(limit, 1000))
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    start, end = contents_service._content_capture_bounds(captured_from, captured_before)
    with get_session() as s:
        selected_source = watch_records_service._selected_watch_source(s, DanmakuWatch, watch_id, platform)
        stmt = watch_records_service._watch_record_filter(select(DanmakuRecord), DanmakuRecord, watch_id)
        stmt = metadata_service._report_window(stmt, DanmakuRecord, start, end)
        if platform:
            stmt = stmt.where(DanmakuRecord.platform == platform)
        if aweme_id:
            stmt = stmt.where(DanmakuRecord.aweme_id == aweme_id)
        text_query = q.strip()
        if text_query:
            stmt = stmt.where(or_(DanmakuRecord.text.contains(text_query),
                                  DanmakuRecord.user_id.contains(text_query),
                                  DanmakuRecord.user_nickname.contains(text_query)))
        if min_video_time_ms > 0:
            stmt = stmt.where(DanmakuRecord.video_time_ms >= min_video_time_ms)
        if max_video_time_ms > 0:
            stmt = stmt.where(DanmakuRecord.video_time_ms <= max_video_time_ms)
        if min_like_count > 0:
            stmt = stmt.where(DanmakuRecord.like_count >= min_like_count)
        group_name, tag = metadata_service._meta_text(group_name, 40), metadata_service._meta_text(tag, 24)
        if group_name or tag:
            watch_query = select(DanmakuWatch)
            if platform:
                watch_query = watch_query.where(DanmakuWatch.platform == platform)
            watches = s.exec(watch_query).all()
            ids = [w.id for w in watches if w.id is not None
                   and metadata_service._meta_matches(w, group_name, tag)]
            if not ids:
                if not paginate:
                    return []
                return {
                    "items": [], "total": 0, "page": page,
                    "page_size": page_size, "pages": 1,
                    "has_prev": page > 1, "has_next": False, "watch_source": selected_source,
                }
            stmt = stmt.where(DanmakuRecord.watch_id.in_(ids))
        ordering = watch_records_service._watch_record_ordering(DanmakuRecord, sort)
        if not paginate:
            rows = s.exec(stmt.order_by(*ordering).limit(limit)).all()
            return watch_records_service._watch_records(s, rows, DanmakuWatch)

        total = int(s.exec(select(func.count()).select_from(stmt.subquery())).one())
        pages = max(1, (total + page_size - 1) // page_size)
        rows = s.exec(
            stmt.order_by(*ordering)
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return {
            "items": watch_records_service._watch_records(s, rows, DanmakuWatch), "watch_source": selected_source,
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": pages,
            "has_prev": page > 1,
            "has_next": page < pages,
        }


@router.delete("/api/danmaku/{did}")
async def delete_danmaku(did: int):
    with get_session() as s:
        row = s.get(DanmakuRecord, did)
        if row:
            s.delete(row)
            s.commit()
    return {"ok": True}


@router.post("/api/danmaku/batch-delete")
async def batch_delete_danmaku(body: contents_service.IdsIn):
    deleted = 0
    with get_session() as s:
        for did in body.ids:
            row = s.get(DanmakuRecord, did)
            if row:
                s.delete(row)
                deleted += 1
        s.commit()
    return {"ok": True, "deleted": deleted}


@router.delete("/api/danmaku")
async def clear_danmaku(watch_id: int | None = None, platform: str | None = None):
    with get_session() as s:
        q = select(DanmakuRecord)
        if watch_id is not None:
            q = q.where(DanmakuRecord.watch_id == watch_id)
        if platform:
            q = q.where(DanmakuRecord.platform == platform)
        rows = s.exec(q).all()
        for row in rows:
            s.delete(row)
        s.commit()
        return {"ok": True, "deleted": len(rows)}
