"""抖音 Web 客户端。对应逆向 internal/douyin.NativeClient。
负责:拼接公共参数 + a_bogus 签名 + 携带 Cookie 请求 douyin.com Web API。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import time
import urllib.parse
import uuid
from copy import deepcopy
from http.cookiejar import CookieJar
from http.cookies import CookieError, SimpleCookie
from email.utils import parsedate_to_datetime
from contextlib import asynccontextmanager
from typing import Any, Callable, Dict, List, Optional, Set

from curl_cffi.requests import AsyncSession

import re

from .signing import sign_url, gen_false_ms_token, gen_real_ms_token
from .signing.web_request import (
    UIFID_COOKIES, requires_web_signature, sign_web_query, valid_device_id,
    web_query_headers,
)
from .auth import parse_self_profile_response
from .detail import detail_http_error, normalize_detail_error, parse_detail_response
from .web_session import (
    DETAIL_ORIGINS, DETAIL_PATH, cookie_header, cookie_value,
    observed_detail_context, read_state,
)
from ...netfp import impersonate_for_ua

BASE = "https://www.douyin.com"
WRITE_BASE = "https://www-hj.douyin.com"
IMAPI_SEND_URL = "https://imapi.douyin.com/v1/message/send"
IMAPI_CREATE_URL = "https://imapi.douyin.com/v2/conversation/create"
FOLLOW_MAX_PAGES = 10_000
FOLLOW_PAGE_DELAY_SECONDS = 0.15
SECURE_SDK_WRITE_PATHS = frozenset({
    "/aweme/v1/web/commit/item/digg/",
    "/aweme/v1/web/commit/follow/user/",
    "/aweme/v1/web/comment/publish/",
    "/aweme/v1/web/commit/user/",
    "/aweme/v1/web/danmaku/post_v2/",
})
SEARCH_STREAM_PATH = "/aweme/v1/web/general/search/stream/"
SEARCH_SINGLE_PATH = "/aweme/v1/web/general/search/single/"
SEARCH_PATHS = frozenset({SEARCH_SINGLE_PATH, SEARCH_STREAM_PATH})


def cookie_from_state(storage_state_json: str) -> str:
    """从 Patchright storage_state JSON 提取抖音 Cookie 串(name=value; ...)。
    只取抖音相关域,直连接口(签名 + 该 Cookie)即可请求。"""
    try:
        state = json.loads(storage_state_json or "{}")
    except Exception:
        return ""
    parts = []
    for c in state.get("cookies", []):
        dom = c.get("domain", "")
        if "douyin" in dom or "bytedance" in dom or "amemv" in dom or dom == "":
            name, val = c.get("name"), c.get("value")
            if name and val is not None:
                parts.append(f"{name}={val}")
    return "; ".join(parts)

# douyin Web 公共 query 参数(对应 signing.DefaultRequestParams)
DEFAULT_PARAMS = {
    "device_platform": "webapp",
    "aid": "6383",
    "channel": "channel_pc_web",
    "pc_client_type": "1",
    "version_code": "190600",
    "version_name": "19.6.0",
    "cookie_enabled": "true",
    "screen_width": "1536",
    "screen_height": "864",
    "browser_language": "zh-CN",
    "browser_platform": "Win32",
    "browser_name": "Chrome",
    "browser_version": "130.0.0.0",
    "browser_online": "true",
    "engine_name": "Blink",
    "engine_version": "130.0.0.0",
    "os_name": "Windows",
    "os_version": "10",
    "platform": "PC",
    "downlink": "10",
    "effective_type": "4g",
    "round_trip_time": "150",
}


class DouyinClient:
    def __init__(self, cookie: str, user_agent: str, timeout: float = 20.0,
                 proxy: str = "", *, locale: str = "zh-CN",
                 accept_language: str = "", screen_width: int = 1536,
                 screen_height: int = 864, storage_state: str = "",
                 detail_context: dict | None = None):
        self.cookie = cookie or ""
        self.ua = user_agent
        self.timeout = timeout
        self.proxy = (proxy or "").strip()
        self.locale = str(locale or "zh-CN")
        self.accept_language = (str(accept_language or "").strip()
                                or f"{self.locale},{self.locale.split('-', 1)[0]};q=0.9")
        self.screen_width = max(320, int(screen_width or 1536))
        self.screen_height = max(240, int(screen_height or 864))
        self.impersonate = impersonate_for_ua(user_agent)  # TLS 指纹复刻,绕 JA3 风控
        self._session: AsyncSession | None = None
        self._ms_token: str | None = None
        self._token_source = ""
        self._search_id = ""
        self._storage_state = deepcopy(read_state(storage_state))
        saved_cookies = self._storage_state.get("cookies")
        self._cookies = ([deepcopy(c) for c in saved_cookies if isinstance(c, dict)]
                        if isinstance(saved_cookies, list) else [
            {"name": part.split("=", 1)[0].strip(), "value": part.split("=", 1)[1].strip(),
             "domain": ".douyin.com", "path": "/"}
            for part in self.cookie.split(";") if "=" in part
        ])
        self._account_query = self._account_query_params()
        # The caller verifies this account/session binding; additionally limit
        # the client to supported first-party origins and non-secret fields.
        detail_context = detail_context if isinstance(detail_context, dict) else {}
        self.detail_context = {}
        if (detail_context.get("origin") in DETAIL_ORIGINS
                and detail_context.get("ua") == self.ua):
            self.detail_context = observed_detail_context(
                detail_context["origin"] + DETAIL_PATH + "?" + urllib.parse.urlencode(
                    detail_context.get("params") or {}), detail_context.get("headers") or {},
                ua=self.ua, fingerprint=str(detail_context.get("fingerprint") or ""))
        self.last_response: dict = {}
        self._im_sequence_id = 10000
        self.last_error: str = ""
        # 只有没有拿到可判定的响应时才置 True；上层据此把任务标记为
        # uncertain，绝不以浏览器重试同一条写请求造成重复评论/私信/关注。
        self.last_write_uncertain: bool = False

    @asynccontextmanager
    async def session_scope(self):
        """在一个读取任务内复用 curl 会话、Cookie jar 与 msToken。"""
        if self._session is not None:
            yield self
            return
        self._session = AsyncSession(impersonate=self.impersonate)
        try:
            yield self
        finally:
            session, self._session = self._session, None
            if session is not None:
                await session.close()
            self._ms_token = None

    def _headers(self, referer: str = BASE + "/", *, url: str = BASE + "/") -> Dict[str, str]:
        headers = {
            "User-Agent": self.ua,
            "Referer": referer,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": self.accept_language,
            "Cookie": cookie_header(self._cookies, url),
        }
        parsed = urllib.parse.urlsplit(url)
        if (requires_web_signature(parsed.path)
                and f"{parsed.scheme}://{parsed.netloc}" in DETAIL_ORIGINS):
            for name, value in (self.detail_context.get("headers") or {}).items():
                for previous in list(headers):
                    if previous.lower() == name.lower():
                        headers.pop(previous)
                headers[name] = value
            headers.update(web_query_headers(parsed.query))
        return headers

    @staticmethod
    def _normalized_path(path: str) -> str:
        value = "/" + str(path or "").lstrip("/")
        return value if value.endswith("/") else value + "/"

    def _write_security_headers(self, path: str, url: str) -> Dict[str, str]:
        """Attach deterministic account-bound parts of the web SDK context.

        Douyin's current first-party writer also generates a volatile DTrait
        header in JavaScript.  That value is deliberately not fabricated here;
        a confirmed denial is surfaced so hybrid mode can use the logged-in
        browser, whose official SDK generates it for the real request.
        """
        if self._normalized_path(path) not in SECURE_SDK_WRITE_PATHS:
            return {}
        headers: Dict[str, str] = {}
        device_id = self._web_signature_device_id(path)
        if device_id:
            headers["uifid"] = device_id
        csrf = cookie_value(self._cookies, url, "passport_csrf_token")
        if csrf:
            headers["x-secsdk-csrf-token"] = csrf
        ticket = cookie_value(self._cookies, url, "bd_ticket_guard_client_data")
        if ticket:
            headers["bd-ticket-guard-client-data"] = ticket
        return headers

    def export_storage_state(self) -> dict:
        state = deepcopy(self._storage_state)
        state["cookies"] = deepcopy(self._cookies)
        state.setdefault("origins", [])
        return state

    def replace_storage_state(self, storage_state: dict | str) -> None:
        """Refresh a direct client from a verified first-party browser state."""
        state = deepcopy(read_state(storage_state))
        cookies = state.get("cookies")
        if not isinstance(cookies, list):
            return
        self._storage_state = state
        self._cookies = [deepcopy(c) for c in cookies if isinstance(c, dict)]
        self.cookie = cookie_from_state(json.dumps(state, ensure_ascii=False))
        self._account_query = self._account_query_params()
        self._ms_token = None
        self._token_source = ""
        self._search_id = ""

    def _account_query_params(self) -> Dict[str, str]:
        """Read stable first-party request identity from browser storage."""
        result: Dict[str, str] = {}
        for origin in self._storage_state.get("origins", []):
            if not isinstance(origin, dict) or origin.get("origin") != BASE:
                continue
            values = {
                str(item.get("name") or ""): str(item.get("value") or "")
                for item in origin.get("localStorage", [])
                if isinstance(item, dict)
            }
            try:
                tea = json.loads(values.get("__tea_cache_tokens_6383", "{}"))
            except (TypeError, ValueError):
                tea = {}
            if isinstance(tea, dict):
                webid = str(tea.get("user_unique_id") or tea.get("web_id") or "")
                if webid.isdigit() and len(webid) <= 32:
                    result["webid"] = webid
            break
        visitor = cookie_value(self._cookies, BASE + "/", "s_v_web_id")
        if visitor and len(visitor) <= 256 and not re.search(r"[\s;&]", visitor):
            result.update(verifyFp=visitor, fp=visitor)
        return result

    def _local_storage_value(self, name: str) -> str:
        """Read a value from the saved first-party Douyin LocalStorage."""
        for origin in self._storage_state.get("origins", []):
            if not isinstance(origin, dict) or origin.get("origin") != BASE:
                continue
            for item in origin.get("localStorage", []):
                if isinstance(item, dict) and item.get("name") == name:
                    return str(item.get("value") or "")
        return ""

    def _api_origin(self, path: str) -> str:
        if path == DETAIL_PATH:
            return self.detail_context.get("origin", BASE)
        if self._normalized_path(path) in SECURE_SDK_WRITE_PATHS:
            return WRITE_BASE
        return BASE

    def _web_signature_device_id(self, path: str) -> str:
        url = self._api_origin(path) + path
        for name in UIFID_COOKIES:
            value = cookie_value(self._cookies, url, name)
            if valid_device_id(value):
                return value
        value = ((self.detail_context.get("params") or {}).get("uifid", "")
                 if path == DETAIL_PATH else "")
        return value if valid_device_id(value) else ""

    async def _prepare_ms_token(self, url: str) -> None:
        if self._ms_token is not None:
            return
        token = cookie_value(self._cookies, url, "msToken")
        if token:
            self._ms_token, self._token_source = token, "account_cookie"
            return
        token = self._local_storage_value("xmst")
        if token:
            self._ms_token, self._token_source = token, "account_local_storage_xmst"
            return
        self._ms_token = await gen_real_ms_token(
            self.ua, timeout=min(self.timeout, 10.0),
            # Auth cookies for douyin.com must not be forwarded to the SDK's
            # different domain. Only its own domain-scoped cookies belong here.
            cookie=cookie_header(self._cookies, "https://mssdk.bytedance.com/web/report"),
            proxy=self.proxy)
        self._token_source = getattr(self._ms_token, "source", "sdk_or_fallback")

    def _accept_response_cookies(self, response, url: str) -> None:
        def key(cookie):
            return (cookie.get("name"), str(cookie.get("domain") or "").lstrip(".").lower(),
                    cookie.get("path") or "/")

        jar = getattr(getattr(response, "cookies", None), "jar", None)
        if isinstance(jar, CookieJar):
            for item in jar:
                domain = item.domain or (urllib.parse.urlsplit(url).hostname or "")
                if item.domain_specified and not domain.startswith("."):
                    domain = "." + domain
                same_site = str(item.get_nonstandard_attr("SameSite", "Lax")).title()
                value = {"name": item.name, "value": item.value or "", "domain": domain,
                         "path": item.path or "/", "secure": item.secure,
                         "expires": item.expires if item.expires is not None else -1,
                         "httpOnly": item.has_nonstandard_attr("HttpOnly"),
                         "sameSite": same_site if same_site in {"Strict", "Lax", "None"} else "Lax"}
                self._cookies = [c for c in self._cookies if key(c) != key(value)]
                self._cookies.append(value)
        headers = getattr(response, "headers", {}) or {}
        # Expired Set-Cookie entries may already have been removed from curl's
        # response jar. Honor deletion explicitly rather than reviving the old
        # database cookie on the next request.
        get_list = getattr(headers, "get_list", None)
        lines = get_list("set-cookie") if callable(get_list) else [headers.get("set-cookie", "")]
        parsed_url = urllib.parse.urlsplit(url)
        host = parsed_url.hostname or ""
        for line in lines if isinstance(lines, (list, tuple)) else []:
            if not isinstance(line, str):
                continue
            parsed_cookie = SimpleCookie()
            try:
                parsed_cookie.load(line)
                for name, morsel in parsed_cookie.items():
                    expired = bool(morsel["max-age"]) and int(morsel["max-age"]) <= 0
                    if not morsel["max-age"] and morsel["expires"]:
                        expired = parsedate_to_datetime(morsel["expires"]).timestamp() <= time.time()
                    if not expired:
                        continue
                    domain = (morsel["domain"] or host).lstrip(".").lower()
                    if not (host == domain or host.endswith("." + domain)):
                        continue
                    path = morsel["path"] or parsed_url.path.rpartition("/")[0] or "/"
                    self._cookies = [c for c in self._cookies if key(c) != (name, domain, path)]
                    if name == "msToken":
                        self._ms_token = None
            except (CookieError, ValueError, TypeError, OverflowError):
                continue
        token = headers.get("x-ms-token", "")
        if isinstance(token, str) and token and len(token) < 4096 and not re.search(r"[\s;]", token):
            self._ms_token, self._token_source = token, "response_header"
            # Keep an existing browser cookie's scope and expiry; do not invent
            # an expiry or broaden its domain when the server returns a header.
            from .web_session import cookies_for_url
            existing = next((c for c in cookies_for_url(self._cookies, url)
                             if c["name"] == "msToken"), None)
            if existing is not None:
                existing["value"] = token
        else:
            refreshed = cookie_value(self._cookies, url, "msToken")
            if refreshed and refreshed != self._ms_token:
                self._ms_token, self._token_source = refreshed, "response_cookie"

    def _im_request_options(self, referer: str = BASE + "/") -> Dict[str, Any]:
        self._im_sequence_id += 1
        return {
            "sequence_id": self._im_sequence_id,
            "user_agent": self.ua,
            "locale": self.locale,
            "screen_width": self.screen_width,
            "screen_height": self.screen_height,
            "referer": referer,
        }

    def _build_url(self, path: str, params: Dict[str, Any], *, body: str = "") -> str:
        if path == DETAIL_PATH and self.detail_context:
            # A calibrated request must not retain obsolete defaults that the
            # current first-party endpoint no longer sends.
            q = dict(self.detail_context.get("params") or {})
        else:
            q = dict(DEFAULT_PARAMS)
            # Cookie, UA, locale and viewport belong to the same account.
            q.update({
                "screen_width": str(self.screen_width),
                "screen_height": str(self.screen_height),
                "browser_language": self.locale,
            })
            # Keep query browser versions aligned with the account's UA.
            m = re.search(r"Chrome/(\d+)", self.ua or "")
            if m:
                ver = f"{m.group(1)}.0.0.0"
                q["browser_version"] = ver
                q["engine_version"] = ver
            q.update(self._account_query)
            if self._normalized_path(path) in SECURE_SDK_WRITE_PATHS:
                q.update({
                    "pc_libra_divert": "Windows",
                    "update_version_code": "170400",
                    "support_h265": "0",
                    "support_dash": "0",
                    "version_code": "170400",
                    "version_name": "17.4.0",
                })
        q.update({k: v for k, v in params.items() if v is not None})
        normalized_path = self._normalized_path(path)
        if normalized_path == SEARCH_STREAM_PATH:
            q.pop("msToken", None)
            device_id = self._web_signature_device_id(path)
            if device_id:
                q["uifid"] = device_id
        else:
            q["msToken"] = self._ms_token or gen_false_ms_token()
        if path == DETAIL_PATH:
            for name in ("timestamp", "x-secsdk-web-signature", "uifid", "a_bogus"):
                q.pop(name, None)
            visitor = cookie_value(self._cookies, self._api_origin(path) + path, "s_v_web_id")
            if visitor:
                q.update(verifyFp=visitor, fp=visitor)
        qs = urllib.parse.urlencode(q)
        fingerprint = (self.detail_context.get("fingerprint") if path == DETAIL_PATH else "")
        # Current first-party/open-source search clients send search/single
        # without a_bogus. Keep the signature for all other API families.
        signed = qs if normalized_path in SEARCH_PATHS else sign_url(
            qs, self.ua, body=body,
            fp=fingerprint or self._abogus_fingerprint())
        device_id = self._web_signature_device_id(path)
        if requires_web_signature(path) and device_id:
            signed = sign_web_query(signed, device_id)
        return f"{self._api_origin(path)}{path}?{signed}"

    def _abogus_fingerprint(self) -> str:
        """Build the signer fingerprint from the account's stable viewport."""
        width, height = self.screen_width, self.screen_height
        platform = "MacIntel" if "Macintosh" in self.ua else "Win32"
        return (
            f"{width}|{height}|{width + 24}|{height + 80}|0|0|0|0|"
            f"{width}|{height}|{width}|{height}|{width}|{height}|24|24|"
            f"{platform}"
        )

    async def _get_payloads(self, path: str, params: Dict[str, Any],
                            referer: str = BASE + "/") -> List[dict]:
        self.last_error = ""
        self.last_response = {}
        if requires_web_signature(path) and not self._web_signature_device_id(path):
            self.last_error = "api_signature:missing_uifid"
            self.last_response = {"origin": self._api_origin(path), "path": path,
                                  "rejection_reason": "missing_uifid", "request_sent": False}
            return []
        owned = self._session is None
        cli = self._session or AsyncSession(impersonate=self.impersonate)
        try:
            await self._prepare_ms_token(self._api_origin(path) + path)
            url = self._build_url(path, params)
            request_token_source = self._token_source
            request_headers = self._headers(referer, url=url)
            r = await cli.get(url, headers=request_headers,
                              impersonate=self.impersonate, timeout=self.timeout,
                              proxy=self.proxy or None, allow_redirects=False)
            self._accept_response_cookies(r, url)
            headers = getattr(r, "headers", {}) or {}
            self.last_response = {
                "origin": self._api_origin(path), "path": path,
                "http_status": r.status_code, "body_bytes": len(r.content or b""),
                "content_type": str(headers.get("content-type", ""))[:100],
                "token_source": request_token_source,
                "observed_context_used": bool(path == DETAIL_PATH and self.detail_context),
                "web_signature_attached": "x-secsdk-web-signature" in request_headers,
                "device_header_attached": "uifid" in request_headers,
            }
            if r.status_code != 200 or not r.content:
                # HTTP 200 + 空 body = 被风控拒了(接口本身可能是活的:follower/list 直连
                # 空 body,页面里发同一个请求却正常)。静默 return None 会让上层把「被拒」
                # 和「没有数据」混为一谈 —— 那正是 raw=0 查了半天的原因。
                self.last_error = f"http_{r.status_code}" if r.status_code != 200 else "empty_body"
                if requires_web_signature(path) and r.status_code != 200:
                    self.last_error, reason = detail_http_error(r.status_code, r.content)
                    if reason:
                        self.last_response["rejection_reason"] = reason
                print(f"[dy-client] {path} → HTTP {r.status_code} len={len(r.content)}"
                      f"{' (空 body:多半被风控拒,非无数据)' if r.status_code == 200 else ''}")
                return []
            try:
                payload = r.json()
                if isinstance(payload, dict):
                    code = payload.get("status_code")
                    self.last_response["business_code"] = code if isinstance(code, int) else None
                    self.last_response["response_keys"] = sorted(payload)[:20]
                return [payload] if isinstance(payload, dict) else []
            except Exception:
                body = r.content.decode("utf-8", errors="ignore") if isinstance(r.content, bytes) else ""
                from ...browser.fetcher import decode_douyin_search_payloads
                payloads = decode_douyin_search_payloads(r.content or b"")
                if payloads:
                    self.last_response["stream_payloads"] = len(payloads)
                    self.last_response["response_keys"] = sorted({
                        key for payload in payloads for key in payload
                    })[:20]
                    return payloads
                self.last_error = ("risk:verification_required:html" if re.search(
                    r"<title[^>]*>[^<]*(?:验证码中间页|安全验证|人机验证)", body, re.I)
                    else "invalid_json")
                print(f"[dy-client] {path} → 非 JSON len={len(r.content)}")
                return []
        except Exception as exc:
            self.last_error = f"network:{type(exc).__name__}"
            raise
        finally:
            if owned:
                await cli.close()
                self._ms_token = None

    async def _get_json(self, path: str, params: Dict[str, Any],
                        referer: str = BASE + "/") -> Optional[dict]:
        payloads = await self._get_payloads(path, params, referer=referer)
        return payloads[0] if payloads else None

    async def _post_json(self, path: str, params: Dict[str, Any],
                         form: Dict[str, Any], *,
                         referer: str = BASE + "/") -> Optional[dict]:
        """发送签名的网页表单 POST，且为非幂等请求保留不确定状态。

        接口明确返回非 2xx 或业务 ``status_code`` 时调用方可选择页面回退；
        超时、连接中断、5xx、200 空响应均无法确认服务端是否已经落库，调用方
        必须停止重试并把任务标为 ``uncertain``。
        """
        self.last_error = ""
        self.last_response = {}
        self.last_write_uncertain = False
        owned = self._session is None
        cli = self._session or AsyncSession(impersonate=self.impersonate)
        try:
            await self._prepare_ms_token(self._api_origin(path) + path)
            # a_bogus hashes the exact application/x-www-form-urlencoded body.
            # Build it once and use the same spelling for signing and transport.
            form_fields = {k: str(v) for k, v in form.items() if v is not None}
            encoded_form = urllib.parse.urlencode(form_fields)
            url = self._build_url(path, params, body=encoded_form)
            request_token_source = self._token_source
            headers = self._headers(referer, url=url)
            headers.update({
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "Origin": BASE,
            })
            headers.update(self._write_security_headers(path, url))
            from app.licensing import require_active
            require_active()
            response = await cli.post(
                url, data=encoded_form,
                headers=headers, impersonate=self.impersonate, timeout=self.timeout,
                proxy=self.proxy or None, allow_redirects=False)
            self._accept_response_cookies(response, url)
            response_headers = getattr(response, "headers", {}) or {}
            secure_path = self._normalized_path(path) in SECURE_SDK_WRITE_PATHS
            parsed_url = urllib.parse.urlsplit(url)
            self.last_response = {
                "origin": f"{parsed_url.scheme}://{parsed_url.netloc}",
                "path": path,
                "http_status": response.status_code,
                "body_bytes": len(response.content or b""),
                "content_type": str(response_headers.get("content-type", ""))[:100],
                "token_source": request_token_source,
                "a_bogus_body_bound": True,
                "device_header_attached": "uifid" in headers,
                "csrf_header_attached": "x-secsdk-csrf-token" in headers,
                "ticket_guard_client_attached": (
                    "bd-ticket-guard-client-data" in headers),
                "dtrait_header_attached": "x-tt-session-dtrait" in headers,
                "browser_security_context_required": secure_path,
                "ticket_guard_result": str(
                    response_headers.get("bd-ticket-guard-result", ""))[:100],
                "verify_passport_decision": bool(
                    response_headers.get("x-tt-verify-passport-decision")),
            }
            if response.status_code != 200 or not response.content:
                if response.status_code == 401:
                    self.last_error = "logged_out"
                elif response.status_code == 403 and secure_path:
                    self.last_error = "api_requires_browser_security:http_403"
                    self.last_response["rejection_reason"] = (
                        "browser_security_context_required")
                elif response.status_code == 429:
                    self.last_error = "risk:rate_limited:http_429"
                else:
                    self.last_error = (f"http_{response.status_code}"
                                       if response.status_code != 200 else "empty_body")
                self.last_write_uncertain = (
                    response.status_code >= 500 or response.status_code == 200)
                return None
            try:
                payload = response.json()
                if isinstance(payload, dict):
                    code = payload.get("status_code")
                    self.last_response["business_code"] = (
                        code if isinstance(code, int) else None)
                    self.last_response["response_keys"] = sorted(payload)[:20]
                return payload
            except Exception:
                body = (response.content.decode("utf-8", errors="ignore")
                        if isinstance(response.content, bytes) else "")
                self.last_error = ("risk:verification_required:html" if re.search(
                    r"<title[^>]*>[^<]*(?:验证码中间页|安全验证|人机验证)",
                    body, re.I) else "invalid_json")
                self.last_write_uncertain = True
                return None
        except Exception as exc:
            self.last_error = f"network:{type(exc).__name__}"
            self.last_write_uncertain = True
            return None
        finally:
            if owned:
                await cli.close()
                self._ms_token = None

    def _write_result(self, data: Optional[dict], fallback_error: str) -> tuple[bool, str]:
        """归一网页写接口的业务状态；返回 ``(success, error)``。"""
        if not isinstance(data, dict):
            return False, fallback_error
        code = data.get("status_code")
        if code is None:
            self.last_error = self.last_error or "invalid_response"
            self.last_write_uncertain = True
            return False, self._write_error(fallback_error)
        if str(code) == "0":
            return True, ""
        message = str(data.get("status_msg") or data.get("message") or "")
        if str(code) == "8" and any(marker in message for marker in (
                "用户未登录", "未登录", "登录态已失效")):
            if (self.last_response.get("browser_security_context_required")
                    and not self.last_response.get("dtrait_header_attached")):
                self.last_error = "api_requires_browser_security:status_code=8"
                return False, (
                    "api_requires_browser_security:status_code=8"
                    f"{(' ' + message) if message else ''}")
            self.last_error = "logged_out"
            return False, f"logged_out{(':' + message) if message else ''}"
        return False, f"api_rejected:status_code={code}{(' ' + message) if message else ''}"

    def _write_error(self, fallback: str) -> str:
        error = self.last_error or fallback
        return f"write_uncertain:{error}" if self.last_write_uncertain else error

    @staticmethod
    def _im_rejection(parsed: dict) -> str:
        """Normalize an IM business envelope without hiding auth expiry."""
        message = str(parsed.get("msg") or "")
        error_code = int(parsed.get("error_code") or 0)
        if error_code == 1 and "session length" in message.casefold():
            return f"logged_out:imapi {message}"
        return (f"api_rejected:code={error_code}"
                f"{(' ' + message) if message else ''}")

    # ── 写操作(网页接口,需显式 douyin_write_mode=api|hybrid) ──
    async def post_comment(self, aweme_id: str, text: str, *,
                           reply_comment_id: str = "") -> tuple[bool, str, str]:
        """直连发表一级评论或回复，返回 ``(ok, comment_id, error)``。"""
        aweme_id, text = str(aweme_id or ""), str(text or "").strip()
        if not aweme_id:
            return False, "", "缺少 aweme_id"
        if not text:
            return False, "", "空评论"
        reply_comment_id = str(reply_comment_id or "")
        data = await self._post_json(
            "/aweme/v1/web/comment/publish/",
            {"aweme_id": aweme_id, "item_type": 0},
            {
                "aweme_id": aweme_id,
                "text": text,
                "item_type": 0,
                # 一级评论保持空值；回复同时带两种页面版本使用的关联字段。
                "reply_id": reply_comment_id,
                "reply_comment_id": reply_comment_id,
                "at_users": "[]",
            },
            referer=f"{BASE}/video/{aweme_id}")
        ok, error = self._write_result(data, self._write_error("comment_no_response"))
        if not ok:
            return False, "", error
        comment = data.get("comment") or {}
        cid = str(comment.get("cid") or comment.get("comment_id")
                  or data.get("comment_id") or "ok")
        return True, cid, ""

    async def set_follow_state(self, user_id: str, sec_uid: str = "", *,
                               unfollow: bool = False) -> tuple[bool, str]:
        """直连关注/取关网页接口；缺 user_id 时先以 sec_uid 解析资料。"""
        user_id, sec_uid = str(user_id or ""), str(sec_uid or "")
        if not user_id and sec_uid:
            profile = await self.fetch_profile(sec_uid)
            user_id = str((profile or {}).get("uid")
                          or (profile or {}).get("user_id") or "")
        if not user_id:
            return False, "缺少 target_uid，且无法由 sec_uid 解析用户"
        data = await self._post_json(
            "/aweme/v1/web/commit/follow/user/",
            {},
            {
                "user_id": user_id,
                "type": 2 if unfollow else 1,
            },
            referer=f"{BASE}/user/{sec_uid or user_id}")
        return self._write_result(
            data, self._write_error("follow_no_response"))

    async def create_dm_conversation(
            self, target_uid: str, self_uid: str, *, target_sec_uid: str = ""
    ) -> tuple[Optional[dict], str]:
        """通过网页端 cmd=609 创建单聊，返回 cmd=100 所需会话标识。"""
        from ...browser.douyin_im_pb import (
            build_create_conversation_request,
            parse_create_conversation_response,
        )

        target_uid = str(target_uid or "").strip()
        self_uid = str(self_uid or "").strip()
        if not target_uid.isdigit() or not self_uid.isdigit():
            return None, "建会需要目标和当前账号的数字 uid"
        if target_uid == self_uid:
            return None, "不能给当前账号创建单聊"
        self.last_error = ""
        self.last_write_uncertain = False
        owned = self._session is None
        cli = self._session or AsyncSession(impersonate=self.impersonate)
        referer = f"{BASE}/user/{target_sec_uid or target_uid}"
        try:
            body = build_create_conversation_request(
                target_uid, self_uid,
                **self._im_request_options(referer))
            from app.licensing import require_active
            require_active()
            response = await cli.post(
                IMAPI_CREATE_URL, data=body,
                headers={**self._headers(referer),
                         "Accept": "application/x-protobuf",
                         "Content-Type": "application/x-protobuf",
                         "Origin": BASE},
                impersonate=self.impersonate, timeout=self.timeout,
                proxy=self.proxy or None)
            raw = response.content
            if response.status_code != 200 or not raw:
                self.last_error = (f"http_{response.status_code}"
                                   if response.status_code != 200 else "empty_body")
                self.last_write_uncertain = (
                    response.status_code >= 500 or response.status_code == 200)
                return None, self._write_error("imapi_create_no_response")
            parsed = parse_create_conversation_response(raw)
            if parsed.get("ok"):
                return dict(parsed["conversation"]), ""
            message = str(parsed.get("msg") or "")
            error_code = int(parsed.get("error_code") or 0)
            if not error_code and message in {"", "OK"}:
                self.last_error = "invalid_response"
                self.last_write_uncertain = True
                return None, self._write_error("imapi_create_invalid_response")
            return None, self._im_rejection(parsed)
        except Exception as exc:
            self.last_error = f"network:{type(exc).__name__}"
            self.last_write_uncertain = True
            return None, self._write_error("imapi_create_network")
        finally:
            if owned:
                await cli.close()
                self._ms_token = None

    async def send_dm(self, conv_id: str, conv_short_id: str, ticket: str,
                      text: str, *, conv_type: int = 1) -> tuple[bool, str]:
        """直连 imapi protobuf 发送已有会话私信，不创建或猜测会话。"""
        from ...browser.douyin_im_pb import build_send_request, parse_send_response

        text = str(text or "").strip()
        if not text:
            return False, "空内容"
        if not (conv_id and conv_short_id and ticket):
            return False, "缺 conv_id/short_id/ticket(先同步会话列表)"
        self.last_error = ""
        self.last_write_uncertain = False
        owned = self._session is None
        cli = self._session or AsyncSession(impersonate=self.impersonate)
        try:
            body = build_send_request(
                str(conv_id), int(conv_type or 1), int(conv_short_id), str(ticket),
                text, str(uuid.uuid4()), int(time.time() * 1000),
                **self._im_request_options())
            from app.licensing import require_active
            require_active()
            response = await cli.post(
                IMAPI_SEND_URL, data=body,
                headers={**self._headers(),
                         "Accept": "application/x-protobuf",
                         "Content-Type": "application/x-protobuf",
                         "Origin": BASE},
                impersonate=self.impersonate, timeout=self.timeout,
                proxy=self.proxy or None)
            raw = response.content
            if response.status_code != 200 or not raw:
                self.last_error = (f"http_{response.status_code}"
                                   if response.status_code != 200 else "empty_body")
                self.last_write_uncertain = (
                    response.status_code >= 500 or response.status_code == 200)
                return False, self._write_error("imapi_no_response")
            parsed = parse_send_response(raw)
            if parsed.get("ok") and parsed.get("cmd") == 100:
                return True, ""
            if (parsed.get("ok") or
                    (not parsed.get("msg") and not parsed.get("error_code"))):
                self.last_error = "invalid_response"
                self.last_write_uncertain = True
                return False, self._write_error("imapi_invalid_response")
            return False, self._im_rejection(parsed)
        except Exception as exc:
            self.last_error = f"network:{type(exc).__name__}"
            self.last_write_uncertain = True
            return False, self._write_error("imapi_network")
        finally:
            if owned:
                await cli.close()
                self._ms_token = None

    @staticmethod
    def _profile_avatar(profile: dict) -> str:
        for key in ("avatar_thumb", "avatar_small", "avatar_larger", "avatar"):
            value = profile.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value
            if isinstance(value, dict):
                urls = value.get("url_list") or value.get("urlList") or []
                if isinstance(urls, list) and urls:
                    return str(urls[0])
                for nested in ("url", "url_default"):
                    if isinstance(value.get(nested), str):
                        return value[nested]
        return ""

    async def fetch_im_user_profiles(self, sec_uids: List[str]) -> Dict[str, dict]:
        """用签名网页接口批量补齐 IM protobuf 中缺失的昵称和头像。"""
        unique = list(dict.fromkeys(str(value) for value in sec_uids if value))
        profiles: Dict[str, dict] = {}
        for offset in range(0, len(unique), 20):
            chunk = unique[offset:offset + 20]
            data = await self._post_json(
                "/aweme/v1/web/im/user/info/", {},
                {"sec_user_ids": json.dumps(chunk, separators=(",", ":"))},
                referer=f"{BASE}/follow")
            if not data or str(data.get("status_code", 0)) != "0":
                if not self.last_error:
                    self.last_error = "im_user_info_rejected"
                continue
            for row in data.get("data") or []:
                if not isinstance(row, dict):
                    continue
                sec_uid = str(row.get("sec_uid") or row.get("secUid") or "")
                if not sec_uid:
                    continue
                profiles[sec_uid] = {
                    "sec_uid": sec_uid,
                    "nickname": str(row.get("nickname")
                                    or row.get("alias_nickname") or ""),
                    "avatar": self._profile_avatar(row),
                }
        return profiles

    async def fetch_dm_conversations(self) -> List[dict]:
        """纯协议读取当前账号会话快照并补齐对端资料。"""
        from ...browser.douyin_im_pb import (
            GET_MESSAGE_BY_INIT_URL,
            build_init_request,
            parse_conversations,
            parse_send_response,
        )

        self.last_error = ""
        owned = self._session is None
        cli = self._session or AsyncSession(impersonate=self.impersonate)
        try:
            body = build_init_request(
                int(time.time() * 1_000_000), **self._im_request_options())
            from app.licensing import require_active
            require_active()
            response = await cli.post(
                GET_MESSAGE_BY_INIT_URL, data=body,
                headers={**self._headers(),
                         "Accept": "application/x-protobuf",
                         "Content-Type": "application/x-protobuf",
                         "Origin": BASE},
                impersonate=self.impersonate, timeout=self.timeout,
                proxy=self.proxy or None)
            raw = response.content
            if response.status_code != 200 or not raw:
                self.last_error = (f"http_{response.status_code}"
                                   if response.status_code != 200 else "empty_body")
                return []
            status = parse_send_response(raw)
            if not status.get("ok") or status.get("cmd") != 2043:
                self.last_error = (self._im_rejection(status)
                                   if status.get("msg") or status.get("error_code")
                                   else "imapi_invalid_response")
                return []
            conversations = parse_conversations(raw)
            profiles = await self.fetch_im_user_profiles(
                [row.get("peer_sec_uid", "") for row in conversations])
            # 资料水合失败不应丢掉已经成功解析的会话快照。
            if conversations:
                self.last_error = ""
            out: List[dict] = []
            for row in conversations:
                profile = profiles.get(row.get("peer_sec_uid", ""), {})
                out.append({
                    "conv_id": row["conv_id"],
                    "peer_uid": row["peer_uid"],
                    "peer_sec_uid": row.get("peer_sec_uid")
                    or profile.get("sec_uid", ""),
                    "peer_nickname": profile.get("nickname", ""),
                    "peer_avatar": profile.get("avatar", ""),
                    "last_text": row.get("last_text", ""),
                    "last_time": row.get("last_time", 0),
                    "unread_count": 0,
                    "conv_short_id": row.get("conv_short_id", ""),
                    "ticket": row.get("ticket", ""),
                    "raw_json": json.dumps({
                        "last_sender_uid": row.get("last_sender_uid", ""),
                        "self_uid": row.get("self_uid", ""),
                        "last_msg_type": row.get("last_msg_type", 0),
                    }, ensure_ascii=False),
                })
            return out
        except Exception as exc:
            self.last_error = f"network:{type(exc).__name__}"
            return []
        finally:
            if owned:
                await cli.close()
                self._ms_token = None

    async def search_awemes_page(self, keyword: str, *, offset: int = 0,
                                 count: int = 20, search_sort: str = "general",
                                 publish_time: str = "all",
                                 content_type: str = "all") -> dict:
        """调用抖音网页搜索接口，返回原始作品卡片及分页状态。"""
        from ...browser.fetcher import extract_search_awemes
        sort_codes = {"general": "0", "most_liked": "1", "latest": "2"}
        time_codes = {"all": "0", "day": "1", "week": "7", "half_year": "180"}
        params = {
            "keyword": keyword,
            "search_channel": "aweme_general",
            "search_source": "tab_search",
            "query_correct_type": "1",
            "is_filter_search": "0",
            "offset": max(0, int(offset or 0)),
            "count": max(1, min(50, int(count or 15))),
            "list_type": "multi",
            "search_id": self._search_id,
            "need_filter_settings": "1",
            "enable_history": "1",
            "from_group_id": "",
            "type": "video" if content_type == "video" else None,
        }
        if search_sort != "general" or publish_time != "all":
            params["is_filter_search"] = "1"
            params["filter_selected"] = json.dumps({
                "sort_type": sort_codes.get(search_sort, "0"),
                "publish_time": time_codes.get(publish_time, "0"),
            }, ensure_ascii=False, separators=(",", ":"))
        payloads = await self._get_payloads(
            SEARCH_SINGLE_PATH, params,
            referer=f"{BASE}/search/{urllib.parse.quote(keyword, safe='')}")
        single_error = self.last_error
        structurally_unsupported = bool(payloads) and all(
            not any(key in data for key in (
                "data", "search_nil_info", "has_more", "extra", "log_pb"))
            and data.get("status_code") in (None, 0, "0")
            for data in payloads)
        if ((not payloads and single_error in {"invalid_json", "http_404", "http_405"})
                or structurally_unsupported):
            stream_params = dict(params)
            stream_params["list_type"] = "single"
            payloads = await self._get_payloads(
                SEARCH_STREAM_PATH, stream_params,
                referer=f"{BASE}/search/{urllib.parse.quote(keyword, safe='')}")
        if not payloads:
            return {"items": [], "has_more": False, "offset": offset,
                    "error": self.last_error or "empty_response"}
        found: Dict[str, dict] = {}
        has_more = False
        next_offset = offset
        for data in payloads:
            next_search_id = str(
                (data.get("extra") or {}).get("logid")
                or (data.get("log_pb") or {}).get("impr_id") or "")
            if next_search_id:
                self._search_id = next_search_id
            code = data.get("status_code")
            message = str(data.get("status_msg") or data.get("message") or "")
            if code not in (None, 0, "0"):
                marker = message.casefold()
                if str(code) in {"8", "2483"} or any(word in marker for word in (
                        "未登录", "登录后", "请先登录", "login")):
                    self.last_error = "logged_out"
                    return {"items": [], "has_more": False, "offset": offset,
                            "error": "logged_out"}
                if any(word in marker for word in (
                        "verify", "captcha", "risk", "验证", "风控")):
                    self.last_error = "verification_required"
                    return {"items": [], "has_more": False, "offset": offset,
                            "error": "verification_required"}
                self.last_error = f"api_rejected:status_code={code}"
                return {"items": [], "has_more": False, "offset": offset,
                        "error": self.last_error}
            nil_info = data.get("search_nil_info") or {}
            marker = " ".join(str(nil_info.get(k) or "") for k in
                              ("search_nil_type", "search_nil_item", "text_type"))
            if any(word in marker.casefold() for word in ("verify", "captcha", "risk")):
                self.last_error = "verification_required"
                return {"items": [], "has_more": False, "offset": offset,
                        "error": "verification_required"}
            for item in extract_search_awemes(data):
                found.setdefault(str(item.get("aweme_id") or ""), item)
            has_more = has_more or bool(data.get("has_more") or data.get("has_more_item"))
            next_offset = max(next_offset, int(data.get("offset") or 0))
        items = [item for key, item in found.items() if key]
        return {"items": items,
                "has_more": has_more,
                "offset": next_offset or (offset + len(items)),
                "error": ""}

    async def search_awemes(self, keyword: str, *, max_results: int = 20,
                            max_pages: int = 3, search_sort: str = "general",
                            publish_time: str = "all", content_type: str = "all",
                            min_likes: int = 0, min_comments: int = 0) -> tuple[List[dict], str]:
        """分页搜索并做本地筛选；返回 (作品列表, 可回退原因)。"""
        import time
        # search_id belongs to one keyword pagination chain, not the account.
        self._search_id = ""
        found: Dict[str, dict] = {}
        offset = 0
        for _ in range(max(1, min(10, int(max_pages or 1)))):
            page = await self.search_awemes_page(
                keyword, offset=offset, count=min(50, max_results),
                search_sort=search_sort, publish_time=publish_time,
                content_type=content_type)
            if page.get("error"):
                return list(found.values())[:max_results], str(page["error"])
            for item in page.get("items") or []:
                stats = item.get("statistics") or {}
                if content_type == "video" and not item.get("video"):
                    continue
                if content_type == "images" and not item.get("images"):
                    continue
                if int(stats.get("digg_count") or 0) < max(0, int(min_likes or 0)):
                    continue
                if int(stats.get("comment_count") or 0) < max(0, int(min_comments or 0)):
                    continue
                if publish_time != "all":
                    windows = {"day": 86400, "week": 7 * 86400, "half_year": 180 * 86400}
                    created = int(item.get("create_time") or 0)
                    if created and created < time.time() - windows.get(publish_time, 0):
                        continue
                aid = str(item.get("aweme_id") or "")
                if aid:
                    found.setdefault(aid, item)
            if len(found) >= max_results or not page.get("has_more"):
                break
            next_offset = int(page.get("offset") or offset + len(page.get("items") or []))
            if next_offset <= offset:
                break
            offset = next_offset
        items = list(found.values())[:max_results]
        if search_sort == "latest":
            items.sort(key=lambda x: int(x.get("create_time") or 0), reverse=True)
        elif search_sort == "most_liked":
            items.sort(key=lambda x: int((x.get("statistics") or {}).get("digg_count") or 0), reverse=True)
        return items, ("empty_response" if not items else "")

    async def fetch_all_video_list(self, sec_uid: str, max_pages: int = 10,
                                   count: int = 20) -> List[dict]:
        """读取账号公开作品，供本账号作品同步和健康监控复用。"""
        out: List[dict] = []
        cursor = 0
        seen: Set[str] = set()
        for _ in range(max(1, min(30, int(max_pages or 1)))):
            page = await self.fetch_video_list(sec_uid, cursor, count)
            items = page.get("items") or []
            if not items:
                break
            for item in items:
                aid = str(item.get("aweme_id") or "")
                if aid and aid not in seen:
                    seen.add(aid)
                    out.append(item)
            if not page.get("has_more"):
                break
            nxt = int(page.get("max_cursor") or 0)
            if nxt == cursor:
                break
            cursor = nxt
        return out

    # ── 用户资料(对应 NativeClient.FetchProfile)──
    async def fetch_self_profile(self) -> Optional[dict]:
        """读取当前 Cookie 对应账号，不依赖已知 sec_uid。"""
        data = await self._get_json(
            "/aweme/v1/web/user/profile/self/", {}, referer=f"{BASE}/user/self")
        if data is None:
            if self.last_error == "http_401":
                self.last_error = "logged_out"
            return None
        profile, self.last_error = parse_self_profile_response(data)
        return profile

    async def fetch_profile(self, sec_uid: str) -> Optional[dict]:
        data = await self._get_json(
            "/aweme/v1/web/user/profile/other/",
            {"sec_user_id": sec_uid, "publish_video_strategy_type": "2"},
            referer=f"{BASE}/user/{sec_uid}",
        )
        if data and data.get("user"):
            return data["user"]
        return None

    async def resolve_user_identifier(
            self, identifier: str) -> tuple[Optional[dict], str]:
        """Resolve a visible Douyin ID to the numeric UID used by IM.

        A bare numeric value is not assumed to be an IM UID. Douyin exposes a
        separate visible ``unique_id``/``short_id`` namespace, so new-message
        flows must exact-match the first-party user-search response before
        building cmd=609.
        """
        raw = str(identifier or "").strip().lstrip("@")
        if not raw:
            return None, "缺少抖音号"
        try:
            parsed_url = urllib.parse.urlsplit(raw)
            if parsed_url.scheme in {"http", "https"}:
                if not ((parsed_url.hostname or "").casefold() == "douyin.com"
                        or (parsed_url.hostname or "").casefold().endswith(
                            ".douyin.com")):
                    return None, "目标主页不是 douyin.com"
                match = re.search(r"/user/([^/?#]+)", parsed_url.path)
                raw = urllib.parse.unquote(match.group(1)) if match else ""
        except ValueError:
            return None, "目标主页格式无效"
        if not raw:
            return None, "目标主页缺少用户标识"

        if raw.startswith("MS4wLjAB"):
            profile = await self.fetch_profile(raw)
            if not profile:
                return None, self.last_error or "sec_uid 查询无结果"
            return self._resolved_user(profile), ""

        payload = await self._get_json(
            "/aweme/v1/web/discover/search/",
            {
                "keyword": raw,
                "search_channel": "aweme_user_web",
                "search_source": "normal_search",
                "query_correct_type": "1",
                "is_filter_search": "0",
                "offset": 0,
                "count": 20,
            },
            referer=f"{BASE}/search/{urllib.parse.quote(raw, safe='')}?type=user",
        )
        if not payload:
            return None, self.last_error or "用户搜索无响应"
        containers = [payload]
        for key in ("data", "result"):
            if isinstance(payload.get(key), dict):
                containers.append(payload[key])
        rows: list = []
        for container in containers:
            for key in ("user_list", "users", "data"):
                value = container.get(key)
                if isinstance(value, list):
                    rows.extend(value)

        for row in rows:
            if not isinstance(row, dict):
                continue
            user = (row.get("user_info") or row.get("user")
                    or row.get("aweme_user") or row)
            if not isinstance(user, dict):
                continue
            visible_ids = {
                str(user.get("unique_id") or "").strip(),
                str(user.get("short_id") or "").strip(),
            }
            if raw not in visible_ids:
                continue
            resolved = self._resolved_user(user)
            if resolved.get("uid") and resolved.get("sec_uid"):
                return resolved, ""
            return None, "搜索结果缺少 uid/sec_uid"
        return None, "未找到完全匹配的抖音号"

    @staticmethod
    def _resolved_user(user: dict) -> dict:
        return {
            "uid": str(user.get("uid") or user.get("user_id") or ""),
            "sec_uid": str(user.get("sec_uid") or user.get("sec_user_id") or ""),
            "unique_id": str(user.get("unique_id") or ""),
            "short_id": str(user.get("short_id") or ""),
            "nickname": str(user.get("nickname") or ""),
        }

    # ── 作品列表(对应 NativeClient.FetchVideoList)──
    async def fetch_video_list(self, sec_uid: str, max_cursor: int = 0,
                               count: int = 20) -> Dict[str, Any]:
        data = await self._get_json(
            "/aweme/v1/web/aweme/post/",
            {
                "sec_user_id": sec_uid,
                "max_cursor": max_cursor,
                "count": count,
                "publish_video_strategy_type": "2",
            },
            referer=f"{BASE}/user/{sec_uid}",
        )
        if not data:
            return {"items": [], "has_more": False, "max_cursor": 0}
        return {
            "items": data.get("aweme_list") or [],
            "has_more": bool(data.get("has_more")),
            "max_cursor": data.get("max_cursor", 0),
        }

    async def fetch_all_new_videos(self, sec_uid: str, known_ids: set,
                                   max_pages: int = 5) -> List[dict]:
        """对应 NativeClient.FetchAllNewVideos:翻页直到遇到已知作品。"""
        new_items: List[dict] = []
        cursor = 0
        for _ in range(max_pages):
            page = await self.fetch_video_list(sec_uid, cursor)
            if not page["items"]:
                break
            stop = False
            for it in page["items"]:
                aweme_id = str(it.get("aweme_id", ""))
                if aweme_id in known_ids:
                    stop = True
                    continue
                new_items.append(it)
            if stop or not page["has_more"]:
                break
            cursor = page["max_cursor"]
        return new_items

    # ── 作品详情(对应 NativeClient.FetchVideoDetail)──
    async def fetch_video_detail(self, aweme_id: str) -> Optional[dict]:
        data = await self._get_json(
            "/aweme/v1/web/aweme/detail/",
            {"aweme_id": aweme_id},
            referer=f"{BASE}/video/{aweme_id}",
        )
        if data is None:
            self.last_error = normalize_detail_error(self.last_error or "empty_response")
            return None
        item, self.last_error = parse_detail_response(data, aweme_id)
        return item

    # ── 评论(直连 comment/list + reply,分页拉全量;参考 CommentAll)──
    async def fetch_comments_page(self, aweme_id: str, cursor: int = 0,
                                  count: int = 20) -> dict:
        data = await self._get_json(
            "/aweme/v1/web/comment/list/",
            {"aweme_id": aweme_id, "cursor": cursor, "count": count, "item_type": 0},
            referer=f"{BASE}/video/{aweme_id}",
        )
        return data or {}

    async def fetch_replies_page(self, aweme_id: str, comment_id: str, cursor: int = 0,
                                 count: int = 20) -> dict:
        data = await self._get_json(
            "/aweme/v1/web/comment/list/reply/",
            {"item_id": aweme_id, "comment_id": comment_id, "cursor": cursor,
             "count": count, "item_type": 0},
            referer=f"{BASE}/video/{aweme_id}",
        )
        return data or {}

    async def fetch_all_comments(self, aweme_id: str, max_pages: int = 30,
                                 with_replies: bool = True, max_reply_pages: int = 12
                                 ) -> List[dict]:
        """分页拉一条作品的全部一级评论;with_replies 时顺带把有回复的评论的子评论拉全。
        返回原始评论项(含子评论)一维列表,交由上层 parse_comment 归一 + 去重。"""
        out: List[dict] = []
        cursor = 0
        for _ in range(max_pages):
            page = await self.fetch_comments_page(aweme_id, cursor)
            comments = page.get("comments") or []
            out.extend(comments)
            if with_replies:
                for c in comments:
                    if int(c.get("reply_comment_total") or 0) <= 0:
                        continue
                    cid = str(c.get("cid") or "")
                    if not cid:
                        continue
                    rcur = 0
                    for _ in range(max_reply_pages):
                        rp = await self.fetch_replies_page(aweme_id, cid, rcur)
                        out.extend(rp.get("comments") or [])
                        if not rp.get("has_more"):
                            break
                        rcur = rp.get("cursor") or 0
            if not page.get("has_more"):
                break
            cursor = page.get("cursor") or 0
        return out

    # ── 短视频弹幕(播放页接口,与 comment/list 分开)──
    async def fetch_danmaku_page(self, aweme_id: str, start_time: int = 0,
                                 end_time: int = 0, duration: int = 0) -> dict:
        """请求一个视频时间窗口的弹幕页，浏览器拦截器是主路径。"""
        data = await self._get_json(
            "/aweme/v1/web/danmaku/get_v2/",
            {
                "item_id": aweme_id,
                "aweme_id": aweme_id,
                "start_time": max(0, int(start_time or 0)),
                "end_time": max(0, int(end_time or 0)),
                "duration": max(0, int(duration or 0)),
            },
            referer=f"{BASE}/video/{aweme_id}",
        )
        return data or {}

    async def fetch_all_danmaku(self, aweme_id: str, *, start_time: int = 0,
                                end_time: int = 0, duration: int = 0,
                                max_pages: int = 4) -> List[dict]:
        """读取播放页弹幕并从不同版本的响应结构中提取条目。

        抖音网页端目前通常在一次 ``get_v2`` 响应中返回完整列表；保留
        ``max_pages`` 参数以兼容后续按 cursor/时间窗口拆分的版本。
        """
        out: List[dict] = []
        seen: Set[str] = set()

        def walk(value: Any, depth: int = 0) -> None:
            if depth > 8:
                return
            if isinstance(value, list):
                for row in value:
                    if isinstance(row, dict):
                        # 弹幕条目至少应有文本或稳定 id，避免把分页元数据当条目。
                        if any(row.get(k) not in (None, "") for k in
                               ("content", "text", "danmaku_text", "body",
                                "danmaku_id", "barrage_id", "bullet_id", "cid")):
                            key = str(row.get("danmaku_id") or row.get("barrage_id")
                                      or row.get("bullet_id") or row.get("cid")
                                      or row.get("id") or "")
                            if not key:
                                key = json.dumps(row, ensure_ascii=False, sort_keys=True)
                            if key not in seen:
                                seen.add(key)
                                out.append(row)
                    walk(row, depth + 1)
                return
            if isinstance(value, dict):
                for child in value.values():
                    if isinstance(child, (dict, list)):
                        walk(child, depth + 1)

        for _ in range(max(1, min(10, int(max_pages or 1)))):
            page = await self.fetch_danmaku_page(
                aweme_id, start_time=start_time, end_time=end_time,
                duration=duration)
            before = len(out)
            walk(page)
            if len(out) == before:
                break
            # get_v2 没有稳定公开 cursor，重复窗口只会返回同一批数据。
            break
        return out

    # ── 关注 / 粉丝(直连 following/follower list;offset + max_time 分页)──
    #    ⚠️ 参数按抖音 web 常见形态实现;拿不到时上层回退浏览器拦截,故失败无副作用。
    async def _follow_page(self, path: str, user_id: str, sec_uid: str, offset: int,
                           max_time: int, count: int, source_type: int) -> dict:
        data = await self._get_json(
            path,
            {"user_id": user_id, "sec_user_id": sec_uid, "offset": offset,
             "min_time": 0, "max_time": max_time, "count": count,
             "source_type": source_type, "gps_access": 0, "address_book_access": 0},
            referer=f"{BASE}/user/{sec_uid}" if sec_uid else BASE + "/",
        )
        return data or {}

    async def fetch_all_follows(
            self, user_id: str, sec_uid: str, direction: str,
            max_pages: int = FOLLOW_MAX_PAGES, count: int = 20,
            *, page_delay: float = FOLLOW_PAGE_DELAY_SECONDS,
            on_page: Optional[Callable[[List[dict], dict], Any]] = None,
            collect: bool = True) -> List[dict]:
        """direction=following(我关注的) / fan(关注我的)。返回原始 user 对象列表。"""
        if not user_id and sec_uid:
            profile = await self.fetch_profile(sec_uid)
            user_id = str((profile or {}).get("uid")
                          or (profile or {}).get("user_id") or "")
        following = direction == "following"
        path = ("/aweme/v1/web/user/following/list/" if following
                else "/aweme/v1/web/user/follower/list/")
        list_key = "followings" if following else "followers"
        out: List[dict] = []
        seen: Set[str] = set()
        offset = 0
        page_limit = max(1, min(int(max_pages or 1), FOLLOW_MAX_PAGES))
        count = max(1, min(int(count or 20), 20))
        self.last_follow_meta = {
            "pages": 0, "fetched": 0, "has_more": False,
            "complete": False, "stop_reason": "starting",
        }
        # The fan endpoint expects the first page's upper time cursor. The web
        # client uses the current Unix timestamp; zero yields HTTP 200 + empty
        # body even with a valid current signature.
        max_time = int(time.time()) if not following else 0
        for page_index in range(page_limit):
            page = await self._follow_page(path, user_id, sec_uid, offset, max_time,
                                           count, source_type=1)
            users = page.get(list_key) or []
            new = 0
            fresh: List[dict] = []
            for u in users:
                uid = str(u.get("uid") or u.get("sec_uid") or "")
                if uid and uid not in seen:
                    seen.add(uid)
                    fresh.append(u)
                    if collect:
                        out.append(u)
                    new += 1
            has_more = bool(page.get("has_more"))
            meta = {
                "pages": page_index + 1,
                "fetched": len(seen),
                "has_more": has_more,
                "complete": not has_more,
                "stop_reason": "complete" if not has_more else "paging",
            }
            self.last_follow_meta = meta
            if on_page is not None:
                callback_result = on_page(fresh, dict(meta))
                if inspect.isawaitable(callback_result):
                    await callback_result
            if not has_more:
                break
            if not users or new == 0:
                self.last_error = "pagination_stalled"
                self.last_follow_meta.update(
                    complete=False, stop_reason="pagination_stalled")
                break
            offset = page.get("offset") or (offset + count)
            max_time = page.get("max_time") or max_time
            if page_delay > 0:
                await asyncio.sleep(page_delay)
        else:
            self.last_error = f"page_limit:{page_limit}"
            self.last_follow_meta.update(
                has_more=True, complete=False, stop_reason="page_limit")
        return out
