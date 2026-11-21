"""抖音、快手、TikTok、视频号、B站、Instagram 与 Twitter 交互式登录流程。"""
from __future__ import annotations

from typing import Tuple
import asyncio
import json

from app.browser.douyin_auth import douyin_login_prompt_visible
from app.browser.identity import Identity
from app.browser.login_common import _LOGIN_COOKIES, _focus, _open_first_environment_check, _read_ks_nickname, _read_nickname
from app.browser.manager import BrowserManager


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
            names = {c["name"] for c in cookies if c.get("value")}
            if names & _LOGIN_COOKIES:
                # Cookie 写入后再确认页面已离开登录态。扫码弹窗尚在时继续等，
                # 避免平台刚轮换游客/旧 Cookie 就提前结束登录流程。
                login_visible = await douyin_login_prompt_visible(page)
                if login_visible is False:
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


async def interactive_tiktok_login(
        mgr: BrowserManager, identity: Identity, timeout_seconds: int = 300,
        force_reauth: bool = False) -> Tuple[bool, str, str]:
    """TikTok Web visible login using the hydrated app context as authority."""
    ctx = await mgr.open_headed(identity)
    if force_reauth:
        await ctx.clear_cookies()
    await _open_first_environment_check(mgr, identity, ctx)
    page = await ctx.new_page()
    await _focus(page)
    logged, nickname, state_json = False, "", ""
    try:
        await page.goto("https://www.tiktok.com/", wait_until="domcontentloaded", timeout=30000)
        await _focus(page)
        for selector in ('[data-e2e="top-login-button"]',
                         'button[data-e2e="login-button"]',
                         'button:has-text("Log in")'):
            try:
                await page.locator(selector).first.click(timeout=2000)
                break
            except Exception:
                continue
        waited = 0.0
        while waited < timeout_seconds:
            if page.is_closed():
                break
            try:
                context = await page.evaluate("""() => {
                  const el = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
                  if (!el || !el.textContent) return null;
                  try { return JSON.parse(el.textContent).__DEFAULT_SCOPE__?.['webapp.app-context'] || null; }
                  catch (_) { return null; }
                }""")
            except Exception:
                context = None
            user = context.get("user") if isinstance(context, dict) else None
            if isinstance(user, dict) and (user.get("uniqueId") or user.get("uid")):
                logged = True
                try:
                    identity.observed_login_profile = dict(context)
                except Exception:
                    pass
                nickname = str(user.get("nickname") or user.get("uniqueId") or "")
                break
            await asyncio.sleep(0.5)
            waited += 0.5
        if logged:
            await page.wait_for_timeout(800)
            state_json = json.dumps(await ctx.storage_state())
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


_BILI_REQUIRED_COOKIES = {"SESSDATA", "bili_jct", "DedeUserID"}


async def interactive_bilibili_login(
    mgr: BrowserManager,
    identity: Identity,
    timeout_seconds: int = 240,
    force_reauth: bool = False,
) -> Tuple[bool, str, str]:
    """哔哩哔哩扫码/网页登录。主站、创作中心与私信共享 `.bilibili.com` 顶域登录态。

    判定登录：必须同时具备 `SESSDATA`、`bili_jct`、`DedeUserID` Cookie，且
    `api.bilibili.com/x/web-interface/nav` 实测返回 `code == 0` 且 `data.isLogin == true`。
    """
    ctx = await mgr.open_headed(identity)
    if force_reauth:
        await ctx.clear_cookies()
    await _open_first_environment_check(mgr, identity, ctx)
    page = await ctx.new_page()
    await _focus(page)
    logged, nickname, state_json = False, "", ""
    nav_user: dict = {}

    async def on_response(resp):
        try:
            if "/x/web-interface/nav" not in str(resp.url or ""):
                return
            payload = await resp.json()
            if not isinstance(payload, dict) or payload.get("code") != 0:
                return
            data = payload.get("data")
            if isinstance(data, dict) and data.get("isLogin"):
                nav_user.update(data)
        except Exception:
            return

    page.on("response", on_response)
    try:
        await page.goto(
            "https://passport.bilibili.com/login",
            wait_until="domcontentloaded",
            timeout=40000,
        )
        await _focus(page)
        waited = 0.0
        verified_cookie_sig = ""
        while waited < timeout_seconds:
            if page.is_closed():
                break
            try:
                cookies = await ctx.cookies()
            except Exception:
                break
            cmap = {c.get("name"): c.get("value") for c in cookies if c.get("name")}
            if all(cmap.get(k) for k in _BILI_REQUIRED_COOKIES):
                sig = f"{cmap.get('DedeUserID')}:{cmap.get('bili_jct')}"
                if not nav_user and sig != verified_cookie_sig:
                    verified_cookie_sig = sig
                    try:
                        probe = await page.evaluate(
                            """async () => {
                                try {
                                    const r = await fetch('https://api.bilibili.com/x/web-interface/nav?_t=' + Date.now(), {
                                        credentials: 'include'
                                    });
                                    return await r.json();
                                } catch (_) { return null; }
                            }"""
                        )
                        if (
                            isinstance(probe, dict)
                            and probe.get("code") == 0
                            and isinstance(probe.get("data"), dict)
                            and probe["data"].get("isLogin")
                        ):
                            nav_user.update(probe["data"])
                    except Exception:
                        pass
                if nav_user.get("isLogin"):
                    logged = True
                    nickname = str(nav_user.get("uname") or "").strip()[:40]
                    break
            await asyncio.sleep(1.0)
            waited += 1.0

        if logged:
            await page.wait_for_timeout(1200)
            state_json = json.dumps(await ctx.storage_state())
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass
        try:
            await ctx.close()
        except Exception:
            pass
    return logged, state_json, nickname


