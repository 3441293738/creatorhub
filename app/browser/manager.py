"""Playwright 浏览器管理器(多账号隔离版)。

每个账号一套**独立持久化 context**(launch_persistent_context):
  - 独立 user-data-dir(cookie/localStorage 天然隔离)
  - 独立代理 / UA / 视口 / 时区 / 指纹
常驻这些 context 并按 LRU 控制同时存活数量(省内存)。
登录/发布用同一 profile 的**有头** context(headless=False)。
对应原项目用 chromedp 的角色。
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, Dict, List, Optional
from playwright.async_api import BrowserContext, async_playwright

from ..windowing import (CHROMIUM_WINDOW_CLASSES, bring_window_to_front,
                         capture_window_snapshot)
from .identity import Identity, fingerprint_script
from .cdp import (CdpLaunchError, CdpProfileConflictError, CdpProxyError,
                  CdpProxyAuthController, XhsCdpBackend)
from .proxy import ProxyConfigError, ProxyPlan, try_proxy_plan
from .xhs_interaction import XhsInteractionPolicy, XhsVisibleActionGate

_PROXY_WEBRTC_ARGS = [
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
]

# 存量 legacy 账号必须保持原来的启动参数，避免已建立的浏览器画像突然漂移。
_LEGACY_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-infobars",
    # 关键:禁止 WebRTC 走非代理 UDP。否则真实 Chromium 会通过 STUN 直接暴露宿主
    # 公网/内网 IP,绕过我们在 HTTP 层设的账号代理 —— 所有号在 WebRTC 上露同一真实
    # 出口 IP,一号一代理的防关联就白做了。这个 flag 让 WebRTC 只认代理路径。
    *_PROXY_WEBRTC_ARGS,
]

# storage_state 里允许注入的 Cookie 字段(playwright add_cookies 接受的键)
_COOKIE_KEYS = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")


def _parse_proxy(s: str) -> Optional[Dict[str, str]]:
    """把 http://user:pass@host:port / socks5://host:port 解析成 Playwright proxy 配置。"""
    plan = try_proxy_plan(s)
    return plan.playwright() if plan else None


def normalize_proxy(s: str) -> str:
    """把用户输入规范成带协议头的代理 URL(httpx 必须带 scheme)。
    裸 host:port -> http://host:port;保留账号密码;无法解析则原样返回。
    例:'1.2.3.4:8080' -> 'http://1.2.3.4:8080'。"""
    s = (s or "").strip()
    plan = try_proxy_plan(s)
    return plan.normalized if plan else s


def _sanitize_cookies(cookies: List[dict]) -> List[dict]:
    out = []
    for c in cookies:
        if not c.get("name"):
            continue
        ck = {k: c[k] for k in _COOKIE_KEYS if k in c}
        if ck.get("sameSite") not in ("Strict", "Lax", "None"):
            ck.pop("sameSite", None)
        out.append(ck)
    return out


def _cookie_key(cookie: dict) -> tuple[str, str, str]:
    """Cookie identity used when merging DB storage_state into a profile."""
    return (
        str(cookie.get("name") or ""),
        str(cookie.get("domain") or "").lstrip(".").lower(),
        str(cookie.get("path") or "/"),
    )


def _bridge_cookies(states: tuple, existing: List[dict] | None = None) -> List[dict]:
    """Return usable storage_state cookies missing from the live profile.

    Chromium removes session cookies when a persistent context is closed.  A
    profile can therefore be non-empty while an auth cookie captured in
    ``storage_state`` is already absent.  This is common for Channels'
    ``_finder_auth``/``sessionid`` hand-off from the headed login context to
    the background context.

    Existing profile cookies win so an older DB snapshot never overwrites a
    cookie Chromium has refreshed since the last snapshot.
    """
    existing_keys = {_cookie_key(c) for c in (existing or [])}
    candidates: Dict[tuple[str, str, str], dict] = {}
    now = time.time()
    for raw_state in states or ():
        try:
            cookies = json.loads(raw_state or "{}").get("cookies") or []
        except Exception:
            continue
        for cookie in _sanitize_cookies(cookies):
            key = _cookie_key(cookie)
            if not key[0] or key in existing_keys:
                continue
            expires = cookie.get("expires")
            try:
                if expires is not None and float(expires) > 0 and float(expires) <= now:
                    continue
            except (TypeError, ValueError):
                pass
            candidates[key] = cookie
    return list(candidates.values())


