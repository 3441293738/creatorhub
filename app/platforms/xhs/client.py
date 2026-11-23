"""小红书签名直连 API 客户端。
用 curl_cffi 直接调 edith.xiaohongshu.com,请求头用 xhshow 纯算法签名(X-S/X-T/x-S-Common)。
登录态(含 a1 / web_session 等 Cookie)来自浏览器扫码登录后的 storage_state。

这是显式启用的兼容通道，默认读取仍复用账号浏览器。
签名、请求字段与会话约定需要一起验证，
本地签名函数可运行并不代表平台当前接受该请求。

⚠️ TLS 指纹:走 curl_cffi 的 impersonate,对齐真实浏览器的 JA3/HTTP2 特征。
纯 httpx 的 TLS 指纹与浏览器不同,容易被风控按"非浏览器客户端"识别;impersonate
版本按下方 UA 的 Chrome 大版本自动选最接近的目标,UA 升级后无需改这里。
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from ...netfp import impersonate_for_ua
from .cookies import (
    cookie_str_from_state,
    extract_b1_from_state,
    has_a1,
    has_creator_cookies,
)
from .responses import XhsApiError, parse_response

_HOST = "https://edith.xiaohongshu.com"
_SEARCH_HOST = "https://so.xiaohongshu.com"
_DOMAIN = "https://www.xiaohongshu.com"
_DEFAULT_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
_STANDARD_B64 = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_CUSTOM_B64 = "ZmserbBoHQtNP+wOcza/LpngG8yJq42KWYj0DSfdikx3VT16IlUAFM97hECvuRX5"
_CUSTOM_B64_TRANS = str.maketrans(_STANDARD_B64, _CUSTOM_B64)


def _x2_for_ua(user_agent: str) -> str:
    ua = str(user_agent or "")
    if "Mac OS" in ua or "Macintosh" in ua:
        return "Mac OS"
    if "Linux" in ua and "Android" not in ua:
        return "Linux"
    return "Windows"


def _crc32_js_int(text: str) -> int:
    crc = 0xFFFFFFFF
    for ch in text:
        t = (crc & 0xFF) ^ (ord(ch) & 0xFF)
        for _ in range(8):
            t = ((t >> 1) ^ 0xEDB88320) if (t & 1) else (t >> 1)
        crc = (t ^ (crc >> 8)) & 0xFFFFFFFF
    u = (0xFFFFFFFF ^ crc ^ 0xEDB88320) & 0xFFFFFFFF
    return u - 0x100000000 if (u & 0x80000000) else u


def _build_xs_common_with_b1(cookie_str: str, b1: str, *, x2: str = "Windows") -> str:
    a1 = ""
    for part in str(cookie_str or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            if k.strip() == "a1":
                a1 = v.strip()
                break
    struct_obj = {
        "s0": 5,
        "s1": "",
        "x0": "1",
        "x1": "4.3.5",
        "x2": x2,
        "x3": "xhs-pc-web",
        "x4": "4.86.0",
        "x5": a1,
        "x6": "",
        "x7": "",
        "x8": b1,
        "x9": _crc32_js_int(b1),
        "x10": 0,
        "x11": "normal",
    }
    raw_json = json.dumps(struct_obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.b64encode(raw_json).decode("ascii").translate(_CUSTOM_B64_TRANS)


def _coherent_direct_headers(user_agent: str, impersonate: str) -> dict[str, str]:
    """Keep UA, Client Hints and curl_cffi's TLS target on one major."""
    target = re.search(r"chrome(\d+)", str(impersonate or ""), re.IGNORECASE)
    major = int(target.group(1)) if target else 136
    ua = str(user_agent or "").strip() or _DEFAULT_CHROME_UA
    if major and ua:
        ua = re.sub(
            r"Chrome/\d+(?:\.\d+){0,3}",
            f"Chrome/{major}.0.0.0", ua)
        ua = re.sub(
            r"Edg/\d+(?:\.\d+){0,3}",
            f"Edg/{major}.0.0.0", ua)
    platform = (
        '"macOS"' if ("Mac OS" in ua or "Macintosh" in ua)
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


class XhsApiClient:
    def __init__(self, cookie_str: str, user_agent: str = "", timeout: float = 30.0,
                 proxy: str = "", *, storage_state: str = "", b1: str = ""):
        from xhshow import Xhshow            # 延迟导入,未装库时也能加载本模块
        from xhshow.config import CryptoConfig
        from curl_cffi.requests import AsyncSession
        from ...browser.manager import normalize_proxy
        self._session_cls = AsyncSession
        self._session = None
        self._session_owner = None
        self.storage_state = storage_state or ""
        self.cookie_str = cookie_str or (
            cookie_str_from_state(self.storage_state) if self.storage_state else ""
        )
        self.b1 = str(b1 or "").strip() or extract_b1_from_state(self.storage_state)
        self.timeout = timeout
        from ...netfp import curl_proxy_url
        # 规范化:裸 host:port 补成 http://...; socks5 自动转 socks5h 远端 DNS
        self.proxy = curl_proxy_url(proxy) or None  # 该账号专属代理(防多账号同 IP 关联)
        raw_ua = str(user_agent or "").strip() or _DEFAULT_CHROME_UA
        self.impersonate = impersonate_for_ua(raw_ua)  # TLS/HTTP2 浏览器特征目标
        coherent = _coherent_direct_headers(raw_ua, self.impersonate)
        self.user_agent = coherent["user-agent"]
        self.x2 = _x2_for_ua(self.user_agent)
        cfg = CryptoConfig()
        cfg = cfg.with_overrides(
            PUBLIC_USERAGENT=self.user_agent,
            SIGNATURE_DATA_TEMPLATE={**cfg.SIGNATURE_DATA_TEMPLATE, "x2": self.x2},
            SIGNATURE_XSCOMMON_TEMPLATE={**cfg.SIGNATURE_XSCOMMON_TEMPLATE, "x2": self.x2},
        )
        self._signer = Xhshow(cfg)
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
    @asynccontextmanager
    async def session_scope(self):
        """Reuse one transport for a bounded operation, never across accounts.

        Nested scopes share their owner's session. Requests by other tasks use
        independent one-shot transports. Cookies remain the explicit login
        snapshot used by the signer, rather than an independently evolving jar.
        """
        owner = asyncio.current_task()
        if self._session_owner is not None:
            if self._session_owner is not owner:
                raise RuntimeError("API session_scope 已由另一个任务持有")
            yield self
            return
        # Reserve ownership before entering a transport that may suspend.
        self._session_owner = owner
        try:
            async with self._session_cls(discard_cookies=True, trust_env=False) as session:
                self._session = session
                try:
                    yield self
                finally:
                    self._session = None
        finally:
            self._session_owner = None

    async def _request(self, method: str, url: str, **kwargs):
        async def send(session):
            request = getattr(session, method)
            return await request(
                url, impersonate=self.impersonate, proxy=self.proxy,
                timeout=self.timeout, **kwargs)
        if self._session is not None and self._session_owner is asyncio.current_task():
            return await send(self._session)
        # A one-shot request owns only its own transport. Concurrent unscoped
        # requests must not borrow a session that another request will close.
        async with self._session_cls(discard_cookies=True, trust_env=False) as session:
            return await send(session)

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
            # 网页主签名(xhs_main_260411.js)+ method=GET,对"路径?query"签名
            spliced = creator_sign.splice_str(uri, params)
            a1 = creator_sign.trans_cookies(self.cookie_str).get("a1", "")
            headers = {
                **self.base_headers,
                **creator_sign.generate_xsc_main(
                    a1, spliced, "", "GET", b1=self.b1, user_agent=self.user_agent
                ),
                "Cookie": self.cookie_str,
            }
            url = _HOST + spliced
        else:
            sign = self._signer.sign_headers_get(uri, self.cookie_str, params=params or {})
            if self.b1:
                sign["x-s-common"] = _build_xs_common_with_b1(
                    self.cookie_str, self.b1, x2=self.x2
                )
            headers = {**self.base_headers, **sign, "Cookie": self.cookie_str}
            url = _HOST + (self._signer.build_url(uri, params) if params else uri)
        r = await self._request("get", url, headers=headers)
        return self._unwrap(r)

    async def _post(self, uri: str, data: Dict[str, Any], *,
                    host: str = _HOST) -> dict:
        sign = self._signer.sign_headers_post(uri, self.cookie_str, payload=data or {})
        if self.b1:
            sign["x-s-common"] = _build_xs_common_with_b1(
                self.cookie_str, self.b1, x2=self.x2
            )
        headers = {**self.base_headers, **sign, "Cookie": self.cookie_str}
        body = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        r = await self._request(
            "post", f"{host}{uri}", data=body.encode("utf-8"), headers=headers)
        return self._unwrap(r)

    @staticmethod
    def _unwrap(r) -> dict:
        return parse_response(r).get("data") or {}

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
            "extra": {"need_body_topic": "1"},
            "xsec_source": xsec_source or "pc_search",
            "xsec_token": xsec_token,
        }
        try:
            d = await self._post("/api/sns/web/v1/feed", data)
        except XhsApiError as exc:
            if exc.code != -510000 or exc.category != "business":
                raise
            raise XhsApiError(
                "笔记详情未返回（code=-510000，平台提示：笔记不存在）。"
                "请核对同账号可打开的完整笔记链接及访问参数；此结果未确认作品已删除。",
                category="business", status_code=exc.status_code,
                signal="note_unavailable", payload=exc.payload, code=exc.code) from exc
        items = d.get("items") or []
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise XhsApiError("笔记详情返回了异常的列表结构", category="risk", signal="ambiguous_response")
        for item in items:
            card = item.get("note_card") or {}
            if not isinstance(card, dict):
                raise XhsApiError("笔记详情卡片结构异常", category="risk", signal="ambiguous_response")
            actual_id = str(card.get("note_id") or card.get("id") or item.get("id") or "")
            if actual_id == note_id:
                return item
        if items:
            raise XhsApiError("笔记详情与请求的笔记 ID 不一致", signal="note_id_mismatch")
        return {}

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

    async def favorite_boards(self, user_id: str) -> List[dict]:
        data = await self._get("/api/sns/web/v1/board/user", {
            "user_id": user_id,
            "image_formats": "jpg,webp,avif",
        })
        return data.get("boards") or []

    async def favorite_notes(self, *, board_id: str = "", cursor: str = "",
                             page_size: int = 30) -> dict:
        page_size = max(1, min(50, int(page_size or 30)))
        if board_id:
            return await self._get("/api/sns/web/v1/board/note", {
                "board_id": board_id, "cursor": cursor, "num": page_size,
                "image_formats": "jpg,webp,avif",
            })
        return await self._get("/api/sns/web/v2/note/collect/page", {
            "cursor": cursor, "num": page_size,
            "image_formats": "jpg,webp,avif",
        })

    async def note_comments(self, note_id: str, xsec_token: str = "",
                            cursor: str = "", xsec_source: str = "") -> dict:
        params = {
            "note_id": note_id, "cursor": cursor, "top_comment_id": "",
            "image_formats": "jpg,webp,avif", "xsec_token": xsec_token,
        }
        if xsec_source:
            params["xsec_source"] = xsec_source
        return await self._get("/api/sns/web/v2/comment/page", params)

    async def note_sub_comments(self, note_id: str, root_comment_id: str,
                                xsec_token: str = "", cursor: str = "",
                                page_size: int = 10) -> dict:
        """Read one reply page; pagination and pacing live in comments.py."""
        if not note_id or not root_comment_id:
            raise ValueError("读取回复需要笔记 ID 和根评论 ID")
        return await self._get("/api/sns/web/v2/comment/sub/page", {
            "note_id": note_id, "root_comment_id": root_comment_id,
            "num": max(1, min(30, int(page_size))), "cursor": cursor,
            "image_formats": "jpg,webp,avif", "top_comment_id": "",
            "xsec_token": xsec_token,
        })

    async def collect_note_comments(self, note_id: str, xsec_token: str = "", *,
                                    xsec_source: str = "pc_feed",
                                    max_comments: int = 200, max_requests: int = 20,
                                    include_replies: bool = True,
                                    request_interval: float = 0.5) -> dict:
        from .comments import collect_note_comments
        return await collect_note_comments(
            self, note_id, xsec_token, xsec_source=xsec_source,
            max_comments=max_comments, max_requests=max_requests,
            include_replies=include_replies, request_interval=request_interval)

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
