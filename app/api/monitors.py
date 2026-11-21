"""作品监控目标增删改查、启停切换与立即扫描接口。"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field as PydanticField
from sqlalchemy import func, or_
from sqlmodel import select

from app import runtime as app_runtime
from app.db import get_session
from app.engine.cadence import row_deadline
from app.models import ContentRecord, DouyinAccount, MonitorTarget
from app.platforms.bilibili import resolve_bili_mid
from app.platforms.douyin import resolve_sec_uid
from app.platforms.instagram import looks_like_ig_work, resolve_ig_user_target
from app.platforms.kuaishou import resolve_ks_user_id
from app.platforms.rednote import resolve_rednote_user
from app.platforms.tiktok import tiktok_display_text
from app.platforms.twitter import looks_like_twitter_tweet, resolve_twitter_short_url, resolve_twitter_user_target
from app.platforms.xhs import resolve_user as xhs_resolve_user
from app.scheduling import utc_iso
from app.services import collections as collections_service
from app.services import contents as contents_service
from app.services import metadata as metadata_service
from app.submissions import replay_if_exists, submit_once

router = APIRouter()


class TargetIn(BaseModel):
    url_or_secuid: str                       # 抖音/小红书主页链接 或 小红书关键词
    platform: str = "douyin"                # douyin | xhs
    target_kind: str = "creator"            # creator | keyword(仅小红书)
    account_id: int | None = None
    interval_seconds: int = 300
    initial_backfill_count: int | None = None
    download_dir: str = ""
    video_quality: str = ""
    download_enabled: bool = True
    media_filter: str = "all"
    max_scrolls: int = 0
    max_items_per_scan: int = 0
    record_media_filter: str = "all"
    min_like_count: int = 0
    min_comment_count: int = 0
    recent_days: int = 0
    include_keywords: list[str] = PydanticField(default_factory=list)
    exclude_keywords: list[str] = PydanticField(default_factory=list)
    alias: str = ""
    group_name: str = ""
    tags: list[str] = PydanticField(default_factory=list)


class TargetUpdate(BaseModel):
    download_dir: str | None = None
    interval_seconds: int | None = None
    initial_backfill_count: int | None = None
    video_quality: str | None = None
    download_enabled: bool | None = None
    media_filter: str | None = None
    max_scrolls: int | None = None
    max_items_per_scan: int | None = None
    record_media_filter: str | None = None
    min_like_count: int | None = None
    min_comment_count: int | None = None
    recent_days: int | None = None
    include_keywords: list[str] | None = None
    exclude_keywords: list[str] | None = None
    account_id: int | None = None
    alias: str | None = None
    group_name: str | None = None
    tags: list[str] | None = None


def _validate_monitor_strategy(*, max_scrolls: int | None,
                               max_items_per_scan: int | None,
                               record_media_filter: str | None,
                               min_like_count: int | None,
                               min_comment_count: int | None,
                               recent_days: int | None) -> None:
    if max_scrolls is not None and not 0 <= max_scrolls <= 30:
        raise HTTPException(400, "抓取深度须为 0~30；0 表示使用平台默认值")
    if max_items_per_scan is not None and not 0 <= max_items_per_scan <= 100:
        raise HTTPException(400, "每轮作品上限须为 0~100；0 表示使用平台默认值")
    if record_media_filter is not None and record_media_filter not in (
            "all", "video", "images"):
        raise HTTPException(400, "作品类型筛选须为 all、video 或 images")
    if min_like_count is not None and not 0 <= min_like_count <= 1_000_000_000:
        raise HTTPException(400, "最低点赞数须为 0~1000000000")
    if min_comment_count is not None and not 0 <= min_comment_count <= 1_000_000_000:
        raise HTTPException(400, "最低评论数须为 0~1000000000")
    if recent_days is not None and not 0 <= recent_days <= 3650:
        raise HTTPException(400, "发布时间范围须为 0~3650 天；0 表示不限")


def _ensure_creator_monitor_available(session, *, platform: str, sec_uid: str,
                                     account_id: int | None,
                                     exclude_id: int | None = None) -> None:
    """主页监控按执行账号去重；暂停的任务仍占用该账号的目标。"""
    query = select(MonitorTarget).where(
        MonitorTarget.platform == platform, MonitorTarget.sec_uid == sec_uid)
    if account_id:
        query = query.where(MonitorTarget.account_id == account_id)
    else:
        # 兼容历史数据中的 0：与 None 都表示匿名执行。
        query = query.where(or_(MonitorTarget.account_id.is_(None),
                                MonitorTarget.account_id == 0))
    if exclude_id is not None:
        query = query.where(MonitorTarget.id != exclude_id)
    if session.exec(query).first():
        detail = "该账号已存在此主页的监控" if account_id else "该主页的匿名监控已存在"
        raise HTTPException(409, detail)


@router.post("/api/monitors")
async def add_monitor(body: TargetIn, request: Request = None):
    with get_session() as session:
        replay = replay_if_exists(session, request=request, scope="monitor", body=body)
        if replay is not None:
            return replay
    platform = body.platform if body.platform in ("douyin", "xhs", "rednote", "kuaishou", "shipinhao", "tiktok", "bilibili", "instagram", "twitter") else "douyin"
    sec_uid = keyword = xsec_token = ""
    nickname = avatar = ""
    kind = "creator"

    if platform in ("xhs", "rednote", "instagram", "twitter") and body.target_kind == "keyword":
        kind = "keyword"
        keyword = body.url_or_secuid.strip()
        if not keyword:
            raise HTTPException(400, "请输入要监控的搜索关键词")
    else:
        target_input = metadata_service._clean_platform_target_input(body.url_or_secuid, platform)

    if platform == "xhs" and kind != "keyword":
        ref = await xhs_resolve_user(target_input, app_runtime.cfg.engine.user_agent)
        if not ref:
            raise HTTPException(400, "无法解析小红书 user_id,请粘贴完整分享文案、创作者主页链接 / xhslink 短链 / 24 位 user_id")
        sec_uid, xsec_token = ref.user_id, ref.xsec_token
    elif platform == "rednote" and kind != "keyword":
        ref = await resolve_rednote_user(target_input, app_runtime.cfg.engine.user_agent)
        if not ref:
            raise HTTPException(400, "无法解析海外小红书 user_id,请粘贴 www.rednote.com/zh/user/profile/... 主页链接或 24 位 user_id")
        sec_uid, xsec_token = ref.user_id, ref.xsec_token
    elif platform == "kuaishou":
        sec_uid = await resolve_ks_user_id(target_input, app_runtime.cfg.engine.user_agent)
        if not sec_uid:
            raise HTTPException(400, "无法解析快手 user_id,请粘贴完整分享文案、创作者主页链接 / v.kuaishou.com 短链 / user_id")
    elif platform == "shipinhao":
        channels_res = await collections_service._resolve_channels_creator_target(target_input)
        sec_uid = channels_res.username
        nickname = channels_res.nickname
        avatar = channels_res.avatar
    elif platform == "tiktok":
        target_input, video_ref, profile_id = await metadata_service._resolve_tiktok_target(target_input)
        sec_uid = profile_id or (video_ref.author_unique_id if video_ref else "")
        if not sec_uid:
            raise HTTPException(400, "无法解析 TikTok @username，请粘贴完整主页/作品链接、短链或 @username")
    elif platform == "bilibili":
        sec_uid = await resolve_bili_mid(target_input, app_runtime.cfg.engine.user_agent)
        if not sec_uid:
            raise HTTPException(400, "无法解析哔哩哔哩 UP 主 UID，请粘贴空间链接 space.bilibili.com/<mid>、b23.tv 短链或纯数字 UID")
    elif platform == "instagram" and kind != "keyword":
        if looks_like_ig_work(target_input):
            raise HTTPException(400, "检测到 Instagram 单帖/Reels 链接；作品监控请填写创作者 @username、主页链接或切换为关键词监控")
        sec_uid = resolve_ig_user_target(target_input)
        if not sec_uid:
            raise HTTPException(400, "无法解析 Instagram 创作者，请粘贴 instagram.com/<username> 主页链接、@username 或数字 UID")
        if not sec_uid.isdigit():
            nickname = f"@{sec_uid}"
    elif platform == "twitter" and kind != "keyword":
        expanded_tw = await resolve_twitter_short_url(target_input, user_agent=app_runtime.cfg.engine.user_agent)
        if looks_like_twitter_tweet(expanded_tw):
            raise HTTPException(400, "检测到 X (Twitter) 单条推文链接；作品监控请填写创作者 @username、x.com/<username> 主页链接或切换为关键词监控")
        sec_uid = resolve_twitter_user_target(expanded_tw)
        if not sec_uid:
            raise HTTPException(400, "无法解析 X (Twitter) 创作者，请粘贴 x.com/<username> 主页链接、@username 或数字 UID")
        if not sec_uid.isdigit():
            nickname = f"@{sec_uid}"
    elif platform == "douyin":
        sec_uid = await resolve_sec_uid(target_input, app_runtime.cfg.engine.user_agent)
        if not sec_uid:
            raise HTTPException(400, "无法解析 sec_uid,请粘贴完整分享文案、主页链接 / v.douyin.com 短链 / sec_uid")

    dl = body.download_dir.strip()
    if not 1 <= body.interval_seconds <= 86400:
        raise HTTPException(400, "监控间隔须为 1~86400 秒")
    if body.media_filter not in ("all", "video", "images"):
        raise HTTPException(400, "媒体筛选须为 all、video 或 images")
    _validate_monitor_strategy(
        max_scrolls=body.max_scrolls,
        max_items_per_scan=body.max_items_per_scan,
        record_media_filter=body.record_media_filter,
        min_like_count=body.min_like_count,
        min_comment_count=body.min_comment_count,
        recent_days=body.recent_days)
    if dl:
        try:
            Path(dl).expanduser().mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise HTTPException(400, f"下载目录不可用: {e}")
    with get_session() as s:
        def create():
            if platform in {"douyin", "tiktok", "instagram", "twitter"}:
                platform_name = (
                    "TikTok" if platform == "tiktok"
                    else "Instagram" if platform == "instagram"
                    else "Twitter" if platform == "twitter"
                    else "抖音"
                )
                if not body.account_id:
                    raise HTTPException(
                        400, f"{platform_name}作品监控必须选择已登录账号")
                monitor_acc = s.get(DouyinAccount, body.account_id)
                if (not monitor_acc or monitor_acc.platform != platform
                        or monitor_acc.status != "active"):
                    raise HTTPException(400, f"所选{platform_name}账号不存在或登录态已失效")
                if (platform in {"tiktok", "instagram", "twitter"}
                        and not (monitor_acc.storage_state
                                 or monitor_acc.creator_storage_state
                                 or monitor_acc.cookie)):
                    raise HTTPException(400, f"所选 {platform_name} 账号缺少登录会话，请重新登录")
            elif body.account_id:
                monitor_acc = s.get(DouyinAccount, body.account_id)
                if not monitor_acc or monitor_acc.platform != platform:
                    raise HTTPException(400, "所选账号不存在或与监控平台不匹配")
            if kind == "keyword":
                dup = s.exec(select(MonitorTarget).where(MonitorTarget.platform == platform)
                             .where(MonitorTarget.keyword == keyword)).first()
                if dup:
                    raise HTTPException(409, "该监控目标已存在")
            else:
                _ensure_creator_monitor_available(
                    s, platform=platform, sec_uid=sec_uid, account_id=body.account_id)
            q = body.video_quality.strip()
            if q and q not in metadata_service.QUALITY_CHOICES:
                raise HTTPException(400, f"画质取值无效: {q}")
            backfill_count = (app_runtime.cfg.engine.monitor_initial_backfill_count
                              if body.initial_backfill_count is None
                              else body.initial_backfill_count)
            if backfill_count < -1 or backfill_count > 1000:
                raise HTTPException(400, "首次回填数须为 -1(尽可能全量)或 0~1000")
            t = MonitorTarget(platform=platform, target_kind=kind, keyword=keyword,
                              sec_uid=sec_uid, xsec_token=xsec_token,
                              nickname=("#" + keyword) if kind == "keyword" else nickname,
                              avatar=avatar,
                              alias=metadata_service._meta_text(body.alias, 60),
                              group_name=metadata_service._meta_text(body.group_name, 40),
                              tags=metadata_service._dump_meta_tags(metadata_service._meta_tags(body.tags)),
                              account_id=body.account_id or None,
                              interval_seconds=body.interval_seconds, download_dir=dl,
                              initial_backfill_count=backfill_count, video_quality=q,
                              download_enabled=body.download_enabled,
                              media_filter=body.media_filter,
                              max_scrolls=body.max_scrolls,
                              max_items_per_scan=body.max_items_per_scan,
                              record_media_filter=body.record_media_filter,
                              min_like_count=body.min_like_count,
                              min_comment_count=body.min_comment_count,
                              recent_days=body.recent_days,
                              include_keywords=metadata_service._dump_meta_tags(
                                  metadata_service._meta_tags(body.include_keywords)),
                              exclude_keywords=metadata_service._dump_meta_tags(
                                  metadata_service._meta_tags(body.exclude_keywords)))
            s.add(t); s.flush()
            return _target_dict(t)

        payload, _created = submit_once(s, request=request, scope="monitor", body=body, create=create)
        return payload


@router.put("/api/monitors/{tid}")
async def update_monitor(tid: int, body: TargetUpdate):
    with get_session() as s:
        t = s.get(MonitorTarget, tid)
        if not t:
            raise HTTPException(404)
        if body.download_dir is not None:
            dl = body.download_dir.strip()
            if dl:
                try:
                    Path(dl).expanduser().mkdir(parents=True, exist_ok=True)
                except Exception as e:
                    raise HTTPException(400, f"下载目录不可用: {e}")
            t.download_dir = dl
        if body.interval_seconds is not None:
            if not 1 <= body.interval_seconds <= 86400:
                raise HTTPException(400, "监控间隔须为 1~86400 秒")
            t.interval_seconds = body.interval_seconds
        if body.initial_backfill_count is not None:
            if body.initial_backfill_count < -1 or body.initial_backfill_count > 1000:
                raise HTTPException(400, "首次回填数须为 -1 或 0~1000")
            if (body.initial_backfill_count != t.initial_backfill_count
                    and t.last_scan_at is not None):
                has_contents = s.exec(
                    select(ContentRecord.id)
                    .where(ContentRecord.target_id == t.id)
                    .limit(1)
                ).first() is not None
                if has_contents:
                    raise HTTPException(400, "首次历史回填仅能在抓到作品前修改")
            t.initial_backfill_count = body.initial_backfill_count
        if body.video_quality is not None:
            q = body.video_quality.strip()
            if q and q not in metadata_service.QUALITY_CHOICES:
                raise HTTPException(400, f"画质取值无效: {q}")
            t.video_quality = q
        if body.download_enabled is not None:
            t.download_enabled = body.download_enabled
        if body.media_filter is not None:
            if body.media_filter not in ("all", "video", "images"):
                raise HTTPException(400, "媒体筛选须为 all、video 或 images")
            t.media_filter = body.media_filter
        _validate_monitor_strategy(
            max_scrolls=body.max_scrolls,
            max_items_per_scan=body.max_items_per_scan,
            record_media_filter=body.record_media_filter,
            min_like_count=body.min_like_count,
            min_comment_count=body.min_comment_count,
            recent_days=body.recent_days)
        if body.max_scrolls is not None:
            t.max_scrolls = body.max_scrolls
        if body.max_items_per_scan is not None:
            t.max_items_per_scan = body.max_items_per_scan
        if body.record_media_filter is not None:
            t.record_media_filter = body.record_media_filter
        if body.min_like_count is not None:
            t.min_like_count = body.min_like_count
        if body.min_comment_count is not None:
            t.min_comment_count = body.min_comment_count
        if body.recent_days is not None:
            t.recent_days = body.recent_days
        if body.include_keywords is not None:
            t.include_keywords = metadata_service._dump_meta_tags(metadata_service._meta_tags(body.include_keywords))
        if body.exclude_keywords is not None:
            t.exclude_keywords = metadata_service._dump_meta_tags(metadata_service._meta_tags(body.exclude_keywords))
        if body.account_id is not None:
            acc = s.get(DouyinAccount, body.account_id)
            if not acc or acc.platform != t.platform or acc.status != "active":
                raise HTTPException(400, "账号不存在、登录态失效或与监控平台不匹配")
            if (t.platform in {"tiktok", "instagram", "twitter"}
                    and not (acc.storage_state or acc.creator_storage_state or acc.cookie)):
                pname_t = "Instagram" if t.platform == "instagram" else "Twitter" if t.platform == "twitter" else "TikTok"
                raise HTTPException(400, f"所选 {pname_t} 账号缺少登录会话，请重新登录")
            if t.target_kind != "keyword":
                _ensure_creator_monitor_available(
                    s, platform=t.platform, sec_uid=t.sec_uid,
                    account_id=body.account_id, exclude_id=t.id)
            t.account_id = body.account_id
        if body.alias is not None:
            t.alias = metadata_service._meta_text(body.alias, 60)
        if body.group_name is not None:
            t.group_name = metadata_service._meta_text(body.group_name, 40)
        if body.tags is not None:
            t.tags = metadata_service._dump_meta_tags(metadata_service._meta_tags(body.tags))
        s.add(t); s.commit(); s.refresh(t)
        return _target_dict(t)


@router.get("/api/monitors")
async def list_monitors(platform: str | None = None):
    with get_session() as s:
        q = select(MonitorTarget)
        if platform:
            q = q.where(MonitorTarget.platform == platform)
        ts = s.exec(q).all()
        counts = dict(s.exec(select(ContentRecord.target_id, func.count(ContentRecord.id))
                             .where(ContentRecord.target_id.in_([t.id for t in ts]))
                             .group_by(ContentRecord.target_id)).all()) if ts else {}
        out = []
        for t in ts:
            d = _target_dict(t)
            d["content_count"] = counts.get(t.id, 0)
            out.append(d)
        return out


@router.post("/api/monitors/{tid}/toggle")
async def toggle_monitor(tid: int):
    with get_session() as s:
        t = s.get(MonitorTarget, tid)
        if not t:
            raise HTTPException(404)
        t.enabled = not t.enabled
        s.add(t); s.commit()
        return {"enabled": t.enabled}


@router.post("/api/monitors/{tid}/run-now")
async def run_now(tid: int):
    if not app_runtime.engine:
        raise HTTPException(503, "引擎未就绪")
    return await app_runtime.engine.scan_target(tid)


@router.delete("/api/monitors/{tid}")
async def del_monitor(tid: int):
    with get_session() as s:
        t = s.get(MonitorTarget, tid)
        if t:
            s.delete(t); s.commit()
    return {"ok": True}


@router.get("/api/monitors/{tid}/contents")
async def target_contents(tid: int):
    with get_session() as s:
        rows = s.exec(select(ContentRecord)
                      .where(ContentRecord.target_id == tid)
                      .order_by(ContentRecord.create_time.desc())).all()
        return contents_service._content_records(s, rows)


def _target_dict(t: MonitorTarget) -> dict:
    nickname = (tiktok_display_text(t.nickname)
                if t.platform == "tiktok" else t.nickname)
    if t.platform in {"tiktok", "instagram", "twitter"} and not nickname and t.sec_uid and not t.sec_uid.isdigit():
        nickname = f"@{t.sec_uid}"
    return {
        "id": t.id, "platform": t.platform, "target_kind": t.target_kind,
        "keyword": t.keyword,
        "sec_uid": t.sec_uid, "nickname": nickname, "avatar": t.avatar,
        "alias": t.alias, "group_name": t.group_name,
        "tags": metadata_service._load_meta_tags(t.tags),
        "enabled": t.enabled, "interval_seconds": t.interval_seconds,
        "initial_backfill_count": t.initial_backfill_count,
        "download_dir": t.download_dir, "video_quality": t.video_quality,
        "download_enabled": t.download_enabled, "media_filter": t.media_filter,
        "max_scrolls": t.max_scrolls,
        "max_items_per_scan": t.max_items_per_scan,
        "record_media_filter": t.record_media_filter,
        "min_like_count": t.min_like_count,
        "min_comment_count": t.min_comment_count,
        "recent_days": t.recent_days,
        "include_keywords": metadata_service._load_meta_tags(t.include_keywords),
        "exclude_keywords": metadata_service._load_meta_tags(t.exclude_keywords),
        "account_id": t.account_id,
        "last_scan_at": t.last_scan_at.isoformat() if t.last_scan_at else None,
        "next_auto_run_at": utc_iso(row_deadline(t, app_runtime.cfg, kind="monitor")),
        "last_error": t.last_error,
    }
