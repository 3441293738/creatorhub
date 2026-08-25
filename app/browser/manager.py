"""Patchright 浏览器管理器(多账号隔离版)。

每个账号一套**独立持久化 context**(launch_persistent_context):
  - 独立 user-data-dir(cookie/localStorage 天然隔离)
  - 独立代理 / UA / 视口 / 时区 / 指纹
常驻这些 context 并按 LRU 控制同时存活数量(省内存)。
登录/发布用同一 profile 的**有头** context(headless=False)。
对应原项目用 chromedp 的角色。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, Dict, List, Optional
from patchright.async_api import BrowserContext, async_playwright

from ..windowing import (CHROMIUM_WINDOW_CLASSES, bring_window_to_front,
                         capture_window_snapshot)
from .backends import (
    ACCOUNT_BROWSER_BACKENDS,
    DEFAULT_BACKEND,
    FINGERPRINT_CHROMIUM_BACKEND,
    LOCAL_BACKEND,
    BrowserBackendUnavailableError,
    FingerprintChromiumBackend,
)
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
    "--disable-infobars",
    # 关键:禁止 WebRTC 走非代理 UDP。否则真实 Chromium 会通过 STUN 直接暴露宿主
    # 公网/内网 IP,绕过我们在 HTTP 层设的账号代理 —— 所有号在 WebRTC 上露同一真实
    # 出口 IP,一号一代理的防关联就白做了。这个 flag 让 WebRTC 只认代理路径。
    *_PROXY_WEBRTC_ARGS,
]

# storage_state 里允许注入的 Cookie 字段(Patchright add_cookies 接受的键)
_COOKIE_KEYS = ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")


class BrowserProfileConflictError(RuntimeError):
    """账号 Profile 已被另一个 CreatorHub 进程占用。"""


class _ProfileProcessLock:
    """Small cross-platform advisory lock kept for a browser context's lifetime."""

    def __init__(self, profile_dir: Path):
        self.path = profile_dir / ".browser.lock"
        self._fh = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        fh = self.path.open("r+b", buffering=0)
        if self.path.stat().st_size == 0:
            fh.write(b" ")
            fh.flush()
        fh.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            fh.close()
            raise BrowserProfileConflictError(
                f"账号浏览器 Profile 已被其他进程占用: {self.path.parent}") from None
        try:
            payload = json.dumps({"pid": os.getpid(), "locked_at": time.time()}) \
                .encode("utf-8")
            fh.seek(0)
            fh.write(payload)
            fh.truncate()
            fh.flush()
        except Exception:
            pass
        self._fh = fh

    def release(self) -> None:
        fh, self._fh = self._fh, None
        if fh is None:
            return
        try:
            fh.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        fh.close()


