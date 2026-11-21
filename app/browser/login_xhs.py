"""小红书主站与创作服务平台交互式扫码及安全验证登录流程。"""
from __future__ import annotations

from typing import Tuple
from urllib.parse import parse_qs, urlparse
import asyncio
import json

from app.browser.identity import Identity
from app.browser.login_common import _LOG, _focus, _open_first_environment_check, _read_xhs_nickname
from app.browser.manager import BrowserManager


# 创作服务平台登录后才会写入的 Cookie(发布需要)
_XHS_CREATOR_COOKIES = {"customerClientId", "galaxy_creator_session_id",
                        "access-token-creator.xiaohongshu.com", "customer-sso-sid"}


class XhsSecurityVerificationRequired(RuntimeError):
    """小红书将当前登录导航到了设备安全验证页。"""


def _is_xhs_security_verification_url(url: str) -> bool:
    """识别小红书设备验证页和已知的 IP 风险错误页。"""
    try:
        parsed = urlparse(str(url or ""))
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    if host != "xiaohongshu.com" and not host.endswith(".xiaohongshu.com"):
        return False
    if parsed.path.rstrip("/").lower() == "/website-login/captcha":
        return True
    return "300012" in parse_qs(parsed.query).get("error_code", [])


async def _xhs_web_session(ctx) -> str:
    """取当前 web_session cookie 值；游客态也可能存在且发生轮换。"""
    try:
        for c in await ctx.cookies():
            if c["name"] == "web_session":
                return c.get("value", "") or ""
    except Exception:
        pass
    return ""


_XHS_USER_ME_API = "/api/sns/web/v2/user/me"


_XHS_QRCODE_STATUS_API = "/api/sns/web/v1/login/qrcode/status"


_XHS_IDENTITY_RECHECK_DELAY_SECONDS = 2.0


_XHS_PAGE_IDENTITY_POLL_SECONDS = 1.0


_XHS_PAGE_IDENTITY_JS = """() => {
  try {
    const marker = 'window.__INITIAL_STATE__=';
    let state = window.__INITIAL_STATE__;
    if (!state || !Object.keys(state).length) {
      for (const script of document.scripts) {
        const text = script.textContent || '';
        const at = text.indexOf(marker);
        if (at < 0) continue;
        try {
          state = (new Function(
            'return (' + text.slice(at + marker.length) + ')'))();
        } catch (_) { state = null; }
        break;
      }
    }
    const unwrap = (input) => {
      let value = input;
      for (let i = 0; i < 3 && value && typeof value === 'object'; i++) {
        if (value._rawValue !== undefined) value = value._rawValue;
        else if (value._value !== undefined) value = value._value;
        else break;
      }
      return value;
    };
    const root = unwrap((state || {}).user) || {};
    const candidates = [
      root.userInfo, root.loginUser, root.currentUser, root.me,
      root.userPageData, root.info
    ];
    for (let candidate of candidates) {
      candidate = unwrap(candidate);
      if (!candidate || typeof candidate !== 'object') continue;
      const basic = unwrap(candidate.basicInfo || candidate.basic_info) || {};
      const userId = candidate.userId || candidate.user_id ||
        basic.userId || basic.user_id || '';
      const redId = candidate.redId || candidate.red_id ||
        basic.redId || basic.red_id || '';
      if (!userId && !redId) continue;
      return {
        user_id: String(userId || ''),
        red_id: String(redId || ''),
        nickname: String(candidate.nickname || candidate.nickName ||
          basic.nickname || basic.nickName || ''),
        guest: candidate.guest === true
      };
    }
    return null;
  } catch (_) { return null; }
}"""


async def _xhs_page_identity(page) -> dict:
    try:
        value = await page.evaluate(_XHS_PAGE_IDENTITY_JS)
    except Exception:
        return {}
    if not isinstance(value, dict) or value.get("guest") is True:
        return {}
    if not (value.get("user_id") or value.get("red_id")):
        return {}
    return value


def _xhs_login_response_handler(
        authenticated_user: dict, login_signals: dict | None = None):
    async def on_response(response):
        try:
            parsed = urlparse(str(response.url or ""))
            host = (parsed.hostname or "").lower()
            if not (host == "xiaohongshu.com"
                    or host.endswith(".xiaohongshu.com")):
                return
            if int(response.status) != 200:
                return
            payload = await response.json()
            path = parsed.path.rstrip("/").lower()
            if path == _XHS_QRCODE_STATUS_API:
                data = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(data, dict):
                    data = payload if isinstance(payload, dict) else {}
                code_status = data.get("codeStatus", data.get("code_status"))
                try:
                    code_status = int(code_status)
                except (TypeError, ValueError):
                    code_status = -1
                # Current XHS web client maps 0=waiting, 1=scanned,
                # 2=login completed, 3=expired and emits onLogin for status 2.
                if code_status == 2 and login_signals is not None:
                    login_signals["qr_confirmed"] = True
                    _LOG.info("XHS QR login confirmed by qrcode/status")
                return
            if path != _XHS_USER_ME_API:
                return
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict) or data.get("guest") is True:
                return
            if not (data.get("user_id") or data.get("red_id")):
                return
            authenticated_user.update(data)
        except Exception:
            return
    return on_response


