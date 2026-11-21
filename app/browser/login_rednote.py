"""海外小红书 (RedNote) 主站与创作者中心交互式登录及域隔离验证。"""
from __future__ import annotations

from typing import Tuple
from urllib.parse import parse_qs, urlparse
import asyncio
import json

from app.browser.identity import Identity
from app.browser.login_common import _LOG, _focus, _open_first_environment_check, _read_xhs_nickname
from app.browser.login_xhs import _XHS_IDENTITY_RECHECK_DELAY_SECONDS, _XHS_PAGE_IDENTITY_POLL_SECONDS, _XHS_QRCODE_STATUS_API, _XHS_USER_ME_API, _xhs_page_identity
from app.browser.manager import BrowserManager


# ── 海外小红书（Rednote）登录 ──
_REDNOTE_CREATOR_COOKIES = {
    "customerClientId",
    "galaxy_creator_session_id",
    "access-token-creator.rednote.com",
    "customer-sso-sid",
}


class RednoteSecurityVerificationRequired(RuntimeError):
    """海外小红书将当前登录导航到了设备安全验证页。"""


def _is_rednote_cookie_domain(domain: str) -> bool:
    clean = str(domain or "").strip().lstrip(".").lower()
    return not clean or clean == "rednote.com" or clean.endswith(".rednote.com")


def _is_rednote_security_verification_url(url: str) -> bool:
    try:
        parsed = urlparse(str(url or ""))
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    if host != "rednote.com" and not host.endswith(".rednote.com"):
        return False
    if parsed.path.rstrip("/").lower() == "/website-login/captcha":
        return True
    return "300012" in parse_qs(parsed.query).get("error_code", [])


async def _rednote_web_session(ctx) -> str:
    """取当前 rednote.com 域下的 web_session cookie 值。"""
    try:
        for c in await ctx.cookies():
            if c.get("name") == "web_session" and _is_rednote_cookie_domain(
                c.get("domain", "")
            ):
                return c.get("value", "") or ""
    except Exception:
        pass
    return ""


def _rednote_login_response_handler(
    authenticated_user: dict, login_signals: dict | None = None
):
    async def on_response(response):
        try:
            parsed = urlparse(str(response.url or ""))
            host = (parsed.hostname or "").lower()
            if host != "rednote.com" and not host.endswith(".rednote.com"):
                return
            if int(response.status) != 200:
                return
            payload = await response.json()
            path = parsed.path.rstrip("/").lower()
            if path in (_XHS_QRCODE_STATUS_API, "/web_api/sns/v1/login/qrcode/status"):
                data = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(data, dict):
                    data = payload if isinstance(payload, dict) else {}
                code_status = data.get("codeStatus", data.get("code_status"))
                try:
                    code_status = int(code_status)
                except (TypeError, ValueError):
                    code_status = -1
                if code_status == 2 and login_signals is not None:
                    login_signals["qr_confirmed"] = True
                    _LOG.info("Rednote QR login confirmed by qrcode/status")
                return
            if path not in (_XHS_USER_ME_API, "/web_api/sns/v2/user/me"):
                return
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict) or data.get("guest") is True:
                return
            if not (data.get("user_id") or data.get("red_id") or data.get("userId")):
                return
            authenticated_user.update(data)
        except Exception:
            return

    return on_response


