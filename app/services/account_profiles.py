"""多平台账号资料拉取、登录态检测与风控受控读取封装。"""
from __future__ import annotations

import asyncio
import json
import logging

from fastapi import HTTPException

from app import runtime as app_runtime
from app.browser import fetch_bili_self_profile, fetch_channels_self_profile, fetch_ig_self_profile, fetch_ks_self_profile, fetch_rednote_self_profile, fetch_self_profile, fetch_twitter_self_profile, fetch_xhs_self_profile
from app.browser.tiktok_fetcher import fetch_tiktok_self_profile
from app.db import get_session
from app.models import DouyinAccount
from app.platforms.bilibili import BilibiliClient
from app.platforms.channels import parse_self_user as parse_channels_self_user
from app.platforms.douyin import DouyinClient, cookie_from_state as douyin_cookie_from_state, parse_self_user
from app.platforms.instagram import InstagramClient
from app.platforms.kuaishou import parse_self_user as parse_ks_self_user
from app.platforms.rednote import RednoteApiClient, parse_rednote_self_user, cookie_str_from_state as rednote_cookie_str_from_state, rednote_creator_profile, has_a1 as rednote_has_a1
from app.platforms.twitter import TwitterClient, build_twitter_storage_state, cookie_str_from_state as twitter_cookie_str_from_state
from app.platforms.xhs import XhsApiClient, XhsApiError, has_a1, parse_self_user as parse_xhs_self_user, cookie_str_from_state as xhs_cookie_str_from_state
from app.risk import OperationKind, RiskCategory, classify_platform_error
from app.transport_matrix import douyin_client_environment, resolve_transport


def _xhs_browser_reads_enabled() -> bool:
    """Whether the configured browser implementation supports page reads."""
    return bool(
        app_runtime.cfg.engine.xhs_read_mode == "browser"
        and app_runtime.browser is not None
        and callable(getattr(app_runtime.browser, "visible_page", None))
    )


