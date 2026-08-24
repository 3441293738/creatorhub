"""小红书签名直连 API 客户端。
用 curl_cffi 直接调 edith.xiaohongshu.com,请求头用 xhshow 纯算法签名(X-S/X-T/x-S-Common)。
登录态(含 a1 / web_session 等 Cookie)来自浏览器扫码登录后的 storage_state。

相比"浏览器拦截"方案:不依赖页面 JS 主动发请求,搜索/笔记/评论都稳定可控。
小红书改版导致签名失效时,升级 xhshow 库即可(pip install -U xhshow)。

⚠️ TLS 指纹:走 curl_cffi 的 impersonate,复刻真实 Chrome 的 JA3/HTTP2 指纹。
纯 httpx 的 TLS 指纹与浏览器不同,容易被风控按"非浏览器客户端"识别;impersonate
版本按下方 UA 的 Chrome 大版本自动选最接近的目标,UA 升级后无需改这里。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from ...netfp import impersonate_for_ua

_HOST = "https://edith.xiaohongshu.com"
_SEARCH_HOST = "https://so.xiaohongshu.com"
_DOMAIN = "https://www.xiaohongshu.com"


def _coherent_direct_headers(user_agent: str, impersonate: str) -> dict[str, str]:
    """Keep UA, Client Hints and curl_cffi's TLS target on one major."""
    target = re.search(r"chrome(\d+)", str(impersonate or ""), re.IGNORECASE)
    major = int(target.group(1)) if target else 0
    ua = str(user_agent or "").strip()
    if major and ua:
        ua = re.sub(
            r"Chrome/\d+(?:\.\d+){0,3}",
            f"Chrome/{major}.0.0.0", ua)
        ua = re.sub(
            r"Edg/\d+(?:\.\d+){0,3}",
            f"Edg/{major}.0.0.0", ua)
    platform = (
        '"macOS"' if "Mac OS" in ua
        else '"Linux"' if "Linux" in ua and "Android" not in ua
        else '"Windows"')
    brand = "Microsoft Edge" if "Edg/" in ua else "Google Chrome"
    return {
        "user-agent": ua,
        "sec-ch-ua": (
            f'"Chromium";v="{major}", "{brand}";v="{major}", '
            '"Not_A Brand";v="99"'),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": platform,
    }


def cookie_str_from_state(storage_state_json: str) -> str:
    """从 Patchright storage_state JSON 提取小红书 Cookie 串(name=value; ...)。"""
    try:
        state = json.loads(storage_state_json or "{}")
    except Exception:
        return ""
    parts = []
    for c in state.get("cookies", []):
        dom = c.get("domain", "")
        if "xiaohongshu" in dom or "xhscdn" in dom or dom == "":
            name, val = c.get("name"), c.get("value")
            if name and val is not None:
                parts.append(f"{name}={val}")
    return "; ".join(parts)


def has_a1(cookie_str: str) -> bool:
    return "a1=" in (cookie_str or "")


_CREATOR_COOKIE_NAMES = ("customerClientId", "galaxy_creator_session_id",
                         "access-token-creator.xiaohongshu.com", "customer-sso-sid")


def has_creator_cookies(storage_state_json: str) -> bool:
    """登录态里是否含创作平台会话 cookie(可用于发布)。"""
    try:
        state = json.loads(storage_state_json or "{}")
    except Exception:
        return False
    return any(c.get("name") in _CREATOR_COOKIE_NAMES for c in state.get("cookies", []))


class XhsApiError(Exception):
    def __init__(self, message: str, *, category: str = "business",
                 status_code: int | None = None, signal: str = ""):
        super().__init__(message)
        self.category = category
        self.status_code = status_code
        self.signal = signal or category


