"""多平台交互式浏览器登录启动、状态轮询与 Cookie/Token 导入接口。"""
from __future__ import annotations

from typing import Any
import asyncio
import re
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import select

from app import runtime as app_runtime
from app.browser import cookie_string_to_state
from app.db import get_session
from app.models import DouyinAccount
from app.platforms.twitter import TwitterClient, build_twitter_storage_state, parse_twitter_credential_input, cookie_str_from_state as twitter_cookie_str_from_state
from app.profiles import assign_proxy_from_pool, ensure_identity
from app.services import login_flows as login_flows_service
from app.services import login_sessions as login_sessions_service

router = APIRouter()


@router.post("/api/login/browser/start")
async def login_browser_start(proxy: str = "auto", browser_backend: str = "default",
                              browser_runtime_id: str = "",
                              fingerprint: dict[str, Any] | None = None):
    if app_runtime.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    browser_backend, browser_runtime_id = login_flows_service._validate_login_browser_backend(
        browser_backend, browser_runtime_id)
    fingerprint_overrides = login_flows_service._validate_prelogin_fingerprint(
        fingerprint, browser_backend, browser_runtime_id)
    reused = await login_sessions_service._reuse_or_reject_interactive_login("douyin", False)
    if reused is not None:
        return reused
    task_id = uuid.uuid4().hex
    app_runtime.login_tasks[task_id] = login_sessions_service._login_task_state(
        status="opening", platform="douyin", creator=False, account_id=None)
    asyncio.create_task(login_flows_service._run_login(
        task_id, proxy_choice=proxy, browser_backend=browser_backend,
        browser_runtime_id=browser_runtime_id,
        fingerprint_overrides=fingerprint_overrides))
    return {"task_id": task_id, "status": "opening",
            "hint": "已打开浏览器窗口,请在其中点击“登录”并用抖音 App 扫码"}


@router.post("/api/login/creator/start")
async def login_creator_start(proxy: str = "auto", browser_backend: str = "default",
                              browser_runtime_id: str = "",
                              fingerprint: dict[str, Any] | None = None):
    """创作中心登录(用于自有账号评论模式;其登录态同样可用于公开抓取)。"""
    if app_runtime.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    browser_backend, browser_runtime_id = login_flows_service._validate_login_browser_backend(
        browser_backend, browser_runtime_id)
    fingerprint_overrides = login_flows_service._validate_prelogin_fingerprint(
        fingerprint, browser_backend, browser_runtime_id)
    reused = await login_sessions_service._reuse_or_reject_interactive_login("douyin", True)
    if reused is not None:
        return reused
    task_id = uuid.uuid4().hex
    app_runtime.login_tasks[task_id] = login_sessions_service._login_task_state(
        status="opening", platform="douyin", creator=True, account_id=None)
    asyncio.create_task(login_flows_service._run_login(
        task_id, creator=True, proxy_choice=proxy,
        browser_backend=browser_backend,
        browser_runtime_id=browser_runtime_id,
        fingerprint_overrides=fingerprint_overrides))
    return {"task_id": task_id, "status": "opening",
            "hint": "已打开创作中心窗口,请在其中扫码登录你的抖音号"}


@router.post("/api/login/xhs/start")
async def login_xhs_start(proxy: str = "auto", browser_backend: str = "default",
                          browser_runtime_id: str = "",
                          fingerprint: dict[str, Any] | None = None):
    """小红书扫码登录(用于监控/读取)。"""
    if app_runtime.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    browser_backend, browser_runtime_id = login_flows_service._validate_login_browser_backend(
        browser_backend, browser_runtime_id, platform="xhs")
    fingerprint_overrides = login_flows_service._validate_prelogin_fingerprint(
        fingerprint, browser_backend, browser_runtime_id)
    reused = await login_sessions_service._reuse_or_reject_interactive_login("xhs", False)
    if reused is not None:
        return reused
    task_id = uuid.uuid4().hex
    app_runtime.login_tasks[task_id] = login_sessions_service._login_task_state(
        status="opening", platform="xhs", creator=False, account_id=None)
    asyncio.create_task(login_flows_service._run_login(
        task_id, platform="xhs", proxy_choice=proxy,
        browser_backend=browser_backend,
        browser_runtime_id=browser_runtime_id,
        fingerprint_overrides=fingerprint_overrides))
    return {"task_id": task_id, "status": "opening",
            "hint": "已打开小红书官网首页,请在窗口中点击登录并用小红书 App 扫码"}