async def interactive_rednote_login(
    mgr: BrowserManager,
    identity: Identity,
    timeout_seconds: int = 240,
    force_reauth: bool = False,
    status_callback=None,
) -> Tuple[bool, str, str]:
    """海外小红书（Rednote）主站扫码/网页登录。

    严格使用 ``https://www.rednote.com/zh``（避免无语言前缀 ``/explore`` 被 CDN 302 跳转至
    国内 ``www.xiaohongshu.com``），不接受任何 ``xiaohongshu.com`` Cookie 或响应。
    """
    from app.browser.manager import install_rednote_domain_guard
    from app.platforms.rednote.domains import HOME_PAGE_URL as REDNOTE_CREATOR_HOME_URL, LOGIN_PAGE_URL as REDNOTE_LOGIN_URL, SHOW_QRCODE_SCRIPT

    ctx = await mgr.context_for(identity)
    await install_rednote_domain_guard(ctx)
    if force_reauth:
        await ctx.clear_cookies()
    await _open_first_environment_check(mgr, identity, ctx)
    logged = False
    nickname = ""
    state_json = ""
    security_verification_seen = False
    authenticated_user: dict = {}
    login_signals: dict = {}
    on_response = _rednote_login_response_handler(authenticated_user, login_signals)
    observed_pages: set[int] = set()

    def observe(candidate) -> None:
        marker = id(candidate)
        if marker in observed_pages:
            return
        observed_pages.add(marker)
        try:
            candidate.on("response", on_response)
        except Exception:
            pass

    def context_pages(fallback) -> list:
        pages = getattr(ctx, "pages", ())
        if not isinstance(pages, (list, tuple)):
            pages = ()
        result = [candidate for candidate in pages if candidate is not None]
        if fallback is not None and fallback not in result:
            result.append(fallback)
        return result

    def usable_page(fallback):
        for candidate in reversed(context_pages(fallback)):
            try:
                current = str(candidate.url or "")
                parsed = urlparse(current)
                host = (parsed.hostname or "").lower()
                if host != "rednote.com" and not host.endswith(".rednote.com"):
                    continue
                if _is_rednote_security_verification_url(current):
                    continue
                if "passport" in current.lower():
                    continue
                return candidate
            except Exception:
                continue
        return fallback

    async def report_status(url: str) -> None:
        if not callable(status_callback):
            return
        try:
            result = status_callback(url)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            pass

    security_notified = False
    security_cleared_notified = False
    last_rechecked_session = ""
    next_identity_recheck_at = 0.0
    next_page_identity_poll_at = 0.0
    next_login_btn_click_at = 0.0

    async def _ensure_rednote_login_modal(target_page) -> bool:
        nonlocal next_login_btn_click_at
        try:
            if bool(await target_page.evaluate(SHOW_QRCODE_SCRIPT)):
                return True
        except Exception:
            pass
        locator_fn = getattr(target_page, "locator", None)
        if not callable(locator_fn):
            return False
        now = asyncio.get_running_loop().time()
        if now < next_login_btn_click_at:
            return False
        try:
            modal = locator_fn(".login-container, .login-modal, .login-box-container").first
            if await modal.is_visible(timeout=300):
                return False
        except Exception:
            pass
        try:
            btn = locator_fn(".login-btn").first
            if await btn.is_visible(timeout=500):
                next_login_btn_click_at = now + 3.0
                await btn.click(timeout=1500)
        except Exception:
            pass
        return False

    async with mgr.visible_page(identity) as page:
        await _focus(page)
        observe(page)
        context_on = getattr(ctx, "on", None)
        if callable(context_on):
            try:
                context_on("page", observe)
            except Exception:
                pass
        await page.goto(
            REDNOTE_LOGIN_URL, wait_until="domcontentloaded", timeout=60000
        )
        qr_triggered = await _ensure_rednote_login_modal(page)
        security_verification_seen = _is_rednote_security_verification_url(page.url)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0, timeout_seconds)
        while loop.time() < deadline:
            if page.is_closed():
                break
            cur_host = (urlparse(str(getattr(page, "url", "") or "")).hostname or "").lower()
            if cur_host == "xiaohongshu.com" or cur_host.endswith(".xiaohongshu.com"):
                try:
                    await page.goto(
                        REDNOTE_LOGIN_URL, wait_until="domcontentloaded", timeout=30000
                    )
                    qr_triggered = False
                except Exception:
                    pass
            if not qr_triggered:
                qr_triggered = await _ensure_rednote_login_modal(page)
            for candidate in context_pages(page):
                observe(candidate)
            security_verification_seen = (
                security_verification_seen
                or _is_rednote_security_verification_url(page.url)
            )
            if security_verification_seen and not security_notified:
                security_notified = True
                _LOG.info("Rednote device verification detected: %s", page.url)
                await report_status(str(page.url or ""))
            active_page = usable_page(page)
            active_url = str(getattr(active_page, "url", "") or "")
            if (
                security_notified
                and not security_cleared_notified
                and not _is_rednote_security_verification_url(active_url)
                and "rednote.com" in active_url.lower()
                and "passport" not in active_url.lower()
            ):
                security_cleared_notified = True
                await report_status(active_url)
            ws = await _rednote_web_session(ctx)
            if (
                ws
                and len(ws) >= 20
                and not authenticated_user
                and ws != last_rechecked_session
                and loop.time() >= next_identity_recheck_at
                and "rednote.com" in active_url.lower()
                and "passport" not in active_url.lower()
                and not _is_rednote_security_verification_url(active_url)
            ):
                last_rechecked_session = ws
                next_identity_recheck_at = (
                    loop.time() + _XHS_IDENTITY_RECHECK_DELAY_SECONDS
                )
                try:
                    _LOG.info(
                        "Rednote web_session changed; reloading page to verify identity"
                    )
                    await active_page.reload(
                        wait_until="domcontentloaded", timeout=30000
                    )
                    active_url = str(active_page.url or "")
                except Exception as exc:
                    _LOG.warning("Rednote identity recheck reload failed: %r", exc)
            if (
                not authenticated_user
                and loop.time() >= next_page_identity_poll_at
                and "rednote.com" in active_url.lower()
                and "passport" not in active_url.lower()
                and not _is_rednote_security_verification_url(active_url)
            ):
                next_page_identity_poll_at = (
                    loop.time() + _XHS_PAGE_IDENTITY_POLL_SECONDS
                )
                page_user = await _xhs_page_identity(active_page)
                if page_user:
                    authenticated_user.update(page_user)
                    _LOG.info(
                        "Rednote authenticated identity found in page state: %s",
                        page_user.get("user_id") or page_user.get("red_id"),
                    )
            identity_confirmed = bool(
                authenticated_user or login_signals.get("qr_confirmed")
            )
            if (
                ws
                and len(ws) >= 20
                and identity_confirmed
                and "passport" not in active_url
                and not _is_rednote_security_verification_url(active_url)
            ):
                try:
                    identity.observed_login_profile = dict(
                        authenticated_user or {}
                    )
                except Exception:
                    pass
                nickname = str(
                    authenticated_user.get("nickname") or ""
                ).strip()[:40]
                if not nickname:
                    try:
                        nickname = await _read_xhs_nickname(active_page)
                    except Exception:
                        pass
                # Best-effort SSO hop to creator.rednote.com/new/note-manager (matching 22366 frontLogin redirectTo)
                try:
                    await active_page.goto(
                        REDNOTE_CREATOR_HOME_URL,
                        wait_until="domcontentloaded",
                        timeout=15000,
                    )
                    for _ in range(4):
                        cks = await ctx.cookies()
                        rn_names = {
                            c["name"]
                            for c in cks
                            if _is_rednote_cookie_domain(c.get("domain", "")) and c.get("value")
                        }
                        if rn_names & _REDNOTE_CREATOR_COOKIES:
                            break
                        await asyncio.sleep(0.6)
                except Exception:
                    pass
                try:
                    state_json = json.dumps(await ctx.storage_state())
                    logged = True
                except Exception:
                    pass
                if logged:
                    break
            await mgr.xhs_interaction.pause(0.70, 1.15)
    if not logged and security_verification_seen:
        raise RednoteSecurityVerificationRequired(
            "海外小红书要求完成设备安全验证；请保持当前海外网络与账号环境稳定，"
            "重新打开登录窗口后按页面提示验证"
        )
    return logged, state_json, nickname


