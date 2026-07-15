"""交互式扫码登录:打开真实浏览器窗口,用户扫码,落地登录态。
对应原项目 internal/douyin.QRLoginManager(chromedp 版)。
"""
from __future__ import annotations

import asyncio
import json
from typing import Optional, Tuple

from .identity import Identity
from .manager import BrowserManager

# 登录后才会出现的 Cookie(用于判断是否已登录)
_LOGIN_COOKIES = {"sessionid", "sessionid_ss", "sid_tt", "uid_tt", "sid_guard"}


async def _focus(page):
    """把扫码窗口提到前台(否则有头浏览器常开在其它窗口后面)。"""
    try:
        await page.bring_to_front()
    except Exception:
        pass


async def interactive_login(mgr: BrowserManager, identity: Identity,
                            timeout_seconds: int = 180,
                            start_url: str = "https://www.douyin.com/"
                            ) -> Tuple[bool, str, str]:
    """返回 (是否成功, storage_state_json, nickname)。
    在账号专属持久 profile(独立 UA/视口/时区/代理/指纹)里有头扫码,
    登录态直接落盘到该 profile;同时返回 storage_state 供库内展示/兜底。
    start_url 用 creator.douyin.com 即为创作中心登录(其登录态因 .douyin.com 共享 Cookie,
    同样可用于 www 公开抓取)。"""
    ctx = await mgr.open_headed(identity)
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
        waited = 0
        while waited < timeout_seconds:
            if page.is_closed():
                break
            try:
                cookies = await ctx.cookies()
            except Exception:
                break
            names = {c["name"] for c in cookies}
            if names & _LOGIN_COOKIES:
                logged = True
                break
            await asyncio.sleep(2)
            waited += 2

        if logged:
            await page.wait_for_timeout(1500)   # 等登录态写全
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
                                    timeout_seconds: int = 180):
    """创作中心登录。返回 (ok, storage_state_json, nickname)。"""
    return await interactive_login(mgr, identity, timeout_seconds,
                                   start_url="https://creator.douyin.com/")


# 创作服务平台登录后才会写入的 Cookie(发布需要)
_XHS_CREATOR_COOKIES = {"customerClientId", "galaxy_creator_session_id",
                        "access-token-creator.xiaohongshu.com", "customer-sso-sid"}


async def _xhs_web_session(ctx) -> str:
    """取当前 web_session cookie 值(未登录时为空串/短值)。"""
    try:
        for c in await ctx.cookies():
            if c["name"] == "web_session":
                return c.get("value", "") or ""
    except Exception:
        pass
    return ""


