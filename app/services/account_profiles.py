"""多平台账号资料拉取、登录态检测与风控受控读取封装。"""
from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging

from fastapi import HTTPException

from sqlmodel import select

from app import runtime as app_runtime
from app.browser import fetch_bili_self_profile, fetch_channels_self_profile, fetch_facebook_self_profile, fetch_ig_self_profile, fetch_ks_self_profile, fetch_rednote_self_profile, fetch_self_profile, fetch_twitter_self_profile, fetch_xhs_self_profile, fetch_youtube_self_profile
from app.browser.tiktok_fetcher import fetch_tiktok_self_profile
from app.db import get_session
from app.models import AccountWork, DouyinAccount, FollowEdge
from app.platforms.bilibili import BilibiliClient
from app.platforms.channels import parse_self_user as parse_channels_self_user
from app.platforms.douyin import DouyinClient, cookie_from_state as douyin_cookie_from_state, parse_self_user
from app.platforms.facebook import FacebookClient, build_facebook_storage_state, cookie_str_from_state as facebook_cookie_str_from_state
from app.platforms.youtube import YouTubeClient
from app.platforms.instagram import InstagramClient
from app.platforms.kuaishou import parse_self_user as parse_ks_self_user
from app.platforms.rednote import RednoteApiClient, parse_rednote_self_user, cookie_str_from_state as rednote_cookie_str_from_state, rednote_creator_profile, has_a1 as rednote_has_a1
from app.platforms.twitter import TwitterClient, build_twitter_storage_state, cookie_str_from_state as twitter_cookie_str_from_state
from app.platforms.xhs import XhsApiClient, XhsApiError, has_a1, parse_self_user as parse_xhs_self_user, cookie_str_from_state as xhs_cookie_str_from_state
from app.risk import OperationKind, RiskCategory, classify_platform_error
from app.services.session_keepalive import (
    _cookies_meaningfully_changed,
    _parse_state_dict,
    merge_browser_state,
    persist_refreshed_session,
)
from app.transport_matrix import douyin_client_environment, resolve_transport


def _xhs_browser_reads_enabled() -> bool:
    """Whether the configured browser implementation supports page reads."""
    return bool(
        app_runtime.cfg.engine.xhs_read_mode == "browser"
        and app_runtime.browser is not None
        and callable(getattr(app_runtime.browser, "visible_page", None))
    )


def _direct_request_ua(identity_or_account) -> str:
    if identity_or_account is None:
        return app_runtime.cfg.engine.user_agent
    target = identity_or_account
    if not hasattr(target, "key"):
        builder = getattr(app_runtime.browser, "identity_for", None)
        if callable(builder):
            try:
                target = builder(target)
            except Exception:
                pass
    resolver = getattr(app_runtime.browser, "direct_request_user_agent", None)
    if callable(resolver):
        try:
            return resolver(target)
        except Exception:
            pass
    return str(getattr(target, "ua", "") or app_runtime.cfg.engine.user_agent)


# ─────────── 扫码登录(真实浏览器) ───────────
def _storage_has_cookie(state: str, name: str) -> bool:
    try:
        cookies = json.loads(state or "{}").get("cookies") or []
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return any(
        str(item.get("name") or "") == name
        and bool(str(item.get("value") or ""))
        for item in cookies if isinstance(item, dict)
    )


def _xhs_has_read_login_state(state: str) -> bool:
    """Main-site XHS reads require web_session; creator cookies are separate."""
    return _storage_has_cookie(state, "web_session")


async def _xhs_profile(state: str, proxy: str = "", *,
                       detailed: bool = False, user_agent: str = "",
                       account_id: int | None = None):
    """用签名直连 API 拿小红书账号资料(me 身份 + otherinfo 昵称/头像/粉丝)。
    返回 (user dict, error)。error == "logged_out" 表示登录态失效。"""
    cookie_str = xhs_cookie_str_from_state(state)
    if not has_a1(cookie_str):
        return {}, "logged_out"
    client = XhsApiClient(
        cookie_str,
        user_agent or app_runtime.cfg.engine.user_agent,
        timeout=app_runtime.cfg.engine.request_timeout_seconds,
        proxy=proxy,
        storage_state=state,
    )
    try:
        async with client.session_scope():
            me = await client.self_info()
    except XhsApiError as exc:
        if detailed:
            return {}, exc
        return {}, "logged_out" if exc.category == "auth" else exc.category
    except Exception as e:
        logging.getLogger("creatorhub.profile").warning("小红书 self_info 拉取失败: %r", e)
        return ({}, e) if detailed else ({}, "error")
    if not me or me.get("guest") is True or not me.get("user_id"):
        return {}, "logged_out"
    merged = dict(me)
    try:
        other = await client.user_info(me["user_id"])
        if other:
            merged = {**other, **me}      # me 提供身份,otherinfo 提供 basic_info/粉丝
    except Exception as exc:
        category, _signal = classify_platform_error(exc)
        if detailed and category in {
                RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK}:
            return {}, exc
    if account_id and getattr(app_runtime.cfg.engine, "keepalive_cookie_sync_enabled", True):
        new_state, changed = client.export_refreshed_state(state)
        if new_state and changed:
            persist_refreshed_session(
                account_id,
                "xhs",
                new_state,
                expected_state=state,
                scope="read",
                keepalive_note="小红书主站 Cookie 已同步",
            )
    return merged, ""