@router.post("/api/login/xhs-creator/start")
async def login_xhs_creator_start(proxy: str = "auto", browser_backend: str = "default",
                                  browser_runtime_id: str = "",
                                  fingerprint: dict[str, Any] | None = None):
    """小红书「创作服务平台」登录(用于发布/已发布列表)。"""
    if app_runtime.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    browser_backend, browser_runtime_id = login_flows_service._validate_login_browser_backend(
        browser_backend, browser_runtime_id, platform="xhs")
    fingerprint_overrides = login_flows_service._validate_prelogin_fingerprint(
        fingerprint, browser_backend, browser_runtime_id)
    reused = await login_sessions_service._reuse_or_reject_interactive_login("xhs", True)
    if reused is not None:
        return reused
    task_id = uuid.uuid4().hex
    app_runtime.login_tasks[task_id] = login_sessions_service._login_task_state(
        status="opening", platform="xhs", creator=True, account_id=None)
    asyncio.create_task(login_flows_service._run_login(
        task_id, creator=True, platform="xhs", proxy_choice=proxy,
        browser_backend=browser_backend,
        browser_runtime_id=browser_runtime_id,
        fingerprint_overrides=fingerprint_overrides))
    return {"task_id": task_id, "status": "opening",
            "hint": "已打开小红书创作平台窗口,请扫码登录(发布用)"}


@router.post("/api/login/kuaishou/start")
async def login_ks_start(proxy: str = "auto", browser_backend: str = "default",
                         browser_runtime_id: str = "",
                         fingerprint: dict[str, Any] | None = None):
    """快手扫码登录(用于监控/读取)。"""
    if app_runtime.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    browser_backend, browser_runtime_id = login_flows_service._validate_login_browser_backend(
        browser_backend, browser_runtime_id)
    fingerprint_overrides = login_flows_service._validate_prelogin_fingerprint(
        fingerprint, browser_backend, browser_runtime_id)
    reused = await login_sessions_service._reuse_or_reject_interactive_login("kuaishou", False)
    if reused is not None:
        return reused
    task_id = uuid.uuid4().hex
    app_runtime.login_tasks[task_id] = login_sessions_service._login_task_state(
        status="opening", platform="kuaishou", creator=False,
        account_id=None)
    asyncio.create_task(login_flows_service._run_login(
        task_id, platform="kuaishou", proxy_choice=proxy,
        browser_backend=browser_backend,
        browser_runtime_id=browser_runtime_id,
        fingerprint_overrides=fingerprint_overrides))
    return {"task_id": task_id, "status": "opening",
            "hint": "已打开快手窗口,请在其中用快手 App 扫码登录"}


@router.post("/api/login/kuaishou-creator/start")
async def login_ks_creator_start(proxy: str = "auto", browser_backend: str = "default",
                                 browser_runtime_id: str = "",
                                 fingerprint: dict[str, Any] | None = None):
    """快手「创作者服务平台」登录(用于发布)。"""
    if app_runtime.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    browser_backend, browser_runtime_id = login_flows_service._validate_login_browser_backend(
        browser_backend, browser_runtime_id)
    fingerprint_overrides = login_flows_service._validate_prelogin_fingerprint(
        fingerprint, browser_backend, browser_runtime_id)
    reused = await login_sessions_service._reuse_or_reject_interactive_login("kuaishou", True)
    if reused is not None:
        return reused
    task_id = uuid.uuid4().hex
    app_runtime.login_tasks[task_id] = login_sessions_service._login_task_state(
        status="opening", platform="kuaishou", creator=True,
        account_id=None)
    asyncio.create_task(login_flows_service._run_login(
        task_id, creator=True, platform="kuaishou", proxy_choice=proxy,
        browser_backend=browser_backend,
        browser_runtime_id=browser_runtime_id,
        fingerprint_overrides=fingerprint_overrides))
    return {"task_id": task_id, "status": "opening",
            "hint": "已打开快手创作平台窗口,请扫码登录(发布用)"}