def _parse_proxy(s: str) -> Optional[Dict[str, str]]:
    """把 http://user:pass@host:port / socks5://host:port 解析成 Patchright proxy 配置。"""
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
                 xhs_cdp_idle_seconds: int = 900,
                 native_write_gate_enabled: bool = True,
                 native_write_require_system_chrome: bool = True,
                 native_write_require_verified_proxy: bool = True,
                 native_write_proxy_max_age_seconds: int = 86400,
                 browser_exit_probe_url: str = "https://ipinfo.io/json",
                 browser_backend: str = LOCAL_BACKEND,
                 fingerprint_chromium_path: str = "",
                 fingerprint_chromium_allow_headless: bool = False,
                 fingerprint_chromium_platform: str = "auto",
                 fingerprint_chromium_runtimes: list[dict] | None = None,
                 fingerprint_default_runtime_id: str = ""):
        self.default_ua = default_ua
        self.profiles_root = profiles_root
        self.max_live = max(1, max_live)
        self._native_ua_callback = native_ua_callback
        # ``playwright`` 是 1.61 以前配置文件使用的后端名；读取时平滑
        # 迁移到 Patchright，避免升级后意外切回 auto/CDP。
        requested_mode = "patchright" if xhs_browser_mode == "playwright" else xhs_browser_mode
        self.xhs_browser_mode = (
            requested_mode if requested_mode in {"auto", "cdp", "patchright"}
            else "auto"
        )
        self.xhs_cdp_idle_seconds = max(0, int(xhs_cdp_idle_seconds))
        self.native_write_gate_enabled = bool(native_write_gate_enabled)
        self.native_write_require_system_chrome = bool(
            native_write_require_system_chrome)
        self.native_write_require_verified_proxy = bool(
            native_write_require_verified_proxy)
        self.native_write_proxy_max_age_seconds = max(
            0, int(native_write_proxy_max_age_seconds))
        self.browser_exit_probe_url = str(browser_exit_probe_url or "").strip()
        requested_backend = str(browser_backend or LOCAL_BACKEND).strip().lower()
        self.default_browser_backend = (
            requested_backend
            if requested_backend in {LOCAL_BACKEND, FINGERPRINT_CHROMIUM_BACKEND}
            else LOCAL_BACKEND
        )
        self._fingerprint_backends: Dict[str, FingerprintChromiumBackend] = {}
        self._fingerprint_runtime_enabled: Dict[str, bool] = {}
        self.default_fingerprint_runtime_id = str(
            fingerprint_default_runtime_id or "").strip()
        if fingerprint_chromium_path:
            configured_id = self.default_fingerprint_runtime_id or "configured"
            self.register_fingerprint_runtime(
                configured_id, fingerprint_chromium_path,
                allow_headless=fingerprint_chromium_allow_headless,
                platform=fingerprint_chromium_platform,
                label="Fingerprint Chromium · 开源内核",
                enabled=True,
            )
            if not self.default_fingerprint_runtime_id:
                self.default_fingerprint_runtime_id = configured_id
        for runtime in fingerprint_chromium_runtimes or []:
            runtime_id = str(runtime.get("runtime_id") or "").strip()
            if not runtime_id:
                continue
            self.register_fingerprint_runtime(
                runtime_id,
                str(runtime.get("executable_path") or ""),
                allow_headless=bool(runtime.get("allow_headless", False)),
                platform=str(runtime.get("platform") or "auto"),
                version=str(runtime.get("version") or ""),
                label=str(runtime.get("name") or ""),
                enabled=bool(runtime.get("enabled", True)),
            )
            if runtime.get("is_default"):
                self.default_fingerprint_runtime_id = runtime_id
        if not self.default_fingerprint_runtime_id and self._fingerprint_backends:
            self.default_fingerprint_runtime_id = next(iter(self._fingerprint_backends))
        # Compatibility alias retained for callers/tests written for one runtime.
        self._fingerprint_backend = self._default_fingerprint_backend()
        self._pw = None
        self._contexts: Dict[Any, BrowserContext] = {}   # key -> 持久化 context
        self._cdp_sessions: Dict[Any, Any] = {}
        self._backend_by_key: Dict[Any, str] = {}
        self._runtime_by_key: Dict[Any, str] = {}
        self._fallback_reason_by_key: Dict[Any, str] = {}
        self._proxy_signature_by_key: Dict[Any, str] = {}
        self._profile_process_locks: Dict[Any, _ProfileProcessLock] = {}
        self._cdp_backend = None
        self.xhs_interaction = XhsInteractionPolicy()
        self._xhs_visible_gate = XhsVisibleActionGate()
        self._xhs_page_locks: Dict[Any, asyncio.Lock] = {}
        self._last_used: Dict[Any, float] = {}
        self._locks: Dict[Any, asyncio.Lock] = {}
        self._cv_lock = asyncio.Lock()                   # 保护 context 字典的创建/驱逐
        self._chrome_major: Optional[int] = None         # 实际 Chromium 大版本(启动时探测)
        # 优先使用机器上安装的稳定版 Chrome；没有时回退到 Patchright
        # 附带的 Chrome for Testing。登录窗口因此更接近用户日常浏览器环境。
        self._browser_channel: Optional[str] = None

    def register_fingerprint_runtime(
            self, runtime_id: str, executable_path: str, *,
            allow_headless: bool = False, platform: str = "auto",
            version: str = "", label: str = "", enabled: bool = True) -> None:
        runtime_id = str(runtime_id or "").strip()
        if not runtime_id:
            raise ValueError("runtime_id 不能为空")
        self._fingerprint_backends[runtime_id] = FingerprintChromiumBackend(
            executable_path,
            allow_headless=allow_headless,
            platform=platform,
            runtime_id=runtime_id,
            version=version,
            label=(label or f"Fingerprint Chromium {version or runtime_id}"),
        )
        self._fingerprint_runtime_enabled[runtime_id] = bool(enabled)
        if enabled and not self.default_fingerprint_runtime_id:
            self.default_fingerprint_runtime_id = runtime_id
        self._fingerprint_backend = self._default_fingerprint_backend()

    def unregister_fingerprint_runtime(self, runtime_id: str) -> None:
        runtime_id = str(runtime_id or "").strip()
        self._fingerprint_backends.pop(runtime_id, None)
        self._fingerprint_runtime_enabled.pop(runtime_id, None)
        if self.default_fingerprint_runtime_id == runtime_id:
            self.default_fingerprint_runtime_id = next(
                (key for key in self._fingerprint_backends
                 if self._fingerprint_runtime_enabled.get(key, False)), "")
        self._fingerprint_backend = self._default_fingerprint_backend()

    def set_default_fingerprint_runtime(self, runtime_id: str) -> None:
        runtime_id = str(runtime_id or "").strip()
        if runtime_id not in self._fingerprint_backends:
            raise ValueError("内核运行时不存在")
        if not self._fingerprint_runtime_enabled.get(runtime_id, False):
            raise ValueError("内核运行时已停用")
        self.default_fingerprint_runtime_id = runtime_id
        self._fingerprint_backend = self._default_fingerprint_backend()

    def clear_default_fingerprint_runtime(self) -> None:
        self.default_fingerprint_runtime_id = ""
        self._fingerprint_backend = self._default_fingerprint_backend()

    def _default_fingerprint_backend(self) -> FingerprintChromiumBackend:
        backend = self._fingerprint_backends.get(
            self.default_fingerprint_runtime_id)
        if backend is not None:
            return backend
        for runtime_id, candidate in self._fingerprint_backends.items():
            if self._fingerprint_runtime_enabled.get(runtime_id, False):
                return candidate
        return FingerprintChromiumBackend("")

    def effective_fingerprint_runtime_id(self, identity: Identity) -> str:
        requested = str(
            getattr(identity, "browser_runtime_id", "") or "").strip()
        return requested or self.default_fingerprint_runtime_id

    def _fingerprint_backend_for(
            self, identity: Identity, *, require_available: bool = True,
            ) -> FingerprintChromiumBackend:
        runtime_id = self.effective_fingerprint_runtime_id(identity)
        backend = self._fingerprint_backends.get(runtime_id)
        if backend is None:
            raise BrowserBackendUnavailableError(
                f"账号选择的内核运行时不存在: {runtime_id or '未配置'}")
        if not self._fingerprint_runtime_enabled.get(runtime_id, False):
            raise BrowserBackendUnavailableError(
                f"账号选择的内核运行时已停用: {runtime_id}")
        if require_available and not backend.available:
            raise BrowserBackendUnavailableError(backend.unavailable_reason)
        return backend

    @staticmethod
    def _runtime_profile_dir(identity: Identity, runtime_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", runtime_id or "default")
        return Path(identity.profile_dir) / "runtimes" / safe

    async def start(self):
        self._pw = await async_playwright().start()
        self._chrome_major = await self._detect_chrome_major()
        self._cdp_backend = XhsCdpBackend(
            self._pw, self.profiles_root)

    async def _detect_chrome_major(self) -> Optional[int]:
        """选择可用浏览器并探测其 Chromium 大版本。

        优先系统稳定版 Chrome，缺失时使用 Patchright bundled Chromium。
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
            effective = self.effective_browser_backend(identity)
            if effective == FINGERPRINT_CHROMIUM_BACKEND:
                backend = FINGERPRINT_CHROMIUM_BACKEND
            else:
                backend = "cdp" if self._uses_xhs_cdp(identity) else "patchright"
        is_cdp = backend == "cdp"
        is_fingerprint = backend == FINGERPRINT_CHROMIUM_BACKEND
        runtime_id = ""
        runtime_version = ""
        runtime_label = ""
        fingerprint_backend = self._fingerprint_backend
        if is_fingerprint:
            runtime_id = self._runtime_by_key.get(
                identity.key, self.effective_fingerprint_runtime_id(identity))
            fingerprint_backend = self._fingerprint_backends.get(
                runtime_id, self._fingerprint_backend)
            runtime_version = fingerprint_backend.version
            runtime_label = fingerprint_backend.label
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
            else runtime_label if is_fingerprint
            else "Patchright Chromium · 回退" if fallback
            else "Patchright Chromium"
        )
        snapshot = {
            "browser": (
                "chrome" if is_cdp
                else "fingerprint-chromium" if is_fingerprint
                else (self._browser_channel or "chromium")
            ),
            "chrome_major": (
                int(runtime_version.split(".", 1)[0])
                if is_fingerprint and runtime_version.split(".", 1)[0].isdigit()
                else None if is_fingerprint else self._chrome_major),
            "headless": (
                False if is_cdp
                else bool(headless and fingerprint_backend.allow_headless)
                if is_fingerprint else bool(headless)
            ),
            "identity_mode": identity.identity_mode,
            "profile_dir": (str(self._runtime_profile_dir(identity, runtime_id))
                            if is_fingerprint else identity.profile_dir),
            "has_proxy": bool(str(identity.proxy or "").strip()),
            "backend": backend,
            "backend_label": backend_label,
            "fallback": fallback,
            "fallback_reason": fallback_reason,
        }
        if is_fingerprint:
            snapshot["runtime_id"] = runtime_id
            snapshot["runtime_version"] = runtime_version
        return snapshot

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
        for key in list(self._profile_process_locks):
            self._release_profile_lock(key)

    # ── 画像 ──
    def identity_for(self, acc) -> Identity:
        return Identity.from_account(acc, self.profiles_root, self.default_ua)

    def direct_request_user_agent(self, identity: Identity) -> str:
        """Return a UA whose Chromium major matches the account runtime.

        Browser-created cookies are also reused by a few explicit HTTP
        compatibility paths.  Normalising their UA avoids presenting an old
        configured Chrome version beside a newer account browser.
        """
        ua = str(identity.ua or self.default_ua or "").strip()
        effective = self.effective_browser_backend(identity)
        major = 0
        if effective == FINGERPRINT_CHROMIUM_BACKEND:
            try:
                backend = self._fingerprint_backend_for(
                    identity, require_available=False)
                head = str(backend.version or "").split(".", 1)[0]
                major = int(head) if head.isdigit() else 0
            except (BrowserBackendUnavailableError, ValueError):
                major = 0
        else:
            major = int(self._chrome_major or 0)
        if major and ua:
            ua = re.sub(
                r"Chrome/\d+(?:\.\d+){0,3}",
                f"Chrome/{major}.0.0.0", ua)
            ua = re.sub(
                r"Edg/\d+(?:\.\d+){0,3}",
                f"Edg/{major}.0.0.0", ua)
        return ua or self.default_ua

    async def _capture_context_ua(
            self, ctx: BrowserContext, identity: Identity) -> str:
        """Persist the exact UA exposed by Patchright or an attached CDP tab."""
        probe_page = None
        created_probe = False
        try:
            pages = list(ctx.pages)
            if pages:
                probe_page = pages[0]
            else:
                probe_page = await ctx.new_page()
                created_probe = True
            actual_ua = str(
                await probe_page.evaluate("navigator.userAgent") or "").strip()
            if actual_ua:
                identity.ua = actual_ua
                if identity.account_id is not None and self._native_ua_callback:
                    self._native_ua_callback(identity.account_id, actual_ua)
            return actual_ua
        except Exception:
            return ""
        finally:
            if created_probe and probe_page is not None:
                with suppress(Exception):
                    await probe_page.close()

    def effective_browser_backend(self, identity: Identity) -> str:
        """Resolve an account override without silently accepting bad values."""
        requested = str(
            getattr(identity, "browser_backend", DEFAULT_BACKEND)
            or DEFAULT_BACKEND
        ).strip().lower()
        if requested not in ACCOUNT_BROWSER_BACKENDS:
            requested = DEFAULT_BACKEND
        return (
            self.default_browser_backend
            if requested == DEFAULT_BACKEND else requested
        )

    def backend_status(
            self, requested: str = DEFAULT_BACKEND,
            runtime_id: str = "") -> Dict[str, Any]:
        value = str(requested or DEFAULT_BACKEND).strip().lower()
        if value not in ACCOUNT_BROWSER_BACKENDS:
            return {
                "name": value,
                "available": False,
                "detail": "不支持的浏览器后端",
            }
        name = self.default_browser_backend if value == DEFAULT_BACKEND else value
        if name == FINGERPRINT_CHROMIUM_BACKEND:
            selected_id = str(runtime_id or self.default_fingerprint_runtime_id).strip()
            backend = self._fingerprint_backends.get(selected_id)
            if backend is None:
                return {
                    "name": name, "runtime_id": selected_id,
                    "available": False,
                    "detail": "未注册可用的 Fingerprint Chromium 内核",
                }
            enabled = self._fingerprint_runtime_enabled.get(selected_id, False)
            return {
                "name": name,
                "runtime_id": selected_id,
                "available": bool(enabled and backend.available),
                "detail": ("内核运行时已停用" if not enabled
                           else backend.unavailable_reason),
            }
        return {"name": LOCAL_BACKEND, "available": True, "detail": ""}

    def fingerprint_runtime_catalog(self) -> List[Dict[str, Any]]:
        rows = []
        for runtime_id, backend in self._fingerprint_backends.items():
            enabled = self._fingerprint_runtime_enabled.get(runtime_id, False)
            rows.append({
                "runtime_id": runtime_id,
                "name": backend.label,
                "version": backend.version,
                "platform": backend.platform,
                "allow_headless": backend.allow_headless,
                "enabled": enabled,
                "is_default": runtime_id == self.default_fingerprint_runtime_id,
                "available": bool(enabled and backend.available),
                "detail": ("内核运行时已停用" if not enabled
                           else backend.unavailable_reason),
            })
        return rows

    def backend_catalog(self) -> Dict[str, Any]:
        return {
            "default": self.default_browser_backend,
            "backends": [
                {
                    "name": LOCAL_BACKEND,
                    "label": "本地 Patchright / 系统 Chrome",
                    "available": True,
                    "detail": "",
                },
                {
                    "name": FINGERPRINT_CHROMIUM_BACKEND,
                    "label": self._fingerprint_backend.label,
                    "available": self.backend_status(
                        FINGERPRINT_CHROMIUM_BACKEND)["available"],
                    "detail": self.backend_status(
                        FINGERPRINT_CHROMIUM_BACKEND)["detail"],
                },
            ],
            "default_runtime_id": self.default_fingerprint_runtime_id,
            "runtimes": self.fingerprint_runtime_catalog(),
        }

    def anon_identity(self) -> Identity:
        return Identity(account_id=None,
                        profile_dir=str(Path(self.profiles_root) / "_anon"),
                        ua=self.default_ua)

    def _uses_xhs_cdp(self, identity: Identity) -> bool:
        return (
            identity.platform == "xhs"
            and self.xhs_browser_mode in {"auto", "cdp"}
            and self.effective_browser_backend(identity) == LOCAL_BACKEND
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

    @staticmethod
    def proxy_signature(proxy: str) -> str:
        plan = try_proxy_plan(proxy)
        return plan.signature if plan else "direct"

    @classmethod
    def _context_signature(
            cls, identity: Identity, effective_backend: str,
            runtime_id: str, proxy_signature: str) -> str:
        """Hash every setting that defines an account browser environment.

        The in-memory context cache is keyed by the database account id for
        compatibility with account locks.  SQLite can reuse that id after an
        account is deleted, so the id alone is not an isolation boundary.  A
        changed Profile path or fingerprint must force the stale context out.
        """
        profile_dir = (
            cls._runtime_profile_dir(identity, runtime_id)
            if effective_backend == FINGERPRINT_CHROMIUM_BACKEND
            else Path(identity.profile_dir)
        )
        payload = {
            "profile_dir": os.path.normcase(str(
                profile_dir.expanduser().resolve())),
            "backend": effective_backend,
            "runtime_id": runtime_id,
            "proxy": proxy_signature,
            "identity_mode": identity.identity_mode,
            "ua": identity.ua,
            "viewport": [identity.viewport_w, identity.viewport_h],
            "timezone_id": identity.timezone_id,
            "locale": identity.locale,
            "fp_seed": identity.fp_seed,
            "fp_platform": identity.fp_platform,
            "fp_platform_version": identity.fp_platform_version,
            "fp_brand": identity.fp_brand,
            "fp_brand_version": identity.fp_brand_version,
            "fp_hardware_concurrency": identity.fp_hardware_concurrency,
            "fp_gpu_vendor": identity.fp_gpu_vendor,
            "fp_gpu_renderer": identity.fp_gpu_renderer,
            "fp_accept_languages": identity.fp_accept_languages,
            "fp_disable_spoofing": identity.fp_disable_spoofing,
            "fp_language_mode": identity.fp_language_mode,
            "fp_timezone_mode": identity.fp_timezone_mode,
            "fp_viewport_mode": identity.fp_viewport_mode,
            "fp_location_mode": identity.fp_location_mode,
            "fp_geolocation_permission": identity.fp_geolocation_permission,
            "fp_webrtc_mode": identity.fp_webrtc_mode,
            "fp_extra_args": identity.fp_extra_args,
            "geo": [identity.geo_lat, identity.geo_lon],
        }
        raw = json.dumps(
            payload, ensure_ascii=True, sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _acquire_profile_lock(
            self, identity: Identity, profile_dir: Path | None = None) -> None:
        if identity.key in self._profile_process_locks:
            return
        lock = _ProfileProcessLock(profile_dir or Path(identity.profile_dir))
        lock.acquire()
        self._profile_process_locks[identity.key] = lock

    def _release_profile_lock(self, key: Any) -> None:
        lock = self._profile_process_locks.pop(key, None)
        if lock is not None:
            lock.release()

    def native_write_gate_error(self, account, *, headed: bool = True,
                                browser_mode: bool = True,
                                now=None) -> str:
        """仅对 native 账号执行写操作环境门禁；legacy 保持原行为。"""
        if not self.native_write_gate_enabled \
                or getattr(account, "identity_mode", "legacy") != "native":
            return ""
        if self._pw is None:
            return "write_env_blocked:浏览器管理器未启动"
        if browser_mode and not headed:
            return "write_env_blocked:native 账号写操作必须使用有头浏览器"
        # Gate tests and queued task projections may pass a lightweight account
        # view, so resolving the backend must not require full profile fields.
        effective_backend = self.effective_browser_backend(account)
        fingerprint_status = self.backend_status(
            effective_backend,
            str(getattr(account, "browser_runtime_id", "") or ""))
        if effective_backend == FINGERPRINT_CHROMIUM_BACKEND \
                and not fingerprint_status["available"]:
            return (
                "write_env_blocked:开源指纹浏览器不可用:"
                + str(fingerprint_status["detail"])
            )
        if self.native_write_require_system_chrome \
                and self._browser_channel != "chrome" \
                and effective_backend != FINGERPRINT_CHROMIUM_BACKEND:
            return "write_env_blocked:未检测到系统稳定版 Chrome 或可用指纹 Chromium"
        proxy = str(getattr(account, "proxy", "") or "").strip()
        if not proxy or not self.native_write_require_verified_proxy:
            return ""
        if getattr(account, "proxy_status", "unknown") != "ok":
            return "write_env_blocked:账号代理尚未通过浏览器出口验证"
        signature = self.proxy_signature(proxy)
        if not getattr(account, "exit_ip", "") \
                or getattr(account, "exit_proxy_signature", "") != signature:
            return "write_env_blocked:账号代理缺少当前线路的浏览器出口基线"
        checked = getattr(account, "exit_checked_at", None)
        if not checked:
            return "write_env_blocked:账号代理出口基线未记录检查时间"
        if self.native_write_proxy_max_age_seconds > 0:
            from datetime import datetime
            sampled = now or datetime.utcnow()
            if (sampled - checked).total_seconds() \
                    > self.native_write_proxy_max_age_seconds:
                return "write_env_blocked:账号代理的浏览器出口验证已过期"
        return ""

    # ── 持久化 context ──
    async def _launch_persistent(self, identity: Identity, headless: bool = True
                                 ) -> BrowserContext:
        effective_backend = self.effective_browser_backend(identity)
        fingerprint_backend = None
        runtime_id = ""
        if effective_backend == FINGERPRINT_CHROMIUM_BACKEND:
            fingerprint_backend = self._fingerprint_backend_for(identity)
            runtime_id = fingerprint_backend.runtime_id
            pdir = self._runtime_profile_dir(identity, runtime_id)
        else:
            pdir = Path(identity.profile_dir)
        pdir.mkdir(parents=True, exist_ok=True)
        was_empty = not any(p.name != ".browser.lock" for p in pdir.iterdir())
        self._acquire_profile_lock(identity, pdir)
        ua = self._normalize_ua(identity.ua or self.default_ua)
        fingerprint_plan = None
        if effective_backend == FINGERPRINT_CHROMIUM_BACKEND:
            try:
                fingerprint_plan = fingerprint_backend.launch_plan(
                    identity, requested_headless=headless)
            except BrowserBackendUnavailableError:
                self._release_profile_lock(identity.key)
                raise
            headless = fingerprint_plan.headless
        kwargs: Dict[str, Any] = dict(
            user_data_dir=str(pdir), headless=headless,
        )
        # Patchright 默认会添加 --no-sandbox。Windows 的 Chrome 可以正常
        # 使用沙箱，新旧账号都显式开启，避免安全警告和不必要的启动差异。
        if os.name == "nt":
            kwargs["chromium_sandbox"] = True
        if fingerprint_plan is not None:
            kwargs["executable_path"] = fingerprint_plan.executable_path
        elif self._browser_channel:
            kwargs["channel"] = self._browser_channel
        # Engine-level fingerprint runtimes own the entire identity surface;
        # mixing the legacy JS hooks into them would make the profile drift.
        legacy = (
            identity.identity_mode != "native"
            and fingerprint_plan is None
        )
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
        if fingerprint_plan is not None:
            args = list(fingerprint_plan.args)
            args.append(
                f"--window-size={int(identity.viewport_w)},{int(identity.viewport_h)}")
            if proxy and str(identity.fp_webrtc_mode or "conceal") != "allow":
                for item in _PROXY_WEBRTC_ARGS:
                    if item not in args:
                        args.append(item)
            geo_permission = str(
                identity.fp_geolocation_permission or "allow").lower()
            if geo_permission == "deny":
                args.append("--deny-permission-prompts")
            kwargs["args"] = args
            kwargs["no_viewport"] = True
            if geo_permission != "deny":
                kwargs["geolocation"] = identity.geolocation
            if geo_permission == "allow":
                kwargs["permissions"] = ["geolocation"]
        elif not legacy:
            # native 账号使用 Chrome/操作系统自己的视口、语言、时区与硬件画像。
            # 仅在显式配置代理时约束 WebRTC，避免 UDP 绕过代理出口。
            kwargs["no_viewport"] = True
            if proxy:
                kwargs["args"] = list(_PROXY_WEBRTC_ARGS)
        if proxy:
            kwargs["proxy"] = proxy
        try:
            ctx = await self._pw.chromium.launch_persistent_context(**kwargs)
        except Exception:
            self._release_profile_lock(identity.key)
            raise
        with suppress(Exception):
            ctx.on("close", lambda *_: self._release_profile_lock(identity.key))
        if not legacy:
            # Persist the UA exposed by this exact native context. It is
            # diagnostic state only; native launches still omit any override.
            await self._capture_context_ua(ctx, identity)
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
            *, assume_empty: bool = False, overwrite: bool = False) -> None:
        if not identity.bridge_states:
            return
        try:
            # 常驻后台 context 默认保护 profile 中平台刚刷新的 Cookie。显式切到
            # 临时有头采集窗口时则以数据库账号登录态为准，覆盖 profile 里可能残留的
            # 访客/登出 Cookie，行为与“账号 → 打开浏览器”保持一致。
            existing = [] if (assume_empty or overwrite) else await ctx.cookies()
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
        self._runtime_by_key.pop(key, None)
        self._fallback_reason_by_key.pop(key, None)
        self._proxy_signature_by_key.pop(key, None)
        try:
            if session is not None and self._cdp_backend is not None:
                with suppress(Exception):
                    await self._cdp_backend.close(session)
                return
            if ctx is not None:
                with suppress(Exception):
                    await ctx.close()
        finally:
            self._release_profile_lock(key)

    async def context_for(self, identity: Identity) -> BrowserContext:
        """取(或惰性创建)账号专属常驻 context。"""
        key = identity.key
        async with self._cv_lock:
            effective_backend = self.effective_browser_backend(identity)
            runtime_id = (
                self.effective_fingerprint_runtime_id(identity)
                if effective_backend == FINGERPRINT_CHROMIUM_BACKEND else "")
            plan = (self._xhs_proxy_plan(identity)
                    if identity.platform == "xhs"
                    else try_proxy_plan(identity.proxy))
            proxy_signature = plan.signature if plan else "direct"
            signature = self._context_signature(
                identity, effective_backend, runtime_id, proxy_signature)
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
                        self._backend_by_key[key] = "patchright"
                    else:
                        ctx = session.context
                        await self._capture_context_ua(ctx, identity)
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
                    self._backend_by_key[key] = (
                        FINGERPRINT_CHROMIUM_BACKEND
                        if effective_backend == FINGERPRINT_CHROMIUM_BACKEND
                        else "patchright"
                    )
                    if effective_backend == FINGERPRINT_CHROMIUM_BACKEND:
                        self._runtime_by_key[key] = runtime_id
                self._contexts[key] = ctx
                self._proxy_signature_by_key[key] = signature
            self._last_used[key] = time.time()
            if key in self._cdp_sessions:
                self._cdp_sessions[key].last_used = self._last_used[key]
            return ctx

    async def probe_browser_exit(self, identity: Identity,
                                 timeout_ms: int = 15000) -> Dict[str, str]:
        """用账号真实 BrowserContext 探测出口，不经独立 HTTP 客户端。"""
        if not self.browser_exit_probe_url:
            raise RuntimeError("未配置 browser_exit_probe_url")
        ctx = await self.context_for(identity)
        page = await ctx.new_page()
        try:
            response = await page.goto(
                self.browser_exit_probe_url, wait_until="domcontentloaded",
                timeout=max(1000, int(timeout_ms)))
            status = getattr(response, "status", 0) if response else 0
            if status and not 200 <= int(status) < 300:
                raise RuntimeError(f"出口探测返回 HTTP {status}")
            raw = await page.locator("body").inner_text(timeout=3000)
            data = json.loads(raw or "{}")
            ip = str(data.get("ip") or "").strip()
            if not ip:
                raise RuntimeError("出口探测未返回 IP")
            org = str(data.get("org") or "").strip()
            asn = org.split(" ", 1)[0] if org.upper().startswith("AS") else org
            return {
                "ip": ip,
                "country": str(data.get("country") or "").strip().upper(),
                "asn": asn,
                "timezone": str(data.get("timezone") or "").strip(),
                "city": str(data.get("city") or "").strip(),
            }
        finally:
            with suppress(Exception):
                await page.close()

    async def probe_fingerprint_runtime(
            self, runtime_id: str, profile_root: str | Path) -> Dict[str, Any]:
        """Launch one registered runtime with an isolated disposable profile."""
        if self._pw is None:
            raise RuntimeError("浏览器管理器尚未启动")
        runtime_id = str(runtime_id or "").strip()
        status = self.backend_status(FINGERPRINT_CHROMIUM_BACKEND, runtime_id)
        if not status["available"]:
            raise BrowserBackendUnavailableError(str(status["detail"]))
        profile = Path(profile_root) / f"probe_{runtime_id}_{time.time_ns()}"
        identity = Identity(
            account_id=None,
            profile_dir=str(profile),
            identity_mode="native",
            browser_backend=FINGERPRINT_CHROMIUM_BACKEND,
            browser_runtime_id=runtime_id,
            fp_seed=f"runtime-probe-{runtime_id}",
        )
        context = None
        page = None
        try:
            context = await self._launch_persistent(identity, headless=False)
            pages = list(context.pages)
            page = pages[0] if pages else await context.new_page()
            details = await page.evaluate("""() => ({
                userAgent: navigator.userAgent,
                platform: navigator.platform,
                language: navigator.language,
                webdriver: navigator.webdriver
            })""")
            return {
                "ok": True,
                "runtime_id": runtime_id,
                "user_agent": str((details or {}).get("userAgent") or ""),
                "platform": str((details or {}).get("platform") or ""),
                "language": str((details or {}).get("language") or ""),
                "webdriver": (details or {}).get("webdriver"),
            }
        finally:
            if context is not None:
                with suppress(Exception):
                    await context.close()
            self._release_profile_lock(identity.key)
            import shutil
            with suppress(Exception):
                shutil.rmtree(profile)

    async def new_page(self, identity: Identity, block_media: bool = False):
        """从账号常驻 context 开一个新 page(可屏蔽图片/视频/字体)。用完请 page.close()。"""
        ctx = await self.context_for(identity)
        try:
            page = await ctx.new_page()
        except Exception as exc:
            # A user can close the headed Chromium window directly. Patchright
            # then leaves the now-closed BrowserContext object in our cache for
            # a short time, so the next click would otherwise fail forever with
            # TargetClosedError. Evict and relaunch the same account profile
            # once; login cookies are bridged again by context_for().
            detail = f"{type(exc).__name__}: {exc}".lower()
            if "targetclosed" not in detail and "has been closed" not in detail:
                raise
            await self.close_context(identity.key)
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
    async def visible_action(self, identity: Identity, *, keep_context: bool = False):
        """Serialize visible XHS work and close one-shot browser contexts.

        A persistent Chromium context always owns a bootstrap about:blank page.
        Closing only the task page therefore leaves a blank window behind. The
        outermost one-shot action releases the whole context; the explicit
        manual-account window opts out with ``keep_context=True``.
        """
        nested = self._xhs_visible_gate.owned_by_current_task
        if nested:
            async with self._xhs_visible_gate.acquire(identity.key):
                yield
            return
        page_lock = self._xhs_page_locks.setdefault(
            identity.key, asyncio.Lock())
        async with page_lock:
            try:
                async with self._xhs_visible_gate.acquire(identity.key):
                    yield
            finally:
                if not keep_context:
                    with suppress(Exception):
                        await self.close_context(identity.key)

    @asynccontextmanager
    async def visible_page(self, identity: Identity, *, url: str = ""):
        """Lease one foreground XHS task page and close its one-shot context."""
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

    @asynccontextmanager
    async def temporary_headed_context(self, identity: Identity):
        """Lease a visible persistent context and always save/close it afterwards."""
        ctx = await self.open_headed(identity)
        try:
            await self._bridge_identity_cookies(
                ctx, identity, overwrite=True)
            yield ctx
        finally:
            if identity.platform == "xhs":
                await self.close_context(identity.key)
            else:
                with suppress(Exception):
                    await ctx.close()


# 各平台 Cookie 顶域(子域如 creator./edith. 都吃顶域 cookie,一个就够)
_COOKIE_DOMAIN = {
    "douyin": ".douyin.com",
    "xhs": ".xiaohongshu.com",
    "kuaishou": ".kuaishou.com",
    "shipinhao": ".weixin.qq.com",   # 视频号:finder 登录态(_finder_auth/sessionid)挂在 .weixin.qq.com
}


def cookie_string_to_state(cookie_str: str, platform: str = "douyin") -> str:
    """把粘贴的 Cookie 串转成 Patchright storage_state JSON(兜底登录用)。"""
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