async def _fetch_channels_profile_with_retry(identity, attempts: int = 3) -> tuple[dict, str]:
    """视频号扫码后的会话传播窗口内重试，避免一次跳登录页就误判失效。"""
    result: tuple[dict, str] = ({}, "logged_out")
    for attempt in range(max(1, attempts)):
        if attempt:
            await asyncio.sleep(1.5 * attempt)
        result = await fetch_channels_self_profile(app_runtime.browser, identity)
        profile, error = result
        if profile or error != "logged_out":
            break
    return result


async def _run_account_read(account_id: int, kind: OperationKind, key: str,
                            operation, *, empty_result,
                            unexpected_detail: str = "",
                            allow_invalid_probe: bool = False,
                            interactive_read: bool = False):
    """Run one account-bound API read through the engine's unified gates."""
    if app_runtime.engine is None:
        raise HTTPException(503, "引擎未就绪")
    guarded_empty = empty_result
    if unexpected_detail and isinstance(empty_result, dict):
        guarded_empty = dict(empty_result)
        guarded_empty["_guard_error"] = True
    options = {"allow_invalid_probe": allow_invalid_probe}
    if interactive_read:
        options["interactive_read"] = True
    payload, error = await app_runtime.engine.guarded_read_pair(
        account_id, kind, key, operation, empty_result=guarded_empty,
        **options)
    guard_error = False
    if isinstance(payload, dict):
        payload = dict(payload)
        guard_error = bool(payload.pop("_guard_error", False))
    error_text = str(error or "")
    if error_text.startswith("risk_deferred:"):
        deferred = ({key: value for key, value in empty_result.items()
                     if not str(key).startswith("_")}
                    if isinstance(empty_result, dict) else {})
        deferred.update({
            "skipped": True,
            "reason": error_text.split(":", 1)[-1],
        })
        return None, deferred
    if unexpected_detail and guard_error and error:
        raise HTTPException(500, unexpected_detail)
    return (payload, None) if not error else (payload, error_text)