async def interactive_bilibili_creator_login(
    mgr: BrowserManager,
    identity: Identity,
    timeout_seconds: int = 240,
    force_reauth: bool = False,
) -> Tuple[bool, str, str]:
    """B 站创作中心 (`member.bilibili.com`) 复用 `.bilibili.com` 顶域登录态。"""
    return await interactive_bilibili_login(
        mgr, identity, timeout_seconds=timeout_seconds, force_reauth=force_reauth
    )


_IG_AUTH_BLOCKED_PATHS = ("/accounts/login", "/challenge/", "/checkpoint/", "/two_factor", "/suspended")


async def interactive_instagram_login(
    mgr: BrowserManager,
    identity: Identity,
    timeout_seconds: int = 240,
    force_reauth: bool = False,
) -> Tuple[bool, str, str]:
    """Instagram 网页端登录（在账号专属隔离 Profile 内完成密码 / 2FA / 安全挑战验证）。

    判定成功：同时具备 `sessionid` 与 `ds_user_id` Cookie，且当前 URL 已离开登录/挑战/2FA 页。
    随后提取保存的 `storage_state` 供移动端 `Bearer IGT:2:` 与 Web 会话双栈复用。
    """
    ctx = await mgr.open_headed(identity)
    if force_reauth:
        await ctx.clear_cookies()
    await _open_first_environment_check(mgr, identity, ctx)
    page = await ctx.new_page()
    await _focus(page)
    logged, nickname, state_json = False, "", ""

    try:
        await page.goto(
            "https://www.instagram.com/accounts/login/",
            wait_until="domcontentloaded",
            timeout=45000,
        )
        await _focus(page)
        waited = 0.0
        stable_hits = 0
        while waited < timeout_seconds:
            if page.is_closed():
                break
            try:
                cookies = await ctx.cookies(["https://www.instagram.com", "https://i.instagram.com"])
            except Exception:
                break
            cmap = {c.get("name"): c.get("value") for c in cookies if c.get("name")}
            cur_url = str(page.url or "").lower()
            on_blocked = any(p in cur_url for p in _IG_AUTH_BLOCKED_PATHS)
            if cmap.get("sessionid") and cmap.get("ds_user_id") and "instagram.com" in cur_url and not on_blocked:
                stable_hits += 1
                if stable_hits >= 2:
                    logged = True
                    break
            else:
                stable_hits = 0
            await asyncio.sleep(1.5)
            waited += 1.5

        if logged:
            await page.wait_for_timeout(1500)
            cur_url = str(page.url or "").lower()
            cookies = await ctx.cookies(["https://www.instagram.com", "https://i.instagram.com"])
            cmap = {c.get("name"): c.get("value") for c in cookies if c.get("name")}
            if cmap.get("sessionid") and not any(p in cur_url for p in _IG_AUTH_BLOCKED_PATHS):
                try:
                    probe = await page.evaluate(
                        """async () => {
                            try {
                                const r = await fetch('https://www.instagram.com/api/v1/accounts/edit/web_form_data/', {
                                    credentials: 'include',
                                    headers: {
                                        'X-IG-App-ID': '936619743392459',
                                        'X-Requested-With': 'XMLHttpRequest'
                                    }
                                });
                                if (!r.ok) return null;
                                return await r.json();
                            } catch (_) { return null; }
                        }"""
                    )
                    if isinstance(probe, dict) and isinstance(probe.get("form_data"), dict):
                        fd = probe["form_data"]
                        nickname = str(fd.get("first_name") or fd.get("username") or "").strip()[:40]
                except Exception:
                    pass
                state_json = json.dumps(await ctx.storage_state())
            else:
                logged = False
    finally:
        try:
            await ctx.close()
        except Exception:
            pass
    return logged, state_json, nickname