@router.post("/api/login/shipinhao/start")
async def login_channels_start(proxy: str = "auto", browser_backend: str = "default",
                               browser_runtime_id: str = "",
                               fingerprint: dict[str, Any] | None = None):
    """视频号扫码登录(读取/发布共用,微信扫码)。"""
    if app_runtime.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    browser_backend, browser_runtime_id = login_flows_service._validate_login_browser_backend(
        browser_backend, browser_runtime_id)
    fingerprint_overrides = login_flows_service._validate_prelogin_fingerprint(
        fingerprint, browser_backend, browser_runtime_id)
    reused = await login_sessions_service._reuse_or_reject_interactive_login("shipinhao", False)
    if reused is not None:
        return reused
    task_id = uuid.uuid4().hex
    app_runtime.login_tasks[task_id] = login_sessions_service._login_task_state(
        status="opening", platform="shipinhao", creator=False,
        account_id=None)
    asyncio.create_task(login_flows_service._run_login(
        task_id, platform="shipinhao", proxy_choice=proxy,
        browser_backend=browser_backend,
        browser_runtime_id=browser_runtime_id,
        fingerprint_overrides=fingerprint_overrides))
    return {"task_id": task_id, "status": "opening",
            "hint": "已打开视频号助手窗口,请用微信扫码登录"}


@router.post("/api/login/tiktok/start")
async def login_tiktok_start(proxy: str = "auto", browser_backend: str = "default",
                             browser_runtime_id: str = "",
                             fingerprint: dict[str, Any] | None = None):
    """TikTok Web 登录；页面内动态签名和后续读写共用此 Profile。"""
    if app_runtime.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    browser_backend, browser_runtime_id = login_flows_service._validate_login_browser_backend(
        browser_backend, browser_runtime_id)
    fingerprint_overrides = login_flows_service._validate_prelogin_fingerprint(
        fingerprint, browser_backend, browser_runtime_id)
    reused = await login_sessions_service._reuse_or_reject_interactive_login("tiktok", False)
    if reused is not None:
        return reused
    task_id = uuid.uuid4().hex
    app_runtime.login_tasks[task_id] = login_sessions_service._login_task_state(
        status="opening", platform="tiktok", creator=False, account_id=None)
    asyncio.create_task(login_flows_service._run_login(
        task_id, platform="tiktok", proxy_choice=proxy,
        browser_backend=browser_backend,
        browser_runtime_id=browser_runtime_id,
        fingerprint_overrides=fingerprint_overrides))
    return {"task_id": task_id, "status": "opening",
            "hint": "已打开 TikTok 窗口，请完成登录"}


@router.post("/api/login/bilibili/start")
async def login_bilibili_start(proxy: str = "auto", browser_backend: str = "default",
                               browser_runtime_id: str = "",
                               fingerprint: dict[str, Any] | None = None):
    """哔哩哔哩扫码/网页登录；主站、创作中心与私信共用同一 `.bilibili.com` 会话。"""
    if app_runtime.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    browser_backend, browser_runtime_id = login_flows_service._validate_login_browser_backend(
        browser_backend, browser_runtime_id)
    fingerprint_overrides = login_flows_service._validate_prelogin_fingerprint(
        fingerprint, browser_backend, browser_runtime_id)
    reused = await login_sessions_service._reuse_or_reject_interactive_login("bilibili", False)
    if reused is not None:
        return reused
    task_id = uuid.uuid4().hex
    app_runtime.login_tasks[task_id] = login_sessions_service._login_task_state(
        status="opening", platform="bilibili", creator=False, account_id=None)
    asyncio.create_task(login_flows_service._run_login(
        task_id, platform="bilibili", proxy_choice=proxy,
        browser_backend=browser_backend,
        browser_runtime_id=browser_runtime_id,
        fingerprint_overrides=fingerprint_overrides))
    return {"task_id": task_id, "status": "opening",
            "hint": "已打开哔哩哔哩登录窗口，请使用 B 站 App 扫码或完成网页登录"}


