"""评论监控目标管理与已采集评论查询清理接口。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field as PydanticField
from sqlalchemy import func, or_
from sqlmodel import select

from app import runtime as app_runtime
from app.db import get_session
from app.engine.cadence import row_deadline
from app.models import CommentRecord, CommentWatch, DouyinAccount
from app.platforms.bilibili import looks_like_bili_work, resolve_bili_mid, resolve_bili_work
from app.platforms.channels import looks_like_photo as channels_looks_like_photo, parse_channels_url, resolve_channels_photo_id
from app.platforms.douyin import resolve_aweme_id, resolve_sec_uid
from app.platforms.instagram import looks_like_ig_work, resolve_ig_user_target, resolve_ig_work_ref
from app.platforms.kuaishou import looks_like_photo as ks_looks_like_photo, resolve_ks_photo_id, resolve_ks_user_id
from app.platforms.rednote import looks_like_rednote_note, resolve_rednote_note, resolve_rednote_user
from app.platforms.twitter import looks_like_twitter_tweet, resolve_twitter_target, resolve_twitter_user_target
from app.platforms.xhs import looks_like_note as xhs_looks_like_note, resolve_note as xhs_resolve_note, resolve_user as xhs_resolve_user
from app.scheduling import utc_iso
from app.services import collections as collections_service
from app.services import contents as contents_service
from app.services import metadata as metadata_service
from app.services import watch_records as watch_records_service

router = APIRouter()


# ─────────── 评论监控(独立实体)───────────
class WatchIn(BaseModel):
    url_or_id: str                       # 视频/笔记链接、主页链接、id
    platform: str = "douyin"            # douyin | xhs
    kind: str = "auto"                  # auto | video(单条视频/笔记) | user(账号/创作者)
    mode: str = "public"               # public | creator(仅抖音 user)
    account_id: int | None = None
    interval_seconds: int = 600
    recent_works: int = 0
    recent_days: int = 0
    max_scrolls: int = 0
    alias: str = ""
    group_name: str = ""
    tags: list[str] = PydanticField(default_factory=list)


class WatchUpdate(BaseModel):
    enabled: bool | None = None
    interval_seconds: int | None = None
    mode: str | None = None
    account_id: int | None = None
    recent_works: int | None = None
    recent_days: int | None = None
    max_scrolls: int | None = None
    alias: str | None = None
    group_name: str | None = None
    tags: list[str] | None = None


def _watch_dict(w: CommentWatch) -> dict:
    return {
        "id": w.id, "platform": w.platform,
        "kind": w.kind, "aweme_id": w.aweme_id, "sec_uid": w.sec_uid,
        "title": w.title, "avatar": w.avatar, "mode": w.mode,
        "alias": w.alias, "group_name": w.group_name,
        "tags": metadata_service._load_meta_tags(w.tags),
        "account_id": w.account_id, "interval_seconds": w.interval_seconds,
        "recent_works": w.recent_works, "recent_days": w.recent_days,
        "max_scrolls": w.max_scrolls,
        "enabled": w.enabled, "comment_count": w.comment_count,
        "last_scan_at": w.last_scan_at.isoformat() if w.last_scan_at else None,
        "next_auto_run_at": utc_iso(row_deadline(w, app_runtime.cfg, kind="comment_watch")),
        "last_error": w.last_error,
    }


@router.get("/api/comment-watches")
async def list_watches(platform: str | None = None):
    with get_session() as s:
        q = select(CommentWatch)
        if platform:
            q = q.where(CommentWatch.platform == platform)
        watches = s.exec(q).all()
        counts = dict(s.exec(select(CommentRecord.watch_id, func.count(CommentRecord.id))
                             .where(CommentRecord.watch_id.in_([w.id for w in watches]))
                             .group_by(CommentRecord.watch_id)).all()) if watches else {}
        return [dict(_watch_dict(w), comment_count=counts.get(w.id, 0)) for w in watches]


@router.post("/api/comment-watches")
async def add_watch(body: WatchIn):
    platform = body.platform if body.platform in ("douyin", "xhs", "rednote", "kuaishou", "shipinhao", "tiktok", "bilibili", "instagram", "twitter") else "douyin"
    aweme_id = sec_uid = xsec_token = ""
    title = ""
    target_input = metadata_service._clean_platform_target_input(body.url_or_id, platform)

    if not 1 <= body.interval_seconds <= 86400:
        raise HTTPException(400, "监控间隔须为 1~86400 秒")
    if not 0 <= body.recent_works <= 50:
        raise HTTPException(400, "近期作品数须为 0~50，0 表示跟随全局设置")
    if not 0 <= body.recent_days <= 365:
        raise HTTPException(400, "近期天数须为 0~365，0 表示跟随全局设置")
    if not 0 <= body.max_scrolls <= 50:
        raise HTTPException(400, "抓取深度须为 0~50，0 表示跟随全局设置")
    eff_proxy = metadata_service._resolve_ephemeral_proxy(platform, body.account_id)
    if platform == "xhs":
        kind = body.kind
        if kind == "auto":
            note_ref = None
            if xhs_looks_like_note(target_input) or "://" in target_input:
                note_ref = await metadata_service._call_resolver(
                    xhs_resolve_note, target_input, app_runtime.cfg.engine.user_agent, proxy=eff_proxy
                )
            if note_ref:
                kind = "video"
                aweme_id, xsec_token = note_ref.note_id, note_ref.xsec_token
            else:
                kind = "user"
        if kind == "video":
            if not aweme_id:
                ref = await metadata_service._call_resolver(
                    xhs_resolve_note, target_input, app_runtime.cfg.engine.user_agent, proxy=eff_proxy
                )
                if not ref:
                    raise HTTPException(400, "无法解析小红书笔记,请粘贴完整分享文案、explore 笔记链接 / xhslink 短链 / 24 位 note_id")
                aweme_id, xsec_token = ref.note_id, ref.xsec_token
            title = "笔记 " + aweme_id
        else:
            ref = await metadata_service._call_resolver(
                xhs_resolve_user, target_input, app_runtime.cfg.engine.user_agent, proxy=eff_proxy
            )
            if not ref:
                raise HTTPException(400, "无法解析小红书创作者,请粘贴完整分享文案、主页链接 / xhslink 短链 / 24 位 user_id")
            sec_uid, xsec_token = ref.user_id, ref.xsec_token
        mode = "public"
    elif platform == "rednote":
        kind = body.kind
        if kind == "auto":
            note_ref = None
            if looks_like_rednote_note(target_input) or "://" in target_input:
                note_ref = await metadata_service._call_resolver(
                    resolve_rednote_note, target_input, app_runtime.cfg.engine.user_agent, proxy=eff_proxy
                )
            if note_ref:
                kind = "video"
                aweme_id, xsec_token = note_ref.note_id, note_ref.xsec_token
            else:
                kind = "user"
        if kind == "video":
            if not aweme_id:
                ref = await metadata_service._call_resolver(
                    resolve_rednote_note, target_input, app_runtime.cfg.engine.user_agent, proxy=eff_proxy
                )
                if not ref:
                    raise HTTPException(400, "无法解析海外小红书笔记,请粘贴 www.rednote.com/zh/discovery/item/... 笔记链接或 24 位 note_id")
                aweme_id, xsec_token = ref.note_id, ref.xsec_token
            title = "笔记 " + aweme_id
        else:
            ref = await metadata_service._call_resolver(
                resolve_rednote_user, target_input, app_runtime.cfg.engine.user_agent, proxy=eff_proxy
            )
            if not ref:
                raise HTTPException(400, "无法解析海外小红书创作者,请粘贴 www.rednote.com/zh/user/profile/... 主页链接或 24 位 user_id")
            sec_uid, xsec_token = ref.user_id, ref.xsec_token
        mode = "public"
    elif platform == "kuaishou":
        kind = body.kind
        if kind == "auto":
            if ks_looks_like_photo(target_input) or "://" in target_input:
                aweme_id = await metadata_service._call_resolver(
                    resolve_ks_photo_id, target_input, app_runtime.cfg.engine.user_agent, proxy=eff_proxy
                ) or ""
            kind = "video" if aweme_id else "user"
        if kind == "video":
            if not aweme_id:
                aweme_id = await metadata_service._call_resolver(
                    resolve_ks_photo_id, target_input, app_runtime.cfg.engine.user_agent, proxy=eff_proxy
                )
            if not aweme_id:
                raise HTTPException(400, "无法解析快手作品 id,请粘贴完整分享文案、作品链接 / v.kuaishou.com 短链 / photo_id")
            title = "作品 " + aweme_id
        else:
            sec_uid = await metadata_service._call_resolver(
                resolve_ks_user_id, target_input, app_runtime.cfg.engine.user_agent, proxy=eff_proxy
            )
            if not sec_uid:
                raise HTTPException(400, "无法解析快手 user_id,请粘贴完整分享文案、主页链接 / 短链 / user_id")
        mode = "public"
    elif platform == "shipinhao":
        kind = body.kind if body.kind in {"auto", "video", "user"} else "auto"
        parsed_ref = parse_channels_url(target_input)
        if kind == "auto":
            if parsed_ref is not None and parsed_ref.kind == "creator":
                kind = "user"
            elif parsed_ref is not None or channels_looks_like_photo(target_input):
                kind = "video"
            else:
                kind = "user"
        if kind == "video":
            if parsed_ref is not None and parsed_ref.object_nonce_id:
                xsec_token = parsed_ref.object_nonce_id
            aweme_id = await metadata_service._call_resolver(
                resolve_channels_photo_id, target_input, app_runtime.cfg.engine.user_agent, proxy=eff_proxy
            ) or ""
            if not aweme_id and parsed_ref is not None:
                aweme_id = (
                    parsed_ref.object_id
                    or parsed_ref.export_id
                    or (f"sph:{parsed_ref.share_id}" if parsed_ref.share_id else "")
                )
            if not aweme_id:
                raise HTTPException(
                    400,
                    "无法解析视频号作品，请粘贴 weixin.qq.com/sph/... 分享链、"
                    "channels.weixin.qq.com/web/pages/feed 链接或十进制 objectId",
                )
            title = "视频号作品 " + aweme_id
        else:
            channels_res = await collections_service._resolve_channels_creator_target(target_input)
            sec_uid = channels_res.username
            if channels_res.nickname and not title:
                title = channels_res.nickname
        mode = "public"
    elif platform == "tiktok":
        target_input, video_ref, profile_id = await metadata_service._resolve_tiktok_target(
            target_input, proxy=eff_proxy, account_id=body.account_id
        )
        kind = body.kind if body.kind in {"auto", "video", "user"} else "auto"
        if kind == "auto":
            kind = "video" if video_ref else "user"
        if kind == "video":
            if not video_ref:
                raise HTTPException(400, "无法解析 TikTok 作品，请粘贴包含作者 @username 的完整作品链接或短链")
            aweme_id = video_ref.aweme_id
            sec_uid = video_ref.author_unique_id
            title = f"@{sec_uid} 视频 {aweme_id}"
        else:
            sec_uid = profile_id or (video_ref.author_unique_id if video_ref else "")
            if not sec_uid:
                raise HTTPException(400, "无法解析 TikTok @username，请粘贴完整主页/作品链接、短链或 @username")
        mode = "public"
    elif platform == "bilibili":
        kind = body.kind if body.kind in {"auto", "video", "user"} else "auto"
        if kind == "auto":
            if looks_like_bili_work(target_input):
                aweme_id = await metadata_service._call_resolver(
                    resolve_bili_work, target_input, app_runtime.cfg.engine.user_agent, proxy=eff_proxy
                ) or ""
                kind = "video" if aweme_id else "user"
            else:
                sec_uid = await metadata_service._call_resolver(
                    resolve_bili_mid, target_input, app_runtime.cfg.engine.user_agent, proxy=eff_proxy
                ) or ""
                if sec_uid:
                    kind = "user"
                else:
                    aweme_id = await metadata_service._call_resolver(
                        resolve_bili_work, target_input, app_runtime.cfg.engine.user_agent, proxy=eff_proxy
                    ) or ""
                    kind = "video" if aweme_id else "user"
        if kind == "video":
            if not aweme_id:
                aweme_id = await metadata_service._call_resolver(
                    resolve_bili_work, target_input, app_runtime.cfg.engine.user_agent, proxy=eff_proxy
                ) or ""
            if not aweme_id:
                raise HTTPException(400, "无法解析哔哩哔哩稿件，请粘贴 BV/av 号、视频/动态链接或 b23.tv 短链")
            title = "B站稿件 " + aweme_id
        else:
            if not sec_uid:
                sec_uid = await metadata_service._call_resolver(
                    resolve_bili_mid, target_input, app_runtime.cfg.engine.user_agent, proxy=eff_proxy
                ) or ""
            if not sec_uid:
                raise HTTPException(400, "无法解析哔哩哔哩 UP 主 UID，请粘贴空间链接、b23.tv 短链或纯数字 UID")
            title = "UP主 " + sec_uid
        mode = body.mode if body.mode in ("public", "creator") else "public"
    elif platform == "instagram":
        kind = body.kind if body.kind in {"auto", "video", "user"} else "auto"
        work_ref = resolve_ig_work_ref(target_input) if looks_like_ig_work(target_input) else None
        if kind == "auto":
            kind = "video" if work_ref else "user"
        if kind == "video":
            if work_ref is None:
                work_ref = resolve_ig_work_ref(target_input)
            if work_ref is None or not work_ref.shortcode:
                raise HTTPException(400, "无法解析 Instagram 帖子/Reels，请粘贴包含 /p/<code>/ 或 /reel/<code>/ 的链接或数字 media_id")
            aweme_id = work_ref.shortcode
            title = f"IG {work_ref.kind} {aweme_id}"
        else:
            if looks_like_ig_work(target_input):
                raise HTTPException(400, "检测到 Instagram 帖子/Reels 链接；创作者评论监控请粘贴 @username 或主页链接")
            sec_uid = resolve_ig_user_target(target_input)
            if not sec_uid:
                raise HTTPException(400, "无法解析 Instagram 创作者 @username，请粘贴主页链接、@username 或数字 UID")
            title = f"@{sec_uid}" if not sec_uid.isdigit() else f"IG 用户 {sec_uid}"
        mode = "public"
    elif platform == "twitter":
        expanded_tw, tw_ref, tw_user = await metadata_service._call_resolver(
            resolve_twitter_target,
            target_input,
            user_agent=app_runtime.cfg.engine.user_agent,
            proxy=eff_proxy,
        )
        kind = body.kind if body.kind in {"auto", "video", "user"} else "auto"
        if kind == "auto":
            kind = "video" if tw_ref else "user"
        if kind == "video":
            if tw_ref is None or not tw_ref.tweet_id:
                raise HTTPException(400, "无法解析 X (Twitter) 推文，请粘贴 x.com/<user>/status/<id> 链接、t.co 短链或推文数字 ID")
            aweme_id = tw_ref.tweet_id
            sec_uid = tw_ref.screen_name
            title = f"@{sec_uid} 推文 {aweme_id}" if sec_uid else f"推文 {aweme_id}"
        else:
            if looks_like_twitter_tweet(expanded_tw):
                raise HTTPException(400, "检测到 X (Twitter) 推文链接；创作者评论监控请粘贴 @username 或 x.com/<username> 主页链接")
            sec_uid = tw_user or resolve_twitter_user_target(expanded_tw)
            if not sec_uid:
                raise HTTPException(400, "无法解析 X (Twitter) 创作者 @username，请粘贴 x.com/<username> 主页链接、@username 或数字 UID")
            title = f"@{sec_uid}" if not sec_uid.isdigit() else f"X 用户 {sec_uid}"
        mode = "public"
    else:
        kind = body.kind
        if kind == "auto":
            aweme_id = await metadata_service._call_resolver(
                resolve_aweme_id, target_input, app_runtime.cfg.engine.user_agent, proxy=eff_proxy
            ) or ""
            kind = "video" if aweme_id else "user"
        if kind == "video":
            if not aweme_id:
                aweme_id = await metadata_service._call_resolver(
                    resolve_aweme_id, target_input, app_runtime.cfg.engine.user_agent, proxy=eff_proxy
                )
            if not aweme_id:
                raise HTTPException(400, "无法解析视频 id,请粘贴完整分享文案、作品链接 / 短链 / 数字 id")
            title = "视频 " + aweme_id
        else:
            sec_uid = await metadata_service._call_resolver(
                resolve_sec_uid, target_input, app_runtime.cfg.engine.user_agent, proxy=eff_proxy
            )
            if not sec_uid:
                raise HTTPException(400, "无法解析 sec_uid,请粘贴完整分享文案、主页链接 / 短链 / sec_uid")
        mode = body.mode if body.mode in ("public", "creator") else "public"
        if mode == "creator":
            if kind != "user":
                raise HTTPException(400, "创作中心模式只能用于「账号」类型")
            with get_session() as s:
                acc = s.get(DouyinAccount, body.account_id) if body.account_id else None
                has_creator = bool(acc and acc.creator_storage_state)
            if not has_creator:
                raise HTTPException(400, "创作中心模式需要选择一个已“创作者登录”的账号")

    with get_session() as s:
        if body.account_id:
            watch_acc = s.get(DouyinAccount, body.account_id)
            if (
                watch_acc is not None
                and mode == "public"
                and str(watch_acc.account_role or "hybrid").strip().lower() == "publish"
            ):
                raise HTTPException(
                    400,
                    "该账号角色设为「发布主号 (publish)」，禁止绑定公开作品评论轮询采集；请使用「创作中心模式」或改选「仅采集号 (crawler)」/「混合号 (hybrid)」",
                )
        if platform in {"tiktok", "instagram", "twitter"}:
            pname = "Instagram" if platform == "instagram" else "Twitter" if platform == "twitter" else "TikTok"
            account = s.get(DouyinAccount, body.account_id) if body.account_id else None
            if not account or account.platform != platform or account.status != "active":
                raise HTTPException(400, f"{pname} 评论监控必须选择一个登录有效的 {pname} 账号")
            if not (account.storage_state or account.creator_storage_state or account.cookie):
                raise HTTPException(400, f"所选 {pname} 账号缺少登录会话，请重新登录")
        if kind == "video":
            dup = s.exec(select(CommentWatch).where(CommentWatch.platform == platform)
                         .where(CommentWatch.aweme_id == aweme_id)).first()
        else:
            dup = s.exec(select(CommentWatch).where(CommentWatch.platform == platform)
                         .where(CommentWatch.sec_uid == sec_uid)
                         .where(CommentWatch.mode == mode)).first()
        if dup:
            raise HTTPException(409, "已存在相同的评论监控")
        w = CommentWatch(platform=platform, kind=kind, aweme_id=aweme_id, sec_uid=sec_uid,
                          xsec_token=xsec_token, mode=mode, account_id=body.account_id,
                          interval_seconds=body.interval_seconds, title=title,
                          recent_works=body.recent_works,
                          recent_days=body.recent_days,
                          max_scrolls=body.max_scrolls,
                          alias=metadata_service._meta_text(body.alias, 60),
                          group_name=metadata_service._meta_text(body.group_name, 40),
                          tags=metadata_service._dump_meta_tags(metadata_service._meta_tags(body.tags)))
        s.add(w); s.commit(); s.refresh(w)
        return _watch_dict(w)


@router.put("/api/comment-watches/{wid}")
async def update_watch(wid: int, body: WatchUpdate):
    with get_session() as s:
        w = s.get(CommentWatch, wid)
        if not w:
            raise HTTPException(404)
        if body.interval_seconds is not None and not 1 <= body.interval_seconds <= 86400:
            raise HTTPException(400, "监控间隔须为 1~86400 秒")
        if body.enabled is not None:
            w.enabled = body.enabled
        if body.interval_seconds is not None:
            w.interval_seconds = body.interval_seconds
        if body.recent_works is not None:
            if not 0 <= body.recent_works <= 50:
                raise HTTPException(400, "近期作品数须为 0~50")
            w.recent_works = body.recent_works
        if body.recent_days is not None:
            if not 0 <= body.recent_days <= 365:
                raise HTTPException(400, "近期天数须为 0~365")
            w.recent_days = body.recent_days
        if body.max_scrolls is not None:
            if not 0 <= body.max_scrolls <= 50:
                raise HTTPException(400, "抓取深度须为 0~50")
            w.max_scrolls = body.max_scrolls
        if body.mode is not None and body.mode not in ("public", "creator"):
            raise HTTPException(400, "评论来源须为 public 或 creator")
        new_mode = body.mode if body.mode is not None else w.mode
        new_account_id = body.account_id if body.account_id is not None else w.account_id
        if body.account_id is not None:
            acc = s.get(DouyinAccount, body.account_id)
            if not acc or acc.platform != w.platform or acc.status != "active":
                raise HTTPException(400, "账号不存在、登录态失效或与评论监控平台不匹配")
            if (w.platform in {"tiktok", "instagram", "twitter"}
                    and not (acc.storage_state or acc.creator_storage_state or acc.cookie)):
                pname_w = "Instagram" if w.platform == "instagram" else "Twitter" if w.platform == "twitter" else "TikTok"
                raise HTTPException(400, f"所选 {pname_w} 账号缺少登录会话，请重新登录")
            w.account_id = body.account_id
        if new_mode == "creator":
            if w.platform != "douyin" or w.kind != "user":
                raise HTTPException(400, "创作中心模式仅支持抖音账号类型评论监控")
            acc = s.get(DouyinAccount, new_account_id) if new_account_id else None
            if not acc or not acc.creator_storage_state:
                raise HTTPException(400, "创作中心模式需要绑定已完成创作者登录的抖音账号")
        if body.mode is not None:
            w.mode = new_mode
        if body.alias is not None:
            w.alias = metadata_service._meta_text(body.alias, 60)
        if body.group_name is not None:
            w.group_name = metadata_service._meta_text(body.group_name, 40)
        if body.tags is not None:
            w.tags = metadata_service._dump_meta_tags(metadata_service._meta_tags(body.tags))
        s.add(w); s.commit(); s.refresh(w)
        return _watch_dict(w)


@router.delete("/api/comment-watches/{wid}")
async def del_watch(wid: int, with_comments: bool = False):
    with get_session() as s:
        w = s.get(CommentWatch, wid)
        if not w:
            return {"ok": True}
        if with_comments:
            for c in s.exec(select(CommentRecord).where(CommentRecord.watch_id == wid)).all():
                s.delete(c)
        s.delete(w); s.commit()
    return {"ok": True}


@router.post("/api/comment-watches/{wid}/scan-now")
async def scan_watch_now(wid: int):
    if not app_runtime.engine:
        raise HTTPException(503, "引擎未就绪")
    return await app_runtime.engine.scan_comment_watch(wid)


@router.get("/api/comments")
async def list_comments(limit: int = 100, watch_id: int | None = None,
                        aweme_id: str | None = None, platform: str | None = None,
                        group_name: str = "", tag: str = "", q: str = "",
                        ip_label: str = "",
                        reply_type: str = "", min_like_count: int = 0,
                        sort: str = "latest", page: int = 1,
                        page_size: int = 10, paginate: bool = False,
                        captured_from: datetime | None = None,
                        captured_before: datetime | None = None):
    """Return captured comments with optional SQL filters and pagination."""
    limit = max(1, min(limit, 1000))
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    start, end = contents_service._content_capture_bounds(captured_from, captured_before)
    with get_session() as s:
        selected_source = watch_records_service._selected_watch_source(s, CommentWatch, watch_id, platform)
        stmt = watch_records_service._watch_record_filter(select(CommentRecord), CommentRecord, watch_id)
        stmt = metadata_service._report_window(stmt, CommentRecord, start, end)
        if platform is not None:
            stmt = stmt.where(CommentRecord.platform == platform)
        if aweme_id is not None:
            stmt = stmt.where(CommentRecord.aweme_id == aweme_id)
        text_query = q.strip()
        region_query = ip_label.strip()[:64]
        if text_query:
            stmt = stmt.where(or_(CommentRecord.text.contains(text_query),
                                  CommentRecord.user_nickname.contains(text_query),
                                  CommentRecord.user_sec_uid.contains(text_query),
                                  CommentRecord.ip_label.contains(text_query),
                                  CommentRecord.aweme_id.contains(text_query)))
        if region_query:
            stmt = stmt.where(CommentRecord.ip_label.contains(region_query))
        if reply_type == "top":
            stmt = stmt.where(CommentRecord.reply_to == "")
        elif reply_type == "reply":
            stmt = stmt.where(CommentRecord.reply_to != "")
        if min_like_count > 0:
            stmt = stmt.where(CommentRecord.like_count >= min_like_count)
        group_name, tag = metadata_service._meta_text(group_name, 40), metadata_service._meta_text(tag, 24)
        if group_name or tag:
            watch_query = select(CommentWatch)
            if platform:
                watch_query = watch_query.where(CommentWatch.platform == platform)
            watches = s.exec(watch_query).all()
            eligible_ids = [w.id for w in watches if w.id is not None
                            and metadata_service._meta_matches(w, group_name, tag)]
            if not eligible_ids:
                if not paginate:
                    return []
                return {"items": [], "total": 0, "page": page,
                        "page_size": page_size, "pages": 1,
                        "has_prev": page > 1, "has_next": False, "watch_source": selected_source}
            stmt = stmt.where(CommentRecord.watch_id.in_(eligible_ids))
        ordering = watch_records_service._watch_record_ordering(CommentRecord, sort)
        if not paginate:
            rows = s.exec(stmt.order_by(*ordering).limit(limit)).all()
            return watch_records_service._watch_records(s, rows, CommentWatch)

        total = int(s.exec(select(func.count()).select_from(stmt.subquery())).one())
        pages = max(1, (total + page_size - 1) // page_size)
        rows = s.exec(stmt.order_by(*ordering)
                      .offset((page - 1) * page_size)
                      .limit(page_size)).all()
        return {
            "items": watch_records_service._watch_records(s, rows, CommentWatch), "watch_source": selected_source,
            "total": total, "page": page, "page_size": page_size,
            "pages": pages, "has_prev": page > 1, "has_next": page < pages,
        }


@router.delete("/api/comments/{cmid}")
async def del_comment(cmid: int):
    with get_session() as s:
        c = s.get(CommentRecord, cmid)
        if c:
            s.delete(c); s.commit()
    return {"ok": True}


@router.post("/api/comments/batch-delete")
async def batch_del_comments(body: contents_service.IdsIn):
    deleted = 0
    with get_session() as s:
        for cid in body.ids:
            c = s.get(CommentRecord, cid)
            if c:
                s.delete(c); deleted += 1
        s.commit()
    return {"ok": True, "deleted": deleted}


@router.delete("/api/comments")
async def clear_comments(watch_id: int | None = None):
    with get_session() as s:
        q = select(CommentRecord)
        if watch_id is not None:
            q = q.where(CommentRecord.watch_id == watch_id)
        rows = s.exec(q).all()
        for c in rows:
            s.delete(c)
        s.commit()
        return {"ok": True, "deleted": len(rows)}
