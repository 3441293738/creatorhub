"""Selective loopback HTTPS proxy + same-origin WebSocket bridge for WeChat Channels."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import h11
import httpx
from curl_cffi.requests import AsyncSession
from websockets.asyncio.server import Server as WsServer
from websockets.asyncio.server import ServerConnection
from websockets.asyncio.server import serve as ws_serve
from websockets.exceptions import ConnectionClosed

from app.platforms.channels.client import (
    ChannelsProtocolTransportError,
    normalize_capabilities,
    sanitize_bridge_error,
)
from app.platforms.channels.local_ca import (
    ALLOWED_INTERCEPT_HOSTS,
    LocalCaManager,
    _write_private_file,
)
from app.platforms.channels.system_proxy import (
    SystemProxyBackend,
    enable_system_proxy,
    get_system_proxy_state,
    resolve_upstream_proxy,
    restore_system_proxy,
)
from app.platforms.channels.wmpf_cdp import discover_wechat_processes

logger = logging.getLogger(__name__)

BRIDGE_SCRIPT_PATH = "/__creatorhub_bridge.js"
BRIDGE_WS_PATH = "/__creatorhub_ws"
BRIDGE_LANDING_PATHS = frozenset(
    {
        "/platform/bridge",
        "/web/pages/platform/bridge",
        "/web/pages/bridge",
        "/__creatorhub_bridge",
    }
)
DEFAULT_BRIDGE_ENTRY_URL = "https://channels.weixin.qq.com/platform/bridge"
ALT_BRIDGE_ENTRY_URL = "https://channels.weixin.qq.com/web/pages/bridge"
_WECHAT_TRAFFIC_HOSTS = frozenset(
    {
        "liteapp.weixin.qq.com",
        "finder.video.qq.com",
        "findermp.video.qq.com",
        "findera4.video.qq.com",
        "servicewechat.com",
        "mp.weixin.qq.com",
        "res.wx.qq.com",
    }
)
_WECHAT_UA_MARKERS = (b"micromessenger", b"windowswechat", b"macwechat", b"unifiedpc")
_FINDER_MODULE_RE = re.compile(
    r'((?:https?:)?//res\.wx\.qq\.com/[^\s"\'<>]*virtual_svg-icons-register\.publish[^\s"\'<>]*\.js)',
    re.IGNORECASE,
)
_SYNTHETIC_WECHAT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 NetType/WIFI "
    "MicroMessenger/7.0.20.1781(0x6700143B) WindowsWechat(0x63090c11) XWEB/11581 Flue"
)

ALLOWED_BRIDGE_METHODS = frozenset(
    {
        "fetchCommentsPage",
        "fetchFeedDetail",
        "fetchUserPage",
        "search",
        "fetchFeedShareUrl",
        "initFinder",
    }
)
_HOP_BY_HOP_HEADERS = frozenset(
    {
        b"connection",
        b"keep-alive",
        b"proxy-authenticate",
        b"proxy-authorization",
        b"proxy-connection",
        b"te",
        b"trailers",
        b"transfer-encoding",
        b"upgrade",
    }
)
_RESPONSE_STRIP_HEADERS = _HOP_BY_HOP_HEADERS | frozenset(
    {
        b"content-encoding",
        b"content-length",
    }
)
_HTML_INJECTED_STRIP_HEADERS = frozenset({b"etag", b"last-modified", b"cache-control"})

_JS_DIR = Path(__file__).resolve().parent / "js"
_BOOTSTRAP_JS_PATH = _JS_DIR / "finder_protocol_browser_bootstrap.js"
_BRIDGE_JS_PATH = _JS_DIR / "finder_proxy_bridge.js"

_ACTIVE_PROXY_MANAGER: "ChannelsProxyManager | None" = None


def get_active_proxy_manager() -> "ChannelsProxyManager | None":
    return _ACTIVE_PROXY_MANAGER


def set_active_proxy_manager(manager: "ChannelsProxyManager | None") -> None:
    global _ACTIVE_PROXY_MANAGER
    _ACTIVE_PROXY_MANAGER = manager


_SENSITIVE_PAGE_QUERY_KEYS = frozenset(
    {
        "exportkey",
        "pass_ticket",
        "wx_header",
        "uin",
        "key",
        "ascene",
        "devicetype",
        "version",
        "nettype",
        "abtest_cookie",
        "lang",
        "fontgear",
    }
)
_WS_TOKEN_FILENAME = "channels_proxy_ws.key"


def _sanitize_page_url(raw_url: str) -> str:
    """Strip ephemeral WeChat WebView query tickets (`exportkey`, `pass_ticket`) for clean UI display."""
    text = str(raw_url or "").strip()
    if not text or "?" not in text:
        return text
    try:
        from urllib.parse import parse_qsl, urlencode, urlunsplit

        parsed = urlsplit(text)
        kept = [
            (k, v)
            for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            if k.strip().lower() not in _SENSITIVE_PAGE_QUERY_KEYS
        ]
        clean_query = urlencode(kept) if kept else ""
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, clean_query, ""))
    except Exception:
        return text.split("?", 1)[0]


@dataclass
class ConnectedFinderPage:
    """State of a live top-level WeChat Channels webview connected over `__creatorhub_ws`."""

    page_id: str
    websocket: ServerConnection
    url: str = ""
    title: str = ""
    ready: bool = False
    origin: str = ""
    reason: str = ""
    capabilities: dict[str, Any] = field(default_factory=dict)
    has_weixin_js_bridge: bool = False
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "page_id": self.page_id,
            "url": _sanitize_page_url(self.url),
            "title": self.title,
            "ready": self.ready,
            "origin": self.origin,
            "reason": self.reason,
            "capabilities": dict(self.capabilities),
            "has_weixin_js_bridge": self.has_weixin_js_bridge,
            "connected_at": self.connected_at,
            "last_seen": self.last_seen,
        }


def _parse_host_port(authority: str, default_port: int = 443) -> tuple[str, int]:
    text = (authority or "").strip()
    if text.startswith("["):
        end = text.find("]")
        if end != -1:
            host = text[1:end]
            rest = text[end + 1 :].lstrip(":")
            return host, int(rest) if rest else default_port
    if ":" in text:
        host, _, port_str = text.rpartition(":")
        if host and port_str.isdigit():
            return host, int(port_str)
    return text, default_port


def _relax_csp_for_bridge(csp_value: str) -> str:
    """Ensure CSP on intercepted channels.weixin.qq.com HTML allows our same-origin bridge + WS."""
    directives: list[str] = []
    seen_script = False
    seen_connect = False
    for raw_part in csp_value.split(";"):
        part = raw_part.strip()
        if not part:
            continue
        tokens = part.split()
        name = tokens[0].lower()
        rest = tokens[1:]
        if name == "script-src":
            seen_script = True
            if "'self'" not in rest and "*" not in rest:
                rest = ["'self'", *rest]
        elif name == "connect-src":
            seen_connect = True
            for needed in ("'self'", "wss://channels.weixin.qq.com"):
                if needed not in rest and "*" not in rest:
                    rest.append(needed)
        directives.append(" ".join([tokens[0], *rest]))
    if not seen_script:
        directives.append("script-src 'self' 'unsafe-inline' 'unsafe-eval' https:")
    if not seen_connect:
        directives.append("connect-src 'self' wss://channels.weixin.qq.com https:")
    return "; ".join(directives)


def _inject_bridge_script_into_html(html_text: str, script_url: str) -> str:
    """Inject `<script src="/__creatorhub_bridge.js?...">` into HTML, reusing any page nonce."""
    if BRIDGE_SCRIPT_PATH in html_text:
        return html_text
    nonce_match = re.search(r'<script\b[^>]*\bnonce=["\']([^"\']+)["\']', html_text, re.IGNORECASE)
    nonce_attr = ""
    if nonce_match:
        nonce_val = nonce_match.group(1).replace('"', "").replace("'", "")
        if nonce_val:
            nonce_attr = f' nonce="{nonce_val}"'
    tag = f'<script{nonce_attr} src="{script_url}" defer></script>'
    head_close = re.search(r"</head>", html_text, re.IGNORECASE)
    if head_close:
        idx = head_close.start()
        return html_text[:idx] + tag + html_text[idx:]
    body_close = re.search(r"</body>", html_text, re.IGNORECASE)
    if body_close:
        idx = body_close.start()
        return html_text[:idx] + tag + html_text[idx:]
    return tag + html_text


def _build_bridge_landing_html(script_url: str, module_preload_url: str = "") -> bytes:
    """Render same-origin Finder protocol bridge page on `https://channels.weixin.qq.com` for WeChat 4.2+."""
    preload_tag = ""
    if module_preload_url and module_preload_url.startswith("https://res.wx.qq.com/"):
        safe_mod = (
            module_preload_url.replace("&", "&amp;")
            .replace('"', "&quot;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        preload_tag = f'  <link rel="modulepreload" crossorigin href="{safe_mod}">\n'
    html = (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n'
        "<head>\n"
        '  <meta charset="utf-8">\n'
        '  <meta name="viewport" content="width=device-width,initial-scale=1">\n'
        "  <title>微信视频号桥接 · CreatorHub</title>\n"
        f"{preload_tag}"
        "  <style>\n"
        "    :root{color-scheme:light dark;font-family:system-ui,-apple-system,'Microsoft YaHei',sans-serif}\n"
        "    body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;background:#f5f6f8;color:#18191c}\n"
        "    @media(prefers-color-scheme:dark){body{background:#111317;color:#eef0f4}.card{background:#1b1e24!important;border-color:#2b303b!important}}\n"
        "    .card{max-width:520px;width:100%;background:#fff;border:1px solid #e3e6ec;border-radius:16px;padding:24px;box-shadow:0 8px 28px rgba(0,0,0,.06)}\n"
        "    .badge{display:inline-block;padding:5px 12px;border-radius:999px;font-size:13px;font-weight:600;background:#eef2ff;color:#3b5bdb;margin-bottom:12px}\n"
        "    .badge[data-state='ready']{background:#e6fcf5;color:#087f5b}\n"
        "    .badge[data-state='warn']{background:#fff4e6;color:#d9480f}\n"
        "    h1{font-size:18px;margin:0 0 10px}\n"
        "    p{font-size:14px;line-height:1.65;margin:0 0 10px;opacity:.86}\n"
        "    code{font-size:12px;padding:2px 6px;border-radius:6px;background:rgba(127,127,127,.14);word-break:break-all}\n"
        "  </style>\n"
        f'  <script src="{script_url}" defer></script>\n'
        "</head>\n"
        "<body>\n"
        '  <div class="card">\n'
        '    <div class="badge" id="creatorhub-bridge-badge" data-state="pending">正在握手本机 CreatorHub…</div>\n'
        "    <h1>CreatorHub 微信视频号原生协议桥接页</h1>\n"
        '    <p id="creatorhub-bridge-detail">正在检测微信内置浏览器 WeixinJSBridge 环境并连接本机回环代理…</p>\n'
        "    <p>连接成功后将此窗口最小化留在后台即可；CreatorHub 重启后本窗口会自动重连，无需重复操作。建议点击右上角「⋯ → 收藏」或保留在「文件传输助手」，下次开机点一下即可使用。</p>\n"
        f"    <p>桥接地址：<code>{DEFAULT_BRIDGE_ENTRY_URL}</code></p>\n"
        "  </div>\n"
        "</body>\n"
        "</html>\n"
    )
    return html.encode("utf-8")


async def _relay_streams(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (asyncio.CancelledError, ConnectionError, EOFError, OSError):
        pass
    finally:
        try:
            if writer.can_write_eof():
                writer.write_eof()
        except Exception:
            pass


class ChannelsProxyManager:
    """In-process selective loopback proxy + CA + WinINet proxy + WebSocket RPC bridge."""

    def __init__(
        self,
        data_dir: Path | None = None,
        *,
        ca_manager: LocalCaManager | None = None,
        system_proxy_backend: SystemProxyBackend | None = None,
        upstream_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._data_dir = Path(data_dir or "data").expanduser()
        self.ca = ca_manager or LocalCaManager(self._data_dir)
        self._proxy_backend = system_proxy_backend
        self._upstream_transport = upstream_transport
        self._ws_token = self._load_or_create_ws_token()

        self._proxy_port: int = 2023
        self._bound_port: int | None = None
        self._proxy_server: asyncio.AbstractServer | None = None
        self._ws_server: WsServer | None = None
        self._ws_hub_port: int | None = None
        self._upstream_client: httpx.AsyncClient | AsyncSession | None = None
        self._client_tasks: set[asyncio.Task[Any]] = set()

        self._pages: dict[str, ConnectedFinderPage] = {}
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}

        self._system_proxy_managed: bool = False
        self._upstream_proxy: dict[str, Any] = {
            "enabled": False,
            "host": "",
            "port": 0,
            "server": "",
            "note": "",
        }
        self._started_at: float | None = None
        self._last_error: str = ""
        self._commands_forwarded: int = 0
        self._commands_succeeded: int = 0
        self._connect_intercepted: int = 0
        self._connect_passthrough: int = 0
        self._html_injected: int = 0
        self._tls_handshake_failed: int = 0
        self._wechat_traffic_seen: bool = False
        self._liteapp_detected: bool = False
        self._wechat_preexisting: bool = False
        self._cached_finder_module_url: str = ""
        self._cached_finder_module_at: float = 0.0
        self._lock = asyncio.Lock()

    def _ws_token_path(self) -> Path:
        return (self._data_dir / _WS_TOKEN_FILENAME).resolve()

    def _load_or_create_ws_token(self) -> str:
        """Persist local WS bridge token across restarts so minimized WeChat bridge tabs auto-reconnect."""
        token_path = self._ws_token_path()
        try:
            if token_path.is_file():
                existing = token_path.read_text(encoding="ascii", errors="ignore").strip()
                if len(existing) >= 24 and re.fullmatch(r"[A-Za-z0-9_-]+", existing):
                    return existing
        except OSError:
            pass
        fresh = secrets.token_urlsafe(24)
        try:
            _write_private_file(token_path, fresh.encode("ascii"))
        except OSError:
            pass
        return fresh

    def clear_wechat_preexisting(self) -> None:
        self._wechat_preexisting = False

    def configure_data_dir(self, data_dir: Path) -> None:
        new_dir = Path(data_dir).expanduser()
        changed = new_dir != self._data_dir
        self._data_dir = new_dir
        self.ca.configure_data_dir(self._data_dir)
        if changed and not self.running:
            self._ws_token = self._load_or_create_ws_token()

    @property
    def running(self) -> bool:
        return bool(
            self._proxy_server is not None
            and self._proxy_server.is_serving()
            and self._ws_server is not None
        )

    @property
    def bound_port(self) -> int | None:
        return self._bound_port

    def _build_bridge_bundle(self) -> bytes:
        bootstrap = _BOOTSTRAP_JS_PATH.read_text(encoding="utf-8")
        bridge = _BRIDGE_JS_PATH.read_text(encoding="utf-8")
        cfg_json = json.dumps(
            {
                "wsPath": BRIDGE_WS_PATH,
                "token": self._ws_token,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        configured_bridge = bridge.replace("/*__CREATORHUB_PROXY_CONFIG__*/ null", cfg_json, 1)
        return (bootstrap + "\n" + configured_bridge).encode("utf-8")

    def _upstream_proxy_url(self) -> str | None:
        up = self._upstream_proxy
        if not up.get("enabled"):
            return None
        up_host = str(up.get("host") or "")
        up_port = int(up.get("port") or 0)
        if not up_host or not up_port or up_port == self._bound_port:
            raise ConnectionError(
                f"Upstream proxy is enabled ({up_host}:{up_port}) but invalid or matches bound port"
            )
        return f"http://{up_host}:{up_port}"

    async def _send_upstream_request(
        self,
        method: str,
        url: str,
        *,
        headers: Any,
        body: bytes | None = None,
        timeout: float | None = None,
    ) -> Any:
        assert self._upstream_client is not None
        extra: dict[str, Any] = {"timeout": timeout} if timeout is not None else {}
        if isinstance(self._upstream_client, httpx.AsyncClient):
            return await self._upstream_client.request(
                method,
                url,
                headers=headers,
                content=body,
                **extra,
            )
        return await self._upstream_client.request(
            method,
            url,
            headers=headers,
            data=body,
            proxy=self._upstream_proxy_url(),
            allow_redirects=False,
            discard_cookies=True,
            **extra,
        )

    async def _discover_upstream_finder_module_url(self) -> str:
        now = time.time()
        if self._cached_finder_module_url and (now - self._cached_finder_module_at) < 1800:
            return self._cached_finder_module_url
        if self._upstream_client is None:
            return ""
        try:
            resp = await self._send_upstream_request(
                "GET",
                "https://channels.weixin.qq.com/web/pages/home",
                headers={
                    "User-Agent": _SYNTHETIC_WECHAT_UA,
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Encoding": "gzip, deflate",
                },
                timeout=4.0,
            )
            if resp.status_code == 200 and "text/html" in resp.headers.get("content-type", "").lower():
                match = _FINDER_MODULE_RE.search(resp.text)
                if match:
                    raw_url = match.group(1)
                    if raw_url.startswith("//"):
                        raw_url = "https:" + raw_url
                    self._cached_finder_module_url = raw_url
                    self._cached_finder_module_at = now
                    return raw_url
        except Exception:
            pass
        return self._cached_finder_module_url

    def status(self) -> dict[str, Any]:
        ca_status = self.ca.trust_status()
        sys_proxy = get_system_proxy_state(data_dir=self._data_dir, backend=self._proxy_backend)
        pages = [
            page.to_public_dict()
            for page in sorted(
                self._pages.values(),
                key=lambda item: (item.ready, item.has_weixin_js_bridge, item.last_seen),
                reverse=True,
            )
        ]
        ready_pages = [p for p in pages if p["ready"]]
        if not self.running:
            state = "stopped"
        elif ready_pages:
            state = "ready"
        elif pages:
            state = "initializing"
        else:
            state = "waiting_wechat"

        wechat_procs = discover_wechat_processes()
        wechat_running = any(
            str(p.get("name") or "").lower() in {"weixin.exe", "wechat.exe"}
            for p in wechat_procs
        )
        wechat_needs_restart = bool(
            self.running
            and wechat_running
            and self._wechat_preexisting
            and not self._wechat_traffic_seen
            and not ready_pages
        )

        return {
            "running": self.running,
            "state": state,
            "listen_host": "127.0.0.1",
            "proxy_port": int(self._bound_port or self._proxy_port),
            "system_proxy_managed": self._system_proxy_managed,
            "system_proxy": sys_proxy,
            "upstream_proxy": dict(self._upstream_proxy),
            "ca": ca_status,
            "connected_pages": len(pages),
            "ready_pages": len(ready_pages),
            "pages": pages,
            "commands_forwarded": self._commands_forwarded,
            "commands_succeeded": self._commands_succeeded,
            "connect_intercepted": self._connect_intercepted,
            "connect_passthrough": self._connect_passthrough,
            "html_injected": self._html_injected,
            "tls_handshake_failed": self._tls_handshake_failed,
            "wechat_traffic_seen": self._wechat_traffic_seen,
            "liteapp_detected": self._liteapp_detected,
            "wechat_running": wechat_running,
            "wechat_needs_restart": wechat_needs_restart,
            "bridge_entry_url": DEFAULT_BRIDGE_ENTRY_URL,
            "alt_bridge_entry_url": ALT_BRIDGE_ENTRY_URL,
            "started_at": self._started_at,
            "last_error": self._last_error,
        }

    async def start(
        self,
        *,
        proxy_port: int = 2023,
        enable_system_proxy_flag: bool = True,
    ) -> dict[str, Any]:
        async with self._lock:
            port = int(proxy_port)
            if port != 0 and not (1024 <= port <= 65535):
                raise ValueError("代理端口必须在 1024-65535 之间（或测试用 0）")

            ca_status = self.ca.trust_status()
            if enable_system_proxy_flag and not ca_status.get("trusted"):
                self.ca.ensure_ca()

            if self.running:
                if port == self._proxy_port or (port != 0 and port == self._bound_port):
                    if enable_system_proxy_flag and not self._system_proxy_managed:
                        active_port = int(self._bound_port or self._proxy_port)
                        self._upstream_proxy = await asyncio.to_thread(
                            resolve_upstream_proxy,
                            data_dir=self._data_dir,
                            proxy_port=active_port,
                            backend=self._proxy_backend,
                        )
                        sys_res = enable_system_proxy(
                            host="127.0.0.1",
                            port=active_port,
                            data_dir=self._data_dir,
                            backend=self._proxy_backend,
                        )
                        if not sys_res.get("ok"):
                            self._last_error = str(sys_res.get("error") or "")
                        else:
                            self._system_proxy_managed = True
                    return self.status()
                await self._stop_unlocked()

            self._last_error = ""
            self._ws_token = self._load_or_create_ws_token()
            self._proxy_port = port
            self._wechat_traffic_seen = False
            self._liteapp_detected = False
            pre_procs = await asyncio.to_thread(discover_wechat_processes)
            self._wechat_preexisting = any(
                str(p.get("name") or "").lower() in {"weixin.exe", "wechat.exe"}
                for p in pre_procs
            )

            # Pre-generate CA & leaf SSL context for channels.weixin.qq.com before listening.
            self.ca.ssl_context_for_host("channels.weixin.qq.com")

            if self._upstream_client is None:
                if self._upstream_transport is not None:
                    self._upstream_client = httpx.AsyncClient(
                        trust_env=False,
                        follow_redirects=False,
                        timeout=30.0,
                        transport=self._upstream_transport,
                    )
                else:
                    self._upstream_client = AsyncSession(
                        impersonate="chrome131",
                        trust_env=False,
                        allow_redirects=False,
                        discard_cookies=True,
                        default_headers=False,
                        timeout=30.0,
                    )

            try:
                self._ws_server = await ws_serve(
                    self._handle_ws_client,
                    "127.0.0.1",
                    0,
                    max_size=16 * 1024 * 1024,
                    compression=None,
                )
                ws_sockets = getattr(self._ws_server, "sockets", None) or []
                if not ws_sockets:
                    raise RuntimeError("内部 WebSocket Hub 未能绑定端口")
                self._ws_hub_port = int(ws_sockets[0].getsockname()[1])

                try:
                    self._proxy_server = await asyncio.start_server(
                        self._spawn_client_handler,
                        host="127.0.0.1",
                        port=port,
                    )
                except OSError as bind_err:
                    import errno

                    if enable_system_proxy_flag and port != 0 and bind_err.errno in {errno.EADDRINUSE, 10048}:
                        self._proxy_server = await asyncio.start_server(
                            self._spawn_client_handler,
                            host="127.0.0.1",
                            port=0,
                        )
                    else:
                        raise
                proxy_sockets = self._proxy_server.sockets or []
                if not proxy_sockets:
                    raise RuntimeError("本地 HTTPS 代理服务器未能绑定端口")
                self._bound_port = int(proxy_sockets[0].getsockname()[1])
                self._started_at = time.time()

                # Snapshot pre-existing local HTTP proxy before applying CreatorHub's WinINet setting.
                self._upstream_proxy = await asyncio.to_thread(
                    resolve_upstream_proxy,
                    data_dir=self._data_dir,
                    proxy_port=self._bound_port,
                    backend=self._proxy_backend,
                )

                if enable_system_proxy_flag:
                    sys_res = enable_system_proxy(
                        host="127.0.0.1",
                        port=self._bound_port,
                        data_dir=self._data_dir,
                        backend=self._proxy_backend,
                    )
                    if not sys_res.get("ok"):
                        self._last_error = str(sys_res.get("error") or "系统代理启用失败")
                        await self._stop_unlocked()
                        raise ChannelsProtocolTransportError(self._last_error)
                    self._system_proxy_managed = True
            except Exception as exc:
                logger.exception("Channels proxy start failed")
                self._last_error = sanitize_bridge_error(exc)
                await self._stop_unlocked()
                raise

            return self.status()

    async def stop(self) -> dict[str, Any]:
        async with self._lock:
            await self._stop_unlocked()
            return self.status()

    async def _stop_unlocked(self) -> None:
        restore_error = ""
        try:
            expected = self._bound_port or self._proxy_port
            res = restore_system_proxy(
                data_dir=self._data_dir,
                expected_port=expected,
                backend=self._proxy_backend,
            )
            if not res.get("ok"):
                restore_error = str(res.get("error") or "")
        except Exception as exc:
            restore_error = sanitize_bridge_error(exc)
        finally:
            self._system_proxy_managed = False
            self._upstream_proxy = {
                "enabled": False,
                "host": "",
                "port": 0,
                "server": "",
                "note": "",
            }

        for req_id, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(ChannelsProtocolTransportError("本地视频号代理已停止"))
            self._pending.pop(req_id, None)

        for page in list(self._pages.values()):
            try:
                await page.websocket.close(code=1001, reason="proxy stopped")
            except Exception:
                pass
        self._pages.clear()

        if self._proxy_server is not None:
            self._proxy_server.close()
            try:
                await asyncio.wait_for(self._proxy_server.wait_closed(), timeout=3.0)
            except Exception:
                pass
            self._proxy_server = None
        self._bound_port = None

        for task in list(self._client_tasks):
            task.cancel()
        if self._client_tasks:
            await asyncio.gather(*self._client_tasks, return_exceptions=True)
        self._client_tasks.clear()

        if self._ws_server is not None:
            self._ws_server.close()
            try:
                await asyncio.wait_for(self._ws_server.wait_closed(), timeout=3.0)
            except Exception:
                pass
            self._ws_server = None
        self._ws_hub_port = None

        if self._upstream_client is not None:
            try:
                if hasattr(self._upstream_client, "aclose"):
                    await self._upstream_client.aclose()
                else:
                    await self._upstream_client.close()
            except Exception:
                pass
            self._upstream_client = None

        self._started_at = None
        self._wechat_traffic_seen = False
        self._liteapp_detected = False
        self._wechat_preexisting = False
        if restore_error:
            self._last_error = restore_error

    def _select_page(self) -> ConnectedFinderPage | None:
        if not self._pages:
            return None
        candidates = sorted(
            self._pages.values(),
            key=lambda item: (
                item.ready,
                item.has_weixin_js_bridge,
                any(
                    marker in item.url
                    for marker in ("/web/pages/", "/platform/bridge", "/__creatorhub_bridge")
                ),
                "/web/pages/login" not in item.url,
                item.last_seen,
            ),
            reverse=True,
        )
        return candidates[0] if candidates else None

    async def call_method(
        self,
        method: str,
        payload: dict[str, Any],
        *,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        if method not in ALLOWED_BRIDGE_METHODS:
            raise ChannelsProtocolTransportError(f"不支持的视频号代理 RPC 方法: {method}")
        if not self.running:
            raise ChannelsProtocolTransportError("本地视频号代理尚未启动，请先在设置中启动代理")
        page = self._select_page()
        if page is None:
            raise ChannelsProtocolTransportError(
                f"本地代理运行中，但尚未检测到微信视频号页面连接；请在桌面微信「文件传输助手」发送并点开一次 {DEFAULT_BRIDGE_ENTRY_URL}（或旧版微信打开视频号网页）后再试"
            )

        req_id = f"rpc-{secrets.token_hex(8)}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[req_id] = fut
        self._commands_forwarded += 1
        try:
            await page.websocket.send(
                json.dumps(
                    {
                        "type": "call",
                        "id": req_id,
                        "method": method,
                        "payload": payload or {},
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
            result = await asyncio.wait_for(fut, timeout=timeout)
            self._commands_succeeded += 1
            return result
        except asyncio.TimeoutError as exc:
            self._last_error = f"代理调用 {method} 超时 ({timeout:.0f}s)"
            raise ChannelsProtocolTransportError(self._last_error) from exc
        except ChannelsProtocolTransportError as exc:
            self._last_error = str(exc)
            raise
        except Exception as exc:
            msg = sanitize_bridge_error(exc)
            self._last_error = msg
            raise ChannelsProtocolTransportError(f"代理调用 {method} 失败: {msg}") from exc
        finally:
            self._pending.pop(req_id, None)

    def _waiting_page_hint(self, *, ca_trusted: bool) -> str:
        if not ca_trusted:
            return "请先安装并信任本地 CA 根证书，然后重启桌面微信并点开桥接链接"
        if self._liteapp_detected:
            return (
                f"已检测到微信 4.2+ LiteApp 视频号流量！新版微信侧边栏视频号不再加载网页，"
                f"请复制桥接链接 {DEFAULT_BRIDGE_ENTRY_URL} 发送到微信「文件传输助手」并在微信内点击打开一次"
            )
        if not self._wechat_traffic_seen:
            return (
                f"尚未收到微信流量：若开启代理前微信已在运行，请先彻底退出并重开微信（微信启动时缓存代理），"
                f"然后在微信「文件传输助手」发送并点开 {DEFAULT_BRIDGE_ENTRY_URL}"
            )
        return (
            f"请在微信「文件传输助手」发送并点开桥接链接 {DEFAULT_BRIDGE_ENTRY_URL}"
            f"（或旧版微信打开视频号主页/搜索页）以建立页面桥接"
        )

    async def probe(self, timeout: float = 8.0) -> dict[str, Any]:
        if not self.running:
            return {
                "ok": False,
                "ready": False,
                "transport": "channels-local-proxy",
                "error": "内置视频号代理尚未启动，请先点击「启动代理并应用」",
            }
        page = self._select_page()
        if page is None:
            ca_trusted = bool(self.ca.trust_status().get("trusted"))
            hint = self._waiting_page_hint(ca_trusted=ca_trusted)
            return {
                "ok": False,
                "ready": False,
                "transport": "channels-local-proxy",
                "connected_pages": 0,
                "wechat_traffic_seen": self._wechat_traffic_seen,
                "liteapp_detected": self._liteapp_detected,
                "bridge_entry_url": DEFAULT_BRIDGE_ENTRY_URL,
                "error": f"代理已监听 127.0.0.1:{self._bound_port or self._proxy_port}，等待微信视频号页面接入（{hint}）",
            }

        req_id = f"probe-{secrets.token_hex(6)}"
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[req_id] = fut
        try:
            await page.websocket.send(
                json.dumps({"type": "probe", "id": req_id}, separators=(",", ":"))
            )
            res = await asyncio.wait_for(fut, timeout=timeout)
            page.ready = bool(res.get("ready"))
            page.origin = str(res.get("origin") or page.origin)
            page.reason = str(res.get("reason") or "")
            page.capabilities = normalize_capabilities(res.get("capabilities")) or page.capabilities
            page.url = str(res.get("url") or page.url)
            page.title = str(res.get("title") or page.title)
            if "hasWeixinJSBridge" in res:
                page.has_weixin_js_bridge = bool(res.get("hasWeixinJSBridge"))
            page.last_seen = time.time()
            not_ready_hint = (
                "当前页面缺少微信 WeixinJSBridge（请勿在外部浏览器打开，需在电脑微信内点击桥接链接）"
                if not page.ready and not page.has_weixin_js_bridge
                else (res.get("reason") or "页面尚未加载视频号运行时模块")
            )
            return {
                "ok": bool(res.get("ready")),
                "ready": bool(res.get("ready")),
                "transport": "channels-local-proxy",
                "url": _sanitize_page_url(page.url),
                "title": page.title,
                "origin": page.origin,
                "capabilities": page.capabilities,
                "has_weixin_js_bridge": page.has_weixin_js_bridge,
                "connected_pages": len(self._pages),
                "error": "" if res.get("ready") else not_ready_hint,
            }
        except Exception as exc:
            return {
                "ok": False,
                "ready": False,
                "transport": "channels-local-proxy",
                "url": _sanitize_page_url(page.url),
                "connected_pages": len(self._pages),
                "error": f"探测已连接视频号页面失败: {sanitize_bridge_error(exc)}",
            }
        finally:
            self._pending.pop(req_id, None)

    async def _handle_ws_client(self, ws: ServerConnection) -> None:
        page_id = f"ws-{secrets.token_hex(6)}"
        registered_id: str | None = None
        try:
            async for raw in ws:
                if not isinstance(raw, str):
                    continue
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if not isinstance(msg, dict):
                    continue
                msg_type = str(msg.get("type") or "")
                if msg_type in {"hello", "pong"}:
                    pid = str(msg.get("pageId") or page_id)
                    if registered_id and registered_id != pid:
                        self._pages.pop(registered_id, None)
                    registered_id = pid
                    existing = self._pages.get(pid)
                    if existing is None:
                        existing = ConnectedFinderPage(page_id=pid, websocket=ws)
                        self._pages[pid] = existing
                    existing.websocket = ws
                    existing.url = str(msg.get("url") or existing.url)
                    existing.title = str(msg.get("title") or existing.title)
                    existing.ready = bool(msg.get("ready"))
                    existing.origin = str(msg.get("origin") or "")
                    existing.reason = str(msg.get("reason") or "")
                    existing.capabilities = normalize_capabilities(msg.get("capabilities"))
                    existing.has_weixin_js_bridge = bool(msg.get("hasWeixinJSBridge"))
                    if existing.has_weixin_js_bridge:
                        self._wechat_traffic_seen = True
                    existing.last_seen = time.time()
                elif msg_type == "bye":
                    pid = str(msg.get("pageId") or registered_id or "")
                    if pid:
                        self._pages.pop(pid, None)
                    break
                elif msg_type == "result":
                    req_id = str(msg.get("id") or "")
                    fut = self._pending.get(req_id)
                    if fut is not None and not fut.done():
                        if msg.get("ok"):
                            result = msg.get("result")
                            if not isinstance(result, dict):
                                fut.set_exception(
                                    ChannelsProtocolTransportError("视频号页面代理返回了非对象结果")
                                )
                            else:
                                fut.set_result(result)
                        else:
                            fut.set_exception(
                                ChannelsProtocolTransportError(
                                    str(msg.get("error") or "视频号页面执行协议请求失败")
                                )
                            )
        except ConnectionClosed:
            pass
        except Exception as exc:
            logger.warning("Channels proxy WebSocket client error (%s): %s", registered_id or page_id, exc)
        finally:
            if registered_id and self._pages.get(registered_id, None) and self._pages[registered_id].websocket is ws:
                self._pages.pop(registered_id, None)

    def _spawn_client_handler(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.create_task(self._handle_proxy_connection(reader, writer))
        self._client_tasks.add(task)
        task.add_done_callback(self._client_tasks.discard)

    async def _handle_proxy_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            head_bytes = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=15.0)
        except Exception:
            await self._close_writer(writer)
            return

        if len(head_bytes) > 65536:
            await self._close_writer(writer)
            return

        lines = head_bytes.decode("latin-1", errors="replace").split("\r\n")
        request_line = lines[0] if lines else ""
        parts = request_line.split(" ")
        if len(parts) < 3:
            await self._close_writer(writer)
            return
        method, target, version = parts[0].upper(), parts[1], parts[2]
        headers_raw: list[tuple[bytes, bytes]] = []
        for line in lines[1:]:
            if not line or ":" not in line:
                continue
            k, _, v = line.partition(":")
            headers_raw.append((k.strip().encode("latin-1"), v.strip().encode("latin-1")))

        if method == "CONNECT":
            await self._handle_connect(target, reader, writer, headers_raw=headers_raw)
            return

        # Plain HTTP request through proxy (e.g. non-HTTPS resources) — forward without JS injection.
        await self._handle_plain_http(method, target, headers_raw, reader, writer, version=version)

    async def _open_passthrough_connection(
        self,
        host: str,
        port: int,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Connect to target via pre-existing local HTTP proxy when active; fail-closed if upstream fails."""
        is_loopback = host in {"127.0.0.1", "localhost", "::1"}
        up = self._upstream_proxy
        if not is_loopback and up.get("enabled"):
            up_host = str(up.get("host") or "")
            up_port = int(up.get("port") or 0)
            if not up_host or not up_port or up_port == self._bound_port:
                raise ConnectionError(
                    f"Upstream proxy is enabled ({up_host}:{up_port}) but invalid or matches bound port"
                )
            up_writer: asyncio.StreamWriter | None = None
            try:
                up_reader, up_writer = await asyncio.wait_for(
                    asyncio.open_connection(up_host, up_port),
                    timeout=4.0,
                )
                authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
                connect_req = (
                    f"CONNECT {authority} HTTP/1.1\r\n"
                    f"Host: {authority}\r\n"
                    f"Proxy-Connection: Keep-Alive\r\n\r\n"
                ).encode("latin-1")
                up_writer.write(connect_req)
                await up_writer.drain()
                resp_head = await asyncio.wait_for(up_reader.readuntil(b"\r\n\r\n"), timeout=10.0)
                status_line = resp_head.split(b"\r\n", 1)[0]
                if b" 200 " in status_line or status_line.endswith(b" 200"):
                    return up_reader, up_writer
                await self._close_writer(up_writer)
                up_writer = None
                raise ConnectionError(
                    f"Upstream proxy {up_host}:{up_port} rejected CONNECT to {authority}: "
                    f"{status_line.decode('latin-1', errors='replace')}"
                )
            except ConnectionError:
                await self._close_writer(up_writer)
                raise
            except Exception as exc:
                await self._close_writer(up_writer)
                raise ConnectionError(
                    f"Upstream proxy {up_host}:{up_port} connection failed for {host}:{port}: {exc}"
                ) from exc

        return await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=15.0,
        )

    async def _handle_connect(
        self,
        authority: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        headers_raw: list[tuple[bytes, bytes]] | None = None,
    ) -> None:
        host, port = _parse_host_port(authority, default_port=443)
        host_lower = host.lower()
        if not host_lower or (
            host_lower in {"127.0.0.1", "localhost", "::1"} and port == self._bound_port
        ):
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            await self._close_writer(writer)
            return

        if headers_raw:
            for k, v in headers_raw:
                if k.lower() == b"user-agent":
                    ua_low = v.lower()
                    if any(marker in ua_low for marker in _WECHAT_UA_MARKERS):
                        self._wechat_traffic_seen = True
                    break

        if host_lower == "liteapp.weixin.qq.com":
            self._wechat_traffic_seen = True
            self._liteapp_detected = True
        elif host_lower in _WECHAT_TRAFFIC_HOSTS:
            self._wechat_traffic_seen = True

        if host_lower in ALLOWED_INTERCEPT_HOSTS and port == 443:
            self._connect_intercepted += 1
            await self._handle_intercepted_tls(host_lower, reader, writer)
            return

        self._connect_passthrough += 1
        try:
            up_reader, up_writer = await self._open_passthrough_connection(host, port)
        except Exception:
            try:
                writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
            except Exception:
                pass
            await self._close_writer(writer)
            return

        try:
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            await asyncio.gather(
                _relay_streams(reader, up_writer),
                _relay_streams(up_reader, writer),
            )
        finally:
            await self._close_writer(up_writer)
            await self._close_writer(writer)

    async def _handle_intercepted_tls(
        self,
        hostname: str,
        raw_reader: asyncio.StreamReader,
        raw_writer: asyncio.StreamWriter,
    ) -> None:
        tls_writer: asyncio.StreamWriter | None = None
        try:
            raw_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await raw_writer.drain()

            loop = asyncio.get_running_loop()
            tls_reader = asyncio.StreamReader(limit=512 * 1024)
            tls_protocol = asyncio.StreamReaderProtocol(tls_reader)
            ssl_ctx = self.ca.ssl_context_for_host(hostname)
            tls_transport = await loop.start_tls(
                raw_writer.transport,
                tls_protocol,
                ssl_ctx,
                server_side=True,
                ssl_handshake_timeout=15.0,
            )
            tls_writer = asyncio.StreamWriter(tls_transport, tls_protocol, tls_reader, loop)
        except Exception as exc:
            self._tls_handshake_failed += 1
            logger.warning("TLS intercept handshake failed for %s: %s", hostname, sanitize_bridge_error(exc))
            await self._close_writer(tls_writer or raw_writer)
            return

        try:
            await self._serve_h11_over_tls(hostname, tls_reader, tls_writer)
        finally:
            await self._close_writer(tls_writer)

    async def _serve_h11_over_tls(
        self,
        hostname: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        conn = h11.Connection(h11.SERVER)
        while True:
            event = await self._next_h11_event(conn, reader)
            if event is None or isinstance(event, h11.ConnectionClosed):
                return
            if not isinstance(event, h11.Request):
                return

            req_headers = list(event.headers.raw_items())
            header_map = {k.lower(): v for k, v in req_headers}
            req_body = bytearray()
            while True:
                body_ev = await self._next_h11_event(conn, reader)
                if isinstance(body_ev, h11.Data):
                    req_body.extend(body_ev.data)
                elif isinstance(body_ev, h11.EndOfMessage):
                    break
                else:
                    return

            ua_bytes = header_map.get(b"user-agent", b"").lower()
            if any(marker in ua_bytes for marker in _WECHAT_UA_MARKERS):
                self._wechat_traffic_seen = True

            target = event.target.decode("latin-1", errors="replace")
            parsed = urlsplit(target)
            path = (parsed.path or "/").rstrip("/") or "/"
            raw_path = parsed.path or "/"

            if raw_path.startswith("/web/report-perf") and "pf=lite" in (parsed.query or "").lower():
                self._wechat_traffic_seen = True
                self._liteapp_detected = True

            if event.method == b"GET" and raw_path == BRIDGE_SCRIPT_PATH:
                bundle = self._build_bridge_bundle()
                resp_headers = [
                    (b"Content-Type", b"application/javascript; charset=utf-8"),
                    (b"Cache-Control", b"no-store"),
                    (b"Content-Length", str(len(bundle)).encode("ascii")),
                ]
                await self._send_h11_response(conn, writer, 200, resp_headers, bundle)
                if conn.our_state is h11.MUST_CLOSE:
                    return
                conn.start_next_cycle()
                continue

            if event.method in {b"GET", b"HEAD"} and path in BRIDGE_LANDING_PATHS:
                mod_url = await self._discover_upstream_finder_module_url()
                script_url = f"{BRIDGE_SCRIPT_PATH}?v={int(time.time())}"
                landing = _build_bridge_landing_html(script_url, module_preload_url=mod_url)
                self._html_injected += 1
                resp_headers = [
                    (b"Content-Type", b"text/html; charset=utf-8"),
                    (b"Cache-Control", b"no-store, no-cache, must-revalidate"),
                    (b"Content-Length", str(len(landing)).encode("ascii")),
                ]
                await self._send_h11_response(
                    conn,
                    writer,
                    200,
                    resp_headers,
                    landing,
                    is_head=(event.method == b"HEAD"),
                )
                if conn.our_state is h11.MUST_CLOSE:
                    return
                conn.start_next_cycle()
                continue

            if raw_path == BRIDGE_WS_PATH and b"websocket" in header_map.get(b"upgrade", b"").lower():
                query_params = parse_qs(parsed.query)
                client_token = (query_params.get("token") or [""])[0]
                if not self._ws_token or not secrets.compare_digest(client_token, self._ws_token):
                    forbidden = b"Forbidden"
                    await self._send_h11_response(
                        conn,
                        writer,
                        403,
                        [(b"Content-Length", str(len(forbidden)).encode("ascii"))],
                        forbidden,
                    )
                    return
                trailing, _ = conn.trailing_data
                await self._tunnel_ws_upgrade(
                    target=target,
                    headers=req_headers,
                    trailing=bytes(trailing),
                    client_reader=reader,
                    client_writer=writer,
                )
                return

            close_after = b"close" in header_map.get(b"connection", b"").lower()
            await self._forward_intercepted_http(
                conn=conn,
                writer=writer,
                hostname=hostname,
                method=event.method.decode("ascii", errors="replace"),
                target=target,
                req_headers=req_headers,
                req_body=bytes(req_body),
                close_after=close_after,
            )
            if close_after or conn.our_state is h11.MUST_CLOSE:
                return
            conn.start_next_cycle()

    async def _next_h11_event(
        self,
        conn: h11.Connection,
        reader: asyncio.StreamReader,
    ) -> Any:
        while True:
            ev = conn.next_event()
            if ev is not h11.NEED_DATA:
                return ev
            try:
                data = await asyncio.wait_for(reader.read(65536), timeout=30.0)
            except Exception:
                return None
            if not data:
                conn.receive_data(b"")
                return conn.next_event()
            conn.receive_data(data)

    async def _send_h11_response(
        self,
        conn: h11.Connection,
        writer: asyncio.StreamWriter,
        status_code: int,
        headers: list[tuple[bytes, bytes]],
        body: bytes,
        *,
        is_head: bool = False,
    ) -> None:
        out = bytearray()
        out.extend(conn.send(h11.Response(status_code=status_code, headers=headers)))
        if not is_head and status_code not in {204, 304} and not (100 <= status_code < 200):
            if body:
                out.extend(conn.send(h11.Data(data=body)))
        out.extend(conn.send(h11.EndOfMessage()))
        writer.write(out)
        await writer.drain()

    async def _tunnel_ws_upgrade(
        self,
        *,
        target: str,
        headers: list[tuple[bytes, bytes]],
        trailing: bytes,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
    ) -> None:
        if self._ws_hub_port is None:
            return
        try:
            hub_reader, hub_writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self._ws_hub_port),
                timeout=5.0,
            )
        except Exception:
            return

        req_lines = [f"GET {target} HTTP/1.1\r\n".encode("latin-1")]
        for k, v in headers:
            req_lines.append(k + b": " + v + b"\r\n")
        req_lines.append(b"\r\n")
        try:
            hub_writer.write(b"".join(req_lines) + trailing)
            await hub_writer.drain()
            await asyncio.gather(
                _relay_streams(client_reader, hub_writer),
                _relay_streams(hub_reader, client_writer),
            )
        finally:
            await self._close_writer(hub_writer)

    async def _forward_intercepted_http(
        self,
        *,
        conn: h11.Connection,
        writer: asyncio.StreamWriter,
        hostname: str,
        method: str,
        target: str,
        req_headers: list[tuple[bytes, bytes]],
        req_body: bytes,
        close_after: bool,
    ) -> None:
        assert self._upstream_client is not None
        url = f"https://{hostname}{target}" if target.startswith("/") else target

        ua_bytes = b""
        accept_header = ""
        for k, v in req_headers:
            kl = k.lower()
            if kl == b"user-agent":
                ua_bytes = v.lower()
            elif kl == b"accept":
                accept_header = v.decode("latin-1", errors="ignore").lower()

        parsed_target = urlsplit(target)
        norm_path = (parsed_target.path or "/").rstrip("/") or "/"
        is_wechat_ua = any(marker in ua_bytes for marker in _WECHAT_UA_MARKERS)
        should_inject_bridge = norm_path in BRIDGE_LANDING_PATHS or is_wechat_ua
        likely_html_nav = (
            should_inject_bridge
            and method.upper() in {"GET", "HEAD"}
            and ("text/html" in accept_header or target.startswith("/web/pages/"))
        )

        forward_headers: list[tuple[str, str]] = []
        for k, v in req_headers:
            kl = k.lower()
            if kl in _HOP_BY_HOP_HEADERS or kl in {b"host", b"content-length", b"accept-encoding"}:
                continue
            if likely_html_nav and kl in {b"if-none-match", b"if-modified-since"}:
                continue
            forward_headers.append(
                (k.decode("latin-1", errors="replace"), v.decode("latin-1", errors="replace"))
            )
        # Only advertise gzip/deflate since brotli/zstd may not be installed for httpx transports.
        forward_headers.append(("Accept-Encoding", "gzip, deflate"))

        try:
            resp = await self._send_upstream_request(
                method,
                url,
                headers=forward_headers,
                body=req_body if req_body else None,
            )
        except Exception as exc:
            err_body = f"Upstream proxy error: {sanitize_bridge_error(exc)}".encode("utf-8")
            await self._send_h11_response(
                conn,
                writer,
                502,
                [
                    (b"Content-Type", b"text/plain; charset=utf-8"),
                    (b"Content-Length", str(len(err_body)).encode("ascii")),
                    (b"Connection", b"close"),
                ],
                err_body,
                is_head=(method.upper() == "HEAD"),
            )
            return

        content_type = resp.headers.get("content-type", "").lower()
        body = resp.content
        injected_html = False
        if (
            should_inject_bridge
            and method.upper() == "GET"
            and resp.status_code == 200
            and "text/html" in content_type
        ):
            decoded = resp.text
            mod_match = _FINDER_MODULE_RE.search(decoded)
            if mod_match:
                found_mod = mod_match.group(1)
                self._cached_finder_module_url = (
                    "https:" + found_mod if found_mod.startswith("//") else found_mod
                )
                self._cached_finder_module_at = time.time()
            script_url = f"{BRIDGE_SCRIPT_PATH}?v={int(time.time())}"
            updated = _inject_bridge_script_into_html(decoded, script_url)
            if updated != decoded:
                body = updated.encode("utf-8")
                injected_html = True
                self._html_injected += 1

        out_headers: list[tuple[bytes, bytes]] = []
        has_csp = False
        for k_str, v_str in resp.headers.multi_items():
            kl = k_str.lower().encode("latin-1")
            if kl in _RESPONSE_STRIP_HEADERS:
                continue
            if injected_html and kl in _HTML_INJECTED_STRIP_HEADERS:
                continue
            if kl == b"content-type" and injected_html:
                out_headers.append((b"Content-Type", b"text/html; charset=utf-8"))
                continue
            if injected_html and kl in {
                b"content-security-policy",
                b"content-security-policy-report-only",
            }:
                has_csp = True
                out_headers.append(
                    (k_str.encode("latin-1"), _relax_csp_for_bridge(v_str).encode("latin-1"))
                )
                continue
            out_headers.append((k_str.encode("latin-1"), v_str.encode("latin-1")))

        if injected_html:
            out_headers.append((b"Cache-Control", b"no-store, no-cache, must-revalidate"))
            if not has_csp:
                pass

        is_head = method.upper() == "HEAD"
        if not is_head and resp.status_code not in {204, 304} and not (100 <= resp.status_code < 200):
            out_headers.append((b"Content-Length", str(len(body)).encode("ascii")))
        elif is_head and "content-length" in resp.headers:
            out_headers.append(
                (b"Content-Length", resp.headers["content-length"].encode("ascii", errors="ignore"))
            )
        if close_after:
            out_headers.append((b"Connection", b"close"))

        await self._send_h11_response(
            conn,
            writer,
            resp.status_code,
            out_headers,
            body,
            is_head=is_head,
        )

    async def _handle_plain_http(
        self,
        method: str,
        target: str,
        headers_raw: list[tuple[bytes, bytes]],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        version: str,
    ) -> None:
        if self._upstream_client is None or not target.lower().startswith("http://"):
            try:
                writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
            except Exception:
                pass
            await self._close_writer(writer)
            return

        content_length = 0
        forward_headers: list[tuple[str, str]] = []
        for k, v in headers_raw:
            kl = k.lower()
            if kl == b"content-length":
                try:
                    content_length = max(0, int(v.decode("ascii", errors="ignore")))
                except Exception:
                    content_length = 0
                continue
            if kl in _HOP_BY_HOP_HEADERS:
                continue
            forward_headers.append(
                (k.decode("latin-1", errors="replace"), v.decode("latin-1", errors="replace"))
            )

        body = b""
        if content_length > 0:
            try:
                body = await asyncio.wait_for(reader.readexactly(content_length), timeout=15.0)
            except Exception:
                await self._close_writer(writer)
                return

        try:
            resp = await self._send_upstream_request(
                method,
                target,
                headers=forward_headers,
                body=body or None,
            )
            reason = (
                getattr(resp, "reason_phrase", None)
                or getattr(resp, "reason", None)
                or "OK"
            )
            chunks = [f"{version} {resp.status_code} {reason}\r\n".encode("latin-1")]
            for k_str, v_str in resp.headers.multi_items():
                kl = k_str.lower().encode("latin-1")
                if kl in _RESPONSE_STRIP_HEADERS:
                    continue
                chunks.append(k_str.encode("latin-1") + b": " + v_str.encode("latin-1") + b"\r\n")
            resp_body = b"" if method == "HEAD" else resp.content
            if method != "HEAD" and resp.status_code not in {204, 304}:
                chunks.append(f"Content-Length: {len(resp_body)}\r\n".encode("ascii"))
            chunks.append(b"Connection: close\r\n\r\n")
            writer.write(b"".join(chunks) + resp_body)
            await writer.drain()
        except Exception as exc:
            err = sanitize_bridge_error(exc).encode("utf-8")
            try:
                writer.write(
                    f"HTTP/1.1 502 Bad Gateway\r\nContent-Length: {len(err)}\r\nConnection: close\r\n\r\n".encode(
                        "ascii"
                    )
                    + err
                )
                await writer.drain()
            except Exception:
                pass
        finally:
            await self._close_writer(writer)

    @staticmethod
    async def _close_writer(writer: asyncio.StreamWriter | None) -> None:
        if writer is None:
            return
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
        except Exception:
            pass
