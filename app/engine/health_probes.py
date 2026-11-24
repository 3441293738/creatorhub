"""账号健康巡检、代理区域校验、风险恢复探测与作品状态巡检。"""
from __future__ import annotations

from datetime import datetime, timedelta
import asyncio
import json
import time

from sqlmodel import select

from app.browser import fetch_account_works, fetch_bili_self_profile, fetch_channels_self_profile, fetch_douyin_account_works_api, fetch_facebook_self_profile, fetch_ig_self_profile, fetch_ks_self_profile, fetch_rednote_self_profile, fetch_self_profile, fetch_twitter_self_profile, fetch_xhs_self_profile
from app.browser.tiktok_fetcher import fetch_tiktok_self_profile
from app.db import get_session
from app.engine.monitor_helpers import _AccountCheckBusy, _TZ_COUNTRY, _loads, log
from app.models import AccountActionTask, AccountRiskState, AccountStatSnapshot, AccountWork, CommentTask, DouyinAccount, FollowEdge, KeywordCollectionJob, NotificationChannel, PublishTask
from app.netfp import probe_ip_region
from app.notifier import notify_all
from app.platforms.bilibili import BilibiliClient
from app.platforms.channels import parse_self_user as parse_channels_self_user
from app.platforms.douyin import DouyinClient, cookie_from_state as dy_cookie_from_state, parse_self_user
from app.platforms.facebook import FacebookClient, build_facebook_storage_state
from app.platforms.instagram import InstagramClient
from app.platforms.kuaishou import parse_self_user as parse_ks_self_user
from app.platforms.rednote import (
    RednoteApiError,
    parse_rednote_self_user,
    rednote_creator_check_with_state,
)
from app.platforms.twitter import TwitterClient
from app.platforms.xhs import (
    XhsApiError,
    creator_check_with_state,
    parse_self_user as parse_xhs_self_user,
)
from app.risk import OperationKind, RiskCategory, classify_platform_error
from app.services import thumb_cache
from app.services.session_keepalive import (
    _cookies_meaningfully_changed,
    _parse_state_dict,
    inspect_session_expiry,
    merge_browser_state,
    persist_refreshed_session,
)
from app.transport_matrix import douyin_client_environment, resolve_transport

_PLATFORM_LABELS = {
    "douyin": "抖音",
    "xhs": "小红书",
    "rednote": "海外小红书",
    "kuaishou": "快手",
    "shipinhao": "微信视频号",
    "tiktok": "TikTok",
    "bilibili": "哔哩哔哩",
    "instagram": "Instagram",
    "twitter": "X (Twitter)",
    "facebook": "Facebook",
}


