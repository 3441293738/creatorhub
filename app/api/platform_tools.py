"""TikTok/Twitter/Facebook/YouTube 专有协议探测、实体搜索/互动及小红书/B站创作辅助元数据接口。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field as PydanticField

from app import runtime as app_runtime
from app.browser import (
    do_facebook_follow,
    do_facebook_group_membership,
    do_facebook_like,
    do_facebook_share,
    do_twitter_like,
    do_twitter_retweet,
    do_youtube_change_profile_name,
    do_youtube_like_comment,
    do_youtube_like_video,
    do_youtube_subscribe,
    do_youtube_warmup,
    do_youtube_watch_video,
    fetch_facebook_entities_search,
    fetch_facebook_groups,
    fetch_facebook_pages,
    fetch_twitter_users,
    fetch_youtube_channels_search,
    fetch_youtube_managed_channels,
    fetch_youtube_search,
    post_youtube_comment,
)
from app.browser.tiktok_fetcher import fetch_tiktok_users, probe_tiktok_protocol
from app.db import get_session
from app.models import CommentTask, DouyinAccount
from app.operations import record_account_operation
from app.platforms.bilibili import CREATION_STATEMENT_OPTIONS as BILI_CREATION_STATEMENT_OPTIONS, FALLBACK_PARTITIONS as BILI_FALLBACK_PARTITIONS, BilibiliClient
from app.platforms.facebook import FacebookClient, normalize_facebook_url, resolve_facebook_post_ref
from app.platforms.tiktok import TikTokNativeClient, native_protocol_summary
from app.platforms.tiktok.urls import parse_video_reference
from app.platforms.twitter import resolve_twitter_tweet_ref
from app.services import account_profiles as account_profiles_service
from app.platforms.youtube import YouTubeClient, normalize_youtube_url, resolve_youtube_video_ref
from app.risk import OperationKind
from app.services import metadata as metadata_service

router = APIRouter()


class TikTokUserSearchIn(BaseModel):
    account_id: int
    keyword: str
    max_results: int = PydanticField(default=50, ge=1, le=200)


class TikTokProtocolProbeIn(BaseModel):
    account_id: int
    keyword: str = "claude"
    timeout_ms: int = PydanticField(default=20000, ge=3000, le=45000)
    mode: str = "auto"


class TikTokCommentTaskIn(BaseModel):
    account_id: int
    aweme_id: str = ""
    author_unique_id: str = ""
    content: str
    target_comment_id: str = ""
    target_text: str = ""
    target_nick: str = ""
    run_now: bool = False


@router.post("/api/tiktok/users/search")
async def search_tiktok_users(body: TikTokUserSearchIn):
    keyword = body.keyword.strip()
    if not keyword:
        raise HTTPException(422, "请输入用户关键词")
    if app_runtime.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    with get_session() as session:
        account = session.get(DouyinAccount, body.account_id)
        if (not account or account.platform != "tiktok"
                or account.status != "active" or not account.storage_state):
            raise HTTPException(400, "请选择有效的 TikTok 登录账号")
        identity = app_runtime.browser.identity_for(account)
    if app_runtime.engine is not None:
        async with app_runtime.engine.operation_guard(
            body.account_id, OperationKind.READ_HEAVY,
            fallback_key=f"tt-search:{body.account_id}",
            operation_target=identity,
        ):
            items, error = await fetch_tiktok_users(
                app_runtime.browser, identity, keyword, max_results=body.max_results,
                block_media=app_runtime.cfg.engine.block_media_resources)
    else:
        async with app_runtime.browser.lock_for(body.account_id):
            items, error = await fetch_tiktok_users(
                app_runtime.browser, identity, keyword, max_results=body.max_results,
                block_media=app_runtime.cfg.engine.block_media_resources)
    return {"items": items, "error": error}


@router.post("/api/tiktok/protocol/probe")
async def tiktok_protocol_probe(body: TikTokProtocolProbeIn):
    if app_runtime.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    keyword = body.keyword.strip() or "claude"
    mode = str(body.mode or "auto").strip().lower()
    if mode not in {"auto", "native", "browser"}:
        raise HTTPException(422, "mode 仅支持 auto/native/browser")
    with get_session() as session:
        account = session.get(DouyinAccount, body.account_id)
        if (not account or account.platform != "tiktok"
                or account.status != "active" or not account.storage_state):
            raise HTTPException(400, "请选择有效的 TikTok 登录账号")
        identity = app_runtime.browser.identity_for(account)
        storage_state = account.storage_state
    if mode in {"auto", "native"}:
        client = TikTokNativeClient(
            storage_state=storage_state,
            user_agent=identity.ua,
            proxy=identity.proxy,
            locale=identity.locale,
            timezone=identity.timezone_id,
            viewport=(identity.viewport_w, identity.viewport_h),
            region="US",
            device_seed=identity.fp_seed or str(identity.account_id),
            timeout=max(3, body.timeout_ms / 1000),
        )
        native = await client.search(keyword, count=3)
        result = {
            **native_protocol_summary(),
            "ok": bool(native.items) and not native.error,
            "keyword": keyword,
            "category": native.category,
            "error": native.error,
            "http_status": native.http_status,
            "body_length": native.body_length,
            "tt_orcas_res_present": bool(native.tt_orcas_res),
            "result_count": len(native.items),
            "signature_lengths": native.signature_lengths,
            "sample_item_ids": [
                str((row.get("item") or row.get("itemStruct") or row).get("id") or "")
                for row in native.items[:3]
            ],
        }
        if native.items or mode == "native" or native.category == "empty_valid":
            return result
    if app_runtime.engine is not None:
        async with app_runtime.engine.operation_guard(
            body.account_id, OperationKind.READ_HEAVY,
            fallback_key=f"tt-probe:{body.account_id}",
            operation_target=identity,
        ):
            async with app_runtime.browser.temporary_headed_context(identity):
                result = await probe_tiktok_protocol(
                    app_runtime.browser, identity, keyword, timeout_ms=body.timeout_ms)
    else:
        async with app_runtime.browser.lock_for(body.account_id):
            async with app_runtime.browser.temporary_headed_context(identity):
                result = await probe_tiktok_protocol(
                    app_runtime.browser, identity, keyword, timeout_ms=body.timeout_ms)
    result["fallback_from"] = "native_api" if mode == "auto" else ""
    return result


@router.post("/api/tiktok/comment-tasks")
async def create_tiktok_comment_task(body: TikTokCommentTaskIn):
    ref = parse_video_reference(body.aweme_id, body.author_unique_id)
    content = body.content.strip()
    if not ref:
        raise HTTPException(422, "请输入完整 TikTok 作品链接，或同时填写作品 ID 与作者 @username")
    aweme_id = ref.aweme_id
    if not content:
        raise HTTPException(422, "评论内容为必填")
    if body.target_comment_id and not body.target_text.strip():
        raise HTTPException(422, "回复任务需提供目标评论原文以定位回复区")
    with get_session() as session:
        account = session.get(DouyinAccount, body.account_id)
        if (not account or account.platform != "tiktok"
                or account.status != "active" or not account.storage_state):
            raise HTTPException(400, "请选择有效的 TikTok 登录账号")
        task = CommentTask(
            platform="tiktok", account_id=body.account_id,
            aweme_id=aweme_id, target_url=ref.url, content=content,
            target_comment_id=body.target_comment_id.strip(),
            target_text=body.target_text.strip(),
            target_nick=body.target_nick.strip(),
            status="pending", scheduled_at=datetime.utcnow(),
        )
        session.add(task); session.commit(); session.refresh(task)
        payload = metadata_service._task_dict(task)
    record_account_operation(
        platform="tiktok", account_id=body.account_id,
        task_type="comments", task_id=task.id, action="queued",
        status="pending", target=aweme_id, channel="queue")
    if body.run_now:
        if app_runtime.engine is None:
            raise HTTPException(503, "引擎未就绪")
        result = await app_runtime.engine.execute_comment_task(task.id)
        with get_session() as session:
            current = session.get(CommentTask, task.id)
            payload = metadata_service._task_dict(current) if current else payload
        payload["ran"] = bool(result.get("ok"))
        payload["execution_error"] = result.get("error") or payload.get("error") or ""
    return payload


class TwitterUserSearchIn(BaseModel):
    account_id: int
    keyword: str
    max_results: int = PydanticField(default=50, ge=1, le=200)


class TwitterInteractIn(BaseModel):
    account_id: int
    action: Literal["like", "unlike", "retweet", "unretweet", "quote"]
    tweet_url: str
    quote_text: str = ""


@router.post("/api/twitter/users/search")
async def search_twitter_users(body: TwitterUserSearchIn):
    keyword = body.keyword.strip()
    if not keyword:
        raise HTTPException(422, "请输入用户关键词")
    if app_runtime.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    with get_session() as session:
        account = session.get(DouyinAccount, body.account_id)
        if (
            not account
            or account.platform != "twitter"
            or account.status != "active"
            or not (account.storage_state or account.cookie)
        ):
            raise HTTPException(400, "请选择有效的 Twitter 登录账号")
        identity = app_runtime.browser.identity_for(account)
    if app_runtime.engine is not None:
        async with app_runtime.engine.operation_guard(
            body.account_id, OperationKind.READ_HEAVY,
            fallback_key=f"tw-search:{body.account_id}",
            operation_target=identity,
        ):
            items, error = await fetch_twitter_users(
                app_runtime.browser,
                identity,
                keyword,
                max_results=body.max_results,
                block_media=app_runtime.cfg.engine.block_media_resources,
            )
    else:
        async with app_runtime.browser.lock_for(body.account_id):
            items, error = await fetch_twitter_users(
                app_runtime.browser,
                identity,
                keyword,
                max_results=body.max_results,
                block_media=app_runtime.cfg.engine.block_media_resources,
            )
    return {"items": items, "error": "" if error == "empty" else error}


@router.post("/api/twitter/interact")
async def twitter_interact(body: TwitterInteractIn):
    """受风控与节流门禁保护的 X (Twitter) 推文点赞、转推与引用接口。"""
    if app_runtime.browser is None or app_runtime.engine is None:
        raise HTTPException(503, "浏览器或调度引擎未就绪")
    ref = resolve_twitter_tweet_ref(body.tweet_url)
    if ref is None:
        raise HTTPException(422, "请输入有效的 X (Twitter) 推文链接或数字 status ID")
    if body.action == "quote" and not body.quote_text.strip():
        raise HTTPException(422, "引用推文(Quote)需填写附加评论内容")
    with get_session() as session:
        account = session.get(DouyinAccount, body.account_id)
        if (
            not account
            or account.platform != "twitter"
            or account.status != "active"
            or not (account.storage_state or account.cookie)
        ):
            raise HTTPException(400, "请选择有效的 Twitter 登录账号")
        gate_err = app_runtime.engine._action_gate_error(body.account_id, 30, body.action)
        if gate_err:
            raise HTTPException(429, gate_err)
        identity = app_runtime.browser.identity_for(account)

    async with app_runtime.engine.operation_guard(
        body.account_id, OperationKind.SOCIAL, fallback_key=f"tw-act:{body.account_id}",
        operation_target=identity,
    ):
        if body.action in {"like", "unlike"}:
            ok, err = await do_twitter_like(
                app_runtime.browser,
                identity,
                ref.url,
                unlike=(body.action == "unlike"),
                headed=True,
            )
            res_id = ref.tweet_id
        else:
            ok, res_id, err = await do_twitter_retweet(
                app_runtime.browser,
                identity,
                ref.url,
                quote_text=(body.quote_text.strip() if body.action == "quote" else ""),
                unretweet=(body.action == "unretweet"),
                headed=True,
            )

    if ok:
        app_runtime.engine.risk.record_success(body.account_id, OperationKind.SOCIAL)
    elif not str(err or "").startswith("write_uncertain:"):
        app_runtime.engine.risk.record_failure(body.account_id, OperationKind.SOCIAL, err)

    record_account_operation(
        platform="twitter",
        account_id=body.account_id,
        task_type="actions",
        task_id=ref.tweet_id,
        action=body.action,
        status="done" if ok else ("uncertain" if str(err or "").startswith("write_uncertain:") else "failed"),
        target=ref.url,
        channel="browser",
        detail="" if ok else str(err or "")[:200],
    )
    if not ok:
        raise HTTPException(400, err or f"Twitter {body.action} 执行失败")
    return {"ok": True, "action": body.action, "tweet_id": ref.tweet_id, "result": res_id}


class FacebookSyncIn(BaseModel):
    account_id: int
    max_scrolls: int = PydanticField(default=8, ge=1, le=30)
    force_browser: bool = False


class FacebookEntitySearchIn(BaseModel):
    account_id: int
    keyword: str
    scope: Literal["groups", "pages", "people"] = "groups"
    public_only: bool = True
    max_scrolls: int = PydanticField(default=6, ge=1, le=20)
    limit: int = PydanticField(default=40, ge=1, le=200)


class FacebookInteractIn(BaseModel):
    account_id: int
    action: Literal[
        "like", "unlike", "share",
        "follow", "unfollow", "like_page", "add_friend", "unfriend",
        "join_group", "leave_group",
    ]
    target_url: str
    reaction: Literal["like", "love", "care", "haha", "wow", "sad", "angry"] = "like"
    comment_text: str = ""
    group_url_target: str = ""
    answers: list[str] = PydanticField(default_factory=list, max_length=10)
    page_id: str = ""


def _require_facebook_account(session, account_id: int) -> DouyinAccount:
    account = session.get(DouyinAccount, account_id)
    if (
        not account
        or account.platform != "facebook"
        or account.status != "active"
        or not (account.storage_state or account.creator_storage_state or account.cookie)
    ):
        raise HTTPException(400, "请选择有效的 Facebook 登录账号")
    return account


@router.post("/api/facebook/pages")
async def sync_facebook_pages(body: FacebookSyncIn):
    """同步账号管理的 Facebook 公共主页（Profile+ Pages）。"""
    with get_session() as session:
        account = _require_facebook_account(session, body.account_id)
        state = account.storage_state or account.creator_storage_state or account.cookie or ""
        ua = account_profiles_service._direct_request_ua(account)
        proxy = account.proxy or ""
        identity = app_runtime.browser.identity_for(account) if app_runtime.browser is not None else None

    mode = str(getattr(app_runtime.cfg.engine, "facebook_read_mode", "hybrid") or "hybrid").lower()
    api_err = ""
    if not body.force_browser and mode in {"api", "hybrid"} and state:
        fcli = FacebookClient(state, ua, proxy=proxy, timeout=app_runtime.cfg.engine.request_timeout_seconds)
        async with fcli.session_scope():
            pages, api_err = await fcli.fetch_managed_pages()
        if pages or mode == "api" or api_err.startswith(("logged_out:", "verification_required:")):
            return {"items": pages, "source": "api", "error": "" if api_err == "empty" else api_err}

    if app_runtime.browser is None or identity is None:
        return {"items": [], "source": "api", "error": api_err or "浏览器未就绪"}

    if app_runtime.engine is not None:
        async with app_runtime.engine.operation_guard(
            body.account_id, OperationKind.READ_HEAVY,
            fallback_key=f"fb-pages:{body.account_id}",
            operation_target=identity,
        ):
            items, error = await fetch_facebook_pages(app_runtime.browser, identity)
    else:
        async with app_runtime.browser.lock_for(body.account_id):
            items, error = await fetch_facebook_pages(app_runtime.browser, identity)
    return {"items": items, "source": "browser", "error": "" if error == "empty" else (error or api_err)}


@router.post("/api/facebook/groups")
async def sync_facebook_groups(body: FacebookSyncIn):
    """同步账号已加入的 Facebook 群组列表。"""
    if app_runtime.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    with get_session() as session:
        account = _require_facebook_account(session, body.account_id)
        identity = app_runtime.browser.identity_for(account)

    if app_runtime.engine is not None:
        async with app_runtime.engine.operation_guard(
            body.account_id, OperationKind.READ_HEAVY,
            fallback_key=f"fb-groups:{body.account_id}",
            operation_target=identity,
        ):
            items, error = await fetch_facebook_groups(
                app_runtime.browser, identity, max_scrolls=body.max_scrolls
            )
    else:
        async with app_runtime.browser.lock_for(body.account_id):
            items, error = await fetch_facebook_groups(
                app_runtime.browser, identity, max_scrolls=body.max_scrolls
            )
    return {"items": items, "error": "" if error == "empty" else error}


@router.post("/api/facebook/search")
async def search_facebook_entities(body: FacebookEntitySearchIn):
    """按关键词搜索 Facebook 群组（支持公开群组筛选）、公共主页或用户。"""
    keyword = body.keyword.strip()
    if not keyword:
        raise HTTPException(422, "请输入搜索关键词")
    if app_runtime.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    with get_session() as session:
        account = _require_facebook_account(session, body.account_id)
        identity = app_runtime.browser.identity_for(account)

    if app_runtime.engine is not None:
        async with app_runtime.engine.operation_guard(
            body.account_id, OperationKind.READ_HEAVY,
            fallback_key=f"fb-search:{body.account_id}",
            operation_target=identity,
        ):
            items, error = await fetch_facebook_entities_search(
                app_runtime.browser,
                identity,
                keyword,
                scope=body.scope,
                public_only=body.public_only,
                max_scrolls=body.max_scrolls,
                limit=body.limit,
            )
    else:
        async with app_runtime.browser.lock_for(body.account_id):
            items, error = await fetch_facebook_entities_search(
                app_runtime.browser,
                identity,
                keyword,
                scope=body.scope,
                public_only=body.public_only,
                max_scrolls=body.max_scrolls,
                limit=body.limit,
            )
    return {"items": items, "scope": body.scope, "error": "" if error == "empty" else error}


@router.post("/api/facebook/interact")
async def facebook_interact(body: FacebookInteractIn):
    """受风控与节流门禁保护的 Facebook 互动接口（7 种表情赞、分享、关注/主页赞/加好友、加群/退群，支持 Page 身份切换）。"""
    if app_runtime.browser is None or app_runtime.engine is None:
        raise HTTPException(503, "浏览器或调度引擎未就绪")
    target_raw = body.target_url.strip()
    if not target_raw:
        raise HTTPException(422, "请填写目标 Facebook 链接、用户名或群组地址")
    target_url = normalize_facebook_url(target_raw) if "://" in target_raw else target_raw
    post_ref = resolve_facebook_post_ref(target_raw) if body.action in {"like", "unlike", "share"} else None

    with get_session() as session:
        account = _require_facebook_account(session, body.account_id)
        gate_err = app_runtime.engine._action_gate_error(body.account_id, 30, body.action)
        if gate_err:
            raise HTTPException(429, gate_err)
        identity = app_runtime.browser.identity_for(account)

    res_info = ""
    async with app_runtime.engine.operation_guard(
        body.account_id, OperationKind.SOCIAL,
        fallback_key=f"fb-act:{body.account_id}",
        operation_target=identity,
    ):
        if body.action in {"like", "unlike"}:
            ok, err = await do_facebook_like(
                app_runtime.browser,
                identity,
                post_ref.canonical_url if post_ref else target_url,
                reaction=body.reaction,
                unlike=(body.action == "unlike"),
                page_id=body.page_id.strip(),
                headed=True,
            )
            res_info = body.reaction if body.action == "like" else "unlike"
        elif body.action == "share":
            ok, res_info, err = await do_facebook_share(
                app_runtime.browser,
                identity,
                post_ref.canonical_url if post_ref else target_url,
                comment_text=body.comment_text.strip(),
                group_url_target=body.group_url_target.strip(),
                page_id=body.page_id.strip(),
                headed=True,
            )
        elif body.action in {"join_group", "leave_group"}:
            ok, err = await do_facebook_group_membership(
                app_runtime.browser,
                identity,
                target_url,
                leave=(body.action == "leave_group"),
                answers=body.answers,
                page_id=body.page_id.strip(),
                headed=True,
            )
            res_info = body.action
        else:
            unfollow_flag = body.action in {"unfollow", "unfriend"}
            action_mode = (
                "like_page" if body.action == "like_page"
                else "add_friend" if body.action in {"add_friend", "unfriend"}
                else "follow"
            )
            ok, err = await do_facebook_follow(
                app_runtime.browser,
                identity,
                target_url,
                unfollow=unfollow_flag,
                action_mode=action_mode,
                headed=True,
            )
            res_info = body.action

    if ok:
        app_runtime.engine.risk.record_success(body.account_id, OperationKind.SOCIAL)
    elif not str(err or "").startswith("write_uncertain:"):
        app_runtime.engine.risk.record_failure(body.account_id, OperationKind.SOCIAL, err)

    record_account_operation(
        platform="facebook",
        account_id=body.account_id,
        task_type="actions",
        task_id=(post_ref.post_id if post_ref and post_ref.post_id else target_url[:64]),
        action=body.action,
        status="done" if ok else ("uncertain" if str(err or "").startswith("write_uncertain:") else "failed"),
        target=target_url,
        channel="browser",
        detail="" if ok else str(err or "")[:200],
    )
    if not ok:
        raise HTTPException(400, err or f"Facebook {body.action} 执行失败")
    return {"ok": True, "action": body.action, "target": target_url, "result": res_info}


class YouTubeChannelsSyncIn(BaseModel):
    account_id: int
    force_browser: bool = False


class YouTubeSearchIn(BaseModel):
    account_id: int
    keyword: str
    scope: Literal["videos", "channels"] = "videos"
    upload_date: str = ""
    duration: str = ""
    sort_by: str = ""
    search_type: str = "video"
    max_pages: int = PydanticField(default=3, ge=1, le=10)
    limit: int = PydanticField(default=30, ge=1, le=120)
    force_browser: bool = False


class YouTubeInteractIn(BaseModel):
    account_id: int
    action: Literal[
        "like", "unlike", "dislike", "undislike",
        "subscribe", "unsubscribe",
        "like_comment", "unlike_comment", "like_video_comments",
        "comment", "reply_comment",
        "change_profile_name",
    ]
    target_url: str = ""
    comment_text: str = ""
    target_comment_id: str = ""
    like_count: int = PydanticField(default=1, ge=1, le=20)
    is_random: bool = False
    new_name: str = ""
    channel_name: str = ""


class YouTubeWatchIn(BaseModel):
    account_id: int
    video_url: str
    duration_seconds: int = PydanticField(default=30, ge=5, le=600)
    use_percentage: bool = False
    percentage: int = PydanticField(default=50, ge=5, le=100)
    percentage_limit: int = PydanticField(default=120, ge=10, le=600)
    like_video: bool = False
    subscribe_channel: bool = False
    comment_text: str = ""
    skip_ads: bool = True
    channel_name: str = ""


class YouTubeWarmupIn(BaseModel):
    account_id: int
    keyword: str
    max_videos: int = PydanticField(default=2, ge=1, le=8)
    watch_seconds: int = PydanticField(default=20, ge=5, le=180)
    like_probability: float = PydanticField(default=0.5, ge=0.0, le=1.0)


def _require_youtube_account(session, account_id: int) -> DouyinAccount:
    account = session.get(DouyinAccount, account_id)
    if (
        not account
        or account.platform != "youtube"
        or account.status != "active"
        or not (account.storage_state or account.creator_storage_state or account.cookie)
    ):
        raise HTTPException(400, "请选择有效的 YouTube 登录账号")
    return account


@router.post("/api/youtube/channels")
async def sync_youtube_channels(body: YouTubeChannelsSyncIn):
    """同步当前 Google/YouTube 账号下可切换管理的品牌频道与个人频道（Channel Switcher）。"""
    with get_session() as session:
        account = _require_youtube_account(session, body.account_id)
        state = account.storage_state or account.creator_storage_state or account.cookie or ""
        ua = account_profiles_service._direct_request_ua(account)
        proxy = account.proxy or ""
        identity = app_runtime.browser.identity_for(account) if app_runtime.browser is not None else None

    mode = str(getattr(app_runtime.cfg.engine, "youtube_read_mode", "hybrid") or "hybrid").lower()
    api_err = ""
    if not body.force_browser and mode in {"api", "hybrid"} and state:
        ycli = YouTubeClient(state, ua, proxy=proxy, timeout=app_runtime.cfg.engine.request_timeout_seconds)
        async with ycli.session_scope():
            channels, api_err = await ycli.fetch_managed_channels()
        if channels or mode == "api" or api_err.startswith("verification_required:"):
            return {"items": channels, "source": "api", "error": "" if api_err == "empty" else api_err}

    if app_runtime.browser is None or identity is None:
        return {"items": [], "source": "api", "error": api_err or "浏览器未就绪"}

    if app_runtime.engine is not None:
        async with app_runtime.engine.operation_guard(
            body.account_id, OperationKind.READ_HEAVY,
            fallback_key=f"yt-channels:{body.account_id}",
            operation_target=identity,
        ):
            items, error = await fetch_youtube_managed_channels(app_runtime.browser, identity)
    else:
        async with app_runtime.browser.lock_for(body.account_id):
            items, error = await fetch_youtube_managed_channels(app_runtime.browser, identity)
    return {"items": items, "source": "browser", "error": "" if error == "empty" else (error or api_err)}


@router.post("/api/youtube/search")
async def search_youtube_entities(body: YouTubeSearchIn):
    """按关键词搜索 YouTube 视频（支持上传时间/时长/排序筛选）或频道列表。"""
    keyword = body.keyword.strip()
    if not keyword:
        raise HTTPException(422, "请输入搜索关键词")
    with get_session() as session:
        account = _require_youtube_account(session, body.account_id)
        state = account.storage_state or account.creator_storage_state or account.cookie or ""
        ua = account_profiles_service._direct_request_ua(account)
        proxy = account.proxy or ""
        identity = app_runtime.browser.identity_for(account) if app_runtime.browser is not None else None

    mode = str(getattr(app_runtime.cfg.engine, "youtube_read_mode", "hybrid") or "hybrid").lower()
    api_err = ""
    if not body.force_browser and mode in {"api", "hybrid"}:
        ycli = YouTubeClient(state, ua, proxy=proxy, timeout=app_runtime.cfg.engine.request_timeout_seconds)
        async with ycli.session_scope():
            if body.scope == "channels":
                items, api_err = await ycli.search_channels(
                    keyword, max_items=body.limit, max_pages=body.max_pages
                )
            else:
                items, api_err = await ycli.search_videos(
                    keyword,
                    max_items=body.limit,
                    max_pages=body.max_pages,
                    upload_date=body.upload_date,
                    duration=body.duration,
                    sort_by=body.sort_by,
                    search_type=body.search_type,
                )
        if items or mode == "api" or api_err.startswith("verification_required:"):
            return {"items": items, "scope": body.scope, "source": "api", "error": "" if api_err == "empty" else api_err}

    if app_runtime.browser is None or identity is None:
        return {"items": [], "scope": body.scope, "source": "api", "error": api_err or "浏览器未就绪"}

    async def _run_browser():
        if body.scope == "channels":
            return await fetch_youtube_channels_search(
                app_runtime.browser, identity, keyword,
                max_scrolls=max(2, body.max_pages), limit=body.limit,
            )
        return await fetch_youtube_search(
            app_runtime.browser, identity, keyword,
            max_scrolls=max(2, body.max_pages), limit=body.limit,
            upload_date=body.upload_date, duration=body.duration,
            sort_by=body.sort_by, search_type=body.search_type,
        )

    if app_runtime.engine is not None:
        async with app_runtime.engine.operation_guard(
            body.account_id, OperationKind.READ_HEAVY,
            fallback_key=f"yt-search:{body.account_id}",
            operation_target=identity,
        ):
            items, error = await _run_browser()
    else:
        async with app_runtime.browser.lock_for(body.account_id):
            items, error = await _run_browser()
    return {"items": items, "scope": body.scope, "source": "browser", "error": "" if error == "empty" else (error or api_err)}


@router.post("/api/youtube/interact")
async def youtube_interact(body: YouTubeInteractIn):
    """受风控与节流门禁保护的 YouTube 互动接口（视频点赞/点踩、订阅/取关、评论/回复、评论点赞、批量赞视频评论、修改频道名称，支持多频道切换）。"""
    if app_runtime.browser is None or app_runtime.engine is None:
        raise HTTPException(503, "浏览器或调度引擎未就绪")
    target_raw = body.target_url.strip()
    if body.action != "change_profile_name" and not target_raw:
        raise HTTPException(422, "请填写目标 YouTube 视频、频道或评论链接")
    if body.action in {"comment", "reply_comment"} and not body.comment_text.strip():
        raise HTTPException(422, "请填写评论/回复内容")
    if body.action == "change_profile_name" and not body.new_name.strip():
        raise HTTPException(422, "请填写新的频道名称")

    target_url = normalize_youtube_url(target_raw) if "://" in target_raw else target_raw
    vref = resolve_youtube_video_ref(target_raw) if target_raw else None

    with get_session() as session:
        account = _require_youtube_account(session, body.account_id)
        gate_err = app_runtime.engine._action_gate_error(body.account_id, 30, body.action)
        if gate_err:
            raise HTTPException(429, gate_err)
        identity = app_runtime.browser.identity_for(account)

    res_info: object = body.action
    ok = False
    err = ""
    async with app_runtime.engine.operation_guard(
        body.account_id, OperationKind.SOCIAL,
        fallback_key=f"yt-act:{body.account_id}",
        operation_target=identity,
    ):
        if body.action in {"like", "unlike", "dislike", "undislike"}:
            ok, err = await do_youtube_like_video(
                app_runtime.browser,
                identity,
                vref.watch_url if vref else target_url,
                action=body.action,
                channel_name=body.channel_name.strip(),
                headed=True,
            )
            res_info = body.action
        elif body.action in {"subscribe", "unsubscribe"}:
            ok, err = await do_youtube_subscribe(
                app_runtime.browser,
                identity,
                target_url,
                unsubscribe=(body.action == "unsubscribe"),
                channel_name=body.channel_name.strip(),
                headed=True,
            )
            res_info = body.action
        elif body.action in {"like_comment", "unlike_comment", "like_video_comments"}:
            ok, liked_cnt, err = await do_youtube_like_comment(
                app_runtime.browser,
                identity,
                target_url,
                unlike=(body.action == "unlike_comment"),
                like_count=(body.like_count if body.action == "like_video_comments" else 1),
                is_random=body.is_random,
                channel_name=body.channel_name.strip(),
                headed=True,
            )
            res_info = {"liked_count": liked_cnt}
        elif body.action in {"comment", "reply_comment"}:
            ok, cid, err = await post_youtube_comment(
                app_runtime.browser,
                identity,
                vref.watch_url if vref else target_url,
                body.comment_text.strip(),
                target_comment_id=body.target_comment_id.strip(),
                target_comment_url=(target_url if body.action == "reply_comment" and "lc=" in target_url else ""),
                channel_name=body.channel_name.strip(),
                headed=True,
            )
            res_info = {"comment_id": cid}
        elif body.action == "change_profile_name":
            ok, err = await do_youtube_change_profile_name(
                app_runtime.browser,
                identity,
                body.new_name.strip(),
                headed=True,
            )
            res_info = {"new_name": body.new_name.strip()}

    if ok:
        app_runtime.engine.risk.record_success(body.account_id, OperationKind.SOCIAL)
    elif not str(err or "").startswith("write_uncertain:"):
        app_runtime.engine.risk.record_failure(body.account_id, OperationKind.SOCIAL, err)

    record_account_operation(
        platform="youtube",
        account_id=body.account_id,
        task_type="actions",
        task_id=(vref.video_id if vref and vref.video_id else (target_url[:64] or body.action)),
        action=body.action,
        status="done" if ok else ("uncertain" if str(err or "").startswith("write_uncertain:") else "failed"),
        target=target_url or body.new_name.strip(),
        channel="browser",
        detail="" if ok else str(err or "")[:200],
    )
    if not ok:
        raise HTTPException(400, err or f"YouTube {body.action} 执行失败")
    return {"ok": True, "action": body.action, "target": target_url, "result": res_info}


@router.post("/api/youtube/watch")
async def youtube_watch_video(body: YouTubeWatchIn):
    """YouTube 视频真实观看（支持按时长或观看百分比控制、自动跳过广告、看完后联动点赞/订阅/评论）。"""
    if app_runtime.browser is None or app_runtime.engine is None:
        raise HTTPException(503, "浏览器或调度引擎未就绪")
    vref = resolve_youtube_video_ref(body.video_url)
    if vref is None:
        raise HTTPException(422, "请填写有效的 YouTube 视频/Shorts 链接或 11 位视频 ID")
    with get_session() as session:
        account = _require_youtube_account(session, body.account_id)
        gate_err = app_runtime.engine._action_gate_error(body.account_id, 20, "watch")
        if gate_err:
            raise HTTPException(429, gate_err)
        identity = app_runtime.browser.identity_for(account)

    async with app_runtime.engine.operation_guard(
        body.account_id, OperationKind.SOCIAL,
        fallback_key=f"yt-watch:{body.account_id}",
        operation_target=identity,
    ):
        ok, stats, err = await do_youtube_watch_video(
            app_runtime.browser,
            identity,
            vref.watch_url,
            duration_seconds=body.duration_seconds,
            use_percentage=body.use_percentage,
            percentage=body.percentage,
            percentage_limit=body.percentage_limit,
            like_video=body.like_video,
            subscribe_channel=body.subscribe_channel,
            comment_text=body.comment_text.strip(),
            skip_ads=body.skip_ads,
            channel_name=body.channel_name.strip(),
            headed=True,
        )

    if ok:
        app_runtime.engine.risk.record_success(body.account_id, OperationKind.SOCIAL)
    elif not str(err or "").startswith("write_uncertain:"):
        app_runtime.engine.risk.record_failure(body.account_id, OperationKind.SOCIAL, err)

    record_account_operation(
        platform="youtube",
        account_id=body.account_id,
        task_type="actions",
        task_id=vref.video_id,
        action="watch",
        status="done" if ok else "failed",
        target=vref.watch_url,
        channel="browser",
        detail=(
            f"view_time={stats.get('view_time', 0)}s ({stats.get('view_percentage', 0)}%)"
            if ok else str(err or "")[:200]
        ),
    )
    if not ok:
        raise HTTPException(400, err or "YouTube 观看执行失败")
    return {"ok": True, "video_id": vref.video_id, "stats": stats}


@router.post("/api/youtube/warmup")
async def youtube_warmup(body: YouTubeWarmupIn):
    """YouTube 账号关键词搜索、浏览视频、模拟观看与概率点赞养号流程。"""
    if app_runtime.browser is None or app_runtime.engine is None:
        raise HTTPException(503, "浏览器或调度引擎未就绪")
    kw = body.keyword.strip()
    if not kw:
        raise HTTPException(422, "请填写养号搜索关键词")
    with get_session() as session:
        account = _require_youtube_account(session, body.account_id)
        gate_err = app_runtime.engine._action_gate_error(body.account_id, 60, "warmup")
        if gate_err:
            raise HTTPException(429, gate_err)
        identity = app_runtime.browser.identity_for(account)

    async with app_runtime.engine.operation_guard(
        body.account_id, OperationKind.SOCIAL,
        fallback_key=f"yt-warmup:{body.account_id}",
        operation_target=identity,
    ):
        ok, summary, err = await do_youtube_warmup(
            app_runtime.browser,
            identity,
            kw,
            max_videos=body.max_videos,
            watch_seconds=body.watch_seconds,
            like_probability=body.like_probability,
            headed=True,
        )

    if ok:
        app_runtime.engine.risk.record_success(body.account_id, OperationKind.SOCIAL)
    else:
        app_runtime.engine.risk.record_failure(body.account_id, OperationKind.SOCIAL, err)

    record_account_operation(
        platform="youtube",
        account_id=body.account_id,
        task_type="actions",
        task_id=kw[:64],
        action="warmup",
        status="done" if ok else "failed",
        target=kw,
        channel="browser",
        detail=(
            f"watched={summary.get('watched_count', 0)}, liked={summary.get('liked_count', 0)}"
            if ok else str(err or "")[:200]
        ),
    )
    if not ok:
        raise HTTPException(400, err or "YouTube 养号执行失败")
    return {"ok": True, "keyword": kw, "summary": summary}


@router.get("/api/accounts/{account_id}/bili/partitions")
async def list_bili_partitions(account_id: int):
    """获取哔哩哔哩创作中心分区树与创作者声明列表（失败或浏览器模式时回退内置分区）。"""
    with get_session() as session:
        account = session.get(DouyinAccount, account_id)
        if not account or account.platform != "bilibili":
            raise HTTPException(404, "哔哩哔哩账号不存在")
        state = account.storage_state or account.creator_storage_state or ""
        ua = account_profiles_service._direct_request_ua(account)
        proxy = account.proxy or ""
        active = account.status == "active" and bool(state)

    if (
        not active
        or app_runtime.engine is None
        or str(getattr(app_runtime.cfg.engine, "bilibili_read_mode", "hybrid") or "hybrid") == "browser"
    ):
        return {
            "partitions": list(BILI_FALLBACK_PARTITIONS),
            "statements": list(BILI_CREATION_STATEMENT_OPTIONS),
        }

    async def read_partitions():
        client = BilibiliClient(
            state, ua, proxy=proxy, timeout=app_runtime.cfg.engine.request_timeout_seconds
        )
        async with client.session_scope():
            return await client.fetch_human_type_list(), ""

    partitions, error = await app_runtime.engine.guarded_interactive_read_pair(
        account_id,
        OperationKind.READ_LIGHT,
        f"bili-partitions:{account_id}",
        read_partitions,
        empty_result=[],
    )
    if error and not partitions:
        partitions = list(BILI_FALLBACK_PARTITIONS)
    return {
        "partitions": partitions or list(BILI_FALLBACK_PARTITIONS),
        "statements": list(BILI_CREATION_STATEMENT_OPTIONS),
    }


@router.get("/api/accounts/{account_id}/bili/topics")
async def list_bili_topics(
    account_id: int,
    q: str = "",
    type_pid: str = "",
    title: str = "",
    pn: int = 0,
    ps: int = 20,
):
    """检索或推荐哔哩哔哩创作中心投稿话题。"""
    if app_runtime.engine is None:
        raise HTTPException(503, "采集引擎未就绪")
    with get_session() as session:
        account = session.get(DouyinAccount, account_id)
        if not account or account.platform != "bilibili" or account.status != "active":
            raise HTTPException(400, "请选择有效的哔哩哔哩登录账号")
        state = account.storage_state or account.creator_storage_state or ""
        if not state:
            raise HTTPException(400, "缺少哔哩哔哩登录态")
        ua = account_profiles_service._direct_request_ua(account)
        proxy = account.proxy or ""

    async def read_topics():
        client = BilibiliClient(
            state, ua, proxy=proxy, timeout=app_runtime.cfg.engine.request_timeout_seconds
        )
        async with client.session_scope():
            if q.strip():
                return await client.search_topics(q.strip(), pn=pn, ps=ps), ""
            return await client.fetch_topic_types(
                type_pid=type_pid, title=title, pn=pn, ps=ps
            ), ""

    topics, error = await app_runtime.engine.guarded_interactive_read_pair(
        account_id,
        OperationKind.READ_LIGHT,
        f"bili-topics:{account_id}",
        read_topics,
        empty_result=[],
    )
    if error.startswith("risk_deferred:"):
        raise HTTPException(429, error.split(":", 1)[-1])
    if error and not topics:
        raise HTTPException(400, error)
    return {"topics": topics}


@router.get("/api/accounts/{account_id}/bili/archive-stats")
async def get_bili_archive_stats(account_id: int, bvid: str):
    """获取单条哔哩哔哩稿件创作中心诊断统计与 5 维流量来源占比。"""
    if app_runtime.engine is None:
        raise HTTPException(503, "采集引擎未就绪")
    bv = str(bvid or "").strip()
    if not bv:
        raise HTTPException(400, "请提供稿件 BV 号")
    with get_session() as session:
        account = session.get(DouyinAccount, account_id)
        if not account or account.platform != "bilibili" or account.status != "active":
            raise HTTPException(400, "请选择有效的哔哩哔哩登录账号")
        state = account.storage_state or account.creator_storage_state or ""
        if not state:
            raise HTTPException(400, "缺少哔哩哔哩登录态")
        ua = account_profiles_service._direct_request_ua(account)
        proxy = account.proxy or ""

    async def read_stats():
        client = BilibiliClient(
            state, ua, proxy=proxy, timeout=app_runtime.cfg.engine.request_timeout_seconds
        )
        async with client.session_scope():
            return await client.fetch_work_diagnose(bv)

    stats, error = await app_runtime.engine.guarded_interactive_read_pair(
        account_id,
        OperationKind.READ_LIGHT,
        f"bili-archive-stats:{account_id}:{bv}",
        read_stats,
        empty_result={},
    )
    if error.startswith("risk_deferred:"):
        raise HTTPException(429, error.split(":", 1)[-1])
    if error and not stats:
        raise HTTPException(400, error)
    return stats