@router.post("/api/login/rednote/start")
async def login_rednote_start(proxy: str = "auto", browser_backend: str = "default",
                              browser_runtime_id: str = "",
                              fingerprint: dict[str, Any] | None = None):
    """海外小红书（Rednote）主站扫码/网页登录（www.rednote.com/zh）。"""
    if app_runtime.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    browser_backend, browser_runtime_id = login_flows_service._validate_login_browser_backend(
        browser_backend, browser_runtime_id, platform="rednote")
    fingerprint_overrides = login_flows_service._validate_prelogin_fingerprint(
        fingerprint, browser_backend, browser_runtime_id)
    reused = await login_sessions_service._reuse_or_reject_interactive_login("rednote", False)
    if reused is not None:
        return reused
    task_id = uuid.uuid4().hex
    app_runtime.login_tasks[task_id] = login_sessions_service._login_task_state(
        status="opening", platform="rednote", creator=False, account_id=None)
    asyncio.create_task(login_flows_service._run_login(
        task_id, platform="rednote", proxy_choice=proxy,
        browser_backend=browser_backend,
        browser_runtime_id=browser_runtime_id,
        fingerprint_overrides=fingerprint_overrides))
    return {"task_id": task_id, "status": "opening",
            "hint": "已打开海外小红书（www.rednote.com）窗口，请扫码或完成登录"}


@router.post("/api/login/rednote-creator/start")
async def login_rednote_creator_start(proxy: str = "auto", browser_backend: str = "default",
                                      browser_runtime_id: str = "",
                                      fingerprint: dict[str, Any] | None = None):
    """海外小红书创作服务平台登录（creator.rednote.com/login，发布与笔记管理用）。"""
    if app_runtime.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    browser_backend, browser_runtime_id = login_flows_service._validate_login_browser_backend(
        browser_backend, browser_runtime_id, platform="rednote")
    fingerprint_overrides = login_flows_service._validate_prelogin_fingerprint(
        fingerprint, browser_backend, browser_runtime_id)
    reused = await login_sessions_service._reuse_or_reject_interactive_login("rednote", True)
    if reused is not None:
        return reused
    task_id = uuid.uuid4().hex
    app_runtime.login_tasks[task_id] = login_sessions_service._login_task_state(
        status="opening", platform="rednote", creator=True, account_id=None)
    asyncio.create_task(login_flows_service._run_login(
        task_id, creator=True, platform="rednote", proxy_choice=proxy,
        browser_backend=browser_backend,
        browser_runtime_id=browser_runtime_id,
        fingerprint_overrides=fingerprint_overrides))
    return {"task_id": task_id, "status": "opening",
            "hint": "已打开海外小红书创作服务平台（creator.rednote.com）窗口，请扫码登录"}