async def interactive_xhs_login(mgr: BrowserManager, identity: Identity,
                                timeout_seconds: int = 180
                                ) -> Tuple[bool, str, str]:
    """小红书扫码登录。打开真实窗口让用户扫码,落地登录态。
    返回 (是否成功, storage_state_json, nickname)。

    判定登录:小红书游客态 web_session 为空/短值,登录成功后才会变成一长串 token。
    所以必须等到 web_session「变成有效长串且不同于登录前的初始值」才算成功 ——
    仅凭 customerClientId / customer-sso-sid 之类的 Cookie 判断会误判(它们在登录弹窗
    一出现就被写入,根本没扫码)。用户中途关掉窗口则视为未登录。"""
    ctx = await mgr.open_headed(identity)
    page = await ctx.new_page()
    await _focus(page)
    logged = False
    nickname = ""
    state_json = ""
    try:
        await page.goto("https://www.xiaohongshu.com/explore",
                        wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(1200)
        init_ws = await _xhs_web_session(ctx)     # 登录前的基准值(通常为空)
        waited = 0
        while waited < timeout_seconds:
            ws = await _xhs_web_session(ctx)
            # 真正登录后 web_session 才会变成有效长串,且不同于登录前。
            # 一旦判定登录,立刻抓 storage_state 落袋为安 —— 即使用户随后秒关窗口,
            # 后续步骤报错也不会把已到手的登录态丢掉。
            if ws and len(ws) >= 20 and ws != init_ws and "passport" not in page.url:
                try:
                    state = await ctx.storage_state()
                    state_json = json.dumps(state)
                    logged = True
                except Exception:
                    pass
                if logged:
                    try:
                        nickname = await _read_xhs_nickname(page)
                    except Exception:
                        pass
                    # 顺带授权创作平台(发布需要):跳过去后轮询等创作 cookie 出现,
                    # 给用户时间在同一窗口里完成创作平台登录/同意;拿到或超时才收尾。
                    try:
                        await page.goto("https://creator.xiaohongshu.com",
                                        wait_until="domcontentloaded", timeout=20000)
                        cwaited = 0
                        while cwaited < 90:
                            if page.is_closed():
                                break
                            st = await ctx.storage_state()
                            if any(c["name"] in _XHS_CREATOR_COOKIES
                                   for c in st.get("cookies", [])):
                                state_json = json.dumps(st)   # 含读取态+创作态
                                break
                            await asyncio.sleep(2)
                            cwaited += 2
                    except Exception:
                        pass
                    break
            if page.is_closed():                  # 没登录就关了窗口 -> 视为未登录
                break
            await asyncio.sleep(1)
            waited += 1
    finally:
        try:
            await ctx.close()
        except Exception:
            pass
    return logged, state_json, nickname


async def interactive_xhs_creator_login(mgr: BrowserManager, identity: Identity,
                                        timeout_seconds: int = 180
                                        ) -> Tuple[bool, str, str]:
    """小红书「创作服务平台」登录(发布/已发布列表用)。打开 creator.xiaohongshu.com/login
    扫码,落地含创作者会话的登录态。返回 (是否成功, storage_state_json, nickname)。
    与普通登录区分:这里登录的是创作平台,登录态里含 customerClientId / galaxy_creator_session_id 等。"""
    ctx = await mgr.open_headed(identity)
    page = await ctx.new_page()
    await _focus(page)
    logged = False
    nickname = ""
    state_json = ""
    try:
        await page.goto("https://creator.xiaohongshu.com/login",
                        wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(1200)
        init_ws = await _xhs_web_session(ctx)
        waited = 0
        while waited < timeout_seconds:
            cookies = await ctx.cookies()
            names = {c["name"] for c in cookies}
            ws = next((c.get("value", "") for c in cookies if c["name"] == "web_session"), "")
            # 创作平台登录成功:出现创作者专属 Cookie,或离开 /login 且 web_session 变为有效
            on_creator = "creator.xiaohongshu.com" in page.url and "/login" not in page.url
            if (names & _XHS_CREATOR_COOKIES) or \
                    (on_creator and ws and len(ws) >= 20 and ws != init_ws):
                try:
                    await page.wait_for_timeout(1500)
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
            if page.is_closed():
                break
            await asyncio.sleep(1)
            waited += 1
    finally:
        try:
            await ctx.close()
        except Exception:
            pass
    return logged, state_json, nickname


# ── 快手登录 ──
# 登录判定:出现 passToken 即视为已登录(实测的权威信号);
# userId / web_st 作为附加信号兜底。
_KS_LOGIN_COOKIES = {"passToken", "userId", "kuaishou.server.web_st"}
# 创作平台(cp.kuaishou.com)登录后才会写入的 Cookie(发布需要)
_KS_CREATOR_COOKIES = {"kuaishou.web.cp.api_st", "kuaishou.web.cp.api_ph"}


async def interactive_ks_login(mgr: BrowserManager, identity: Identity,
                               timeout_seconds: int = 180,
                               start_url: str = "https://www.kuaishou.com/"
                               ) -> Tuple[bool, str, str]:
    """快手扫码登录。打开真实窗口让用户扫码,落地登录态。
    返回 (是否成功, storage_state_json, nickname)。
    判定登录:出现 userId + web_st/passToken(游客态没有)。用户中途关窗视为未登录。
    ⚠️ 选择器/登录态 Cookie 名随快手改版可能变化,集中在 _KS_LOGIN_COOKIES。"""
    ctx = await mgr.open_headed(identity)
    page = await ctx.new_page()
    await _focus(page)
    logged = False
    nickname = ""
    state_json = ""
    try:
        await page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
        await _focus(page)
        # 尝试自动弹出登录框(失败也没关系,用户可自行点“登录”)
        for sel in ('text=登录', 'button:has-text("登录")', '[class*="login"]'):
            try:
                await page.click(sel, timeout=2500)
                break
            except Exception:
                continue
        waited = 0
        while waited < timeout_seconds:
            if page.is_closed():
                break
            try:
                cookies = await ctx.cookies()
            except Exception:
                break
            names = {c["name"] for c in cookies}
            # passToken 是登录成功的权威信号(游客态没有)
            if "passToken" in names:
                logged = True
                break
            await asyncio.sleep(2)
            waited += 2
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
                                       timeout_seconds: int = 180
                                       ) -> Tuple[bool, str, str]:
    """快手「创作者服务平台」登录(cp.kuaishou.com,发布用)。扫码后落地含创作者会话的登录态。
    返回 (是否成功, storage_state_json, nickname)。登录成功标志:出现 cp.api_st/api_ph。"""
    ctx = await mgr.open_headed(identity)
    page = await ctx.new_page()
    await _focus(page)
    logged = False
    nickname = ""
    state_json = ""
    try:
        await page.goto("https://cp.kuaishou.com/", wait_until="domcontentloaded",
                        timeout=30000)
        await _focus(page)
        for sel in ('text=登录', 'button:has-text("登录")', '[class*="login"]'):
            try:
                await page.click(sel, timeout=2500)
                break
            except Exception:
                continue
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
            if (names & _KS_CREATOR_COOKIES) or (on_cp and "userId" in names):
                await page.wait_for_timeout(1500)
                try:
                    state_json = json.dumps(await ctx.storage_state())
                    logged = True
                    nickname = await _read_ks_nickname(page)
                except Exception:
                    pass
                if logged:
                    break
            await asyncio.sleep(2)
            waited += 2
    finally:
        try:
            await ctx.close()
        except Exception:
            pass
    return logged, state_json, nickname


# ── 视频号(微信视频号 / finder)登录 ──
# 视频号助手登录后写入的 Cookie(挂在 channels.weixin.qq.com / .weixin.qq.com)。
# sessionid 是权威信号(游客态没有);_finder_auth / wxuin 兜底。
# ⚠️ Cookie 名以真实抓包为准,变化时改这里。
_CHANNELS_LOGIN_COOKIES = {"sessionid", "_finder_auth", "wxuin"}


async def interactive_channels_login(mgr: BrowserManager, identity: Identity,
                                     timeout_seconds: int = 180
                                     ) -> Tuple[bool, str, str]:
    """视频号扫码登录。打开视频号助手登录页让用户用微信扫码,落地登录态。
    返回 (是否成功, storage_state_json, nickname)。
    视频号只有一套登录态(助手即创作平台),读取/发布共用,故无独立创作登录。
    昵称登录页不易读,返回空串,交由「资料刷新」用 fetch_channels_self_profile 补全。
    ⚠️ 登录判定 Cookie 名随视频号改版可能变化,集中在 _CHANNELS_LOGIN_COOKIES。"""
    ctx = await mgr.open_headed(identity)
    page = await ctx.new_page()
    await _focus(page)
    logged = False
    nickname = ""
    state_json = ""
    try:
        await page.goto("https://channels.weixin.qq.com/login.html",
                        wait_until="domcontentloaded", timeout=30000)
        await _focus(page)
        waited = 0
        while waited < timeout_seconds:
            if page.is_closed():
                break
            try:
                cookies = await ctx.cookies()
            except Exception:
                break
            names = {c["name"] for c in cookies}
            # 登录成功:出现 sessionid 且已跳离 login.html(进入 /platform)
            on_platform = "channels.weixin.qq.com" in page.url \
                and "login.html" not in page.url
            if (names & _CHANNELS_LOGIN_COOKIES) and on_platform:
                logged = True
                break
            await asyncio.sleep(2)
            waited += 2
        if logged:
            await page.wait_for_timeout(2000)   # 等登录态/跳转写全
            state_json = json.dumps(await ctx.storage_state())
    finally:
        try:
            await ctx.close()
        except Exception:
            pass
    return logged, state_json, nickname


async def interactive_channels_creator_login(mgr: BrowserManager, identity: Identity,
                                             timeout_seconds: int = 180
                                             ) -> Tuple[bool, str, str]:
    """视频号无独立创作登录 —— 助手即创作平台,直接复用普通登录。"""
    return await interactive_channels_login(mgr, identity, timeout_seconds)


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
    for sel in ('[data-e2e="user-info-nickname"]', 'span.nickname',
                '[data-e2e="live-avatar"] + * span'):
        try:
            t = await page.inner_text(sel, timeout=1500)
            if t and t.strip():
                return t.strip()[:40]
        except Exception:
            continue
    return ""