async def interactive_xhs_login(mgr: BrowserManager, identity: Identity,
                                timeout_seconds: int = 180,
                                force_reauth: bool = False,
                                status_callback=None,
                                ) -> Tuple[bool, str, str]:
    """小红书扫码登录。打开真实窗口让用户扫码,落地登录态。
    返回 (是否成功, storage_state_json, nickname)。

    判定登录:必须同时观察到有效 web_session，以及主站 user/me 返回非游客用户身份。
    游客态 web_session 和登录弹窗 Cookie 都可能在扫码前生成或轮换，不能单独作为
    成功信号。用户中途关掉窗口则视为未登录。"""
    ctx = await mgr.context_for(identity)
    if force_reauth:
        await ctx.clear_cookies()
    await _open_first_environment_check(mgr, identity, ctx)
    logged = False
    nickname = ""
    state_json = ""
    security_verification_seen = False
    authenticated_user: dict = {}
    login_signals: dict = {}
    on_response = _xhs_login_response_handler(
        authenticated_user, login_signals)
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
        # A user may open the homepage manually in a second tab after the first
        # clean-Profile navigation receives device verification.  Follow that
        # real tab instead of keeping the login task pinned to the captcha tab.
        for candidate in reversed(context_pages(fallback)):
            try:
                current = str(candidate.url or "")
                parsed = urlparse(current)
                host = (parsed.hostname or "").lower()
                if host != "xiaohongshu.com" and not host.endswith(
                        ".xiaohongshu.com"):
                    continue
                if _is_xhs_security_verification_url(current):
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
    async with mgr.visible_page(identity) as page:
        await _focus(page)
        observe(page)
        context_on = getattr(ctx, "on", None)
        if callable(context_on):
            try:
                context_on("page", observe)
            except Exception:
                pass
        # 从官网首页进入登录流程。直接访问 /explore 会让全新隔离 profile
        # 更容易被重定向到 website-login/captcha 的设备安全验证页。
        await page.goto(
            "https://www.xiaohongshu.com/",
            wait_until="domcontentloaded", timeout=60000)
        security_verification_seen = _is_xhs_security_verification_url(page.url)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0, timeout_seconds)
        while loop.time() < deadline:
            if page.is_closed():                  # 没登录就关了窗口 -> 视为未登录
                break
            for candidate in context_pages(page):
                observe(candidate)
            security_verification_seen = (
                security_verification_seen
                or _is_xhs_security_verification_url(page.url)
            )
            if security_verification_seen and not security_notified:
                security_notified = True
                _LOG.info("XHS device verification detected: %s", page.url)
                await report_status(str(page.url or ""))
            active_page = usable_page(page)
            active_url = str(getattr(active_page, "url", "") or "")
            # Device verification is an explicit platform decision.  Keep the
            # visible page available for the account owner, but never navigate
            # around it or issue automated recovery retries.
            if (security_notified and not security_cleared_notified
                    and not _is_xhs_security_verification_url(active_url)
                    and "xiaohongshu.com" in active_url.lower()
                    and "passport" not in active_url.lower()):
                security_cleared_notified = True
                await report_status(active_url)
            ws = await _xhs_web_session(ctx)
            # The QR flow can update web_session without issuing user/me again
            # in the tab that was already on /explore.  Cookie alone is not a
            # success signal because guests also receive web_session.  When
            # its value changes, visibly reload the healthy XHS page once so
            # the site's own startup request supplies an authoritative
            # guest/non-guest user/me response to our existing listener.
            if (ws and len(ws) >= 20 and not authenticated_user
                    and ws != last_rechecked_session
                    and loop.time() >= next_identity_recheck_at
                    and "xiaohongshu.com" in active_url.lower()
                    and "passport" not in active_url.lower()
                    and not _is_xhs_security_verification_url(active_url)):
                last_rechecked_session = ws
                next_identity_recheck_at = (
                    loop.time() + _XHS_IDENTITY_RECHECK_DELAY_SECONDS)
                try:
                    _LOG.info(
                        "XHS web_session changed; reloading page to verify identity")
                    await active_page.reload(
                        wait_until="domcontentloaded", timeout=30000)
                    active_url = str(active_page.url or "")
                except Exception as exc:
                    _LOG.warning("XHS identity recheck reload failed: %r", exc)
            # Some successful QR flows update the page's reactive current-user
            # state but neither rotate web_session nor emit another user/me
            # request.  Read only the dedicated current-user branch from the
            # page state; requiring user_id/red_id avoids treating feed authors
            # or a guest web_session as the logged-in account.
            if (not authenticated_user
                    and loop.time() >= next_page_identity_poll_at
                    and "xiaohongshu.com" in active_url.lower()
                    and "passport" not in active_url.lower()
                    and not _is_xhs_security_verification_url(active_url)):
                next_page_identity_poll_at = (
                    loop.time() + _XHS_PAGE_IDENTITY_POLL_SECONDS)
                page_user = await _xhs_page_identity(active_page)
                if page_user:
                    authenticated_user.update(page_user)
                    _LOG.info(
                        "XHS authenticated identity found in page state: %s",
                        page_user.get("user_id") or page_user.get("red_id"))
            # Cookie 只作必要条件；user/me 的非游客身份才是授权完成证据。
            identity_confirmed = bool(
                authenticated_user or login_signals.get("qr_confirmed"))
            if (ws and len(ws) >= 20 and identity_confirmed
                    and "passport" not in active_url
                    and not _is_xhs_security_verification_url(active_url)):
                try:
                    state_json = json.dumps(await ctx.storage_state())
                    logged = True
                except Exception:
                    pass
                if logged:
                    try:
                        identity.observed_login_profile = dict(
                            authenticated_user or {})
                    except Exception:
                        pass
                    nickname = str(
                        authenticated_user.get("nickname") or "").strip()[:40]
                    if not nickname:
                        try:
                            nickname = await _read_xhs_nickname(active_page)
                        except Exception:
                            pass
                    # 普通登录只保存主站读取态。创作平台是独立登录入口，避免在
                    # 扫码完成后跨站跳转导致 Chromium 周期性拉起临时页签。
                    break
            await mgr.xhs_interaction.pause(0.70, 1.15)
    if not logged and security_verification_seen:
        raise XhsSecurityVerificationRequired(
            "小红书要求完成设备安全验证；请保持当前网络和账号环境稳定，"
            "重新打开登录窗口后按页面提示验证"
        )
    return logged, state_json, nickname