@router.post("/api/login/instagram/start")
async def login_instagram_start(proxy: str = "auto", browser_backend: str = "default",
                                browser_runtime_id: str = "",
                                fingerprint: dict[str, Any] | None = None):
    """Instagram 独立浏览器登录；`.instagram.com` 会话同时用于 Mobile API 与 Web 端。"""
    if app_runtime.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    browser_backend, browser_runtime_id = login_flows_service._validate_login_browser_backend(
        browser_backend, browser_runtime_id)
    fingerprint_overrides = login_flows_service._validate_prelogin_fingerprint(
        fingerprint, browser_backend, browser_runtime_id)
    reused = await login_sessions_service._reuse_or_reject_interactive_login("instagram", False)
    if reused is not None:
        return reused
    task_id = uuid.uuid4().hex
    app_runtime.login_tasks[task_id] = login_sessions_service._login_task_state(
        status="opening", platform="instagram", creator=False, account_id=None)
    asyncio.create_task(login_flows_service._run_login(
        task_id, platform="instagram", proxy_choice=proxy,
        browser_backend=browser_backend,
        browser_runtime_id=browser_runtime_id,
        fingerprint_overrides=fingerprint_overrides))
    return {"task_id": task_id, "status": "opening",
            "hint": "已打开 Instagram 登录窗口，请完成登录及双重验证(如有)"}


@router.post("/api/login/twitter/start")
async def login_twitter_start(proxy: str = "auto", browser_backend: str = "default",
                              browser_runtime_id: str = "",
                              fingerprint: dict[str, Any] | None = None):
    """X (Twitter) 独立浏览器登录；`.x.com` 会话同时用于采集、互动、私信与发布。"""
    if app_runtime.browser is None:
        raise HTTPException(503, "浏览器未就绪")
    browser_backend, browser_runtime_id = login_flows_service._validate_login_browser_backend(
        browser_backend, browser_runtime_id)
    fingerprint_overrides = login_flows_service._validate_prelogin_fingerprint(
        fingerprint, browser_backend, browser_runtime_id)
    reused = await login_sessions_service._reuse_or_reject_interactive_login("twitter", False)
    if reused is not None:
        return reused
    task_id = uuid.uuid4().hex
    app_runtime.login_tasks[task_id] = login_sessions_service._login_task_state(
        status="opening", platform="twitter", creator=False, account_id=None)
    asyncio.create_task(login_flows_service._run_login(
        task_id, platform="twitter", proxy_choice=proxy,
        browser_backend=browser_backend,
        browser_runtime_id=browser_runtime_id,
        fingerprint_overrides=fingerprint_overrides))
    return {"task_id": task_id, "status": "opening",
            "hint": "已打开 X (Twitter) 登录窗口，请完成登录及两步验证(如有)"}


@router.get("/api/login/browser/poll")
async def login_browser_poll(task_id: str):
    info = app_runtime.login_tasks.get(task_id)
    if not info:
        raise HTTPException(404, "task 不存在")
    if info.get("status") in ("confirmed", "error", "expired"):
        # 终态:取走后清理
        app_runtime.login_tasks.pop(task_id, None)
    return info


class CookieIn(BaseModel):
    cookie: str
    nickname: str = ""
    platform: str = "douyin"            # douyin | xhs