def _direct_request_ua(identity) -> str:
    resolver = getattr(app_runtime.browser, "direct_request_user_agent", None)
    if callable(resolver):
        return resolver(identity)
    return str(getattr(identity, "ua", "") or app_runtime.cfg.engine.user_agent)


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
                       detailed: bool = False, user_agent: str = ""):
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
                    else (app_runtime.browser.identity_for(a0)
                          if callable(getattr(app_runtime.browser, "identity_for", None))
                          else None))

    if platform not in {"douyin", "bilibili", "instagram", "twitter"} and app_runtime.browser is None:
        return _done("error", "browser_unavailable")

    # XHS 创作者号:用创作平台「我的信息」拿资料 + 判活(www 接口对创作态拿不到)
    if platform == "xhs" and creator_state:
        from app.platforms.xhs import creator_profile, creator_check
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
                    if prof.get("nickname"):
                        acc.nickname = prof["nickname"]
                    acc.sec_uid = prof.get("sec_uid") or acc.sec_uid
                    acc.douyin_id = prof.get("douyin_id") or acc.douyin_id
                    acc.avatar = prof.get("avatar") or acc.avatar
                    acc.follower_count = prof.get("follower_count") or acc.follower_count
                    acc.aweme_count = prof.get("aweme_count") or acc.aweme_count
                    acc.status = "active"
                    s.add(acc); s.commit()
            return _done("ok")
        if profile_error:
            category, _signal = classify_platform_error(profile_error)
            if category in {
                    RiskCategory.RISK, RiskCategory.AUTH,
                    RiskCategory.NETWORK}:
                status = "invalid" if category == RiskCategory.AUTH else "error"
                return _done(status, profile_error)
        check_error = ""
        try:
            if detailed:
                chk, check_error = await creator_check(
                    creator_state, proxy=proxy, preserve_error=True, user_agent=xhs_ua)
            else:
                chk = await creator_check(creator_state, proxy=proxy, user_agent=xhs_ua)
        except TypeError:
            if detailed:
                chk, check_error = await creator_check(
                    creator_state, proxy=proxy, preserve_error=True)
            else:
                chk = await creator_check(creator_state, proxy=proxy)
        if chk is True:
            with get_session() as s:
                acc = s.get(DouyinAccount, account_id)
                if acc:
                    acc.status = "active"
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
                s.add(acc); s.commit()
        return _done("invalid", check_error or "logged_out")

    try:
        if platform == "xhs":
            # Default to the account browser so profile checks keep the same
            # UA/TLS/Client-Hints/profile that created the login cookies.
            if _xhs_browser_reads_enabled():
                u, err = await fetch_xhs_self_profile(app_runtime.browser, identity)
            else:
                u, err = await _xhs_profile(
                    state, proxy, detailed=detailed,
                    user_agent=_direct_request_ua(identity))
        elif platform == "kuaishou":
            u, err = await fetch_ks_self_profile(app_runtime.browser, identity)
        elif platform == "shipinhao":
            # 视频号扫码授权后，服务端会话偶尔要数秒才在新页面中生效。
            # 单次打开被重定向到登录页不能立即把刚添加的账号判为失效。
            u, err = await _fetch_channels_profile_with_retry(identity)
        elif platform == "tiktok":
            u, err = await fetch_tiktok_self_profile(
                app_runtime.browser, identity, block_media=True)
        elif platform == "bilibili":
            bmode = str(getattr(app_runtime.cfg.engine, "bilibili_read_mode", "hybrid") or "hybrid").lower()
            u, err = None, ""
            if bmode in {"api", "hybrid"} and state:
                bcli_kwargs = {
                    "proxy": proxy,
                    "timeout": app_runtime.cfg.engine.request_timeout_seconds,
                    "account_seed": a0.fp_seed or str(a0.id or ""),
                }
                try:
                    bcli = BilibiliClient(
                        state, a0.ua or app_runtime.cfg.engine.user_agent, **bcli_kwargs)
                except TypeError:
                    bcli_kwargs.pop("account_seed", None)
                    bcli = BilibiliClient(
                        state, a0.ua or app_runtime.cfg.engine.user_agent, **bcli_kwargs)
                async with bcli.session_scope():
                    u, err = await bcli.fetch_self_profile()
            auth_failed = classify_platform_error(err)[0] == RiskCategory.AUTH
            if not u and bmode == "hybrid" and not auth_failed and app_runtime.browser is not None:
                u, err = await fetch_bili_self_profile(app_runtime.browser, identity)
            elif not u and bmode == "browser":
                if app_runtime.browser is None:
                    return _done("error", "browser_unavailable")
                u, err = await fetch_bili_self_profile(app_runtime.browser, identity)
        elif platform == "rednote":
            rmode = str(getattr(app_runtime.cfg.engine, "rednote_read_mode", "hybrid") or "hybrid").lower()
            u, err = None, ""
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
                            if prof.get("nickname"):
                                acc.nickname = prof["nickname"]
                            acc.sec_uid = prof.get("sec_uid") or acc.sec_uid
                            acc.douyin_id = prof.get("douyin_id") or acc.douyin_id
                            acc.avatar = prof.get("avatar") or acc.avatar
                            acc.follower_count = prof.get("follower_count") or acc.follower_count
                            acc.aweme_count = prof.get("aweme_count") or acc.aweme_count
                            acc.status = "active"
                            s.add(acc); s.commit()
                    return _done("ok")
                if perr and not err:
                    err = perr
            auth_failed = classify_platform_error(err)[0] == RiskCategory.AUTH
            if not u and (rmode == "browser" or (rmode == "hybrid" and not auth_failed)):
                if app_runtime.browser is None:
                    return _done("error", "browser_unavailable")
                u, err = await fetch_rednote_self_profile(app_runtime.browser, identity)
        elif platform == "instagram":
            imode = str(getattr(app_runtime.cfg.engine, "instagram_read_mode", "hybrid") or "hybrid").lower()
            u, err = None, ""
            if imode in {"api", "hybrid"} and state:
                icli = InstagramClient(
                    state,
                    a0.ua or app_runtime.cfg.engine.user_agent,
                    proxy=proxy,
                    timeout=app_runtime.cfg.engine.request_timeout_seconds,
                    account_seed=a0.fp_seed or a0.profile_dir or "",
                    timezone_id=a0.timezone_id or "Asia/Shanghai",
                )
                async with icli.session_scope():
                    u, err = await icli.fetch_self_profile()
            auth_failed = classify_platform_error(err)[0] == RiskCategory.AUTH
            if not u and imode == "hybrid" and not auth_failed and app_runtime.browser is not None:
                u, err = await fetch_ig_self_profile(app_runtime.browser, identity)
            elif not u and imode == "browser":
                if app_runtime.browser is None:
                    return _done("error", "browser_unavailable")
                u, err = await fetch_ig_self_profile(app_runtime.browser, identity)
        elif platform == "twitter":
            tmode = str(getattr(app_runtime.cfg.engine, "twitter_read_mode", "hybrid") or "hybrid").lower()
            u, err = None, ""
            tw_enriched_state = ""
            if tmode in {"api", "hybrid"} and state:
                tcli = TwitterClient(
                    state,
                    a0.ua or app_runtime.cfg.engine.user_agent,
                    proxy=proxy,
                    timeout=app_runtime.cfg.engine.request_timeout_seconds,
                )
                async with tcli.session_scope():
                    u, err = await tcli.fetch_self_profile(
                        hint_screen_name=(a0.douyin_id or a0.sec_uid or "")
                    )
                if tcli.cookies.get("auth_token") and tcli.cookies.get("ct0"):
                    tw_enriched_state = build_twitter_storage_state(tcli.cookies)
            auth_failed = classify_platform_error(err)[0] == RiskCategory.AUTH
            if not u and tmode == "hybrid" and not auth_failed and app_runtime.browser is not None:
                u, err = await fetch_twitter_self_profile(app_runtime.browser, identity)
            elif not u and tmode == "browser":
                if app_runtime.browser is None:
                    return _done("error", "browser_unavailable")
                u, err = await fetch_twitter_self_profile(app_runtime.browser, identity)
        else:
            mode = dy_transport["effective_mode"]
            u, err = None, ""
            if mode in {"api", "hybrid"}:
                cookie = douyin_cookie_from_state(state)
                if not cookie:
                    err = "api_missing_cookie"
                else:
                    try:
                        client = DouyinClient(
                            cookie, a0.ua or app_runtime.cfg.engine.user_agent,
                            timeout=app_runtime.cfg.engine.request_timeout_seconds,
                            proxy=proxy, **douyin_client_environment(a0))
                        async with client.session_scope():
                            u = await client.fetch_self_profile()
                        err = client.last_error or ("" if u else "empty_response")
                    except Exception as exc:
                        err = exc
            auth_failed = classify_platform_error(err)[0] == RiskCategory.AUTH
            if not u and mode == "hybrid" and not auth_failed:
                if app_runtime.browser is None:
                    return _done("error", err or "browser_unavailable")
                u, browser_err = await fetch_self_profile(app_runtime.browser, identity)
                err = browser_err or err
            elif not u and mode == "browser":
                if app_runtime.browser is None:
                    return _done("error", "browser_unavailable")
                u, err = await fetch_self_profile(app_runtime.browser, identity)
    except Exception as exc:
        category, _signal = classify_platform_error(exc)
        status = "invalid" if detailed and category == RiskCategory.AUTH else "error"
        return _done(status, exc)
    with get_session() as s:
        acc = s.get(DouyinAccount, account_id)
        if not acc:
            return _done("error", "error")
        if u:
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
            elif platform in {"bilibili", "instagram", "twitter"}:
                p = dict(u)
                if platform == "twitter" and locals().get("tw_enriched_state"):
                    acc.storage_state = tw_enriched_state
                    acc.creator_storage_state = tw_enriched_state
                    acc.cookie = twitter_cookie_str_from_state(tw_enriched_state)
            else:
                p = parse_self_user(u)
            if p.get("nickname"):
                acc.nickname = p["nickname"]
            acc.sec_uid = p.get("sec_uid") or acc.sec_uid
            if p.get("uid"):
                acc.uid = str(p["uid"])
            acc.douyin_id = p.get("douyin_id") or acc.douyin_id
            acc.avatar = p.get("avatar") or acc.avatar
            if platform in {"kuaishou", "tiktok", "bilibili", "instagram", "twitter"}:
                # 快手资料接口会明确返回 0；不能用 ``or`` 保留旧值，否则
                # 取消关注、删光作品等场景会让账号卡片一直显示过期数据。
                acc.follower_count = int(p.get("follower_count") or 0)
                acc.following_count = int(p.get("following_count") or 0)
                acc.aweme_count = int(p.get("aweme_count") or 0)
                acc.total_favorited = int(p.get("total_favorited") or 0)
                acc.gender = str(p.get("gender") or "")
            else:
                acc.follower_count = p.get("follower_count") or acc.follower_count
                acc.aweme_count = p.get("aweme_count") or acc.aweme_count
            acc.status = "active"
            s.add(acc); s.commit()
            return _done("ok")
        if classify_platform_error(err)[0] == RiskCategory.AUTH:
            acc.status = "invalid"
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