async def interactive_xhs_creator_login(mgr: BrowserManager, identity: Identity,
                                        timeout_seconds: int = 180,
                                        force_reauth: bool = False,
                                        ) -> Tuple[bool, str, str]:
    """小红书「创作服务平台」登录(发布/已发布列表用)。打开 creator.xiaohongshu.com/login
    扫码,落地含创作者会话的登录态。返回 (是否成功, storage_state_json, nickname)。
    与普通登录区分:这里登录的是创作平台,登录态里含 customerClientId / galaxy_creator_session_id 等。"""
    ctx = await mgr.context_for(identity)
    if force_reauth:
        # Only revoke creator cookies so complementing creator auth on an
        # active read account preserves the main-site session in the profile.
        try:
            await ctx.clear_cookies(domain="creator.xiaohongshu.com")
            for cookie_name in _XHS_CREATOR_COOKIES:
                await ctx.clear_cookies(name=cookie_name)
        except TypeError:
            await ctx.clear_cookies()
    await _open_first_environment_check(mgr, identity, ctx)
    logged = False
    nickname = ""
    state_json = ""
    async with mgr.visible_page(identity) as page:
        await _focus(page)
        # creator.xiaohongshu.com/login loads dozens of large script bundles
        # before DOMContentLoaded; return on commit so slow/proxied networks
        # render visibly instead of aborting the browser window at 30s.
        await page.goto(
            "https://creator.xiaohongshu.com/login",
            wait_until="commit", timeout=60000)
        await _focus(page)
        init_ws = await _xhs_web_session(ctx)
        deadline = asyncio.get_running_loop().time() + max(0, timeout_seconds)
        while asyncio.get_running_loop().time() < deadline:
            if page.is_closed():
                break
            cookies = await ctx.cookies()
            names = {c["name"] for c in cookies}
            ws = next((c.get("value", "") for c in cookies if c["name"] == "web_session"), "")
            # 创作平台登录成功:出现创作者专属 Cookie,或离开 /login 且 web_session 变为有效
            on_creator = "creator.xiaohongshu.com" in page.url and "/login" not in page.url
            if (names & _XHS_CREATOR_COOKIES) or \
                    (on_creator and ws and len(ws) >= 20 and ws != init_ws):
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