@router.post("/api/login/cookie")
async def login_cookie(body: CookieIn):
    """Cookie / Token 粘贴兜底登录:转成浏览器登录态（Twitter 支持 40 位 auth_token、多字段导出文本或 JSON Cookie）。"""
    platform = body.platform if body.platform in ("douyin", "xhs", "rednote", "kuaishou", "shipinhao", "tiktok", "bilibili", "instagram", "twitter") else "douyin"
    if platform == "twitter":
        cookie_raw = (body.cookie or "").strip()
        tw_cookies, hint_user = parse_twitter_credential_input(cookie_raw)
        if not tw_cookies.get("auth_token"):
            if re.search(r"[0-9a-fA-F]{6,}\s*(?:\.{2,}|…)\s*[0-9a-fA-F]{4,}", cookie_raw):
                raise HTTPException(
                    400,
                    "检测到粘贴的 Token 带有省略号(...)，请复制并粘贴完整的 40 位 auth_token",
                )
            raise HTTPException(
                400,
                "未识别到有效的 Twitter Token (auth_token)，可直接粘贴 40 位 Token、账号----密码----Token 文本或完整 Cookie",
            )
        initial_state = build_twitter_storage_state(tw_cookies)
        with get_session() as s:
            probe_proxy = assign_proxy_from_pool(s, app_runtime.cfg, platform="twitter") or (
                app_runtime.cfg.proxies[0] if app_runtime.cfg.proxies else ""
            )
        tcli = TwitterClient(
            initial_state,
            app_runtime.cfg.engine.user_agent,
            proxy=probe_proxy,
            timeout=15.0,
        )
        try:
            async with tcli.session_scope():
                prof, err = await tcli.fetch_self_profile(hint_screen_name=hint_user)
        except Exception as exc:
            prof, err = {}, str(exc)

        if err.startswith("logged_out:"):
            raise HTTPException(400, "该 Twitter Token (auth_token) 无效或已过期，请核对后重试")

        final_cookies = tcli.cookies if tcli.cookies.get("auth_token") else tw_cookies
        final_state = build_twitter_storage_state(final_cookies)
        final_cookie_str = twitter_cookie_str_from_state(final_state)
        resolved_uid = str(prof.get("uid") or tcli.self_uid or "").strip()
        resolved_sn = str(prof.get("douyin_id") or prof.get("unique_id") or hint_user or "").strip()
        resolved_nick = (
            body.nickname.strip()
            or str(prof.get("nickname") or "").strip()
            or (f"@{resolved_sn}" if resolved_sn else "Twitter账号")
        )

        with get_session() as s:
            acc = None
            if resolved_uid:
                acc = s.exec(
                    select(DouyinAccount).where(
                        DouyinAccount.platform == "twitter",
                        DouyinAccount.uid == resolved_uid,
                    )
                ).first()
            if acc is None and resolved_sn:
                acc = s.exec(
                    select(DouyinAccount).where(
                        DouyinAccount.platform == "twitter",
                        DouyinAccount.douyin_id == resolved_sn,
                    )
                ).first()
            if acc is None:
                acc = DouyinAccount(
                    nickname=resolved_nick,
                    platform="twitter",
                    identity_mode="native",
                )
            elif body.nickname.strip():
                acc.nickname = body.nickname.strip()
            elif prof.get("nickname"):
                acc.nickname = str(prof["nickname"]).strip()

            acc.cookie = final_cookie_str
            acc.storage_state = final_state
            acc.creator_storage_state = final_state
            if resolved_uid:
                acc.uid = resolved_uid
            if resolved_sn:
                acc.sec_uid = resolved_sn
                acc.douyin_id = resolved_sn
            if prof.get("avatar"):
                acc.avatar = str(prof["avatar"])
            if prof:
                acc.follower_count = int(prof.get("follower_count") or 0)
                acc.following_count = int(prof.get("following_count") or 0)
                acc.aweme_count = int(prof.get("aweme_count") or 0)
                acc.total_favorited = int(prof.get("total_favorited") or 0)
                acc.status = "active"
            elif err.startswith("verification_required:"):
                acc.status = "verify"
            s.add(acc); s.commit(); s.refresh(acc)
            ensure_identity(acc, app_runtime.cfg, session=s, assign_proxy=True)
            s.add(acc); s.commit(); s.refresh(acc)
            return {
                "account_id": acc.id,
                "nickname": acc.nickname,
                "warning": err if err.startswith("verification_required:") else "",
            }

    state = cookie_string_to_state(body.cookie, platform)
    with get_session() as s:
        acc = DouyinAccount(
            nickname=body.nickname or "Cookie账号", platform=platform,
            identity_mode="native", cookie=body.cookie.strip(), storage_state=state,
            creator_storage_state=(state if platform in {"bilibili", "rednote", "instagram", "twitter"} else ""))
        s.add(acc); s.commit(); s.refresh(acc)
        # 分配画像(profile/UA/指纹/代理):Cookie 会在首次开持久 profile 时桥接注入
        ensure_identity(acc, app_runtime.cfg, session=s, assign_proxy=True)
        s.add(acc); s.commit(); s.refresh(acc)
        return {"account_id": acc.id, "nickname": acc.nickname}