class BrowserManager:
    def __init__(self, default_ua: str, profiles_root: str = "./data/profiles",
                 max_live: int = 6, native_ua_callback=None,
                 xhs_browser_mode: str = "auto",
                 xhs_cdp_idle_seconds: int = 900):
        self.default_ua = default_ua
        self.profiles_root = profiles_root
        self.max_live = max(1, max_live)
        self._native_ua_callback = native_ua_callback
        self.xhs_browser_mode = (
            xhs_browser_mode if xhs_browser_mode in {"auto", "cdp", "playwright"}
            else "auto"
        )
        self.xhs_cdp_idle_seconds = max(0, int(xhs_cdp_idle_seconds))
        self._pw = None
        self._contexts: Dict[Any, BrowserContext] = {}   # key -> 持久化 context
        self._cdp_sessions: Dict[Any, Any] = {}
        self._backend_by_key: Dict[Any, str] = {}
        self._fallback_reason_by_key: Dict[Any, str] = {}
        self._proxy_signature_by_key: Dict[Any, str] = {}
        self._cdp_backend = None
        self.xhs_interaction = XhsInteractionPolicy()
        self._xhs_visible_gate = XhsVisibleActionGate()
        self._xhs_page_locks: Dict[Any, asyncio.Lock] = {}
        self._last_used: Dict[Any, float] = {}
        self._locks: Dict[Any, asyncio.Lock] = {}
        self._cv_lock = asyncio.Lock()                   # 保护 context 字典的创建/驱逐
        self._chrome_major: Optional[int] = None         # 实际 Chromium 大版本(启动时探测)
        # 优先使用机器上安装的稳定版 Chrome；没有时回退到 Playwright
        # 附带的 Chrome for Testing。登录窗口因此更接近用户日常浏览器环境。
        self._browser_channel: Optional[str] = None

    async def start(self):
        self._pw = await async_playwright().start()
        self._chrome_major = await self._detect_chrome_major()
        self._cdp_backend = XhsCdpBackend(
            self._pw, self.profiles_root)

    async def _detect_chrome_major(self) -> Optional[int]:
        """选择可用浏览器并探测其 Chromium 大版本。

        优先系统稳定版 Chrome，缺失时使用 Playwright bundled Chromium。
        账号 UA 池写死了 Chrome 版本,但真实内核可能是另一版本 —— 二者不一致时,
        Sec-CH-UA 请求头 / navigator.userAgentData 由真实内核发出,会和 UA 字符串对不上,
        成为自动化特征。这里读一次真实 UA,后续把账号 UA 的版本号归一到它。"""
        for channel in ("chrome", None):
            launch_kwargs: Dict[str, Any] = {
                "headless": True,
            }
            if channel:
                launch_kwargs["channel"] = channel
            try:
                browser = await self._pw.chromium.launch(**launch_kwargs)
            except Exception:
                continue
            self._browser_channel = channel
            try:
                pg = await browser.new_page()
                ua = await pg.evaluate("navigator.userAgent")
                m = re.search(r"Chrome/(\d+)", ua or "")
                return int(m.group(1)) if m else None
            except Exception:
                return None
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass
        self._browser_channel = None
        return None

    def _normalize_ua(self, ua: str) -> str:
        """把账号 UA 的 Chrome/Edg 大版本对齐到真实内核版本(未探测到则原样返回)。"""
        if not self._chrome_major or not ua:
            return ua
        v = self._chrome_major
        ua = re.sub(r"Chrome/\d+", f"Chrome/{v}", ua)
        ua = re.sub(r"Edg/\d+", f"Edg/{v}", ua)
        return ua

    def environment_snapshot(self, identity: Identity, *, headless: bool) -> Dict[str, Any]:
        """返回不含代理凭据的浏览器环境诊断信息。"""
        backend = self._backend_by_key.get(identity.key)
        if backend is None:
            backend = (
                "cdp" if self._uses_xhs_cdp(identity)
                else "playwright"
            )
        is_cdp = backend == "cdp"
        fallback_reason = self._fallback_reason_by_key.get(identity.key, "")
        # Diagnostics are user-facing, never a transport for debugger URLs or
        # proxy credentials.
        fallback_reason = re.sub(
            r"\b(?:ws|wss)://\S+", "[CDP endpoint]", fallback_reason,
            flags=re.IGNORECASE)
        fallback_reason = re.sub(
            r"\b127\.0\.0\.1:\d+(?:/\S*)?", "[loopback endpoint]",
            fallback_reason)
        fallback_reason = re.sub(
            r"(https?|socks5)://[^/@\s]+@", r"\1://***@",
            fallback_reason, flags=re.IGNORECASE)
        fallback = bool(fallback_reason)
        backend_label = (
            "系统 Chrome · CDP" if is_cdp
            else "Playwright Chromium · 回退" if fallback
            else "Playwright Chromium"
        )
        return {
            "browser": "chrome" if is_cdp else (self._browser_channel or "chromium"),
            "chrome_major": self._chrome_major,
            "headless": False if is_cdp else bool(headless),
            "identity_mode": identity.identity_mode,
            "profile_dir": identity.profile_dir,
            "has_proxy": bool(str(identity.proxy or "").strip()),
            "backend": backend,
            "backend_label": backend_label,
            "fallback": fallback,
            "fallback_reason": fallback_reason,
        }

    def _sec_ch_ua_headers(self, ua: str) -> Optional[Dict[str, str]]:
        """按归一后的 UA 生成一致的 Client Hints 头,覆盖真实内核默认发出的值。"""
        v = self._chrome_major
        if not v:
            return None
        if "Edg/" in ua:
            brands = (f'"Chromium";v="{v}", "Microsoft Edge";v="{v}", '
                      f'"Not?A_Brand";v="99"')
        else:
            brands = (f'"Chromium";v="{v}", "Google Chrome";v="{v}", '
                      f'"Not?A_Brand";v="99"')
        platform = ('"macOS"' if "Mac OS" in ua
                    else '"Linux"' if "Linux" in ua and "Android" not in ua
                    else '"Windows"')
        return {"sec-ch-ua": brands, "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": platform}

    async def stop(self):
        async with self._cv_lock:
            for key in list(self._contexts):
                await self._close_key_unlocked(key)
        if self._pw:
            await self._pw.stop()
        self._pw = None

    # ── 画像 ──
    def identity_for(self, acc) -> Identity:
        return Identity.from_account(acc, self.profiles_root, self.default_ua)

    def anon_identity(self) -> Identity:
        return Identity(account_id=None,
                        profile_dir=str(Path(self.profiles_root) / "_anon"),
                        ua=self.default_ua)

    def _uses_xhs_cdp(self, identity: Identity) -> bool:
        return (
            identity.platform == "xhs"
            and self.xhs_browser_mode in {"auto", "cdp"}
        )

    @staticmethod
    def _xhs_proxy_plan(identity: Identity) -> ProxyPlan | None:
        try:
            return ProxyPlan.parse(identity.proxy)
        except ProxyConfigError as exc:
            raise CdpProxyError(str(exc)) from None

    def lock_for(self, key) -> asyncio.Lock:
        """每账号串行锁:同一账号同一时刻只允许一个浏览器动作。"""
        return self._locks.setdefault(key, asyncio.Lock())

    # ── 持久化 context ──
    async def _launch_persistent(self, identity: Identity, headless: bool = True
                                 ) -> BrowserContext:
        pdir = Path(identity.profile_dir)
        pdir.mkdir(parents=True, exist_ok=True)
        was_empty = not any(pdir.iterdir())
        ua = self._normalize_ua(identity.ua or self.default_ua)
        kwargs: Dict[str, Any] = dict(
            user_data_dir=str(pdir), headless=headless,
        )
        if self._browser_channel:
            kwargs["channel"] = self._browser_channel
        legacy = identity.identity_mode != "native"
        if legacy:
            kwargs.update(
                args=list(_LEGACY_ARGS),
                viewport={"width": identity.viewport_w, "height": identity.viewport_h},
                locale=identity.locale or "zh-CN",
                timezone_id=identity.timezone_id or "Asia/Shanghai",
                user_agent=ua,
                # 存量账号保持既有画像，避免已建立的 profile 突然漂移。
                geolocation=identity.geolocation,
                permissions=["geolocation"],
            )
        proxy = _parse_proxy(identity.proxy)
        if not legacy:
            # native 账号使用 Chrome/操作系统自己的视口、语言、时区与硬件画像。
            # 仅在显式配置代理时约束 WebRTC，避免 UDP 绕过代理出口。
            kwargs["no_viewport"] = True
            if proxy:
                kwargs["args"] = list(_PROXY_WEBRTC_ARGS)
        if proxy:
            kwargs["proxy"] = proxy
        ctx = await self._pw.chromium.launch_persistent_context(**kwargs)
        if not legacy:
            # Persist the UA exposed by this exact native context. It is
            # diagnostic state only; native launches still omit any override.
            probe_page = None
            created_probe = False
            try:
                pages = list(ctx.pages)
                if pages:
                    probe_page = pages[0]
                else:
                    probe_page = await ctx.new_page()
                    created_probe = True
                actual_ua = await probe_page.evaluate("navigator.userAgent")
                if actual_ua:
                    identity.ua = str(actual_ua)
                    if identity.account_id is not None and self._native_ua_callback:
                        self._native_ua_callback(identity.account_id, identity.ua)
            except Exception:
                pass
            finally:
                if created_probe and probe_page is not None:
                    try:
                        await probe_page.close()
                    except Exception:
                        pass
        # Client Hints 与归一后的 UA 保持一致(否则内核按真实版本发 Sec-CH-UA,和 UA 打架)
        sec = self._sec_ch_ua_headers(ua) if legacy else None
        if sec:
            try:
                await ctx.set_extra_http_headers(sec)
            except Exception:
                pass
        if legacy and identity.fp_seed:
            try:
                await ctx.add_init_script(fingerprint_script(identity.fp_seed, ua))
            except Exception:
                pass
        # 登录态桥接:
        # 1) 全新 profile 注入 DB storage_state，兼容旧账号迁移；
        # 2) 非空 profile 也补回“磁盘中缺失”的 Cookie。Chromium 关闭 context
        #    时会丢弃 session cookie，视频号刚扫码成功后从有头切到无头 context
        #    正好会经过这条路径。只补缺失项，不覆盖 profile 中更新过的 Cookie。
        await self._bridge_identity_cookies(
            ctx, identity, assume_empty=was_empty)
        return ctx

    @staticmethod
    async def _bridge_identity_cookies(
            ctx: BrowserContext, identity: Identity,
            *, assume_empty: bool = False) -> None:
        if not identity.bridge_states:
            return
        try:
            existing = [] if assume_empty else await ctx.cookies()
            cookies = _bridge_cookies(identity.bridge_states, existing)
            if cookies:
                await ctx.add_cookies(cookies)
        except Exception:
            pass

    async def _evict_if_needed(self):
        """常驻 context 超过上限时,关掉最久未用且当前未被锁占用的那个。"""
        while len(self._contexts) >= self.max_live:
            cands = [k for k in self._contexts if not self._key_locked(k)]
            if not cands:
                break
            victim = min(cands, key=lambda k: self._last_used.get(k, 0))
            await self._close_key_unlocked(victim)

    def _key_locked(self, key: Any) -> bool:
        candidates = (key, f"acc:{key}") if isinstance(key, int) else (key,)
        if any(
            candidate in self._locks and self._locks[candidate].locked()
            for candidate in candidates
        ):
            return True
        page_lock = self._xhs_page_locks.get(key)
        if page_lock is not None and page_lock.locked():
            return True
        return self._xhs_visible_gate.active_account == key

    @staticmethod
    def _cdp_session_healthy(session: Any) -> bool:
        checker = getattr(getattr(session, "browser", None), "is_connected", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                return False
        return True

    async def _close_key_unlocked(self, key: Any) -> None:
        ctx = self._contexts.pop(key, None)
        session = self._cdp_sessions.pop(key, None)
        self._last_used.pop(key, None)
        self._backend_by_key.pop(key, None)
        self._fallback_reason_by_key.pop(key, None)
        self._proxy_signature_by_key.pop(key, None)
        if session is not None and self._cdp_backend is not None:
            with suppress(Exception):
                await self._cdp_backend.close(session)
            return
        if ctx is not None:
            with suppress(Exception):
                await ctx.close()

    async def context_for(self, identity: Identity) -> BrowserContext:
        """取(或惰性创建)账号专属常驻 context。"""
        key = identity.key
        async with self._cv_lock:
            plan = (
                self._xhs_proxy_plan(identity)
                if identity.platform == "xhs" else None
            )
            signature = plan.signature if plan else "direct"
            ctx = self._contexts.get(key)
            session = self._cdp_sessions.get(key)
            if ctx is not None and (
                    self._proxy_signature_by_key.get(key) != signature):
                await self._close_key_unlocked(key)
                ctx = None
                session = None
            elif session is not None and not self._cdp_session_healthy(session):
                await self._close_key_unlocked(key)
                ctx = None
                session = None
            if ctx is None:
                await self._evict_if_needed()
                if self._uses_xhs_cdp(identity):
                    if self._cdp_backend is None:
                        if self._pw is None:
                            raise CdpLaunchError("浏览器管理器尚未启动")
                        self._cdp_backend = XhsCdpBackend(
                            self._pw, self.profiles_root)
                    try:
                        session = await self._cdp_backend.open(identity, plan)
                    except CdpProfileConflictError:
                        raise
                    except CdpLaunchError as exc:
                        if self.xhs_browser_mode == "cdp":
                            raise
                        self._fallback_reason_by_key[key] = str(exc)[:200]
                        ctx = await self._launch_persistent(
                            identity, headless=False)
                        self._backend_by_key[key] = "playwright"
                    else:
                        ctx = session.context
                        if plan is not None and plan.scheme in {"http", "https"} \
                                and plan.authenticated:
                            session.auth_controller = CdpProxyAuthController(
                                ctx, plan)
                        await self._bridge_identity_cookies(ctx, identity)
                        self._cdp_sessions[key] = session
                        self._backend_by_key[key] = "cdp"
                else:
                    ctx = await self._launch_persistent(
                        identity, headless=(identity.platform != "xhs"))
                    self._backend_by_key[key] = "playwright"
                self._contexts[key] = ctx
                self._proxy_signature_by_key[key] = signature
            self._last_used[key] = time.time()
            if key in self._cdp_sessions:
                self._cdp_sessions[key].last_used = self._last_used[key]
            return ctx

    async def new_page(self, identity: Identity, block_media: bool = False):
        """从账号常驻 context 开一个新 page(可屏蔽图片/视频/字体)。用完请 page.close()。"""
        ctx = await self.context_for(identity)
        page = await ctx.new_page()
        session = self._cdp_sessions.get(identity.key)
        controller = getattr(session, "auth_controller", None)
        if controller is not None:
            await controller.install(page)
        if block_media and identity.platform != "xhs":
            async def _route(route):
                if route.request.resource_type in ("image", "media", "font"):
                    await route.abort()
                else:
                    await route.continue_()
            await page.route("**/*", _route)
        return page

    @asynccontextmanager
    async def visible_action(self, identity: Identity):
        """Serialize one account's page work and all visible XHS actions."""
        if self._xhs_visible_gate.owned_by_current_task:
            async with self._xhs_visible_gate.acquire(identity.key):
                yield
            return
        page_lock = self._xhs_page_locks.setdefault(
            identity.key, asyncio.Lock())
        async with page_lock:
            async with self._xhs_visible_gate.acquire(identity.key):
                yield

    @asynccontextmanager
    async def visible_page(self, identity: Identity, *, url: str = ""):
        """Lease one foreground XHS task page without closing shared Chrome."""
        async with self.visible_action(identity):
            snapshot = capture_window_snapshot(CHROMIUM_WINDOW_CLASSES)
            page = None
            try:
                page = await self.new_page(identity, block_media=False)
                if url:
                    await page.goto(
                        url, wait_until="domcontentloaded", timeout=30_000)
                with suppress(Exception):
                    await page.bring_to_front()
                title = ""
                with suppress(Exception):
                    title = await page.title()
                await asyncio.to_thread(
                    bring_window_to_front, snapshot,
                    CHROMIUM_WINDOW_CLASSES, title or "小红书", 1.5)
                yield page
            finally:
                if page is not None:
                    with suppress(Exception):
                        await page.close()

    async def close_context(self, key):
        async with self._cv_lock:
            await self._close_key_unlocked(key)

    async def collect_idle_cdp(self, now: float | None = None) -> int:
        if self.xhs_cdp_idle_seconds <= 0:
            return 0
        sampled = time.time() if now is None else float(now)
        async with self._cv_lock:
            victims = [
                key for key in self._cdp_sessions
                if not self._key_locked(key)
                and sampled - self._last_used.get(key, sampled)
                >= self.xhs_cdp_idle_seconds
            ]
            for key in victims:
                await self._close_key_unlocked(key)
            return len(victims)

    async def open_headed(self, identity: Identity) -> BrowserContext:
        """Return the shared headed XHS context or a temporary legacy one."""
        if identity.platform == "xhs":
            snapshot = capture_window_snapshot(CHROMIUM_WINDOW_CLASSES)
            ctx = await self.context_for(identity)
            await asyncio.to_thread(bring_window_to_front, snapshot,
                                    CHROMIUM_WINDOW_CLASSES, "", 1.5)
            return ctx
        snapshot = capture_window_snapshot(CHROMIUM_WINDOW_CLASSES)
        await self.close_context(identity.key)
        ctx = await self._launch_persistent(identity, headless=False)
        await asyncio.to_thread(bring_window_to_front, snapshot,
                                CHROMIUM_WINDOW_CLASSES, "", 1.5)
        return ctx


# 各平台 Cookie 顶域(子域如 creator./edith. 都吃顶域 cookie,一个就够)
_COOKIE_DOMAIN = {
    "douyin": ".douyin.com",
    "xhs": ".xiaohongshu.com",
    "kuaishou": ".kuaishou.com",
    "shipinhao": ".weixin.qq.com",   # 视频号:finder 登录态(_finder_auth/sessionid)挂在 .weixin.qq.com
}


def cookie_string_to_state(cookie_str: str, platform: str = "douyin") -> str:
    """把粘贴的 Cookie 串转成 Playwright storage_state JSON(兜底登录用)。"""
    domain = _COOKIE_DOMAIN.get(platform, ".douyin.com")
    cookies: List[Dict[str, Any]] = []
    for part in cookie_str.strip().split(";"):
        if "=" not in part:
            continue
        k, v = part.strip().split("=", 1)
        if not k:
            continue
        cookies.append({
            "name": k.strip(), "value": v.strip(),
            "domain": domain, "path": "/",
        })
    return json.dumps({"cookies": cookies, "origins": []})