async def _enrich_account_profile(account_id: int, state: str, *,
                                  detailed: bool = False):
    """用登录态拉取账号资料；默认返回旧的状态字符串，详细模式附带原始错误。"""
    def _done(status: str, error=""):
        return (status, error) if detailed else status

    if not state:
        return _done("error", "error")
    with get_session() as s:
        a0 = s.get(DouyinAccount, account_id)
        if not a0:
            return _done("error", "error")
        platform = a0.platform
        creator_state = a0.creator_storage_state if a0 else ""
        proxy = (a0.proxy or "") if a0 else ""
        dy_transport = (resolve_transport(app_runtime.cfg, "douyin", "account_profile", a0)
                        if platform == "douyin" else None)
        identity = (None if (dy_transport and dy_transport["effective_mode"] == "api")
                    or (platform == "bilibili" and str(getattr(app_runtime.cfg.engine, "bilibili_read_mode", "hybrid") or "hybrid").lower() == "api")
                    or (platform == "instagram" and str(getattr(app_runtime.cfg.engine, "instagram_read_mode", "hybrid") or "hybrid").lower() == "api")
                    or (platform == "twitter" and str(getattr(app_runtime.cfg.engine, "twitter_read_mode", "hybrid") or "hybrid").lower() == "api")
                    or (platform == "facebook" and str(getattr(app_runtime.cfg.engine, "facebook_read_mode", "hybrid") or "hybrid").lower() == "api")
                    or (platform == "youtube" and str(getattr(app_runtime.cfg.engine, "youtube_read_mode", "hybrid") or "hybrid").lower() == "api")
                    else (app_runtime.browser.identity_for(a0)
                          if callable(getattr(app_runtime.browser, "identity_for", None))
                          else None))

    if platform not in {"douyin", "bilibili", "instagram", "twitter", "facebook", "youtube"} and app_runtime.browser is None:
        return _done("error", "browser_unavailable")

    async def _sync_browser_state(
        pf: str,
        ident,
        exp_state: str,
        exp_creator_state: str = "",
        *,
        scope: str = "read",
        sync_cookie_str: bool = False,
        note: str = "已同步浏览器最新登录态",
    ) -> bool:
        if (
            not getattr(app_runtime.cfg.engine, "keepalive_cookie_sync_enabled", True)
            or app_runtime.browser is None
            or ident is None
        ):
            return False
        capture = getattr(app_runtime.browser, "capture_live_storage_state", None)
        if not callable(capture):
            return False
        try:
            live = await capture(ident)
        except Exception:
            return False
        if not live:
            return False
        base = exp_creator_state if scope == "creator" else exp_state
        merged, changed = merge_browser_state(base, live, platform=pf, scope=scope)
        if not merged or not changed:
            return False
        return persist_refreshed_session(
            account_id,
            pf,
            merged,
            expected_state=exp_state,
            expected_creator_state=exp_creator_state,
            scope=scope,
            sync_cookie_str=sync_cookie_str,
            keepalive_note=note,
        )

    def _sync_api_state(
        pf: str,
        new_state: dict | None,
        changed: bool,
        *,
        exp_state: str = "",
        exp_creator_state: str = "",
        scope: str = "read",
        sync_cookie_str: bool = False,
        note: str = "已同步平台最新 Cookie",
    ) -> bool:
        if (
            not getattr(app_runtime.cfg.engine, "keepalive_cookie_sync_enabled", True)
            or not new_state
            or not changed
        ):
            return False
        return persist_refreshed_session(
            account_id,
            pf,
            new_state,
            expected_state=exp_state,
            expected_creator_state=exp_creator_state,
            scope=scope,
            sync_cookie_str=sync_cookie_str,
            keepalive_note=note,
        )

    keepalive_note = ""
    cookie_refreshed = False

    # XHS 创作者号:用创作平台「我的信息」拿资料 + 判活(www 接口对创作态拿不到)
    if platform == "xhs" and creator_state:
        from app.platforms.xhs import creator_profile, creator_check_with_state
        xhs_ua = _direct_request_ua(identity) if identity is not None else (a0.ua or app_runtime.cfg.engine.user_agent)
        profile_error = ""
        try:
            if detailed:
                prof, profile_error = await creator_profile(
                    creator_state, proxy=proxy, preserve_error=True, user_agent=xhs_ua)
            else:
                prof = await creator_profile(creator_state, proxy=proxy, user_agent=xhs_ua)
        except TypeError:
            if detailed:
                prof, profile_error = await creator_profile(
                    creator_state, proxy=proxy, preserve_error=True)
            else:
                prof = await creator_profile(creator_state, proxy=proxy)
        if prof and (prof.get("nickname") or prof.get("douyin_id")):
            with get_session() as s:
                acc = s.get(DouyinAccount, account_id)
                if acc:
                    now_utc = datetime.utcnow()
                    if prof.get("nickname"):
                        acc.nickname = prof["nickname"]
                    acc.sec_uid = prof.get("sec_uid") or acc.sec_uid
                    acc.douyin_id = prof.get("douyin_id") or acc.douyin_id
                    acc.avatar = prof.get("avatar") or acc.avatar
                    acc.follower_count = prof.get("follower_count") or acc.follower_count
                    acc.aweme_count = prof.get("aweme_count") or acc.aweme_count
                    acc.status = "active"
                    acc.last_active_at = now_utc
                    acc.last_keepalive_at = now_utc
                    acc.keepalive_note = "小红书创作者登录态有效"
                    s.add(acc); s.commit()
            return _done("ok")
        if profile_error:
            category, _signal = classify_platform_error(profile_error)
            if category in {
                    RiskCategory.RISK, RiskCategory.AUTH,
                    RiskCategory.NETWORK}:
                status = "invalid" if category == RiskCategory.AUTH else "error"
                return _done(status, profile_error)
        chk, check_error, c_new, c_changed = await creator_check_with_state(
            creator_state, proxy=proxy, preserve_error=detailed, user_agent=xhs_ua
        )
        if chk is True:
            synced = _sync_api_state(
                "xhs", c_new, c_changed,
                exp_creator_state=creator_state,
                scope="creator",
                note="小红书创作者 Cookie 已同步",
            )
            with get_session() as s:
                acc = s.get(DouyinAccount, account_id)
                if acc:
                    now_utc = datetime.utcnow()
                    acc.status = "active"
                    acc.last_active_at = now_utc
                    acc.last_keepalive_at = now_utc
                    acc.keepalive_note = "小红书创作者 Cookie 已同步" if synced else "小红书创作者登录态有效"
                    s.add(acc); s.commit()
            return _done("ok")
        if check_error:
            category, _signal = classify_platform_error(check_error)
            if category in {
                    RiskCategory.RISK, RiskCategory.AUTH,
                    RiskCategory.NETWORK}:
                status = "invalid" if category == RiskCategory.AUTH else "error"
                return _done(status, check_error)
        if chk is None:
            return _done("error", check_error or profile_error or "error")
        with get_session() as s:
            acc = s.get(DouyinAccount, account_id)
            if acc:
                acc.status = "invalid"
                acc.keepalive_note = "登录态已失效，请重新登录"
                s.add(acc); s.commit()
        return _done("invalid", check_error or "logged_out")

    try:
        if platform == "xhs":
            # Default to the account browser so profile checks keep the same
            # UA/TLS/Client-Hints/profile that created the login cookies.
            if _xhs_browser_reads_enabled():
                u, err = await fetch_xhs_self_profile(app_runtime.browser, identity)
                if u and await _sync_browser_state(
                    "xhs", identity, state, scope="read", note="小红书浏览器登录态已同步"
                ):
                    cookie_refreshed = True
                    keepalive_note = "小红书浏览器登录态已同步"
            else:
                u, err = await _xhs_profile(
                    state, proxy, detailed=detailed,
                    user_agent=_direct_request_ua(identity),
                    account_id=account_id)
        elif platform == "kuaishou":
            u, err = await fetch_ks_self_profile(app_runtime.browser, identity)
            if u and await _sync_browser_state(
                "kuaishou", identity, state, creator_state,
                scope="read", note="快手浏览器登录态已保活同步"
            ):
                cookie_refreshed = True
                keepalive_note = "快手登录态已同步"
        elif platform == "shipinhao":
            # 视频号扫码授权后，服务端会话偶尔要数秒才在新页面中生效。
            # 单次打开被重定向到登录页不能立即把刚添加的账号判为失效。
            u, err = await _fetch_channels_profile_with_retry(identity)
            if u and await _sync_browser_state(
                "shipinhao", identity, state, creator_state,
                scope="read", note="视频号助手会话已保活同步"
            ):
                cookie_refreshed = True
                keepalive_note = "视频号会话 Cookie 已同步"
        elif platform == "tiktok":
            u, err = await fetch_tiktok_self_profile(
                app_runtime.browser, identity, block_media=True)
            if u and await _sync_browser_state(
                "tiktok", identity, state, creator_state,
                scope="read", note="TikTok 浏览器登录态已同步至直连快照"
            ):
                cookie_refreshed = True
                keepalive_note = "TikTok Cookie 已同步"
        elif platform == "bilibili":
            bmode = str(getattr(app_runtime.cfg.engine, "bilibili_read_mode", "hybrid") or "hybrid").lower()
            u, err = None, ""
            used_browser = False
            if bmode in {"api", "hybrid"} and state:
                bcli_kwargs = {
                    "proxy": proxy,
                    "timeout": app_runtime.cfg.engine.request_timeout_seconds,
                    "account_seed": a0.fp_seed or str(a0.id or ""),
                }
                bili_ua = _direct_request_ua(identity if identity is not None else a0)
                try:
                    bcli = BilibiliClient(
                        state, bili_ua, **bcli_kwargs)
                except TypeError:
                    bcli_kwargs.pop("account_seed", None)
                    bcli = BilibiliClient(
                        state, bili_ua, **bcli_kwargs)
                async with bcli.session_scope():
                    u, err = await bcli.fetch_self_profile()
                    if u:
                        if getattr(app_runtime.cfg.engine, "bilibili_auto_cookie_refresh", True):
                            ref = await bcli.maybe_refresh_cookies(state)
                            if ref.get("refreshed") and ref.get("state"):
                                if _sync_api_state(
                                    "bilibili", ref["state"], True,
                                    exp_state=state, scope="read",
                                    note="B站官方 RSA-OAEP Cookie 续期成功",
                                ):
                                    cookie_refreshed = True
                                    keepalive_note = "B站官方 Cookie 续期成功"
                            elif ref.get("missing_refresh_token") and bmode == "hybrid" and app_runtime.browser is not None and identity is not None:
                                bu, _berr = await fetch_bili_self_profile(app_runtime.browser, identity)
                                if bu:
                                    u = bu
                                    used_browser = True
                                    keepalive_note = "B站已通过浏览器触发续期并补采令牌"
                            elif ref.get("changed") and ref.get("state"):
                                if _sync_api_state(
                                    "bilibili", ref["state"], True,
                                    exp_state=state, scope="read",
                                    note="B站 API 响应 Cookie 已同步",
                                ):
                                    cookie_refreshed = True
                                    keepalive_note = "B站响应 Cookie 已同步"
                        else:
                            b_new, b_changed = bcli.export_refreshed_state(state)
                            if _sync_api_state(
                                "bilibili", b_new, b_changed,
                                exp_state=state, scope="read",
                                note="B站 API 响应 Cookie 已同步",
                            ):
                                cookie_refreshed = True
                                keepalive_note = "B站响应 Cookie 已同步"
            auth_failed = classify_platform_error(err)[0] == RiskCategory.AUTH
            if not u and bmode == "hybrid" and not auth_failed and app_runtime.browser is not None:
                u, err = await fetch_bili_self_profile(app_runtime.browser, identity)
                used_browser = True
            elif not u and bmode == "browser":
                if app_runtime.browser is None:
                    return _done("error", "browser_unavailable")
                u, err = await fetch_bili_self_profile(app_runtime.browser, identity)
                used_browser = True
            if u and used_browser and await _sync_browser_state(
                "bilibili", identity, state, creator_state,
                scope="read", note="B站浏览器登录态与续期令牌已同步"
            ):
                cookie_refreshed = True
                keepalive_note = keepalive_note or "B站浏览器登录态已同步"
        elif platform == "rednote":
            rmode = str(getattr(app_runtime.cfg.engine, "rednote_read_mode", "hybrid") or "hybrid").lower()
            u, err = None, ""
            used_browser = False
            rn_state = state or creator_state
            rn_cookie = rednote_cookie_str_from_state(rn_state)
            if rmode in {"api", "hybrid"} and rn_cookie and rednote_has_a1(rn_cookie):
                rcli = RednoteApiClient(
                    rn_cookie,
                    _direct_request_ua(identity),
                    timeout=app_runtime.cfg.engine.request_timeout_seconds,
                    proxy=proxy,
                    storage_state=rn_state,
                )
                try:
                    async with rcli.session_scope():
                        me = await rcli.self_info()
                    if me and not me.get("guest"):
                        u = me
                        rn_scope = "read" if state else "creator"
                        rn_new, rn_changed = rcli.export_refreshed_state(rn_state, scope=rn_scope)
                        if _sync_api_state(
                            "rednote", rn_new, rn_changed,
                            exp_state=(state if rn_scope == "read" else ""),
                            exp_creator_state=(creator_state if rn_scope == "creator" else ""),
                            scope=rn_scope,
                            note="Rednote API 响应 Cookie 已同步",
                        ):
                            cookie_refreshed = True
                            keepalive_note = "Rednote Cookie 已同步"
                except Exception as exc:
                    err = exc
            if not u and creator_state and rmode in {"api", "hybrid"}:
                rn_ua = _direct_request_ua(identity) if identity is not None else (a0.ua or app_runtime.cfg.engine.user_agent)
                try:
                    prof, perr = await rednote_creator_profile(
                        creator_state, proxy=proxy, preserve_error=True, user_agent=rn_ua
                    )
                except TypeError:
                    prof, perr = await rednote_creator_profile(
                        creator_state, proxy=proxy, preserve_error=True
                    )
                if prof and (prof.get("nickname") or prof.get("douyin_id")):
                    with get_session() as s:
                        acc = s.get(DouyinAccount, account_id)
                        if acc:
                            now_utc = datetime.utcnow()
                            if prof.get("nickname"):
                                acc.nickname = prof["nickname"]
                            acc.sec_uid = prof.get("sec_uid") or acc.sec_uid
                            acc.douyin_id = prof.get("douyin_id") or acc.douyin_id
                            acc.avatar = prof.get("avatar") or acc.avatar
                            acc.follower_count = prof.get("follower_count") or acc.follower_count
                            acc.aweme_count = prof.get("aweme_count") or acc.aweme_count
                            acc.status = "active"
                            acc.last_active_at = now_utc
                            acc.last_keepalive_at = now_utc
                            acc.keepalive_note = "Rednote 创作者登录态有效"
                            s.add(acc); s.commit()
                    return _done("ok")
                if perr and not err:
                    err = perr
            auth_failed = classify_platform_error(err)[0] == RiskCategory.AUTH
            if not u and (rmode == "browser" or (rmode == "hybrid" and not auth_failed)):
                if app_runtime.browser is None:
                    return _done("error", "browser_unavailable")
                u, err = await fetch_rednote_self_profile(app_runtime.browser, identity)
                used_browser = True
            if u and used_browser and await _sync_browser_state(
                "rednote", identity, state, creator_state,
                scope="read", note="Rednote 浏览器登录态已同步"
            ):
                cookie_refreshed = True
                keepalive_note = "Rednote 浏览器登录态已同步"
        elif platform == "instagram":
            imode = str(getattr(app_runtime.cfg.engine, "instagram_read_mode", "hybrid") or "hybrid").lower()
            u, err = None, ""
            used_browser = False
            if imode in {"api", "hybrid"} and state:
                icli = InstagramClient(
                    state,
                    _direct_request_ua(identity if identity is not None else a0),
                    proxy=proxy,
                    timeout=app_runtime.cfg.engine.request_timeout_seconds,
                    account_seed=a0.fp_seed or a0.profile_dir or "",
                    timezone_id=a0.timezone_id or "Asia/Shanghai",
                )
                async with icli.session_scope():
                    u, err = await icli.fetch_self_profile()
                if u:
                    ig_new, ig_changed = icli.export_refreshed_state(state)
                    if _sync_api_state(
                        "instagram", ig_new, ig_changed,
                        exp_state=state, scope="read",
                        note="Instagram API 会话凭据/Cookie 已同步",
                    ):
                        cookie_refreshed = True
                        keepalive_note = "Instagram 会话已同步"
            err_category = classify_platform_error(err)[0] if err else RiskCategory.BUSINESS
            hard_block = err_category in {RiskCategory.AUTH, RiskCategory.RISK}
            if not u and imode == "hybrid" and not hard_block and app_runtime.browser is not None:
                u, err = await fetch_ig_self_profile(app_runtime.browser, identity)
                used_browser = True
            elif not u and imode == "browser":
                if app_runtime.browser is None:
                    return _done("error", "browser_unavailable")
                u, err = await fetch_ig_self_profile(app_runtime.browser, identity)
                used_browser = True
            if u and used_browser and await _sync_browser_state(
                "instagram", identity, state, creator_state,
                scope="read", note="Instagram 浏览器登录态已同步"
            ):
                cookie_refreshed = True
                keepalive_note = "Instagram 浏览器登录态已同步"
        elif platform == "twitter":
            tmode = str(getattr(app_runtime.cfg.engine, "twitter_read_mode", "hybrid") or "hybrid").lower()
            u, err = None, ""
            used_browser = False
            tw_enriched_state = ""
            if tmode in {"api", "hybrid"} and state:
                tcli = TwitterClient(
                    state,
                    _direct_request_ua(identity if identity is not None else a0),
                    proxy=proxy,
                    timeout=app_runtime.cfg.engine.request_timeout_seconds,
                )
                async with tcli.session_scope():
                    u, err = await tcli.fetch_self_profile(
                        hint_screen_name=(a0.douyin_id or a0.sec_uid or "")
                    )
                if u:
                    tw_new, tw_changed = tcli.export_refreshed_state(state)
                    if _sync_api_state(
                        "twitter", tw_new, tw_changed,
                        exp_state=state, scope="read", sync_cookie_str=True,
                        note="X (Twitter) API Cookie/ct0 已同步",
                    ):
                        cookie_refreshed = True
                        keepalive_note = "X Cookie/ct0 已同步"
                    elif tcli.cookies.get("auth_token") and tcli.cookies.get("ct0"):
                        tw_enriched_state = build_twitter_storage_state(tcli.cookies)
            auth_failed = classify_platform_error(err)[0] == RiskCategory.AUTH
            if not u and tmode == "hybrid" and not auth_failed and app_runtime.browser is not None:
                u, err = await fetch_twitter_self_profile(app_runtime.browser, identity)
                used_browser = True
            elif not u and tmode == "browser":
                if app_runtime.browser is None:
                    return _done("error", "browser_unavailable")
                u, err = await fetch_twitter_self_profile(app_runtime.browser, identity)
                used_browser = True
            if u and used_browser and await _sync_browser_state(
                "twitter", identity, state, creator_state,
                scope="read", sync_cookie_str=True,
                note="X (Twitter) 浏览器登录态已同步"
            ):
                cookie_refreshed = True
                keepalive_note = "X 浏览器登录态已同步"
        elif platform == "facebook":
            fmode = str(getattr(app_runtime.cfg.engine, "facebook_read_mode", "hybrid") or "hybrid").lower()
            u, err = None, ""
            used_browser = False
            fb_enriched_state = ""
            if fmode in {"api", "hybrid"} and state:
                fcli = FacebookClient(
                    state,
                    _direct_request_ua(identity if identity is not None else a0),
                    proxy=proxy,
                    timeout=app_runtime.cfg.engine.request_timeout_seconds,
                )
                async with fcli.session_scope():
                    u, err = await fcli.fetch_self_profile()
                if u and fcli.cookie_changed and fcli.has_login_cookies:
                    fb_enriched_state = build_facebook_storage_state(fcli.cookies)
            auth_failed = classify_platform_error(err)[0] == RiskCategory.AUTH
            if not u and fmode == "hybrid" and not auth_failed and app_runtime.browser is not None:
                u, err = await fetch_facebook_self_profile(app_runtime.browser, identity)
                used_browser = True
            elif not u and fmode == "browser":
                if app_runtime.browser is None:
                    return _done("error", "browser_unavailable")
                u, err = await fetch_facebook_self_profile(app_runtime.browser, identity)
                used_browser = True
            if u and used_browser and await _sync_browser_state(
                "facebook", identity, state, creator_state,
                scope="read", sync_cookie_str=True,
                note="Facebook 浏览器登录态已同步"
            ):
                cookie_refreshed = True
                keepalive_note = "Facebook 浏览器登录态已同步"
        elif platform == "youtube":
            ymode = str(getattr(app_runtime.cfg.engine, "youtube_read_mode", "hybrid") or "hybrid").lower()
            u, err = None, ""
            used_browser = False
            if ymode in {"api", "hybrid"} and state:
                ycli = YouTubeClient(
                    state,
                    _direct_request_ua(identity if identity is not None else a0),
                    proxy=proxy,
                    timeout=app_runtime.cfg.engine.request_timeout_seconds,
                    locale=a0.locale or "en-US",
                )
                async with ycli.session_scope():
                    u, err = await ycli.fetch_self_profile()
            # hybrid 模式下即使 HTTP Cookie 因 Google CookieMismatch/DBSC 报 logged_out，
            # 仍需回退到隔离浏览器持久化 Profile 校验真实登录态并回写完整 storage_state。
            hard_block = str(err or "").startswith(("verification_required:", "rate_limit:"))
            if not u and ymode == "hybrid" and not hard_block and app_runtime.browser is not None:
                u, err = await fetch_youtube_self_profile(app_runtime.browser, identity)
                used_browser = True
            elif not u and ymode == "browser":
                if app_runtime.browser is None:
                    return _done("error", "browser_unavailable")
                u, err = await fetch_youtube_self_profile(app_runtime.browser, identity)
                used_browser = True
            if u and used_browser and await _sync_browser_state(
                "youtube", identity, state, creator_state,
                scope="read", sync_cookie_str=True,
                note="YouTube 浏览器登录态已同步"
            ):
                cookie_refreshed = True
                keepalive_note = "YouTube 浏览器登录态已同步"
        else:
            mode = dy_transport["effective_mode"]
            u, err = None, ""
            used_browser = False
            if mode in {"api", "hybrid"}:
                cookie = douyin_cookie_from_state(state)
                if not cookie:
                    err = "api_missing_cookie"
                else:
                    try:
                        client = DouyinClient(
                            cookie, _direct_request_ua(identity if identity is not None else a0),
                            timeout=app_runtime.cfg.engine.request_timeout_seconds,
                            proxy=proxy,
                            storage_state=state,
                            **douyin_client_environment(a0))
                        async with client.session_scope():
                            u = await client.fetch_self_profile()
                        err = client.last_error or ("" if u else "empty_response")
                        if u:
                            exported = client.export_storage_state()
                            changed = _cookies_meaningfully_changed(
                                _parse_state_dict(state)["cookies"],
                                exported.get("cookies") or [],
                            )
                            if _sync_api_state(
                                "douyin", exported, changed,
                                exp_state=state, scope="read",
                                note="抖音 API 响应 Cookie/msToken 已续期同步",
                            ):
                                cookie_refreshed = True
                                keepalive_note = "抖音 API Cookie 已同步"
                    except Exception as exc:
                        err = exc
            auth_failed = classify_platform_error(err)[0] == RiskCategory.AUTH
            if not u and mode == "hybrid" and not auth_failed:
                if app_runtime.browser is None:
                    return _done("error", err or "browser_unavailable")
                u, browser_err = await fetch_self_profile(app_runtime.browser, identity)
                err = browser_err or err
                used_browser = True
            elif not u and mode == "browser":
                if app_runtime.browser is None:
                    return _done("error", "browser_unavailable")
                u, err = await fetch_self_profile(app_runtime.browser, identity)
                used_browser = True
            if u and used_browser and await _sync_browser_state(
                "douyin", identity, state, creator_state,
                scope="read", note="抖音浏览器登录态已同步"
            ):
                cookie_refreshed = True
                keepalive_note = "抖音浏览器登录态已同步"
    except Exception as exc:
        category, _signal = classify_platform_error(exc)
        status = "invalid" if detailed and category == RiskCategory.AUTH else "error"
        return _done(status, exc)
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            return _done("error", "error")
        if u:
            now_utc = datetime.utcnow()
            if platform == "xhs":
                p = parse_xhs_self_user(u)
            elif platform == "rednote":
                p = parse_rednote_self_user(u)
            elif platform == "kuaishou":
                p = parse_ks_self_user(u)
            elif platform == "shipinhao":
                p = parse_channels_self_user(u)
            elif platform == "tiktok":
                p = {
                    "nickname": u.get("nickname") or "",
                    "sec_uid": u.get("sec_uid") or "",
                    "douyin_id": u.get("unique_id") or "",
                    "avatar": u.get("avatar") or "",
                    "follower_count": int(u.get("follower_count") or 0),
                    "following_count": int(u.get("following_count") or 0),
                    "aweme_count": int(u.get("video_count") or 0),
                }
            elif platform in {"bilibili", "instagram", "twitter", "facebook", "youtube"}:
                p = dict(u)
                if platform == "twitter" and locals().get("tw_enriched_state") and not cookie_refreshed:
                    acc.storage_state = tw_enriched_state
                    acc.creator_storage_state = tw_enriched_state
                    acc.cookie = twitter_cookie_str_from_state(tw_enriched_state)
                elif platform == "facebook" and locals().get("fb_enriched_state") and not cookie_refreshed:
                    acc.storage_state = fb_enriched_state
                    acc.creator_storage_state = fb_enriched_state
                    acc.cookie = facebook_cookie_str_from_state(fb_enriched_state)
            else:
                p = parse_self_user(u)
            if platform == "youtube":
                # 清理历史误写入的 Google 邮箱（邮箱不是合法频道 handle / UC ID）
                if acc.sec_uid and "@" in acc.sec_uid and not acc.sec_uid.startswith("@"):
                    acc.sec_uid = ""
                if acc.douyin_id and "@" in acc.douyin_id and not acc.douyin_id.startswith("@"):
                    acc.douyin_id = ""
            if p.get("nickname"):
                acc.nickname = p["nickname"]
            acc.sec_uid = p.get("sec_uid") or acc.sec_uid
            if p.get("uid"):
                acc.uid = str(p["uid"])
            acc.douyin_id = p.get("douyin_id") or acc.douyin_id
            acc.avatar = p.get("avatar") or acc.avatar
            if platform in {"kuaishou", "tiktok", "bilibili", "instagram", "twitter"}:
                # 上述平台资料接口会明确返回 0；不能用 ``or`` 保留旧值，否则
                # 取消关注、删光作品等场景会让账号卡片一直显示过期数据。
                acc.follower_count = int(p.get("follower_count") or 0)
                acc.following_count = int(p.get("following_count") or 0)
                acc.aweme_count = int(p.get("aweme_count") or 0)
                acc.total_favorited = int(p.get("total_favorited") or 0)
                acc.gender = str(p.get("gender") or "")
            elif platform == "facebook":
                # Facebook 个人主页 SSR 不返回总作品数，且常只暴露好友数而非粉丝数；
                # 严禁以 0 覆盖已同步的作品/粉丝/关注数据。
                works_cnt = len(s.exec(
                    select(AccountWork.id).where(AccountWork.account_id == account_id)
                ).all())
                fan_cnt = len(s.exec(
                    select(FollowEdge.id).where(
                        FollowEdge.account_id == account_id,
                        FollowEdge.direction == "fan",
                    )
                ).all())
                following_cnt = len(s.exec(
                    select(FollowEdge.id).where(
                        FollowEdge.account_id == account_id,
                        FollowEdge.direction == "following",
                    )
                ).all())
                raw_fans = int(p.get("follower_count") or 0)
                raw_following = int(p.get("following_count") or 0)
                if raw_fans > 0 and not p.get("follower_count_is_friends_fallback"):
                    acc.follower_count = raw_fans
                elif fan_cnt > 0:
                    acc.follower_count = fan_cnt
                elif raw_fans > 0 and not acc.follower_count:
                    acc.follower_count = raw_fans
                if raw_following > 0 and not p.get("following_count_is_friends_fallback"):
                    acc.following_count = raw_following
                elif following_cnt > 0:
                    acc.following_count = following_cnt
                elif raw_following > 0 and not acc.following_count:
                    acc.following_count = raw_following
                acc.aweme_count = max(
                    int(p.get("aweme_count") or 0),
                    int(acc.aweme_count or 0),
                    works_cnt,
                )
                acc.total_favorited = int(p.get("total_favorited") or 0) or acc.total_favorited
                if p.get("gender"):
                    acc.gender = str(p.get("gender") or "")
            elif platform == "youtube":
                works_cnt = len(s.exec(
                    select(AccountWork.id).where(AccountWork.account_id == account_id)
                ).all())
                following_cnt = len(s.exec(
                    select(FollowEdge.id).where(
                        FollowEdge.account_id == account_id,
                        FollowEdge.direction == "following",
                    )
                ).all())
                raw_subs = int(p.get("follower_count") or 0)
                if raw_subs > 0:
                    acc.follower_count = raw_subs
                acc.following_count = max(int(p.get("following_count") or 0), int(acc.following_count or 0), following_cnt)
                acc.aweme_count = max(int(p.get("aweme_count") or 0), int(acc.aweme_count or 0), works_cnt)
                acc.total_favorited = int(p.get("total_favorited") or 0) or acc.total_favorited
            else:
                acc.follower_count = p.get("follower_count") or acc.follower_count
                acc.aweme_count = p.get("aweme_count") or acc.aweme_count
            acc.status = "active"
            acc.last_active_at = now_utc
            acc.last_keepalive_at = now_utc
            acc.keepalive_note = (
                keepalive_note
                or ("已同步最新登录态" if cookie_refreshed else "登录态有效")
            )[:200]
            s.add(acc); s.commit()
            return _done("ok")
        if classify_platform_error(err)[0] == RiskCategory.AUTH:
            acc.status = "invalid"
            acc.keepalive_note = "登录态已失效，请重新登录"
            s.add(acc); s.commit()
            return _done("invalid", err or "logged_out")
    return _done("error", err or "error")