class XhsApiClient:
    def __init__(self, cookie_str: str, user_agent: str, timeout: float = 30.0,
                 proxy: str = ""):
        from xhshow import Xhshow            # 延迟导入,未装库时也能加载本模块
        from curl_cffi.requests import AsyncSession
        from ...browser.manager import normalize_proxy
        self._session_cls = AsyncSession
        self.cookie_str = cookie_str or ""
        self.timeout = timeout
        # 规范化:裸 host:port 补成 http://...(curl_cffi 必须带 scheme)
        self.proxy = normalize_proxy(proxy) or None  # 该账号专属代理(防多账号同 IP 关联)
        self.impersonate = impersonate_for_ua(user_agent)  # TLS/HTTP2 指纹复刻目标
        coherent = _coherent_direct_headers(user_agent, self.impersonate)
        self._signer = Xhshow()
        self.base_headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9",
            "content-type": "application/json;charset=UTF-8",
            "origin": _DOMAIN,
            "referer": f"{_DOMAIN}/",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            **coherent,
        }

    # ── 底层请求 ──
    def _query(self, params: Dict[str, Any]) -> str:
        # 与签名串一致:保留逗号不编码(对齐小红书前端/浏览器行为)
        return "&".join(f"{k}={quote(str(v) if v is not None else '', safe=',')}"
                        for k, v in params.items())

    async def _get(self, uri: str, params: Dict[str, Any]) -> dict:
        # 带参数 GET:优先用 execjs 签名(xhshow 的带参 GET 签名有 bug,会被判"无登录")。
        try:
            from . import creator_sign
            use_execjs = bool(params) and creator_sign.available()
        except Exception:
            use_execjs = False
        if use_execjs:
            from . import creator_sign
            # 网页主签名(xhs_main_260411.js)+ method=GET,对"路径?query"签名(对齐 Spider_XHS)
            spliced = creator_sign.splice_str(uri, params)
            a1 = creator_sign.trans_cookies(self.cookie_str).get("a1", "")
            headers = {**self.base_headers,
                       **creator_sign.generate_xsc_main(a1, spliced, "", "GET"),
                       "Cookie": self.cookie_str}
            url = _HOST + spliced
        else:
            sign = self._signer.sign_headers_get(uri, self.cookie_str, params=params or {})
            headers = {**self.base_headers, **sign, "Cookie": self.cookie_str}
            url = _HOST + (self._signer.build_url(uri, params) if params else uri)
        async with self._session_cls() as cli:
            r = await cli.get(url, headers=headers, impersonate=self.impersonate,
                              proxy=self.proxy, timeout=self.timeout)
        return self._unwrap(r)

    async def _post(self, uri: str, data: Dict[str, Any], *,
                    host: str = _HOST) -> dict:
        sign = self._signer.sign_headers_post(uri, self.cookie_str, payload=data or {})
        headers = {**self.base_headers, **sign, "Cookie": self.cookie_str}
        body = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        async with self._session_cls() as cli:
            r = await cli.post(f"{host}{uri}", data=body.encode("utf-8"),
                               headers=headers, impersonate=self.impersonate,
                               proxy=self.proxy, timeout=self.timeout)
        return self._unwrap(r)

    @staticmethod
    def _unwrap(r) -> dict:
        if r.status_code in (403, 429, 461, 471):
            raise XhsApiError(
                f"触发验证码/风控(HTTP {r.status_code}),请稍后再试",
                category="risk", status_code=r.status_code,
                signal=f"http_{r.status_code}")
        if r.status_code == 401:
            raise XhsApiError(
                "登录状态已失效", category="auth", status_code=401,
                signal="http_401")
        if r.status_code == 407:
            raise XhsApiError(
                "代理认证失败", category="network", status_code=407,
                signal="proxy_auth")
        if r.status_code >= 500:
            raise XhsApiError(
                f"平台服务异常(HTTP {r.status_code})", category="network",
                status_code=r.status_code, signal=f"http_{r.status_code}")
        try:
            j = r.json()
        except Exception:
            category = "risk" if 200 <= r.status_code < 300 else "business"
            raise XhsApiError(
                f"非 JSON 响应(HTTP {r.status_code}): {r.text[:120]}",
                category=category, status_code=r.status_code,
                signal="ambiguous_response" if category == "risk" else "non_json")
        if not j.get("success", True) and "data" not in j:
            code = j.get("code")
            message = str(j.get("msg") or j.get("message") or "")
            text = message.lower()
            if (code in {-100, -101, 401}
                    or any(marker in text for marker in (
                        "登录状态", "登录已失效", "未登录", "login expired"))):
                category, signal = "auth", "auth_expired"
            elif (code in {403, 429, 461, 471}
                  or any(marker in text for marker in (
                      "风控", "频繁", "验证码", "验证", "risk", "captcha"))):
                category, signal = "risk", f"api_code_{code}"
            else:
                category, signal = "business", f"api_code_{code}"
            raise XhsApiError(
                f"接口失败 code={code} msg={message}", category=category,
                status_code=r.status_code, signal=signal)
        return j.get("data") or {}

    # ── 业务接口 ──
    async def search_notes(self, keyword: str, page: int = 1, page_size: int = 20,
                           sort: str = "general", note_type: int = 0) -> List[dict]:
        # v2 对过小 page_size 会 success=true 但返回空 items；网页端的有效
        # 区间从 10 开始，统一收敛避免再次出现“接口成功但无结果”。
        page = max(1, int(page or 1))
        page_size = max(10, min(40, int(page_size or 20)))
        data = {
            "keyword": keyword, "page": page, "page_size": page_size,
            "search_id": self._signer.get_search_id(),
            "sort": sort, "note_type": note_type,
            "image_formats": ["jpg", "webp", "avif"],
        }
        # 网页搜索已迁移到 so.xiaohongshu.com 的 v2。旧 edith/v1 仍返回
        # success=true，但 items 恒为空，会把正常关键词误判为无结果。
        d = await self._post(
            "/api/sns/web/v2/search/notes", data, host=_SEARCH_HOST)
        return d.get("items") or []

    async def note_detail_raw(self, note_id: str, xsec_token: str = "",
                              xsec_source: str = "pc_search") -> dict:
        """返回 feed 的完整 item(含 note_card,可能含新鲜 xsec_token)。"""
        data = {
            "source_note_id": note_id,
            "image_formats": ["jpg", "webp", "avif"],
            "extra": {"need_body_topic": 1},
            "xsec_source": xsec_source or "pc_search",
            "xsec_token": xsec_token,
        }
        d = await self._post("/api/sns/web/v1/feed", data)
        items = d.get("items") or []
        return items[0] if items else {}

    async def note_detail(self, note_id: str, xsec_token: str = "",
                          xsec_source: str = "pc_search") -> dict:
        item = await self.note_detail_raw(note_id, xsec_token, xsec_source)
        return item.get("note_card") or {}

    async def notes_by_creator(self, user_id: str, cursor: str = "", page_size: int = 30,
                               xsec_token: str = "", xsec_source: str = "pc_feed") -> dict:
        params = {
            "num": page_size, "cursor": cursor, "user_id": user_id,
            "image_formats": "jpg,webp,avif",
            "xsec_token": xsec_token, "xsec_source": xsec_source or "pc_feed",
        }
        return await self._get("/api/sns/web/v1/user_posted", params)

    async def note_comments(self, note_id: str, xsec_token: str = "",
                            cursor: str = "", xsec_source: str = "") -> dict:
        params = {
            "note_id": note_id, "cursor": cursor, "top_comment_id": "",
            "image_formats": "jpg,webp,avif", "xsec_token": xsec_token,
        }
        if xsec_source:
            params["xsec_source"] = xsec_source
        return await self._get("/api/sns/web/v2/comment/page", params)

    async def post_comment(self, note_id: str, content: str, xsec_token: str = "",
                           target_comment_id: str = "") -> dict:
        """给笔记发评论 / 回复某条评论(签名直连,走 _post)。
        target_comment_id 非空 = 回复该评论,否则为笔记下的顶层评论。
        ⚠️ 接口字段以小红书 web 实际请求为准,改版/字段不符时对照 F12 调整这里。"""
        data: Dict[str, Any] = {"note_id": note_id, "content": content, "at_users": []}
        if xsec_token:
            data["xsec_token"] = xsec_token
        if target_comment_id:
            data["target_comment_id"] = target_comment_id
        return await self._post("/api/sns/web/v1/comment/post", data)

    async def self_info(self) -> dict:
        return await self._get("/api/sns/web/v2/user/me", {})

    async def user_info(self, user_id: str) -> dict:
        return await self._get("/api/sns/web/v1/user/otherinfo", {"target_user_id": user_id})
