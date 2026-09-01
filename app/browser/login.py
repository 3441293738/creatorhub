"""交互式扫码登录:打开真实浏览器窗口,用户扫码,落地登录态。
对应原项目 internal/douyin.QRLoginManager(chromedp 版)。
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .identity import Identity
from .manager import BrowserManager

_LOG = logging.getLogger(__name__)

# 登录后才会出现的 Cookie(用于判断是否已登录)
_LOGIN_COOKIES = {"sessionid", "sessionid_ss", "sid_tt", "uid_tt", "sid_guard"}


async def _focus(page):
    """把扫码窗口提到前台(否则有头浏览器常开在其它窗口后面)。"""
    try:
        await page.bring_to_front()
    except Exception:
        pass


async def _open_first_environment_check(
        mgr: BrowserManager, identity: Identity, ctx) -> None:
    """Best-effort first-run diagnostic; never block the platform login flow."""
    opener = getattr(mgr, "open_environment_check", None)
    if not callable(opener):
        return
    try:
        await opener(identity, context=ctx)
    except Exception as exc:
        _LOG.warning("Fingerprint environment check page failed to open: %r", exc)


async def _reuse_or_create_login_page(ctx):
    """兼容旧调用：复用持久 Context 的空白页，否则创建一个新页。"""
    for candidate in ctx.pages:
        try:
            if candidate.url == "about:blank" and not candidate.is_closed():
                return candidate
        except Exception:
            continue
    return await ctx.new_page()


async def interactive_login(mgr: BrowserManager, identity: Identity,
                            timeout_seconds: int = 180,
                            start_url: str = "https://www.douyin.com/",
                            force_reauth: bool = False,
                            ) -> Tuple[bool, str, str]:
    """返回 (是否成功, storage_state_json, nickname)。
    在账号专属持久 profile(独立 UA/视口/时区/代理/指纹)里有头扫码,
    登录态直接落盘到该 profile;同时返回 storage_state 供库内展示/兜底。
    start_url 用 creator.douyin.com 即为创作中心登录(其登录态因 .douyin.com 共享 Cookie,
    同样可用于 www 公开抓取)。"""
    ctx = await mgr.open_headed(identity)
    if force_reauth:
        # “重新登录”必须从干净的认证态开始。持久 profile 和数据库快照里可能
        # 仍保存着已被服务端撤销的 sessionid；若不先清掉，下面仅按 Cookie 名
        # 轮询会在窗口刚打开时把旧 Cookie 误判为扫码成功。
        await ctx.clear_cookies()
    await _open_first_environment_check(mgr, identity, ctx)
    page = await ctx.new_page()
    await _focus(page)
    logged = False
    nickname = ""
    state_json = ""

    try:
        await page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
        await _focus(page)
        # 尝试自动弹出登录框(失败也没关系,用户可自行点“登录”)
        for sel in ('text=登录', '[data-e2e="login-button"]', 'button:has-text("登录")'):
            try:
                await page.click(sel, timeout=2500)
                break
            except Exception:
                continue

        # 轮询登录态(用户关窗 -> 立即视为未登录,不抛错)
        waited = 0.0
        while waited < timeout_seconds:
            if page.is_closed():
                break
            try:
                cookies = await ctx.cookies()
            except Exception:
                break
            names = {c["name"] for c in cookies}
            if names & _LOGIN_COOKIES:
                # Cookie 写入后再确认页面已离开登录态。扫码弹窗尚在时继续等，
                # 避免平台刚轮换游客/旧 Cookie 就提前结束登录流程。
                try:
                    login_visible = await page.get_by_text(
                        "登录", exact=True).first.is_visible(timeout=500)
                except Exception:
                    login_visible = None
                current_url = str(page.url or "").lower()
                if login_visible is not True and "passport" not in current_url \
                        and "/login" not in current_url:
                    logged = True
                    break
            # 扫码确认后尽快通知主页面；Cookie 查询很轻量，500ms 足够且比原来的
            # 2 秒轮询明显更跟手。
            await asyncio.sleep(0.5)
            waited += 0.5

        if logged:
            await page.wait_for_timeout(800)   # 给登录跳转/localStorage 一次落盘机会
            state = await ctx.storage_state()
            state_json = json.dumps(state)
            nickname = await _read_nickname(page)
    finally:
        try:
            await ctx.close()
        except Exception:
            pass

    return logged, state_json, nickname


async def interactive_creator_login(mgr: BrowserManager, identity: Identity,
                                    timeout_seconds: int = 180,
                                    force_reauth: bool = False):
    """创作中心登录。返回 (ok, storage_state_json, nickname)。"""
    return await interactive_login(
        mgr, identity, timeout_seconds,
        start_url="https://creator.douyin.com/",
        force_reauth=force_reauth,
    )


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
            wait_until="domcontentloaded", timeout=30000)
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
        await ctx.clear_cookies()
    await _open_first_environment_check(mgr, identity, ctx)
    logged = False
    nickname = ""
    state_json = ""
    async with mgr.visible_page(identity) as page:
        await _focus(page)
        await page.goto(
            "https://creator.xiaohongshu.com/login",
            wait_until="domcontentloaded", timeout=30000)
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


# ── 快手登录 ──
# 快手主站根路径在未登录的新浏览器里偶尔只返回一段 ``result=2`` JSON，
# 不会渲染登录入口；新版推荐页才是稳定的 Web 登录落点。
_KS_HOME_URL = "https://www.kuaishou.com/new-reco"
# 登录判定:passToken 是强信号；新版主站也会写 webday7_st，旧版则是 web_st。
_KS_LOGIN_COOKIES = {
    "passToken", "userId", "kuaishou.server.web_st",
    "kuaishou.server.webday7_st",
}
# 创作平台(cp.kuaishou.com)登录后才会写入的 Cookie(发布需要)
_KS_CREATOR_COOKIES = {"kuaishou.web.cp.api_st", "kuaishou.web.cp.api_ph"}

_KS_WEB_LOGIN_BUTTONS = (
    '[role="button"].sidebar-login-button',
    'span[role="button"]:has-text("登录")',
    'button:has-text("登录")',
    '[class*="login-button"]',
)
_KS_CREATOR_LOGIN_BUTTONS = (
    'button:has-text("立即登录")',
    '[role="button"]:has-text("立即登录")',
    'text=立即登录',
    'button:has-text("登录")',
)


def _ks_web_login_ready(cookie_names) -> bool:
    """兼容当前 webday7_st 与旧 web_st 的快手主站登录态。"""
    names = set(cookie_names or ())
    if "passToken" in names:
        return True
    has_web_st = any(
        name in {"web_st", "kuaishou.server.web_st"}
        or name.endswith(".webday7_st")
        for name in names
    )
    return "userId" in names and has_web_st


async def _click_ks_login_button(page, selectors) -> bool:
    """只点击可见的登录按钮，避免旧的 ``[class*=login]`` 点中整块容器。"""
    for selector in selectors:
        try:
            loc = page.locator(selector)
            for index in range(await loc.count()):
                candidate = loc.nth(index)
                if not await candidate.is_visible():
                    continue
                await candidate.click(timeout=3000)
                return True
        except Exception:
            continue
    return False


async def interactive_ks_login(mgr: BrowserManager, identity: Identity,
                               timeout_seconds: int = 180,
                               start_url: str = _KS_HOME_URL,
                               force_reauth: bool = False,
                               ) -> Tuple[bool, str, str]:
    """快手扫码登录。打开真实窗口让用户扫码,落地登录态。
    返回 (是否成功, storage_state_json, nickname)。
    判定登录:出现 userId + web_st/passToken(游客态没有)。用户中途关窗视为未登录。
    ⚠️ 选择器/登录态 Cookie 名随快手改版可能变化,集中在 _KS_LOGIN_COOKIES。"""
    ctx = await mgr.open_headed(identity)
    if force_reauth:
        await ctx.clear_cookies()
    await _open_first_environment_check(mgr, identity, ctx)
    page = await ctx.new_page()
    await _focus(page)
    logged = False
    nickname = ""
    state_json = ""
    try:
        await page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
        await _focus(page)
        # 新版按钮是 ``span[role=button].sidebar-login-button``，文案被拆成
        # “立即”+“登录”，旧的 text=登录 / [class*=login] 会点不中或点到容器。
        await _click_ks_login_button(page, _KS_WEB_LOGIN_BUTTONS)
        waited = 0
        while waited < timeout_seconds:
            if page.is_closed():
                break
            try:
                cookies = await ctx.cookies()
            except Exception:
                break
            names = {c["name"] for c in cookies}
            if _ks_web_login_ready(names):
                logged = True
                break
            await asyncio.sleep(0.5)
            waited += 0.5
        if logged:
            await page.wait_for_timeout(1500)
            state_json = json.dumps(await ctx.storage_state())
            nickname = await _read_ks_nickname(page)
    finally:
        try:
            await ctx.close()
        except Exception:
            pass
    return logged, state_json, nickname


async def interactive_ks_creator_login(mgr: BrowserManager, identity: Identity,
                                       timeout_seconds: int = 180,
                                       force_reauth: bool = False,
                                       ) -> Tuple[bool, str, str]:
    """快手「创作者服务平台」登录(cp.kuaishou.com,发布用)。扫码后落地含创作者会话的登录态。
    返回 (是否成功, storage_state_json, nickname)。登录成功标志:出现 cp.api_st/api_ph。"""
    ctx = await mgr.open_headed(identity)
    if force_reauth:
        await ctx.clear_cookies()
    await _open_first_environment_check(mgr, identity, ctx)
    page = await ctx.new_page()
    await _focus(page)
    logged = False
    nickname = ""
    state_json = ""
    try:
        await page.goto("https://cp.kuaishou.com/", wait_until="domcontentloaded",
                        timeout=30000)
        await _focus(page)
        await _click_ks_login_button(page, _KS_CREATOR_LOGIN_BUTTONS)
        waited = 0
        while waited < timeout_seconds:
            if page.is_closed():
                break
            try:
                cookies = await ctx.cookies()
            except Exception:
                break
            names = {c["name"] for c in cookies}
            on_cp = "cp.kuaishou.com" in page.url and "/passport" not in page.url
            if (names & _KS_CREATOR_COOKIES) or (
                    on_cp and _ks_web_login_ready(names)):
                await page.wait_for_timeout(1500)
                try:
                    state_json = json.dumps(await ctx.storage_state())
                    logged = True
                    nickname = await _read_ks_nickname(page)
                except Exception:
                    pass
                if logged:
                    break
            await asyncio.sleep(0.5)
            waited += 0.5
    finally:
        try:
            await ctx.close()
        except Exception:
            pass
    return logged, state_json, nickname


# ── 视频号(微信视频号 / finder)登录 ──
# 视频号助手登录后写入的 Cookie(挂在 channels.weixin.qq.com / .weixin.qq.com)。
# sessionid / _finder_auth 才能作为强登录信号。wxuin 在扫码授权过程尚未完成时
# 也可能提前出现，不能单独据此判定登录成功，否则会出现“刚提示成功又登录失效”。
_CHANNELS_STRONG_LOGIN_COOKIES = {"sessionid", "_finder_auth"}
_CHANNELS_AUTH_APIS = (
    "mmfinderassistant-bin/auth/get_auth_info",
    "mmfinderassistant-bin/auth/auth_data",
)


def _channels_auth_response_ok(payload: dict) -> bool:
    """判断视频号 auth/* 响应是否明确表示已认证。

    当前接口常见形态为 ``{errCode: 0, data: {...}}``。只接受明确的成功码，
    避免 HTTP 201 但业务层未登录的响应被当成登录成功。
    """
    if not isinstance(payload, dict):
        return False
    code = None
    for key in ("errCode", "err_code", "ret", "retCode", "code"):
        if key in payload:
            code = payload.get(key)
            break
    if code is None:
        base = payload.get("baseResponse") or payload.get("base_response") or {}
        if isinstance(base, dict):
            for key in ("errCode", "err_code", "ret", "retCode", "code"):
                if key in base:
                    code = base.get(key)
                    break
    if code is None:
        return False
    try:
        return int(code) == 0
    except (TypeError, ValueError):
        return str(code).strip().lower() in {"ok", "success"}


def _channels_login_ready(cookie_names, auth_verified: bool, on_platform: bool) -> bool:
    """登录页跳转后，必须有强 Cookie 或成功的 auth/* 业务响应。"""
    return bool(
        on_platform
        and (auth_verified or (_CHANNELS_STRONG_LOGIN_COOKIES & set(cookie_names or ())))
    )


async def interactive_channels_login(mgr: BrowserManager, identity: Identity,
                                     timeout_seconds: int = 180,
                                     force_reauth: bool = False,
                                     ) -> Tuple[bool, str, str]:
    """视频号扫码登录。打开视频号助手登录页让用户用微信扫码,落地登录态。
    返回 (是否成功, storage_state_json, nickname)。
    视频号只有一套登录态(助手即创作平台),读取/发布共用,故无独立创作登录。
    昵称登录页不易读,返回空串,交由「资料刷新」用 fetch_channels_self_profile 补全。
    ⚠️ 登录判定 Cookie 名随视频号改版可能变化,集中在 _CHANNELS_LOGIN_COOKIES。"""
    ctx = await mgr.open_headed(identity)
    if force_reauth:
        await ctx.clear_cookies()
    await _open_first_environment_check(mgr, identity, ctx)
    page = await ctx.new_page()
    await _focus(page)
    logged = False
    nickname = ""
    state_json = ""
    auth_verified = asyncio.Event()

    async def on_response(resp):
        if not any(api in resp.url for api in _CHANNELS_AUTH_APIS) or resp.status >= 400:
            return
        try:
            payload = await resp.json()
        except Exception:
            return
        if _channels_auth_response_ok(payload):
            auth_verified.set()

    page.on("response", on_response)
    try:
        await page.goto("https://channels.weixin.qq.com/login.html",
                        wait_until="domcontentloaded", timeout=30000)
        await _focus(page)
        waited = 0
        stable_cookie_hits = 0
        while waited < timeout_seconds:
            if page.is_closed():
                break
            try:
                cookies = await ctx.cookies()
            except Exception:
                break
            names = {c["name"] for c in cookies}
            # 登录成功:进入 /platform 后，auth/* 业务响应成功，或强登录 Cookie
            # 连续两轮仍存在。wxuin 单独出现只说明微信身份已写入，不代表助手已登录。
            on_platform = "channels.weixin.qq.com" in page.url \
                and "login.html" not in page.url
            ready = _channels_login_ready(names, auth_verified.is_set(), on_platform)
            if ready and auth_verified.is_set():
                logged = True
                break
            if ready:
                stable_cookie_hits += 1
                if stable_cookie_hits >= 2:
                    logged = True
                    break
            else:
                stable_cookie_hits = 0
            await asyncio.sleep(2)
            waited += 2
        if logged:
            await page.wait_for_timeout(2500)   # 等登录态/跳转写全
            # 落库前再确认页面没有回跳登录页，过滤授权过程中的瞬时假成功。
            names = {c["name"] for c in await ctx.cookies()}
            still_on_platform = "channels.weixin.qq.com" in page.url \
                and "login.html" not in page.url
            logged = _channels_login_ready(
                names, auth_verified.is_set(), still_on_platform)
            if logged:
                state_json = json.dumps(await ctx.storage_state())
    finally:
        try:
            await ctx.close()
        except Exception:
            pass
    return logged, state_json, nickname


async def interactive_channels_creator_login(mgr: BrowserManager, identity: Identity,
                                             timeout_seconds: int = 180,
                                             force_reauth: bool = False,
                                             ) -> Tuple[bool, str, str]:
    """视频号无独立创作登录 —— 助手即创作平台,直接复用普通登录。"""
    return await interactive_channels_login(
        mgr, identity, timeout_seconds, force_reauth=force_reauth)


async def _read_ks_nickname(page) -> str:
    for sel in ('.profile-user-name', '[class*="user-name"]', '[class*="userName"]',
                '.user-name', 'span.name'):
        try:
            t = await page.inner_text(sel, timeout=1500)
            if t and t.strip():
                return t.strip()[:40]
        except Exception:
            continue
    return ""


async def _read_xhs_nickname(page) -> str:
    for sel in ('.user .name', '.reds-avatar + * .name', 'span.name'):
        try:
            t = await page.inner_text(sel, timeout=1500)
            if t and t.strip():
                return t.strip()[:40]
        except Exception:
            continue
    return ""


async def _read_nickname(page) -> str:
    # 新版抖音会把登录用户写进 localStorage；优先读它，避免三个 DOM 选择器
    # 逐个超时（旧实现最坏会额外等待 4.5 秒）。
    try:
        nickname = await page.evaluate(
            """() => {
              for (const store of [window.localStorage, window.sessionStorage]) {
                for (const key of ['user_info', 'userInfo', 'user_info_passport']) {
                  try {
                    const raw = store.getItem(key);
                    if (!raw) continue;
                    let value = JSON.parse(raw);
                    if (typeof value === 'string') value = JSON.parse(value);
                    const name = value && (value.nickname || value.name);
                    if (name) return String(name).trim().slice(0, 40);
                  } catch (_) {}
                }
              }
              return '';
            }"""
        )
        if nickname and str(nickname).strip():
            return str(nickname).strip()[:40]
    except Exception:
        pass

    for sel in ('[data-e2e="user-info-nickname"]', 'span.nickname',
                '[data-e2e="live-avatar"] + * span'):
        try:
            t = await page.inner_text(sel, timeout=600)
            if t and t.strip():
                return t.strip()[:40]
        except Exception:
            continue
    return ""