def _merge_prelogin_fingerprint_fields(
        generated: dict, detected: dict | None,
        overrides: dict | None) -> dict:
    """Merge an IP-derived baseline with the user's first-login choices."""
    detected = detected or {}
    merged = dict(generated)
    merged.update({
        "fp_source_ip": detected.get("source_ip", ""),
        "fp_country": detected.get("country", ""),
        "fp_region": detected.get("region", ""),
        "fp_city": detected.get("city", ""),
        "fp_platform": "",
        "fp_platform_version": "",
        "fp_brand": "",
        "fp_brand_version": "",
        "fp_hardware_concurrency": 0,
        "fp_gpu_vendor": "",
        "fp_gpu_renderer": "",
        "fp_accept_languages": "",
        "fp_disable_spoofing": "",
        "fp_language_mode": "auto",
        "fp_timezone_mode": "auto",
        "fp_viewport_mode": "auto",
        "fp_location_mode": "auto",
        "fp_geolocation_permission": "allow",
        "fp_webrtc_mode": "conceal",
        "fp_extra_args": "",
    })
    if detected:
        for name in ("fp_seed", "timezone_id", "locale", "viewport_w",
                     "viewport_h", "geo_lat", "geo_lon"):
            if name in detected:
                merged[name] = detected[name]
    if not overrides:
        return merged

    for name in (
        "fp_seed", "fp_platform", "fp_platform_version", "fp_brand",
        "fp_brand_version", "fp_hardware_concurrency", "fp_gpu_vendor",
        "fp_gpu_renderer", "fp_disable_spoofing", "fp_language_mode",
        "fp_timezone_mode", "fp_viewport_mode", "fp_location_mode",
        "fp_geolocation_permission", "fp_webrtc_mode", "fp_extra_args",
    ):
        merged[name] = overrides[name]

    # Automatic groups keep the values derived from the selected egress IP.
    # Custom groups replace only their own settings.
    if overrides["fp_language_mode"] == "custom":
        merged["locale"] = overrides["locale"]
        merged["fp_accept_languages"] = overrides["fp_accept_languages"]
    if overrides["fp_timezone_mode"] == "custom":
        merged["timezone_id"] = overrides["timezone_id"]
    if overrides["fp_viewport_mode"] == "custom":
        merged["viewport_w"] = overrides["viewport_w"]
        merged["viewport_h"] = overrides["viewport_h"]
    if overrides["fp_location_mode"] == "custom":
        for name in ("fp_source_ip", "fp_country", "fp_region", "fp_city",
                     "geo_lat", "geo_lon"):
            merged[name] = overrides[name]
    return merged
