"""Rednote (海外小红书) signed direct API client (`webapi.rednote.com` & `creator.rednote.com`).

Design & safety rules:
  - Strictly binds to ``webapi.rednote.com`` (``editDomain``) and ``creator.rednote.com``
    (``creatorDomain``), with ``Origin``/``Referer`` on ``www.rednote.com`` or
    ``creator.rednote.com``; never falls back to domestic ``xiaohongshu.com`` hosts.
  - Uses ``curl_cffi`` Chrome TLS/HTTP2 impersonation and per-account proxy isolation.
  - Signs requests with exact ``XYS_`` / ``XYW_`` + ``rednote.com`` ``b1`` / ``dsllt`` / ``sc``
    environment context and ``//webapi.rednote.com`` ``x-rap-param`` envelopes.
  - Defaults ``rednote_read_mode`` to ``hybrid`` (API-first with automatic browser
    fallback on confirmed compatibility/verification errors) and also supports ``api`` / ``browser``.
"""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Literal, Optional

from ...netfp import impersonate_for_ua
from ..xhs.client import _coherent_direct_headers
from ..xhs.responses import XhsApiError as RednoteApiError, parse_response
from .cookies import cookie_str_from_state, has_a1, has_creator_cookies
from .domains import CREATOR_URL, FRONT_HOME_PAGE_URL, WEBAPI_URL, WWW_URL, is_rednote_url
from .signer import (
    RednoteSignContext,
    RednoteSigner,
    canonical_request,
    resolve_rednote_sign_context,
)


def _accept_language_for_locale(locale: str) -> str:
    loc = str(locale or "").strip()
    if not loc or loc.lower().startswith("zh-cn"):
        return "en-US,en;q=0.9,zh-CN;q=0.8"
    primary = loc.split("-", 1)[0]
    if primary and primary.lower() != loc.lower():
        return f"{loc},{primary};q=0.9,en;q=0.8"
    return f"{loc},en;q=0.9"