async def interactive_instagram_creator_login(
    mgr: BrowserManager,
    identity: Identity,
    timeout_seconds: int = 240,
    force_reauth: bool = False,
) -> Tuple[bool, str, str]:
    """Instagram 发布与主站共用 `.instagram.com` 会话。"""
    return await interactive_instagram_login(
        mgr, identity, timeout_seconds=timeout_seconds, force_reauth=force_reauth
    )


# ── X (Twitter) 登录 ──
_TWITTER_AUTH_BLOCKED_PATHS = (
    "/i/flow/login",
    "/login",
    "/account/access",
    "/account/suspended",
    "/suspended",
    "/disabled",
    "/i/flow/signup",
)


async def interactive_twitter_login(
    mgr: BrowserManager,
    identity: Identity,
    timeout_seconds: int = 300,
    force_reauth: bool = False,
) -> Tuple[bool, str, str]:
    """X (Twitter) 独立浏览器登录（支持用户名/密码、TOTP 二步验证与人工挑战解锁）。

    判定成功：`x.com` / `twitter.com` 存在非空 `auth_token` Cookie，且当前页面已离开登录流与 `/account/access` 锁定页。
    """
    ctx = await mgr.open_headed(identity)
    if force_reauth:
        await ctx.clear_cookies()
    await _open_first_environment_check(mgr, identity, ctx)
    page = await ctx.new_page()
    await _focus(page)
    logged, nickname, state_json = False, "", ""

    try:
        await page.goto(
            "https://x.com/i/flow/login",
            wait_until="domcontentloaded",
            timeout=45000,
        )
        await _focus(page)
        waited = 0.0
        stable_hits = 0
        while waited < timeout_seconds:
            if page.is_closed():
                break
            try:
                cookies = await ctx.cookies(["https://x.com", "https://twitter.com"])
            except Exception:
                break
            cmap = {c.get("name"): c.get("value") for c in cookies if c.get("name")}
            cur_url = str(page.url or "").lower()
            on_x = ("x.com" in cur_url) or ("twitter.com" in cur_url)
            on_blocked = any(p in cur_url for p in _TWITTER_AUTH_BLOCKED_PATHS)
            if cmap.get("auth_token") and on_x and not on_blocked:
                stable_hits += 1
                if stable_hits >= 2:
                    logged = True
                    break
            else:
                stable_hits = 0
            await asyncio.sleep(1.5)
            waited += 1.5

        if logged:
            await page.wait_for_timeout(1500)
            cur_url = str(page.url or "").lower()
            cookies = await ctx.cookies(["https://x.com", "https://twitter.com"])
            cmap = {c.get("name"): c.get("value") for c in cookies if c.get("name")}
            if cmap.get("auth_token") and not any(p in cur_url for p in _TWITTER_AUTH_BLOCKED_PATHS):
                try:
                    nav_info = await page.evaluate(
                        """() => {
                            const sw = document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"]');
                            if (sw) {
                                const lines = (sw.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
                                const name = lines.find(s => !s.startsWith('@')) || lines[0] || '';
                                if (name) return name;
                            }
                            const prof = document.querySelector('a[data-testid="AppTabBar_Profile_Link"]');
                            if (prof) {
                                const h = (prof.getAttribute('href') || '').replace(/^\\/+|\\/+$/g, '');
                                if (h && !h.includes('/')) return h;
                            }
                            return '';
                        }"""
                    )
                    if nav_info and str(nav_info).strip():
                        nickname = str(nav_info).strip()[:40]
                except Exception:
                    pass
                state_json = json.dumps(await ctx.storage_state())
            else:
                logged = False
    finally:
        try:
            await ctx.close()
        except Exception:
            pass
    return logged, state_json, nickname


async def interactive_twitter_creator_login(
    mgr: BrowserManager,
    identity: Identity,
    timeout_seconds: int = 300,
    force_reauth: bool = False,
) -> Tuple[bool, str, str]:
    """X (Twitter) 发布与主站共用 `.x.com` 会话。"""
    return await interactive_twitter_login(
        mgr, identity, timeout_seconds=timeout_seconds, force_reauth=force_reauth
    )