async def interactive_rednote_creator_login(
    mgr: BrowserManager,
    identity: Identity,
    timeout_seconds: int = 240,
    force_reauth: bool = False,
) -> Tuple[bool, str, str]:
    """海外小红书创作服务平台登录（``creator.rednote.com/login``，发布与笔记管理用）。"""
    from app.browser.manager import install_rednote_domain_guard
    from app.platforms.rednote.domains import CREATOR_LOGIN_URL, rednote_listener_decision

    ctx = await mgr.context_for(identity)
    await install_rednote_domain_guard(ctx)
    if force_reauth:
        try:
            await ctx.clear_cookies(domain="creator.rednote.com")
            for cookie_name in _REDNOTE_CREATOR_COOKIES:
                await ctx.clear_cookies(name=cookie_name)
        except TypeError:
            await ctx.clear_cookies()
    await _open_first_environment_check(mgr, identity, ctx)
    logged = False
    nickname = ""
    state_json = ""
    async with mgr.visible_page(identity) as page:
        await _focus(page)
        await page.goto(CREATOR_LOGIN_URL, wait_until="commit", timeout=60000)
        await _focus(page)
        init_ws = await _rednote_web_session(ctx)
        deadline = asyncio.get_running_loop().time() + max(0, timeout_seconds)
        while asyncio.get_running_loop().time() < deadline:
            if page.is_closed():
                break
            cookies = await ctx.cookies()
            rn_cookies = [
                c for c in cookies if _is_rednote_cookie_domain(c.get("domain", ""))
            ]
            names = {c["name"] for c in rn_cookies if c.get("value")}
            ws = next(
                (c.get("value", "") for c in rn_cookies if c["name"] == "web_session"),
                "",
            )
            cur_url = str(getattr(page, "url", "") or "")
            decision = rednote_listener_decision(cur_url)
            on_creator = (
                "creator.rednote.com" in cur_url.lower()
                and "/login" not in cur_url.lower()
            )
            if (
                (names & _REDNOTE_CREATOR_COOKIES)
                or (isinstance(decision, dict) and decision.get("login"))
                or (on_creator and ws and len(ws) >= 20)
            ):
                try:
                    state_json = json.dumps(await ctx.storage_state())
                    logged = True
                except Exception:
                    pass
                if logged:
                    try:
                        nickname = await _read_xhs_nickname(page)
                    except Exception:
                        pass
                    break
            await mgr.xhs_interaction.pause(0.70, 1.15)
    return logged, state_json, nickname