class RednoteApiClient:
    """Signed HTTP client strictly scoped to ``rednote.com`` subdomains."""

    def __init__(
        self,
        cookie_str: str,
        user_agent: str,
        timeout: float = 30.0,
        proxy: str = "",
        *,
        locale: str = "en-US",
        timezone: str = "UTC",
        storage_state: str = "",
        sign_context: RednoteSignContext | None = None,
        user_id: str = "",
    ):
        from curl_cffi.requests import AsyncSession
        from xhshow import Xhshow

        from ...browser.manager import normalize_proxy

        self._session_cls = AsyncSession
        self._session = None
        self._session_owner = None
        self.cookie_str = cookie_str or ""
        self.timeout = timeout
        self.proxy = normalize_proxy(proxy) or None
        self.impersonate = impersonate_for_ua(user_agent)
        coherent = _coherent_direct_headers(user_agent, self.impersonate)
        self._signer = RednoteSigner()
        self._search_id_helper = Xhshow()
        self._comment_api_blocked = False
        self._blocked_get_uris: set[str] = set()
        self.user_id = str(user_id or "").strip()
        self.sign_context = resolve_rednote_sign_context(
            self.cookie_str,
            storage_state=storage_state,
            explicit_context=sign_context,
            user_agent=user_agent,
            locale=locale,
            timezone=timezone,
        )
        self.accept_language = _accept_language_for_locale(locale)
        self.base_headers = {
            "accept": "application/json, text/plain, */*",
            "accept-language": self.accept_language,
            "content-type": "application/json;charset=UTF-8",
            "origin": WWW_URL,
            "referer": f"{FRONT_HOME_PAGE_URL}",
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            **coherent,
        }

    @asynccontextmanager
    async def session_scope(self):
        owner = asyncio.current_task()
        if self._session_owner is not None:
            if self._session_owner is not owner:
                raise RuntimeError("Rednote API session_scope 已由另一个任务持有")
            yield self
            return
        self._session_owner = owner
        try:
            async with self._session_cls(discard_cookies=True) as session:
                self._session = session
                try:
                    yield self
                finally:
                    self._session = None
        finally:
            self._session_owner = None

    async def _request(self, method: str, url: str, **kwargs):
        if not is_rednote_url(url):
            raise RednoteApiError(
                f"拒绝向非 rednote.com 域名发送请求: {url}",
                category="risk",
                signal="cross_domain_blocked",
            )

        async def send(session):
            request_fn = getattr(session, method)
            return await request_fn(
                url,
                impersonate=self.impersonate,
                proxy=self.proxy,
                timeout=self.timeout,
                **kwargs,
            )

        if self._session is not None and self._session_owner is asyncio.current_task():
            return await send(self._session)
        async with self._session_cls(discard_cookies=True) as session:
            return await send(session)

    def _headers_for(self, *, is_creator: bool = False) -> dict[str, str]:
        if not is_creator:
            return dict(self.base_headers)
        return {
            **self.base_headers,
            "origin": CREATOR_URL,
            "referer": f"{CREATOR_URL}/new/note-manager",
        }

    async def _get(
        self,
        uri: str,
        params: Dict[str, Any] | None = None,
        *,
        is_creator: bool = False,
        sign_format: Literal["xys", "xyw"] | None = None,
    ) -> dict:
        if uri in self._blocked_get_uris:
            raise RednoteApiError(
                f"海外小红书接口 {uri} 需页面实时环境签名(HTTP 461)，切换浏览器兜底",
                category="business",
                status_code=461,
                signal="get_requires_browser",
            )
        query_params = params or {}
        host = CREATOR_URL if is_creator else WEBAPI_URL
        base_hdrs = self._headers_for(is_creator=is_creator)
        sign = self._signer.sign_headers(
            "GET",
            uri,
            self.cookie_str,
            params=query_params,
            sign_format=sign_format,
            user_id=self.user_id or None,
            b1=self.sign_context.b1,
            dsl_pair=self.sign_context.dsl_pair,
            sign_count=self.sign_context.next_sign_count(),
        )
        headers = {**base_hdrs, **sign, "Cookie": self.cookie_str}
        url = host + canonical_request("GET", uri, query_params)
        resp = await self._request("get", url, headers=headers)
        try:
            return self._unwrap(resp)
        except RednoteApiError as exc:
            if exc.status_code in (406, 461):
                self._blocked_get_uris.add(uri)
                if "comment" in uri:
                    self._comment_api_blocked = True
                try:
                    body_json = resp.json()
                except Exception:
                    body_json = None
                if (
                    isinstance(body_json, dict)
                    and body_json.get("code") == 0
                    and not body_json.get("data")
                    and not body_json.get("msg")
                ):
                    raise RednoteApiError(
                        f"海外小红书接口 {uri} 需页面实时签名(HTTP {exc.status_code})，建议切换混合/浏览器读取",
                        category="risk",
                        status_code=exc.status_code,
                        signal=f"http_{exc.status_code}",
                    ) from exc
            raise

    async def _post(
        self,
        uri: str,
        data: Dict[str, Any] | None = None,
        *,
        is_creator: bool = False,
        creator_origin: bool = False,
        x_rap: bool | None = None,
    ) -> dict:
        payload = data or {}
        host = CREATOR_URL if is_creator else WEBAPI_URL
        base_hdrs = self._headers_for(is_creator=(is_creator or creator_origin))
        sign = self._signer.sign_headers(
            "POST",
            uri,
            self.cookie_str,
            payload=payload,
            user_id=self.user_id or None,
            x_rap=x_rap,
            b1=self.sign_context.b1,
            dsl_pair=self.sign_context.dsl_pair,
            sign_count=self.sign_context.next_sign_count(),
        )
        headers = {**base_hdrs, **sign, "Cookie": self.cookie_str}
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        resp = await self._request(
            "post", f"{host}{uri}", data=body.encode("utf-8"), headers=headers
        )
        return self._unwrap(resp)

    @staticmethod
    def _unwrap(resp) -> dict:
        return parse_response(resp).get("data") or {}

    # ── Account & Profile ──
    async def self_info(self) -> dict:
        data = await self._get("/api/sns/web/v2/user/me", {})
        if isinstance(data, dict) and data.get("user_id") and not self.user_id:
            self.user_id = str(data["user_id"])
        return data

    async def creator_info(self) -> dict:
        return await self._get("/api/galaxy/user/info", {}, is_creator=True)

    async def user_info(self, user_id: str) -> dict:
        return await self._get("/api/sns/web/v1/user/otherinfo", {"target_user_id": user_id})

    # ── Discovery, Detail & Favorites ──
    async def search_notes(
        self,
        keyword: str,
        page: int = 1,
        page_size: int = 20,
        sort: str = "general",
        note_type: int = 0,
    ) -> List[dict]:
        page = max(1, int(page or 1))
        page_size = max(10, min(40, int(page_size or 20)))
        data = {
            "keyword": keyword,
            "page": page,
            "page_size": page_size,
            "search_id": self._search_id_helper.get_search_id(),
            "sort": sort,
            "note_type": note_type,
            "image_formats": ["jpg", "webp", "avif"],
        }
        res = await self._post("/api/sns/web/v2/search/notes", data)
        return res.get("items") or []

    async def note_detail_raw(
        self,
        note_id: str,
        xsec_token: str = "",
        xsec_source: str = "pc_search",
    ) -> dict:
        data = {
            "source_note_id": note_id,
            "image_formats": ["jpg", "webp", "avif"],
            "extra": {"need_body_topic": "1"},
            "xsec_source": xsec_source or "pc_search",
            "xsec_token": xsec_token,
        }
        try:
            res = await self._post("/api/sns/web/v1/feed", data)
        except RednoteApiError as exc:
            if exc.code != -510000 or exc.category != "business":
                raise
            raise RednoteApiError(
                "海外小红书笔记详情未返回（code=-510000，平台提示：笔记不存在或访问参数失效）。",
                category="business",
                status_code=exc.status_code,
                signal="note_unavailable",
                payload=exc.payload,
                code=exc.code,
            ) from exc
        items = res.get("items") or []
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise RednoteApiError(
                "笔记详情返回了异常的列表结构", category="risk", signal="ambiguous_response"
            )
        for item in items:
            card = item.get("note_card") or {}
            if not isinstance(card, dict):
                raise RednoteApiError(
                    "笔记详情卡片结构异常", category="risk", signal="ambiguous_response"
                )
            actual_id = str(card.get("note_id") or card.get("id") or item.get("id") or "")
            if actual_id == note_id:
                return item
        if items:
            raise RednoteApiError("笔记详情与请求的笔记 ID 不一致", signal="note_id_mismatch")
        return {}

    async def note_detail(
        self,
        note_id: str,
        xsec_token: str = "",
        xsec_source: str = "pc_search",
    ) -> dict:
        item = await self.note_detail_raw(note_id, xsec_token, xsec_source)
        return item.get("note_card") or {}

    async def notes_by_creator(
        self,
        user_id: str,
        cursor: str = "",
        page_size: int = 30,
        xsec_token: str = "",
        xsec_source: str = "pc_feed",
    ) -> dict:
        params = {
            "num": page_size,
            "cursor": cursor,
            "user_id": user_id,
            "image_formats": "jpg,webp,avif",
            "xsec_token": xsec_token,
            "xsec_source": xsec_source or "pc_feed",
        }
        return await self._get("/api/sns/web/v1/user_posted", params)

    async def favorite_boards(self, user_id: str) -> List[dict]:
        data = await self._get(
            "/api/sns/web/v1/board/user",
            {"user_id": user_id, "image_formats": "jpg,webp,avif"},
        )
        return data.get("boards") or []

    async def favorite_notes(
        self, *, board_id: str = "", cursor: str = "", page_size: int = 30
    ) -> dict:
        page_size = max(1, min(50, int(page_size or 30)))
        if board_id:
            return await self._get(
                "/api/sns/web/v1/board/note",
                {
                    "board_id": board_id,
                    "cursor": cursor,
                    "num": page_size,
                    "image_formats": "jpg,webp,avif",
                },
            )
        return await self._get(
            "/api/sns/web/v2/note/collect/page",
            {"cursor": cursor, "num": page_size, "image_formats": "jpg,webp,avif"},
        )

    # ── Public Comments (Read-Only) ──
    async def note_comments(
        self,
        note_id: str,
        xsec_token: str = "",
        cursor: str = "",
        xsec_source: str = "",
    ) -> dict:
        if self._comment_api_blocked:
            raise RednoteApiError(
                "海外小红书评论接口需页面实时环境签名(HTTP 461)，切换浏览器兜底",
                category="business",
                status_code=461,
                signal="comment_page_requires_browser",
            )
        params: Dict[str, Any] = {
            "note_id": note_id,
            "cursor": cursor,
            "top_comment_id": "",
            "image_formats": "jpg,webp,avif",
            "xsec_token": xsec_token,
            "need_translation": "1",
        }
        if xsec_source:
            params["xsec_source"] = xsec_source
        try:
            return await self._get("/api/sns/web/v2/comment/page", params, sign_format="xys")
        except RednoteApiError as exc:
            if exc.status_code in (406, 461):
                self._comment_api_blocked = True
            raise

    async def note_sub_comments(
        self,
        note_id: str,
        root_comment_id: str,
        xsec_token: str = "",
        cursor: str = "",
        page_size: int = 10,
    ) -> dict:
        if not note_id or not root_comment_id:
            raise ValueError("读取回复需要笔记 ID 和根评论 ID")
        try:
            return await self._get(
                "/api/sns/web/v2/comment/sub/page",
                {
                    "note_id": note_id,
                    "root_comment_id": root_comment_id,
                    "num": max(1, min(30, int(page_size))),
                    "cursor": cursor,
                    "image_formats": "jpg,webp,avif",
                    "top_comment_id": "",
                    "xsec_token": xsec_token,
                },
                sign_format="xys",
            )
        except RednoteApiError as exc:
            if exc.status_code in (406, 461):
                self._comment_api_blocked = True
            raise

    async def collect_note_comments(
        self,
        note_id: str,
        xsec_token: str = "",
        *,
        xsec_source: str = "pc_feed",
        max_comments: int = 200,
        max_requests: int = 20,
        include_replies: bool = True,
        request_interval: float = 0.5,
    ) -> dict:
        from ..xhs.comments import collect_note_comments

        return await collect_note_comments(
            self,
            note_id,
            xsec_token,
            xsec_source=xsec_source,
            max_comments=max_comments,
            max_requests=max_requests,
            include_replies=include_replies,
            request_interval=request_interval,
        )

    # ── Creator Helper & Work Management Contracts (Recovered from Rednote Worker) ──
    async def published_notes(self, max_pages: int = 10) -> List[dict]:
        notes: List[dict] = []
        page: Any = None
        for _ in range(max(1, int(max_pages))):
            params: Dict[str, Any] = {"tab": "0"}
            if page is not None:
                params["page"] = str(page)
            data = await self._get(
                "/api/galaxy/v2/creator/note/user/posted",
                params,
                is_creator=True,
            )
            page_notes = (
                data.get("notes")
                or data.get("note_infos")
                or data.get("noteList")
                or []
            )
            notes.extend(page_notes)
            page = data.get("page")
            if page in (-1, None) or not page_notes:
                break
        return notes

    async def search_topic(self, keyword: str, title: str = "") -> List[dict]:
        """Search publish topics on ``webapi.rednote.com`` (`24397` contract)."""
        kw = str(keyword or "").strip()[:20]
        if not kw:
            return []
        data = {
            "keyword": kw,
            "suggest_topic_request": {"title": str(title or ""), "desc": f"#{kw}"},
            "page": {"page_size": 20, "page": 1},
        }
        res = await self._post("/web_api/sns/v1/search/topic", data, creator_origin=True)
        dtos = res.get("topic_info_dtos") or []
        return [row for row in dtos if isinstance(row, dict)] if isinstance(dtos, list) else []

    async def add_topic(self, name: str) -> Optional[dict]:
        """Create/resolve custom topic on ``webapi.rednote.com`` (`24402` contract)."""
        clean = str(name or "").strip().lstrip("#").strip()
        if not clean:
            return None
        res = await self._post(
            "/web_api/sns/capa/postgw/topic/batch_customized",
            {"topic_names": [clean]},
            creator_origin=True,
        )
        infos = res.get("topic_infos") or []
        if isinstance(infos, list) and infos and isinstance(infos[0], dict):
            item = dict(infos[0])
            item.setdefault("type", "topic")
            return item
        return None

    async def search_mention(self, keyword: str) -> List[dict]:
        """Search @mention users on ``webapi.rednote.com`` (`24406` contract)."""
        kw = str(keyword or "").strip()
        if not kw:
            return []
        search_id = f"{int(time.time() * 1000)}.int"
        res = await self._post(
            "/web_api/sns/v1/search/user_info",
            {"keyword": kw, "search_id": search_id, "page": {"page_size": 20, "page": 1}},
            creator_origin=True,
        )
        dtos = res.get("user_info_dtos") or []
        if not isinstance(dtos, list):
            return []
        out: List[dict] = []
        for row in dtos:
            if not isinstance(row, dict):
                continue
            base = row.get("user_base_dto") if isinstance(row.get("user_base_dto"), dict) else row
            uid = str(base.get("user_id") or row.get("user_id") or "").strip()
            nick = str(base.get("user_nickname") or base.get("nickname") or "").strip()
            if uid and nick:
                out.append({
                    "user_id": uid,
                    "user_nickname": nick,
                    "image": str(base.get("image") or base.get("user_avatar") or ""),
                    "red_id": str(base.get("red_id") or ""),
                })
        return out

    async def get_position_list(
        self, keyword: str, *, latitude: float = 0.0, longitude: float = 0.0
    ) -> List[dict]:
        """Search POI locations on ``webapi.rednote.com`` (`24392` contract)."""
        kw = str(keyword or "").strip()
        data: Dict[str, Any] = {"keyword": kw}
        if latitude or longitude:
            data["latitude"] = latitude
            data["longitude"] = longitude
        res = await self._post(
            "/web_api/sns/v1/local/poi/creator/search",
            data,
            creator_origin=True,
        )
        pois = res.get("poi_list") or []
        return [row for row in pois if isinstance(row, dict)] if isinstance(pois, list) else []

    async def delete_note(self, note_id: str) -> dict:
        """Delete own note via creator gateway (`rednote` explicitly supported set)."""
        nid = str(note_id or "").strip()
        if not nid:
            raise ValueError("缺少笔记 ID")
        return await self._post(
            "/web_api/sns/capa/postgw/note/delete",
            {"note_id": nid},
            creator_origin=True,
        )

    async def set_note_privacy(self, note_id: str, privacy_type: int) -> dict:
        """Update own note visibility (`0` public, `1` private) via creator gateway."""
        nid = str(note_id or "").strip()
        if not nid:
            raise ValueError("缺少笔记 ID")
        if privacy_type not in (0, 1):
            raise ValueError("海外小红书可见范围仅支持公开(0)或仅自己可见(1)")
        return await self._post(
            "/web_api/sns/v1/note/privacy",
            {"note_id": nid, "privacy_info": {"op_type": 1, "type": int(privacy_type), "user_ids": []}},
            creator_origin=True,
        )


__all__ = [
    "RednoteApiClient",
    "RednoteApiError",
    "cookie_str_from_state",
    "has_a1",
    "has_creator_cookies",
]