class HealthProbeMixin:
    def _stamp_active(self, account_id) -> None:
        """记录账号「刚被成功摸活」的时刻。任何一次成功的网络/浏览器动作都算活跃,
        闲置保活据此跳过近期已活跃的账号,避免重复请求、减少风控暴露面。"""
        if not account_id:
            return
        with get_session() as s:
            a = s.get(DouyinAccount, account_id)
            if a:
                a.last_active_at = datetime.utcnow()
                s.add(a); s.commit()

    def _keepalive_due(self, last_active_at, *, account_id=None) -> bool:
        """历史闲置判定（登录态体检不再使用此条件）。
        在 idle_keepalive_hours 之后稳定地增加错峰等待。
        idle_keepalive_hours<=0 时退回旧行为(每轮都摸)。"""
        hours = self.cfg.engine.idle_keepalive_hours
        if hours <= 0 or last_active_at is None:
            return True
        return self._due(last_active_at, hours * 3600, key=f"keepalive:{account_id}")

    async def _verify_proxy_region(self, account_id, proxy: str, timezone_id: str) -> None:
        """探测代理出口国家,与账号时区期望不一致时告警(best-effort,只记日志)。
        同一账号+代理只探测一次(缓存),失败静默 —— IP 在境外却时区东八区是强关联信号。"""
        if not self.cfg.engine.verify_proxy_region or not proxy:
            return
        if self._geo_checked.get(account_id) == proxy:
            return
        self._geo_checked[account_id] = proxy
        expected = _TZ_COUNTRY.get(timezone_id or "")
        if not expected:
            return
        geo = await probe_ip_region(proxy)
        if not geo:
            return
        # 把代理出口的真实经纬度写回账号 —— geolocation 伪造坐标据此对齐真实出口地,
        # 避免 navigator.geolocation(城市池兜底)与代理 IP 归属地对不上。
        lat, lon = geo.get("lat") or 0.0, geo.get("lon") or 0.0
        if lat and lon:
            try:
                with get_session() as s:
                    acc = s.get(DouyinAccount, account_id)
                    if acc and (round(acc.geo_lat, 3) != round(lat, 3)
                                or round(acc.geo_lon, 3) != round(lon, 3)):
                        acc.geo_lat, acc.geo_lon = lat, lon
                        s.add(acc)
                        s.commit()
            except Exception:
                pass
        if not geo.get("country"):
            return
        if geo["country"] != expected:
            log.warning("账号 %s 代理出口国家 %s 与时区 %s(期望 %s)不一致,IP=%s"
                        " —— 关联/风控风险,建议换地区一致的长效代理或改账号时区",
                        account_id, geo["country"], timezone_id, expected, geo.get("ip"))

    # ── 账号登录态体检 + 风险恢复探测 ──
    def _account_probe_tuple(self, account):
        return (account.id, account.platform, account.storage_state,
                account.creator_storage_state, account.proxy or "",
                self.browser.identity_for(account))

    def _wake_deferred_tasks(self, account_id: int) -> int:
        """Wake rows that were explicitly deferred by a now-cleared risk gate."""
        woken = 0
        with get_session() as session:
            for model in (PublishTask, CommentTask, AccountActionTask):
                rows = session.exec(select(model).where(
                    model.account_id == account_id,
                    model.status == "pending",
                    model.blocked_reason != "",
                )).all()
                for row in rows:
                    row.error = ""
                    self._clear_row_block(row)
                    session.add(row)
                    woken += 1
            jobs = session.exec(select(KeywordCollectionJob).where(
                KeywordCollectionJob.account_id == account_id,
                KeywordCollectionJob.status == "pending",
                KeywordCollectionJob.blocked_reason != "",
            )).all()
            for job in jobs:
                job.current_step = "账号已恢复，等待继续"
                job.error_count = 0
                job.error = ""
                self._clear_row_block(job)
                session.add(job)
                woken += 1
            if woken:
                session.commit()
        if woken and self._running and not self._collection_tasks:
            try:
                asyncio.create_task(self._process_collection_jobs())
            except RuntimeError:
                pass
        return woken

    async def _sync_browser_session_state(
        self,
        aid: int,
        platform: str,
        identity,
        expected_state: str,
        expected_creator_state: str = "",
        *,
        scope: str = "read",
        sync_cookie_str: bool = False,
        note: str = "已同步浏览器最新登录态",
    ) -> bool:
        """在确认浏览器探活有效后，将常驻上下文最新 Cookie/localStorage 安全合并回 DB。"""
        if not getattr(self.cfg.engine, "keepalive_cookie_sync_enabled", True):
            return False
        capture = getattr(self.browser, "capture_live_storage_state", None)
        if not callable(capture):
            return False
        try:
            live_state = await capture(identity)
        except Exception:
            return False
        if not live_state:
            return False
        base = expected_creator_state if scope == "creator" else expected_state
        merged, changed = merge_browser_state(base, live_state, platform=platform, scope=scope)
        if not merged or not changed:
            return False
        saved = persist_refreshed_session(
            aid,
            platform,
            merged,
            expected_state=expected_state,
            expected_creator_state=expected_creator_state,
            scope=scope,
            sync_cookie_str=sync_cookie_str,
            keepalive_note=note,
        )
        if saved:
            log.info("账号 %s(%s) %s", aid, platform, note)
        return saved

    def _sync_api_session_state(
        self,
        aid: int,
        platform: str,
        new_state: dict | None,
        changed: bool,
        *,
        expected_state: str = "",
        expected_creator_state: str = "",
        scope: str = "read",
        sync_cookie_str: bool = False,
        note: str = "已同步平台最新 Cookie",
    ) -> bool:
        """在确认 API 探活/续期成功后，将最新 Cookie 安全落库。"""
        if not getattr(self.cfg.engine, "keepalive_cookie_sync_enabled", True):
            return False
        if not new_state or not changed:
            return False
        saved = persist_refreshed_session(
            aid,
            platform,
            new_state,
            expected_state=expected_state,
            expected_creator_state=expected_creator_state,
            scope=scope,
            sync_cookie_str=sync_cookie_str,
            keepalive_note=note,
        )
        if saved:
            log.info("账号 %s(%s) %s", aid, platform, note)
        return saved

    async def _notify_account_invalid(
        self,
        aid: int,
        platform: str,
        nickname: str,
        err: object,
    ) -> None:
        """账号从有效变为失效时，向已启用的通知渠道推送掉线提醒。"""
        if not getattr(self.cfg.engine, "keepalive_notify_on_invalid", True):
            return
        try:
            with get_session() as s:
                chans = s.exec(
                    select(NotificationChannel).where(NotificationChannel.enabled == True)  # noqa: E712
                ).all()
                channels = [{"type": c.type, "config": _loads(c.config)} for c in chans]
            if not channels:
                return
            label = _PLATFORM_LABELS.get(platform, platform)
            name = nickname or f"账号{aid}"
            title = f"⚠️ 账号登录态已失效（{label}）"
            body = (
                f"{label}账号「{name}」(#{aid}) 检测到登录态已失效，"
                f"相关自动任务已暂停，请尽快在账号管理中重新登录。"
            )
            await notify_all(channels, title, body)
        except Exception:
            pass

    async def _probe_account_health(
        self,
        probe,
        *,
        allow_invalid_probe: bool = False,
        interactive_read: bool = False,
    ) -> dict:
        aid, platform, state, creator_state, proxy, identity = probe
        # A user-held window owns this lock for its whole lifetime. Never let
        # one busy account stall the scheduler and every other account's check.
        if self.browser.lock_for(f"acc:{aid}").locked():
            return {"ok": False, "deferred": True, "reason": "account_busy"}
        decision = self.risk.preflight(
            aid,
            OperationKind.READ_LIGHT,
            allow_invalid_probe=allow_invalid_probe,
            interactive_read=interactive_read,
        )
        if not decision.allowed:
            return {"ok": False, "deferred": True, "reason": decision.reason,
                    "next_allowed_at": decision.next_allowed_at}
        with get_session() as session:
            before = session.get(AccountRiskState, aid)
            was_recovering = bool(before and before.risk_level > 0)
            if was_recovering:
                before.last_operation_at = datetime.utcnow()
                before.updated_at = before.last_operation_at
                session.add(before)
                session.commit()
        u, err = {}, ""
        cookie_refreshed = False
        keepalive_notes: list[str] = []
        try:
            async with self._operation_guard(aid, OperationKind.READ_LIGHT, skip_busy=True):
                decision = self.risk.preflight(
                    aid,
                    OperationKind.READ_LIGHT,
                    allow_invalid_probe=allow_invalid_probe,
                    interactive_read=interactive_read,
                )
                if not decision.allowed:
                    return {"ok": False, "deferred": True,
                            "reason": decision.reason,
                            "next_allowed_at": decision.next_allowed_at}
                await self._verify_proxy_region(aid, proxy, identity.timezone_id)

                if platform == "xhs" and self.cfg.engine.xhs_read_mode == "browser":
                    if not self._xhs_browser_reads_enabled():
                        return {"ok": False, "indeterminate": True,
                                "error": "浏览器读取能力未就绪，已跳过账号探测"}
                    try:
                        cookies = json.loads(state or "{}").get("cookies", [])
                        has_web_session = any(
                            cookie.get("name") == "web_session" and cookie.get("value")
                            for cookie in cookies if isinstance(cookie, dict))
                    except (ValueError, TypeError, AttributeError):
                        has_web_session = False
                    if creator_state and not has_web_session:
                        return {"ok": False, "indeterminate": True,
                                "error": "仅有创作者登录态，请在账号浏览器中确认；未启动直连探测"}
                    u, err = await fetch_xhs_self_profile(self.browser, identity)
                    if u:
                        if await self._sync_browser_session_state(
                            aid, "xhs", identity, state,
                            scope="read", note="小红书浏览器登录态已同步",
                        ):
                            cookie_refreshed = True
                            keepalive_notes.append("小红书浏览器登录态已同步")

                elif platform == "douyin":
                    # Login health follows the account-profile transport, not
                    # the public content-read mode. Public profiles prove only
                    # that an account exists, never that its Cookie is valid.
                    mode = resolve_transport(
                        self.cfg, "douyin", "account_profile")["effective_mode"]
                    dy_base_state = state or creator_state
                    cookie = dy_cookie_from_state(dy_base_state)
                    used_browser = False
                    if mode in {"api", "hybrid"} and cookie:
                        client = DouyinClient(
                            cookie, identity.ua or self.cfg.engine.user_agent,
                            timeout=self.cfg.engine.request_timeout_seconds,
                            proxy=proxy,
                            storage_state=dy_base_state,
                            **douyin_client_environment(identity))
                        try:
                            async with client.session_scope():
                                profile = await client.fetch_self_profile()
                            if profile:
                                u, err = profile, ""
                                exported = client.export_storage_state()
                                changed = _cookies_meaningfully_changed(
                                    _parse_state_dict(dy_base_state)["cookies"],
                                    exported.get("cookies") or [],
                                )
                                if self._sync_api_session_state(
                                    aid, "douyin", exported, changed,
                                    expected_state=state,
                                    scope="read",
                                    note="抖音 API 响应 Cookie/msToken 已续期同步",
                                ):
                                    cookie_refreshed = True
                                    keepalive_notes.append("抖音 API Cookie 已同步")
                            else:
                                u, err = {}, client.last_error or "empty_response"
                        except Exception as exc:
                            u, err = {}, exc
                    elif mode in {"api", "hybrid"}:
                        u, err = {}, "no_cookie"
                    auth_failed = classify_platform_error(err)[0] == RiskCategory.AUTH
                    if mode == "browser" or (not u and mode == "hybrid" and not auth_failed):
                        u, err = await fetch_self_profile(self.browser, identity)
                        used_browser = True
                    if u and used_browser:
                        if await self._sync_browser_session_state(
                            aid, "douyin", identity, state,
                            expected_creator_state=creator_state,
                            scope="read",
                            note="抖音浏览器登录态已同步",
                        ):
                            cookie_refreshed = True
                            keepalive_notes.append("抖音浏览器登录态已同步")

                elif platform == "xhs":
                    xhs_ua = self._direct_request_ua(identity)
                    creator_ok: bool | None = None
                    web_ok: bool | None = None
                    web_data: dict = {}
                    web_err: object = ""
                    try:
                        cookies = json.loads(state or "{}").get("cookies", [])
                        has_web_session = any(
                            cookie.get("name") == "web_session" and cookie.get("value")
                            for cookie in cookies if isinstance(cookie, dict)
                        )
                    except (ValueError, TypeError, AttributeError):
                        has_web_session = False

                    if creator_state:
                        chk, _cerr, c_state, c_changed = await creator_check_with_state(
                            creator_state, proxy=proxy, user_agent=xhs_ua
                        )
                        creator_ok = chk
                        if chk is True and self._sync_api_session_state(
                            aid, "xhs", c_state, c_changed,
                            expected_creator_state=creator_state,
                            scope="creator",
                            note="小红书创作者 Cookie 已同步",
                        ):
                            cookie_refreshed = True
                            keepalive_notes.append("创作者 Cookie 已同步")

                    if has_web_session or not creator_state:
                        client = self._xhs_client(identity, state, proxy)
                        if client is None:
                            web_ok = False
                            web_err = "logged_out"
                        else:
                            try:
                                async with client.session_scope():
                                    data = await client.self_info()
                                if data and not data.get("guest"):
                                    web_ok = True
                                    web_data = data
                                    w_state, w_changed = client.export_refreshed_state(state)
                                    if self._sync_api_session_state(
                                        aid, "xhs", w_state, w_changed,
                                        expected_state=state,
                                        scope="read",
                                        note="小红书主站 Cookie 已同步",
                                    ):
                                        cookie_refreshed = True
                                        keepalive_notes.append("主站 Cookie 已同步")
                                else:
                                    web_ok = False
                                    web_err = "logged_out"
                            except XhsApiError as exc:
                                if exc.category == "auth":
                                    web_ok = False
                                    web_err = "logged_out"
                                else:
                                    web_ok = None
                                    web_err = exc

                    checked_results = [
                        res for probed, res in (
                            (bool(creator_state), creator_ok),
                            (bool(has_web_session or not creator_state), web_ok),
                        )
                        if probed
                    ]
                    if web_ok is True:
                        u, err = web_data, ""
                        if creator_ok is False:
                            keepalive_notes.append("主站有效，创作者登录态已失效")
                    elif creator_ok is True:
                        u, err = {"ok": 1}, ""
                        if web_ok is False:
                            keepalive_notes.append("创作者有效，主站登录态已失效")
                    elif any(res is None for res in checked_results):
                        if isinstance(web_err, XhsApiError):
                            self.risk.record_failure(aid, OperationKind.READ_LIGHT, web_err)
                            return {"ok": False, "error": str(web_err)}
                        return {"ok": False, "indeterminate": True}
                    else:
                        u, err = {}, "logged_out"

                elif platform == "kuaishou":
                    u, err = await fetch_ks_self_profile(self.browser, identity)
                    if u:
                        if await self._sync_browser_session_state(
                            aid, "kuaishou", identity, state,
                            expected_creator_state=creator_state,
                            scope="read",
                            note="快手浏览器登录态已保活同步",
                        ):
                            cookie_refreshed = True
                            keepalive_notes.append("快手登录态已同步")

                elif platform == "shipinhao":
                    u, err = await fetch_channels_self_profile(self.browser, identity)
                    if u:
                        if await self._sync_browser_session_state(
                            aid, "shipinhao", identity, state,
                            expected_creator_state=creator_state,
                            scope="read",
                            note="视频号助手会话已保活同步",
                        ):
                            cookie_refreshed = True
                            keepalive_notes.append("视频号会话 Cookie 已同步")

                elif platform == "tiktok":
                    # TikTok must be checked against its own authenticated home
                    # hydration. Falling through to Douyin's /user/self probe
                    # falsely marks a valid TikTok profile as logged out.
                    u, err = await fetch_tiktok_self_profile(
                        self.browser, identity, block_media=True)
                    if u:
                        if await self._sync_browser_session_state(
                            aid, "tiktok", identity, state,
                            expected_creator_state=creator_state,
                            scope="read",
                            note="TikTok 浏览器登录态已同步至直连快照",
                        ):
                            cookie_refreshed = True
                            keepalive_notes.append("TikTok Cookie 已同步")

                elif platform == "bilibili":
                    bmode = str(getattr(
                        self.cfg.engine, "bilibili_read_mode", "hybrid") or "hybrid").lower()
                    u, err = {}, ""
                    used_browser = False
                    if bmode in {"api", "hybrid"} and state:
                        bcli = BilibiliClient(
                            state,
                            identity.ua or self.cfg.engine.user_agent,
                            proxy=proxy,
                            timeout=self.cfg.engine.request_timeout_seconds,
                            account_seed=getattr(identity, "fp_seed", "") or str(aid),
                        )
                        async with bcli.session_scope():
                            u, err = await bcli.fetch_self_profile()
                            if u:
                                if getattr(self.cfg.engine, "bilibili_auto_cookie_refresh", True):
                                    ref = await bcli.maybe_refresh_cookies(state)
                                    if ref.get("refreshed") and ref.get("state"):
                                        if self._sync_api_session_state(
                                            aid, "bilibili", ref["state"], True,
                                            expected_state=state,
                                            scope="read",
                                            note="B站官方 RSA-OAEP Cookie 续期成功",
                                        ):
                                            cookie_refreshed = True
                                            keepalive_notes.append("B站官方 Cookie 续期成功")
                                    elif ref.get("missing_refresh_token"):
                                        if bmode == "hybrid":
                                            bu, _berr = await fetch_bili_self_profile(self.browser, identity)
                                            if bu:
                                                u = bu
                                                used_browser = True
                                                keepalive_notes.append("B站已通过浏览器触发续期并补采令牌")
                                            else:
                                                keepalive_notes.append("B站建议续期，缺少 ac_time_value")
                                        else:
                                            keepalive_notes.append("B站建议续期，缺少 ac_time_value")
                                    elif ref.get("changed") and ref.get("state"):
                                        if self._sync_api_session_state(
                                            aid, "bilibili", ref["state"], True,
                                            expected_state=state,
                                            scope="read",
                                            note="B站 API 响应 Cookie 已同步",
                                        ):
                                            cookie_refreshed = True
                                            keepalive_notes.append("B站响应 Cookie 已同步")
                                else:
                                    b_state, b_changed = bcli.export_refreshed_state(state)
                                    if self._sync_api_session_state(
                                        aid, "bilibili", b_state, b_changed,
                                        expected_state=state,
                                        scope="read",
                                        note="B站 API 响应 Cookie 已同步",
                                    ):
                                        cookie_refreshed = True
                                        keepalive_notes.append("B站响应 Cookie 已同步")
                    elif bmode == "api" and not state:
                        u, err = {}, "logged_out:缺少 B 站登录态"
                    auth_failed = classify_platform_error(err)[0] == RiskCategory.AUTH
                    if bmode == "browser" or (not u and bmode == "hybrid" and not auth_failed):
                        u, err = await fetch_bili_self_profile(self.browser, identity)
                        used_browser = True
                    if u and used_browser:
                        if await self._sync_browser_session_state(
                            aid, "bilibili", identity, state,
                            expected_creator_state=creator_state,
                            scope="read",
                            note="B站浏览器登录态与续期令牌已同步",
                        ):
                            cookie_refreshed = True
                            if not keepalive_notes:
                                keepalive_notes.append("B站浏览器登录态已同步")

                elif platform == "rednote":
                    rmode = self._rednote_read_mode()
                    u, err = {}, ""
                    rn_state = state or creator_state
                    used_browser = False
                    if rmode in {"api", "hybrid"} and rn_state:
                        rcli = self._rednote_client(identity, rn_state, proxy)
                        if rcli is not None:
                            try:
                                async with rcli.session_scope():
                                    data = await rcli.self_info()
                                if data and not data.get("guest"):
                                    u, err = data, ""
                                    rn_scope = "read" if state else "creator"
                                    rn_new, rn_changed = rcli.export_refreshed_state(
                                        rn_state, scope=rn_scope
                                    )
                                    if self._sync_api_session_state(
                                        aid, "rednote", rn_new, rn_changed,
                                        expected_state=(state if rn_scope == "read" else ""),
                                        expected_creator_state=(creator_state if rn_scope == "creator" else ""),
                                        scope=rn_scope,
                                        note="Rednote API 响应 Cookie 已同步",
                                    ):
                                        cookie_refreshed = True
                                        keepalive_notes.append("Rednote Cookie 已同步")
                                    # 若同时存在独立的创作者登录态，顺带保活
                                    if state and creator_state and creator_state != state:
                                        c_ok, c_new, c_changed = await rednote_creator_check_with_state(
                                            creator_state, proxy=proxy,
                                            user_agent=self._direct_request_ua(identity),
                                        )
                                        if c_ok and self._sync_api_session_state(
                                            aid, "rednote", c_new, c_changed,
                                            expected_creator_state=creator_state,
                                            scope="creator",
                                            note="Rednote 创作者 Cookie 已同步",
                                        ):
                                            cookie_refreshed = True
                                            keepalive_notes.append("Rednote 创作者 Cookie 已同步")
                                else:
                                    u, err = {}, "logged_out"
                            except RednoteApiError as exc:
                                if exc.category == "auth":
                                    u, err = {}, "logged_out"
                                elif rmode == "api":
                                    self.risk.record_failure(
                                        aid, OperationKind.READ_LIGHT, exc)
                                    return {"ok": False, "error": str(exc)}
                                else:
                                    err = str(exc)
                        elif creator_state:
                            chk, c_new, c_changed = await rednote_creator_check_with_state(
                                creator_state, proxy=proxy,
                                user_agent=self._direct_request_ua(identity),
                            )
                            if chk:
                                u, err = {"ok": 1}, ""
                                if self._sync_api_session_state(
                                    aid, "rednote", c_new, c_changed,
                                    expected_creator_state=creator_state,
                                    scope="creator",
                                    note="Rednote 创作者 Cookie 已同步",
                                ):
                                    cookie_refreshed = True
                                    keepalive_notes.append("Rednote 创作者 Cookie 已同步")
                            elif chk is False and rmode == "api":
                                u, err = {}, "logged_out"
                        elif rmode == "api":
                            u, err = {}, "logged_out"
                    elif rmode == "api":
                        u, err = {}, "logged_out"
                    auth_failed = classify_platform_error(err)[0] == RiskCategory.AUTH
                    if rmode == "browser" or (not u and rmode == "hybrid" and not auth_failed):
                        u, err = await fetch_rednote_self_profile(self.browser, identity)
                        used_browser = True
                    if u and used_browser:
                        if await self._sync_browser_session_state(
                            aid, "rednote", identity, state,
                            expected_creator_state=creator_state,
                            scope="read",
                            note="Rednote 浏览器登录态已同步",
                        ):
                            cookie_refreshed = True
                            keepalive_notes.append("Rednote 浏览器登录态已同步")

                elif platform == "instagram":
                    imode = str(getattr(
                        self.cfg.engine, "instagram_read_mode", "hybrid") or "hybrid").lower()
                    u, err = {}, ""
                    used_browser = False
                    if imode in {"api", "hybrid"} and state:
                        icli = InstagramClient(
                            state,
                            identity.ua or self.cfg.engine.user_agent,
                            proxy=proxy,
                            timeout=self.cfg.engine.request_timeout_seconds,
                            account_seed=getattr(identity, "fp_seed", "") or getattr(identity, "profile_dir", ""),
                            timezone_id=getattr(identity, "timezone_id", "Asia/Shanghai"),
                        )
                        async with icli.session_scope():
                            u, err = await icli.fetch_self_profile()
                        if u:
                            ig_new, ig_changed = icli.export_refreshed_state(state)
                            if self._sync_api_session_state(
                                aid, "instagram", ig_new, ig_changed,
                                expected_state=state,
                                scope="read",
                                note="Instagram API 会话凭据/Cookie 已同步",
                            ):
                                cookie_refreshed = True
                                keepalive_notes.append("Instagram 会话已同步")
                    elif imode == "api" and not state:
                        u, err = {}, "logged_out:缺少 Instagram 登录态"
                    auth_failed = classify_platform_error(err)[0] == RiskCategory.AUTH
                    if imode == "browser" or (not u and imode == "hybrid" and not auth_failed):
                        u, err = await fetch_ig_self_profile(self.browser, identity)
                        used_browser = True
                    if u and used_browser:
                        if await self._sync_browser_session_state(
                            aid, "instagram", identity, state,
                            expected_creator_state=creator_state,
                            scope="read",
                            note="Instagram 浏览器登录态已同步",
                        ):
                            cookie_refreshed = True
                            keepalive_notes.append("Instagram 浏览器登录态已同步")

                elif platform == "twitter":
                    tmode = str(getattr(
                        self.cfg.engine, "twitter_read_mode", "hybrid") or "hybrid").lower()
                    u, err = {}, ""
                    used_browser = False
                    if tmode in {"api", "hybrid"} and state:
                        tcli = TwitterClient(
                            state,
                            identity.ua or self.cfg.engine.user_agent,
                            proxy=proxy,
                            timeout=self.cfg.engine.request_timeout_seconds,
                        )
                        async with tcli.session_scope():
                            u, err = await tcli.fetch_self_profile()
                        if u:
                            tw_new, tw_changed = tcli.export_refreshed_state(state)
                            if self._sync_api_session_state(
                                aid, "twitter", tw_new, tw_changed,
                                expected_state=state,
                                scope="read",
                                sync_cookie_str=True,
                                note="X (Twitter) API Cookie/ct0 已同步",
                            ):
                                cookie_refreshed = True
                                keepalive_notes.append("X Cookie/ct0 已同步")
                    elif tmode == "api" and not state:
                        u, err = {}, "logged_out:缺少 X (Twitter) 登录态"
                    auth_failed = classify_platform_error(err)[0] == RiskCategory.AUTH
                    if tmode == "browser" or (not u and tmode == "hybrid" and not auth_failed):
                        u, err = await fetch_twitter_self_profile(self.browser, identity)
                        used_browser = True
                    if u and used_browser:
                        if await self._sync_browser_session_state(
                            aid, "twitter", identity, state,
                            expected_creator_state=creator_state,
                            scope="read",
                            sync_cookie_str=True,
                            note="X (Twitter) 浏览器登录态已同步",
                        ):
                            cookie_refreshed = True
                            keepalive_notes.append("X 浏览器登录态已同步")

                elif platform == "facebook":
                    fmode = str(getattr(
                        self.cfg.engine, "facebook_read_mode", "hybrid") or "hybrid").lower()
                    u, err = {}, ""
                    used_browser = False
                    if fmode in {"api", "hybrid"} and state:
                        fcli = FacebookClient(
                            state,
                            identity.ua or self.cfg.engine.user_agent,
                            proxy=proxy,
                            timeout=self.cfg.engine.request_timeout_seconds,
                        )
                        async with fcli.session_scope():
                            u, err = await fcli.fetch_self_profile()
                        if u and fcli.cookie_changed and fcli.has_login_cookies:
                            fb_new = build_facebook_storage_state(fcli.cookies)
                            if self._sync_api_session_state(
                                aid, "facebook", fb_new, True,
                                expected_state=state,
                                scope="read",
                                sync_cookie_str=True,
                                note="Facebook Cookie 会话已同步",
                            ):
                                cookie_refreshed = True
                                keepalive_notes.append("Facebook Cookie 已同步")
                    elif fmode == "api" and not state:
                        u, err = {}, "logged_out:缺少 Facebook 登录态"
                    auth_failed = classify_platform_error(err)[0] == RiskCategory.AUTH
                    if fmode == "browser" or (not u and fmode == "hybrid" and not auth_failed):
                        u, err = await fetch_facebook_self_profile(self.browser, identity)
                        used_browser = True
                    if u and used_browser:
                        if await self._sync_browser_session_state(
                            aid, "facebook", identity, state,
                            expected_creator_state=creator_state,
                            scope="read",
                            sync_cookie_str=True,
                            note="Facebook 浏览器登录态已同步",
                        ):
                            cookie_refreshed = True
                            keepalive_notes.append("Facebook 浏览器登录态已同步")

                else:
                    u, err = await fetch_self_profile(self.browser, identity)
                    if u:
                        if await self._sync_browser_session_state(
                            aid, platform, identity, state,
                            expected_creator_state=creator_state,
                            scope="read",
                        ):
                            cookie_refreshed = True

                if u:
                    self.risk.record_success(aid, OperationKind.READ_LIGHT)
                elif err:
                    self.risk.record_failure(aid, OperationKind.READ_LIGHT, err)
        except _AccountCheckBusy:
            return {"ok": False, "deferred": True, "reason": "account_busy"}
        except Exception as exc:
            self.risk.record_failure(aid, OperationKind.READ_LIGHT, exc)
            return {"ok": False, "error": str(exc)}

        got_profile = False
        became_invalid = False
        acc_nickname = ""
        final_note = ""
        with get_session() as session:
            account = session.get(DouyinAccount, aid)
            if not account:
                return {"ok": False, "error": "account_missing"}
            was_active = account.status == "active"
            now_utc = datetime.utcnow()
            if u:
                if platform == "xhs":
                    parsed = parse_xhs_self_user(u)
                elif platform == "rednote":
                    parsed = parse_rednote_self_user(u)
                elif platform == "kuaishou":
                    parsed = parse_ks_self_user(u)
                elif platform == "shipinhao":
                    parsed = parse_channels_self_user(u)
                elif platform == "tiktok":
                    parsed = {
                        "nickname": u.get("nickname") or "",
                        "sec_uid": u.get("sec_uid") or "",
                        "douyin_id": u.get("unique_id") or "",
                        "avatar": u.get("avatar") or "",
                        "follower_count": int(u.get("follower_count") or 0),
                        "following_count": int(u.get("following_count") or 0),
                        "aweme_count": int(u.get("video_count") or 0),
                    }
                elif platform in {"bilibili", "instagram", "twitter", "facebook"}:
                    parsed = dict(u)
                else:
                    parsed = parse_self_user(u)
                account.status = "active"
                account.last_active_at = now_utc
                account.last_keepalive_at = now_utc
                final_note = (
                    "；".join(keepalive_notes)
                    if keepalive_notes
                    else ("已同步最新登录态" if cookie_refreshed else "登录态有效")
                )
                account.keepalive_note = final_note[:200]
                if parsed.get("nickname"):
                    account.nickname = parsed["nickname"]
                account.sec_uid = parsed.get("sec_uid") or account.sec_uid
                if parsed.get("uid"):
                    account.uid = str(parsed["uid"])
                account.douyin_id = parsed.get("douyin_id") or account.douyin_id
                account.avatar = parsed.get("avatar") or account.avatar
                if platform in {"kuaishou", "tiktok", "bilibili", "instagram", "twitter"}:
                    account.follower_count = int(parsed.get("follower_count") or 0)
                    account.following_count = int(parsed.get("following_count") or 0)
                    account.aweme_count = int(parsed.get("aweme_count") or 0)
                    account.total_favorited = int(parsed.get("total_favorited") or 0)
                    account.gender = str(parsed.get("gender") or "")
                elif platform == "facebook":
                    works_cnt = len(session.exec(
                        select(AccountWork.id).where(AccountWork.account_id == aid)
                    ).all())
                    fan_cnt = len(session.exec(
                        select(FollowEdge.id).where(
                            FollowEdge.account_id == aid,
                            FollowEdge.direction == "fan",
                        )
                    ).all())
                    following_cnt = len(session.exec(
                        select(FollowEdge.id).where(
                            FollowEdge.account_id == aid,
                            FollowEdge.direction == "following",
                        )
                    ).all())
                    raw_fans = int(parsed.get("follower_count") or 0)
                    raw_following = int(parsed.get("following_count") or 0)
                    if raw_fans > 0 and not parsed.get("follower_count_is_friends_fallback"):
                        account.follower_count = raw_fans
                    elif fan_cnt > 0:
                        account.follower_count = fan_cnt
                    elif raw_fans > 0 and not account.follower_count:
                        account.follower_count = raw_fans
                    if raw_following > 0 and not parsed.get("following_count_is_friends_fallback"):
                        account.following_count = raw_following
                    elif following_cnt > 0:
                        account.following_count = following_cnt
                    elif raw_following > 0 and not account.following_count:
                        account.following_count = raw_following
                    account.aweme_count = max(
                        int(parsed.get("aweme_count") or 0),
                        int(account.aweme_count or 0),
                        works_cnt,
                    )
                    account.total_favorited = int(parsed.get("total_favorited") or 0) or account.total_favorited
                    if parsed.get("gender"):
                        account.gender = str(parsed.get("gender") or "")
                else:
                    account.follower_count = parsed.get("follower_count") or account.follower_count
                    account.aweme_count = parsed.get("aweme_count") or account.aweme_count
                got_profile = True
            elif classify_platform_error(err)[0] == RiskCategory.AUTH:
                account.status = "invalid"
                final_note = "登录态已失效，请重新登录"
                account.keepalive_note = final_note
                became_invalid = was_active
                log.warning("账号 %s(%s)登录态失效", aid, account.nickname)
            acc_avatar = account.avatar or ""
            acc_nickname = account.nickname or ""
            session.add(account)
            session.commit()
            risk_state = session.get(AccountRiskState, aid)
            recovered = bool(was_recovering and risk_state
                             and risk_state.risk_level == 0)

        if became_invalid:
            await self._notify_account_invalid(aid, platform, acc_nickname, err)

        if got_profile and acc_avatar:
            thumb_cache.schedule_thumb_prewarm([acc_avatar], platform=platform, proxy=proxy)

        if got_profile and self.cfg.engine.work_health_stat_snapshots:
            try:
                self._write_stat_snapshot(aid, platform, [])
            except Exception:
                pass
        woken = self._wake_deferred_tasks(aid) if recovered else 0
        if recovered:
            log.info("账号 %s 风险恢复完成，已唤醒 %s 条任务", aid, woken)
        return {
            "ok": got_profile,
            "recovered": recovered,
            "woken": woken,
            "cookie_refreshed": cookie_refreshed,
            "keepalive_note": final_note,
            "error": str(err) if isinstance(err, BaseException) else err,
        }

    def enqueue_keepalive_accounts(
        self,
        *,
        platform: str = "",
        account_ids: list[int] | None = None,
    ) -> int:
        """将指定或全部有效账号加入后台保活队列，按既有风控错峰节律平滑执行。"""
        pf = str(platform or "").strip().lower()
        target_ids = {int(i) for i in (account_ids or []) if i}
        added = 0
        with get_session() as session:
            for account in session.exec(select(DouyinAccount)).all():
                if not account.id or account.status == "invalid":
                    continue
                if not (account.storage_state or account.creator_storage_state):
                    continue
                if pf and pf != "all" and str(account.platform or "").lower() != pf:
                    continue
                if target_ids and account.id not in target_ids:
                    continue
                if account.id not in self._pending_account_checks:
                    self._pending_account_checks.add(account.id)
                    added += 1
        return added

    async def keepalive_account_now(self, account_id: int) -> dict:
        """立即对单个账号执行一次完整保活与 Cookie/Token 续期检查（遵守硬风控门禁）。"""
        with get_session() as session:
            account = session.get(DouyinAccount, account_id)
            if not account:
                return {"ok": False, "error": "账号不存在"}
            if not (account.storage_state or account.creator_storage_state):
                return {"ok": False, "error": "该账号缺少持久化登录态，请先扫码登录"}
            probe = self._account_probe_tuple(account)
        result = await self._probe_account_health(
            probe,
            allow_invalid_probe=True,
            interactive_read=True,
        )
        with get_session() as session:
            acc = session.get(DouyinAccount, account_id)
            if acc is not None:
                result["status"] = acc.status
                result["last_keepalive_at"] = (
                    acc.last_keepalive_at.isoformat() if acc.last_keepalive_at else None
                )
                result["last_cookie_refresh_at"] = (
                    acc.last_cookie_refresh_at.isoformat() if acc.last_cookie_refresh_at else None
                )
                result["session_health"] = inspect_session_expiry(
                    acc.platform, acc.storage_state, acc.creator_storage_state
                )
        return result

    async def _process_risk_recovery(self) -> int:
        """Probe recovering accounts independently of idle keepalive cadence."""
        if not self.risk.policy.enabled or not self.risk._cooldown_active():
            return 0
        now = datetime.utcnow()
        gap = max(1, self.cfg.risk_control.recovery_probe_gap_seconds)
        with get_session() as session:
            probes = []
            states = session.exec(select(AccountRiskState).where(
                AccountRiskState.risk_level > 0)).all()
            for risk_state in states:
                account = session.get(DouyinAccount, risk_state.account_id)
                if not account or account.status == "invalid" \
                        or not (account.storage_state or account.creator_storage_state):
                    continue
                if risk_state.cooldown_until and risk_state.cooldown_until > now:
                    continue
                last_attempt = max(
                    [value for value in (risk_state.last_recovery_at,
                                         risk_state.last_operation_at)
                     if value is not None], default=None)
                if last_attempt and (now - last_attempt).total_seconds() < gap:
                    continue
                probes.append(self._account_probe_tuple(account))
        completed = 0
        for probe in probes[:3]:
            result = await self._probe_account_health(probe)
            if result.get("ok"):
                completed += 1
        return completed

    async def _check_accounts(self):
        import random
        interval = self.cfg.engine.account_check_interval_seconds
        if interval <= 0:
            return
        now = time.time()
        target_interval = getattr(self, "_next_acct_check_interval", float(interval))
        sweep_due = now - self._last_acct_check >= target_interval

        # 微信视频号会话有效期较短，支持独立更紧凑的保活周期
        sph_interval = int(getattr(self.cfg.engine, "shipinhao_keepalive_interval_seconds", 900) or 0)
        sph_target = getattr(self, "_next_shipinhao_check_interval", float(sph_interval))
        last_sph = getattr(self, "_last_shipinhao_check", 0.0)
        sph_due = bool(sph_interval > 0 and (now - last_sph >= sph_target))

        if not sweep_due and not sph_due and not self._pending_account_checks:
            return
        with get_session() as session:
            rows = [
                account for account in session.exec(select(DouyinAccount)).all()
                if (account.storage_state or account.creator_storage_state)
                and account.status != "invalid"
            ]
            probes = [self._account_probe_tuple(account) for account in rows]
            priority_ids = {
                account.id for account in rows
                if account.id and (
                    account.platform == "shipinhao"
                    or inspect_session_expiry(
                        account.platform, account.storage_state, account.creator_storage_state
                    ).get("expiring_soon")
                )
            }
            shipinhao_ids = {account.id for account in rows if account.id and account.platform == "shipinhao"}

        eligible = {probe[0] for probe in probes}
        self._pending_account_checks.intersection_update(eligible)
        if sweep_due:
            self._last_acct_check = now
            self._next_acct_check_interval = float(interval) * random.uniform(0.84, 1.32)
            self._pending_account_checks.update(eligible)
        if sph_due:
            self._last_shipinhao_check = now
            self._next_shipinhao_check_interval = float(sph_interval) * random.uniform(0.85, 1.25)
            self._pending_account_checks.update(shipinhao_ids)

        # 临期账号与易失效平台（视频号）排在前面优先保活，其余同档随机错峰
        priority_probes = [p for p in probes if p[0] in priority_ids]
        normal_probes = [p for p in probes if p[0] not in priority_ids]
        random.shuffle(priority_probes)
        random.shuffle(normal_probes)
        ordered_probes = priority_probes + normal_probes

        checked_in_tick = 0
        for probe in ordered_probes:
            if probe[0] not in self._pending_account_checks:
                continue
            if checked_in_tick > 0:
                await asyncio.sleep(random.uniform(1.4, 3.8))
            result = await self._probe_account_health(probe)
            checked_in_tick += 1
            if not result.get("deferred"):
                self._pending_account_checks.discard(probe[0])
            # 单轮最多探活 3 个账号，避免整批账号在同一分钟密集请求平台接口
            if checked_in_tick >= 3:
                break

    # ── 本账号作品健康监控与每日数据快照 ──
    async def _check_work_health(self):
        """定期同步本账号作品,检测「持续0播 / 违规下架」并推送;顺带写每日数据快照。"""
        import random
        if not self.cfg.engine.work_health_enabled:
            return
        now = time.time()
        target_interval = getattr(
            self, "_next_work_health_interval", float(self.cfg.engine.work_health_interval_seconds)
        )
        if now - getattr(self, "_last_work_health", 0.0) < target_interval:
            return
        self._last_work_health = now
        self._next_work_health_interval = (
            float(self.cfg.engine.work_health_interval_seconds) * random.uniform(0.85, 1.30)
        )
        with get_session() as s:
            accs = [(a.id, a.platform,
                     (a.sec_uid or a.uid or a.douyin_id or "") if a.platform in {"instagram", "twitter", "facebook"} else (a.sec_uid or ""),
                     self.browser.identity_for(a))
                    for a in s.exec(select(DouyinAccount)).all()
                    if a.status != "invalid" and (a.storage_state or a.creator_storage_state)]
        for aid, platform, uid, identity in accs:
            decision = self.risk.preflight(aid, OperationKind.READ_HEAVY)
            if not decision.allowed:
                continue
            try:
                async with self._operation_guard(aid, OperationKind.READ_HEAVY):
                    if not self.risk.preflight(
                            aid, OperationKind.READ_HEAVY).allowed:
                        continue
                    if platform == "douyin" and self.cfg.engine.douyin_read_mode in {"api", "hybrid"}:
                        with get_session() as s:
                            acc_row = s.get(DouyinAccount, aid)
                            state = (acc_row.storage_state or acc_row.creator_storage_state or "") if acc_row else ""
                            ua = (acc_row.ua or self.cfg.engine.user_agent) if acc_row else self.cfg.engine.user_agent
                            proxy = (acc_row.proxy or "") if acc_row else ""
                        items, err = await fetch_douyin_account_works_api(
                            dy_cookie_from_state(state), ua, uid,
                            timeout=self.cfg.engine.request_timeout_seconds,
                            proxy=proxy,
                            environment=douyin_client_environment(identity))
                        if not items and self.cfg.engine.douyin_read_mode == "hybrid":
                            log.debug("抖音作品健康检查 API 空响应(%s)，账号#%s 回退浏览器", err, aid)
                            items, err = await fetch_account_works(
                                self.browser, identity, platform, uid)
                    else:
                        items, err = await fetch_account_works(self.browser, identity,
                                                               platform, uid)
                    if err:
                        self.risk.record_failure(aid, OperationKind.READ_HEAVY, err)
                    else:
                        self.risk.record_success(aid, OperationKind.READ_HEAVY)
            except Exception as e:
                self.risk.record_failure(aid, OperationKind.READ_HEAVY, e)
                log.warning("作品健康:账号 %s 抓取失败 %s", aid, e)
                continue
            if not items:
                continue
            self._stamp_active(aid)
            try:
                await self._eval_work_health(aid, platform, items)
            except Exception as e:
                log.warning("作品健康:账号 %s 评估失败 %s", aid, e)

    # 视为「异常/受限」的状态关键词(命中即告警)
    _BAD_STATUS = ("违规", "删除", "下架", "不适宜", "限流", "私密", "仅自己", "审核不")

    async def _eval_work_health(self, account_id, platform, items):
        """upsert 本账号作品 + 判定 0播/违规告警 + 写数据快照。"""
        now = datetime.utcnow()
        zero_hours = self.cfg.engine.work_health_zero_play_hours
        cutoff = time.time() - self.cfg.engine.work_health_recent_days * 86400
        # 该平台是否真的暴露播放量:有任一作品 play>0 才启用「0播」判定,避免对不报播放量的
        # 平台(web 抖音等)误报。
        play_reliable = any((w.get("play_count") or 0) > 0 for w in items)
        alerts = []
        with get_session() as s:
            acc = s.get(DouyinAccount, account_id)
            nick = (acc.nickname if acc else "") or f"账号{account_id}"
            for w in items:
                rec = s.exec(select(AccountWork).where(
                    AccountWork.account_id == account_id,
                    AccountWork.item_id == w["item_id"])).first()
                if rec:
                    for k, v in w.items():
                        setattr(rec, k, v)
                    rec.fetched_at = now
                else:
                    rec = AccountWork(platform=platform, account_id=account_id,
                                      fetched_at=now, **w)
                    s.add(rec); s.flush()
                ct = rec.create_time or 0
                if ct and ct < cutoff:
                    s.add(rec); continue          # 太老,不体检
                title = (rec.desc or rec.item_id or "")[:20]
                st = rec.status or ""
                if st and any(k in st for k in self._BAD_STATUS) and rec.status_alerted != st:
                    alerts.append(("⚠️ 视频号/作品状态异常" if platform == "shipinhao"
                                   else "⚠️ 作品状态异常",
                                   f"{nick}:「{title}」当前状态「{st}」"))
                    rec.status_alerted = st
                if play_reliable and ct:
                    age_h = (time.time() - ct) / 3600
                    if (age_h >= zero_hours and (rec.play_count or 0) == 0
                            and not rec.zero_play_alerted):
                        alerts.append(("⚠️ 作品持续0播",
                                       f"{nick}:「{title}」发布 {age_h:.0f} 小时仍 0 播放,疑似限流"))
                        rec.zero_play_alerted = True
                s.add(rec)
            acc_proxy = (acc.proxy or "") if acc else ""
            s.commit()
        thumb_cache.schedule_thumb_prewarm(
            [str(w.get("cover_url") or "") for w in items],
            platform=platform,
            proxy=acc_proxy,
        )
        if self.cfg.engine.work_health_stat_snapshots:
            self._write_stat_snapshot(account_id, platform, items)
        if alerts:
            try:
                with get_session() as s:
                    chans = s.exec(select(NotificationChannel)
                                   .where(NotificationChannel.enabled == True)).all()  # noqa: E712
                    channels = [{"type": c.type, "config": _loads(c.config)} for c in chans]
                for title, body in alerts:
                    if channels:
                        await notify_all(channels, title, body)
                    log.info("作品健康告警: %s | %s", title, body)
            except Exception:
                pass

    def _write_stat_snapshot(self, account_id, platform, items):
        """每账号每天一行数据快照(粉丝/作品/互动合计),供「数据」趋势视图。"""
        day = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d")  # 东八区日期
        with get_session() as s:
            acc = s.get(DouyinAccount, account_id)
            snap = s.exec(select(AccountStatSnapshot).where(
                AccountStatSnapshot.account_id == account_id,
                AccountStatSnapshot.date == day)).first()
            if not snap:
                snap = AccountStatSnapshot(platform=platform, account_id=account_id, date=day)
            snap.follower_count = (acc.follower_count if acc else 0) or snap.follower_count
            snap.aweme_count = (acc.aweme_count if acc else 0) or snap.aweme_count or len(items)
            # 只有带作品列表(作品健康那趟)才更新互动合计;粉丝-only 快照不清零已有合计
            if items:
                snap.total_like = sum((w.get("like_count") or 0) for w in items)
                snap.total_comment = sum((w.get("comment_count") or 0) for w in items)
                snap.total_play = sum((w.get("play_count") or 0) for w in items)
            s.add(snap); s.commit()
