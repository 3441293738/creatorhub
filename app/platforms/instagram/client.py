"""Instagram 双栈直连异步客户端（Android Mobile API v1 + Web Session HTTP）。

架构说明：
- 移动端通道 (`i.instagram.com/api/v1/`): `Bearer IGT:2:` + 设备指纹头 + 空 Cookie 容器，根据响应头动态刷新 `AuthBearer` / `Claims` / `Rur` / `ds_user_id`。
- Web 会话通道 (`www.instagram.com` / `i.instagram.com`): 浏览器 Cookie + `X-CSRFToken` + `X-Ig-App-Id: 936619743392459`。
- 写操作在跨越网络发送边界前统一回调 `notify_submit(on_submit)`；一旦已发送但网络超时/5xx 丢失明确响应，一律标记 `write_uncertain:` 防止重复提交。
"""
from __future__ import annotations

import asyncio
import json
import math
import random
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote, urlencode

from curl_cffi.requests import AsyncSession

from ...browser.manager import normalize_proxy
from ...netfp import impersonate_for_ua
from .._submit import notify_submit
from .extract import (
    Aweme,
    cookie_map_from_state,
    flatten_ig_comments,
    parse_ig_aweme,
    parse_ig_comment,
    parse_ig_creator_work,
    parse_ig_dm_inbox,
    parse_ig_dm_messages,
    parse_ig_follow_users,
    parse_ig_json_or_ndjson,
    parse_ig_reels_chunked_response,
    parse_ig_self_user,
)
from .protocol import (
    BASEAPI_URL,
    BASEAPI_URL_NEW,
    RETRY_CONTEXT_DEFAULT,
    RUPLOAD_PHOTO_URL,
    RUPLOAD_VIDEO_URL,
    SUPPORTED_CAPABILITIES_JSON,
    WWW_BASE,
    InstagramSessionState,
    build_mobile_headers,
    build_web_headers,
    generate_comment_breadcrumb,
    generate_upload_id,
    ig_configure_content,
    ig_video_duration_values,
    signed_dict_content,
    timezone_offset_seconds,
    unique_time_id,
    unsigned_content,
)
from .resolve import (
    clean_ig_username,
    media_id_to_shortcode,
    resolve_ig_user_target,
    resolve_ig_work_ref,
    shortcode_to_media_id,
)

DEFAULT_WEB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

_URL_IN_TEXT_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_HASHTAG_CLEAN_RE = re.compile(r"[#\s]+")


@dataclass
class IgHttpResponse:
    status_code: int
    headers: Dict[str, str]
    text: str

    def json(self) -> Any:
        return json.loads(self.text)


TransportCallable = Callable[
    [str, str, Dict[str, str], Optional[bytes], float],
    Awaitable[IgHttpResponse],
]


class InstagramApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 0, category: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.category = category


def classify_ig_payload_error(status_code: int, payload: Any, fallback_text: str = "") -> str:
    """将 Instagram HTTP 状态与 JSON 错误体映射为 CreatorHub 风控分类前缀。"""
    msg = ""
    err_type = ""
    spam = False
    is_age_gated = False
    if isinstance(payload, Mapping):
        msg = str(
            payload.get("message")
            or payload.get("error_title")
            or payload.get("feedback_message")
            or payload.get("feedback_title")
            or ""
        ).strip()
        err_type = str(payload.get("error_type") or "").strip().lower()
        spam = bool(payload.get("spam"))
        if "age_gated_info" in payload or "mark_user_overage" in str(payload):
            is_age_gated = True

    low = f"{msg} {err_type} {fallback_text[:200]}".lower()
    if is_age_gated:
        return f"age_gated:{msg or 'age_gated'}"
    if (
        status_code == 401
        or "login_required" in low
        or "session has been invalidated" in low
        or "user_has_logged_out" in low
    ):
        return f"logged_out:Instagram 登录态已失效({msg or status_code})"
    if (
        "checkpoint_required" in low
        or "challenge_required" in low
        or "checkpoint_challenge_required" in low
        or "delta_login_review" in low
    ):
        return f"verification_required:Instagram 触发安全挑战({msg or 'challenge_required'})"
    if (
        status_code == 429
        or spam
        or "feedback_required" in low
        or "please wait a few minutes" in low
        or "rate_limit" in low
        or "sentry_block" in low
    ):
        return f"rate_limit:Instagram 触发频控或操作限制({msg or status_code})"
    if status_code == 404 or "media_not_found" in low or "user_not_found" in low or "not found" in low:
        return f"not_found:{msg or '目标不存在或已删除'}"
    if "transcode not finished" in low or "transcode_not_finished" in low or "media_needs_reupload" in low:
        return f"transcode_pending:{msg or '视频转码尚未完成'}"
    return msg or f"http_{status_code}"


class InstagramClient:
    """按账号隔离的 Instagram Mobile API + Web API 异步客户端。"""

    def __init__(
        self,
        storage_state: str | Mapping[str, Any] | None = None,
        user_agent: str = "",
        *,
        proxy: str = "",
        timeout: float = 30.0,
        account_seed: str = "",
        timezone_id: str = "Asia/Shanghai",
        android_device_ua: str = "",
        new_domain: bool = False,
        transport: Optional[TransportCallable] = None,
    ):
        cookies = cookie_map_from_state(storage_state)
        seed = account_seed or cookies.get("ds_user_id") or cookies.get("mid") or "ig-default"
        self.web_user_agent = str(user_agent or DEFAULT_WEB_UA)
        self.proxy = normalize_proxy(proxy) or None
        self.timeout = float(timeout)
        self.impersonate = impersonate_for_ua(self.web_user_agent)
        self.session = InstagramSessionState(
            seed=str(seed),
            android_device_ua=android_device_ua,
            web_user_agent=self.web_user_agent,
            timezone_offset=timezone_offset_seconds(timezone_id),
            new_domain=new_domain,
        )
        self.session.import_browser_cookies(cookies)
        self.last_error: str = ""
        self._http: Optional[AsyncSession] = None
        self._transport = transport

    @property
    def self_uid(self) -> str:
        return str(self.session.auth_ds_user_id or "")

    @property
    def base_api_url(self) -> str:
        return BASEAPI_URL_NEW if self.session.new_domain else BASEAPI_URL

    @asynccontextmanager
    async def session_scope(self):
        if self._transport is not None or self._http is not None:
            yield self
            return
        async with AsyncSession(impersonate=self.impersonate) as sess:
            self._http = sess
            try:
                yield self
            finally:
                self._http = None

    async def _send_raw(
        self,
        method: str,
        url: str,
        headers: Dict[str, str],
        body: Optional[bytes] = None,
        *,
        long_request: bool = False,
    ) -> IgHttpResponse:
        req_timeout = max(self.timeout, 300.0) if long_request else self.timeout
        if self._transport is not None:
            return await self._transport(method.upper(), url, headers, body, req_timeout)

        if self._http is not None:
            resp = await self._http.request(
                method.upper(),
                url,
                data=body,
                headers=headers,
                proxy=self.proxy,
                timeout=req_timeout,
            )
        else:
            async with AsyncSession(impersonate=self.impersonate) as sess:
                resp = await sess.request(
                    method.upper(),
                    url,
                    data=body,
                    headers=headers,
                    proxy=self.proxy,
                    timeout=req_timeout,
                )
        hdrs = {str(k): str(v) for k, v in (resp.headers or {}).items()}
        return IgHttpResponse(status_code=int(resp.status_code), headers=hdrs, text=resp.text or "")

    async def _mobile_request(
        self,
        method: str,
        path_or_url: str,
        *,
        operation: str = "",
        body: Optional[bytes] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
        long_request: bool = False,
        allow_560_retry: bool = True,
    ) -> Tuple[Optional[IgHttpResponse], Any, str]:
        """发送移动端 API 请求并执行响应头回写、operation 挂起及 HTTP 560 单次重试。"""
        if self.session.is_operation_suspended(operation, web=False):
            return None, None, f"rate_limit:操作 {operation} 处于短暂退避冷却中"

        url = path_or_url if path_or_url.startswith("http") else (self.base_api_url + path_or_url.lstrip("/"))
        if self.session.new_domain and url.startswith(BASEAPI_URL):
            url = url.replace(BASEAPI_URL, BASEAPI_URL_NEW, 1)

        hdrs = build_mobile_headers(self.session, url, method=method, extra=extra_headers)
        if body is not None and "Content-Type" not in hdrs:
            hdrs["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"

        attempts = 2 if allow_560_retry else 1
        resp: Optional[IgHttpResponse] = None
        for attempt in range(attempts):
            try:
                resp = await self._send_raw(method, url, hdrs, body, long_request=long_request)
            except Exception as exc:
                err = f"network_error:{type(exc).__name__}:{exc}"
                self.last_error = err
                return None, None, err
            self.session.update_from_response_headers(resp.headers)
            if resp.status_code == 560 and attempt + 1 < attempts:
                await asyncio.sleep(random.uniform(1.0, 3.0))
                continue
            break

        assert resp is not None
        parsed: Any = parse_ig_json_or_ndjson(resp.text)

        if resp.status_code in {401, 429}:
            self.session.suspend_operation(operation, web=False)

        if 200 <= resp.status_code < 300:
            if isinstance(parsed, Mapping) and str(parsed.get("status") or "ok").lower() == "fail":
                err = classify_ig_payload_error(resp.status_code, parsed, resp.text)
                self.last_error = err
                return resp, parsed, err
            return resp, parsed, ""

        err = classify_ig_payload_error(resp.status_code, parsed, resp.text)
        self.last_error = err
        return resp, parsed, err

    async def _web_request(
        self,
        method: str,
        path_or_url: str,
        *,
        operation: str = "",
        referer: str = "https://www.instagram.com/",
        body: Optional[bytes] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> Tuple[Optional[IgHttpResponse], Any, str]:
        """发送 Web 浏览器会话 HTTP 请求。"""
        if not self.session.browser_cookies:
            return None, None, "logged_out:缺少 Instagram 浏览器 Cookie"
        if self.session.is_operation_suspended(operation, web=True):
            return None, None, f"rate_limit:Web 操作 {operation} 处于退避冷却中"

        url = path_or_url if path_or_url.startswith("http") else f"{WWW_BASE}/api/v1/{path_or_url.lstrip('/')}"
        hdrs = build_web_headers(self.session, url, referer=referer, extra=extra_headers)
        if body is not None and "Content-Type" not in hdrs:
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"

        attempts = 2 if method.upper() == "GET" else 1
        resp: Optional[IgHttpResponse] = None
        for attempt in range(attempts):
            try:
                resp = await self._send_raw(method, url, hdrs, body)
            except Exception as exc:
                err = f"network_error:{type(exc).__name__}:{exc}"
                self.last_error = err
                return None, None, err
            self.session.update_from_response_headers(resp.headers)
            if resp.status_code == 560 and attempt + 1 < attempts:
                await asyncio.sleep(random.uniform(1.0, 3.0))
                continue
            break

        assert resp is not None
        if resp.status_code in {401, 429}:
            self.session.suspend_operation(operation, web=True)

        parsed: Any = parse_ig_json_or_ndjson(resp.text)

        if 200 <= resp.status_code < 300:
            if isinstance(parsed, Mapping) and str(parsed.get("status") or "ok").lower() == "fail":
                err = classify_ig_payload_error(resp.status_code, parsed, resp.text)
                self.last_error = err
                return resp, parsed, err
            return resp, parsed, ""

        err = classify_ig_payload_error(resp.status_code, parsed, resp.text)
        self.last_error = err
        return resp, parsed, err

    # ───────────────────── 资料与用户检索 ─────────────────────

    async def fetch_self_profile(self) -> Tuple[Dict[str, Any], str]:
        """获取当前登录账号资料（Mobile `users/{uid}/info/` -> `accounts/current_user/` -> Web 回退）。"""
        if not self.session.is_logged_in:
            return {}, "logged_out:未检测到有效的 Instagram sessionid"

        if self.self_uid.isdigit():
            _r, data, err = await self._mobile_request(
                "GET", f"users/{self.self_uid}/info/", operation="GetSelfUserInfo"
            )
            if not err and isinstance(data, Mapping):
                prof = parse_ig_self_user(data)
                if prof.get("uid") or prof.get("unique_id"):
                    return prof, ""
            if err.startswith(("logged_out:", "verification_required:")):
                return {}, err

        _r, data, err = await self._mobile_request(
            "GET", "accounts/current_user/?edit=true", operation="GetCurrentUser"
        )
        if not err and isinstance(data, Mapping):
            prof = parse_ig_self_user(data)
            uname = prof.get("unique_id") or ""
            if uname and not prof.get("follower_count"):
                full_prof, _ = await self.fetch_user_by_username(uname)
                if full_prof:
                    return full_prof, ""
            if prof.get("uid") or uname:
                return prof, ""
        if err.startswith(("logged_out:", "verification_required:")):
            return {}, err

        _r2, data2, err2 = await self._web_request(
            "GET", "accounts/edit/web_form_data/", operation="WebGetSelfProfile"
        )
        if not err2 and isinstance(data2, Mapping):
            form = data2.get("form_data") if isinstance(data2.get("form_data"), Mapping) else data2
            uname = str(form.get("username") or "").strip()
            if uname:
                return await self.fetch_user_by_username(uname)
        return {}, err or err2 or "无法读取 Instagram 账号资料"

    async def fetch_user_by_username(self, username: str) -> Tuple[Dict[str, Any], str]:
        """按用户名读取资料（`users/{username}/usernameinfo/` 优先，Web `users/web_profile_info/` 回退）。"""
        uname = clean_ig_username(username) or str(username or "").strip().lstrip("@")
        if not uname:
            return {}, "用户名不能为空"

        _r, data, err = await self._mobile_request(
            "GET", f"users/{quote(uname)}/usernameinfo/", operation="GetUserByUsername"
        )
        if not err and isinstance(data, Mapping):
            prof = parse_ig_self_user(data)
            if prof.get("uid") or prof.get("unique_id"):
                return prof, ""
        if err.startswith(("logged_out:", "verification_required:", "not_found:")):
            return {}, err

        _r2, data2, err2 = await self._web_request(
            "GET",
            f"users/web_profile_info/?username={quote(uname)}",
            referer=f"{WWW_BASE}/{uname}/",
            operation="WebGetUserProfileInfo",
        )
        if not err2 and isinstance(data2, Mapping):
            prof = parse_ig_self_user(data2)
            if prof.get("uid") or prof.get("unique_id"):
                return prof, ""
        return {}, err or err2 or "用户查询失败"

    async def fetch_user_by_id(self, user_id: str) -> Tuple[Dict[str, Any], str]:
        """按数字 `user_id` 查询用户资料（`GET users/{userId}/info/`）。"""
        uid = str(user_id or "").strip()
        if not uid.isdigit():
            return await self.fetch_user_by_username(uid)
        _r, data, err = await self._mobile_request(
            "GET", f"users/{uid}/info/", operation="GetUserInfo"
        )
        if not err and isinstance(data, Mapping):
            return parse_ig_self_user(data), ""
        return {}, err or "用户 ID 查询失败"

    async def resolve_user_id(self, user_ref: str) -> Tuple[str, str, Dict[str, Any], str]:
        """将 `@username`、主页 URL 或数字 `pk` 统一解析为 `(pk, username, profile, err)`。"""
        target = resolve_ig_user_target(user_ref) or str(user_ref or "").strip()
        if not target:
            return "", "", {}, "无效的 Instagram 用户标识"
        if target.isdigit():
            prof, err = await self.fetch_user_by_id(target)
            return target, str(prof.get("unique_id") or ""), prof, ("" if prof else err)
        prof, err = await self.fetch_user_by_username(target)
        pk = str(prof.get("uid") or "")
        uname = str(prof.get("unique_id") or target)
        if not pk:
            return "", uname, {}, err or f"无法解析 Instagram 用户 @{target}"
        return pk, uname, prof, ""

    async def search_users(self, query: str, *, count: int = 30) -> Tuple[List[Dict[str, Any]], str]:
        """按关键词搜索 Instagram 用户（`GET users/search/`）。"""
        q = str(query or "").strip()
        if not q:
            return [], "搜索词不能为空"
        path = (
            f"users/search/?search_surface=user_search_page"
            f"&timezone_offset={self.session.timezone_offset}"
            f"&q={quote(q)}&count={max(1, min(50, int(count)))}"
        )
        _r, data, err = await self._mobile_request("GET", path, operation="SearchByName")
        if not err and isinstance(data, Mapping):
            users = [parse_ig_self_user(u) for u in (data.get("users") or []) if isinstance(u, Mapping)]
            return [u for u in users if u.get("uid") or u.get("unique_id")], ""
        return [], err

    # ───────────────────── 用户时间线、Reels、标签搜索与单帖详情 ─────────────────────

    async def fetch_user_feed(
        self,
        user_ref: str,
        *,
        max_id: str = "",
        exclude_comment: bool = True,
        count: int = 12,
    ) -> Tuple[List[Dict[str, Any]], str, bool, str]:
        """分页读取用户主页时间线（`GET feed/user/{userId}/`，移动端受阻时回退 Web `feed/user/{username}/username/`）。"""
        raw_target = resolve_ig_user_target(user_ref) or str(user_ref or "").strip()
        uid = raw_target if raw_target.isdigit() else ""
        uname = "" if raw_target.isdigit() else raw_target
        if not uid:
            uid, uname, _prof, r_err = await self.resolve_user_id(raw_target)
            if not uid and r_err.startswith(("logged_out:", "verification_required:", "not_found:")):
                return [], "", False, r_err

        err = ""
        if uid:
            q_parts = [
                f"exclude_comment={'true' if exclude_comment else 'false'}",
                "only_fetch_first_carousel_media=false",
            ]
            if max_id:
                q_parts.append(f"max_id={max_id}")
            path = f"feed/user/{uid}/?" + "&".join(q_parts)
            _r, data, err = await self._mobile_request("GET", path, operation="UserFeedSearch")
            if not err and isinstance(data, Mapping):
                items = [dict(x) for x in (data.get("items") or []) if isinstance(x, Mapping)]
                next_cursor = str(data.get("next_max_id") or "")
                more = bool(data.get("more_available")) and bool(next_cursor) and next_cursor != max_id
                return items, next_cursor, more, ""
            if err.startswith(("logged_out:", "verification_required:")):
                return [], "", False, err

        if uname:
            eb_q = f"count={max(1, min(33, int(count)))}"
            if max_id:
                eb_q += f"&max_id={quote(max_id)}"
            _r2, data2, err2 = await self._web_request(
                "GET",
                f"feed/user/{quote(uname)}/username/?{eb_q}",
                referer=f"{WWW_BASE}/{uname}/",
                operation="WebUserFeed",
            )
            if not err2 and isinstance(data2, Mapping):
                items = [dict(x) for x in (data2.get("items") or []) if isinstance(x, Mapping)]
                next_cursor = str(data2.get("next_max_id") or "")
                more = bool(data2.get("more_available")) and bool(next_cursor) and next_cursor != max_id
                return items, next_cursor, more, ""
            err = err or err2

        return [], "", False, err or "读取 Instagram 用户作品列表失败"

    async def fetch_user_reels(
        self,
        user_id: str,
        *,
        max_id: str = "",
    ) -> Tuple[List[Dict[str, Any]], str, bool, str]:
        """读取用户 Reels 分块流（`POST clips/user/`，请求头 `X-Ig-Transfer-Encoding: chunked`，合并多行 NDJSON）。"""
        uid = str(user_id or "").strip()
        if not uid:
            return [], "", False, ""
        if not uid.isdigit():
            uid, _, _, err = await self.resolve_user_id(uid)
            if not uid:
                return [], "", False, err

        pairs: List[Tuple[str, Any]] = [
            ("no_of_medias_in_each_chunk", "6"),
            ("_uuid", self.session.unique_id_ex),
            ("target_user_id", uid),
            ("include_feed_video", "true"),
            ("should_have_max_id_in_last_chunk_only", "true"),
            ("should_stream_response", "true"),
        ]
        if max_id:
            pairs.append(("max_id", max_id))
        resp, _parsed, err = await self._mobile_request(
            "POST",
            "clips/user/",
            operation="GetReelPosts",
            body=unsigned_content(pairs),
            extra_headers={"X-Ig-Transfer-Encoding": "chunked"},
        )
        if err or resp is None:
            return [], "", False, err or "Reels 列表请求失败"
        medias, next_cursor, more = parse_ig_reels_chunked_response(resp.text)
        return medias, next_cursor, (more and bool(next_cursor) and next_cursor != max_id), ""

    async def fetch_all_creator_works(
        self,
        user_ref: str,
        *,
        max_pages: int = 3,
    ) -> Tuple[List[Dict[str, Any]], str]:
        """分页拉取账号近期全部公开帖子/Reels，用于本账号作品同步与创作者采集。"""
        works: List[Dict[str, Any]] = []
        seen: set[str] = set()
        cursor = ""
        last_err = ""
        for _ in range(max(1, min(15, int(max_pages)))):
            batch, next_cursor, more, err = await self.fetch_user_feed(user_ref, max_id=cursor)
            if err:
                last_err = err
                break
            for raw in batch:
                w = parse_ig_creator_work(raw)
                if w and w["item_id"] not in seen:
                    seen.add(w["item_id"])
                    works.append(w)
            if not more or not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor
        return works, ("" if works else last_err)

    async def search_hashtag_sections(
        self,
        hashtag: str,
        *,
        max_id: str = "",
        page: Optional[int] = None,
        next_media_ids: Optional[Sequence[str]] = None,
        tab_name: str = "recent",
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any], str]:
        """分页检索 `#hashtag` 话题帖子（`POST tags/{tag}/sections/`，复用会话 `rank_token` 与 `next_media_ids`）。"""
        clean_tag = _HASHTAG_CLEAN_RE.sub("", str(hashtag or "").strip())
        if not clean_tag:
            return [], {}, "话题词不能为空"

        rank_token = self.session.hashtag_rank_tokens.get(
            self.self_uid, clean_tag, max_id or None, tab_name=tab_name
        )
        pairs: List[Tuple[str, Any]] = [
            ("media_recency_filter", "default" if tab_name == "top" else "top_recent_posts"),
            ("_uuid", self.session.unique_id_ex),
            ("rank_token", rank_token),
            ("include_persistent", max_id == ""),  # bool -> "True" / "False"
        ]
        if max_id:
            pairs.append(("max_id", max_id))
        if page is not None:
            pairs.append(("page", str(page)))
            ids_list = list(next_media_ids or [])
            pairs.append(("next_media_ids", "[" + ",".join(str(x) for x in ids_list) + "]"))
        elif next_media_ids is not None:
            pairs.append(("next_media_ids", "[" + ",".join(str(x) for x in next_media_ids) + "]"))

        _r, data, err = await self._mobile_request(
            "POST",
            f"tags/{quote(clean_tag)}/sections/",
            operation="HashtagSearch",
            body=unsigned_content(pairs),
        )
        if not err and isinstance(data, Mapping):
            medias = self._extract_sections_medias(data)
            paging = {
                "next_max_id": str(data.get("next_max_id") or ""),
                "next_page": data.get("next_page"),
                "next_media_ids": [str(x) for x in (data.get("next_media_ids") or [])],
                "more_available": bool(data.get("more_available")),
            }
            return medias, paging, ""
        if err.startswith(("logged_out:", "verification_required:")):
            return [], {}, err

        # Web 回退: tags/web_info
        _r2, data2, err2 = await self._web_request(
            "GET",
            f"tags/web_info/?tag_name={quote(clean_tag)}",
            referer=f"{WWW_BASE}/explore/tags/{quote(clean_tag)}/",
            operation="WebHashtagWebInfo",
        )
        if not err2 and isinstance(data2, Mapping):
            inner = data2.get("data") if isinstance(data2.get("data"), Mapping) else data2
            medias = self._extract_sections_medias(inner)
            return medias, {"next_max_id": "", "more_available": False}, ""
        return [], {}, err or err2 or "话题搜索失败"

    @staticmethod
    def _extract_sections_medias(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        seen: set[str] = set()

        def _push(m: Any) -> None:
            if not isinstance(m, Mapping):
                return
            media = m.get("media") if isinstance(m.get("media"), Mapping) else m
            key = str(media.get("pk") or media.get("id") or media.get("code") or "")
            if key and key not in seen:
                seen.add(key)
                out.append(dict(media))

        for item in (payload.get("items") or []):
            _push(item)
        for sec_group_key in ("sections", "top", "recent"):
            grp = payload.get(sec_group_key)
            sections = grp.get("sections") if isinstance(grp, Mapping) else grp
            if not isinstance(sections, list):
                continue
            for sec in sections:
                if not isinstance(sec, Mapping):
                    continue
                lc = sec.get("layout_content") if isinstance(sec.get("layout_content"), Mapping) else {}
                for m in (lc.get("medias") or []):
                    _push(m)
                for fill in (lc.get("fill_items") or []):
                    _push(fill)
                one_by_two = lc.get("one_by_two_item")
                if isinstance(one_by_two, Mapping):
                    clips = one_by_two.get("clips") if isinstance(one_by_two.get("clips"), Mapping) else {}
                    for it in (clips.get("items") or []):
                        _push(it)
        return out

    async def search_posts(
        self,
        keyword: str,
        *,
        max_pages: int = 3,
        sort: str = "general",
    ) -> Tuple[List[Dict[str, Any]], str]:
        """统一关键词/话题搜索入口：优先 `fbsearch/web/top_serp/` + `#hashtag sections` 双通道聚合。"""
        kw = str(keyword or "").strip()
        if not kw:
            return [], "搜索关键词不能为空"

        results: List[Dict[str, Any]] = []
        seen: set[str] = set()
        last_err = ""

        def _collect(items: Iterable[Mapping[str, Any]]) -> None:
            for it in items:
                key = str(it.get("code") or it.get("shortcode") or it.get("pk") or it.get("id") or "")
                if key and key not in seen:
                    seen.add(key)
                    results.append(dict(it))

        # 1. 若并非纯 #标签 或需要综合搜索，尝试 Web top_serp 发现媒体网格
        if not kw.startswith("#"):
            _r_serp, serp_data, serp_err = await self._web_request(
                "GET",
                f"fbsearch/web/top_serp/?enable_metadata=true&query={quote(kw)}",
                referer=f"{WWW_BASE}/explore/search/keyword/?q={quote(kw)}",
                operation="WebTopSerp",
            )
            if not serp_err and isinstance(serp_data, Mapping):
                grid = serp_data.get("media_grid") if isinstance(serp_data.get("media_grid"), Mapping) else serp_data
                _collect(self._extract_sections_medias(grid))
            elif serp_err.startswith(("logged_out:", "verification_required:")):
                return [], serp_err
            else:
                last_err = serp_err

        # 2. 标签分段分页 (`tags/{tag}/sections/`)
        tab = "top" if sort in {"general", "most_liked"} else "recent"
        clean_tag = _HASHTAG_CLEAN_RE.sub("", kw)
        if clean_tag:
            cursor = ""
            page_no: Optional[int] = None
            next_ids: Optional[List[str]] = None
            for _ in range(max(1, min(8, int(max_pages)))):
                batch, paging, err = await self.search_hashtag_sections(
                    clean_tag,
                    max_id=cursor,
                    page=page_no,
                    next_media_ids=next_ids,
                    tab_name=tab,
                )
                if err:
                    last_err = err
                    break
                _collect(batch)
                next_max = str(paging.get("next_max_id") or "")
                if not paging.get("more_available") or not next_max or next_max == cursor:
                    break
                cursor = next_max
                page_no = paging.get("next_page")
                next_ids = paging.get("next_media_ids")

        return results, ("" if results else last_err)

    async def fetch_media_info(self, work_ref: str) -> Tuple[Optional[Aweme], Dict[str, Any], str]:
        """读取单条作品详情（`GET media/{mediaId}/info/`），支持链接 / Shortcode / 数字 `pk`。"""
        ref = resolve_ig_work_ref(work_ref)
        media_id = ref.media_id if ref else (str(work_ref).strip() if str(work_ref).strip().isdigit() else shortcode_to_media_id(str(work_ref)))
        if not media_id:
            return None, {}, f"无法解析 Instagram 作品链接或 ID: {work_ref}"

        _r, data, err = await self._mobile_request(
            "GET", f"media/{media_id}/info/", operation="GetMediaInfo"
        )
        if not err and isinstance(data, Mapping):
            items = data.get("items")
            first = items[0] if isinstance(items, list) and items and isinstance(items[0], Mapping) else data
            aw = parse_ig_aweme(first)
            if aw is not None:
                return aw, dict(first), ""
        if err.startswith(("logged_out:", "verification_required:", "not_found:")):
            return None, {}, err

        _r2, data2, err2 = await self._web_request(
            "GET", f"media/{media_id}/info/", operation="WebGetMediaInfo"
        )
        if not err2 and isinstance(data2, Mapping):
            items = data2.get("items")
            first = items[0] if isinstance(items, list) and items and isinstance(items[0], Mapping) else data2
            aw = parse_ig_aweme(first)
            if aw is not None:
                return aw, dict(first), ""
        return None, {}, err or err2 or "读取 Instagram 作品详情失败"

    # ───────────────────── 评论读取与发送 ─────────────────────

    async def fetch_comments(
        self,
        work_ref: str,
        *,
        max_count: int = 50,
        include_replies: bool = True,
    ) -> Tuple[List[Dict[str, Any]], str]:
        """分页读取作品评论流（`GET media/{mediaId}/stream_comments/` + `min_id` 游标与二级回复展开）。"""
        if max_count <= 0:
            return [], ""
        ref = resolve_ig_work_ref(work_ref)
        media_id = ref.media_id if ref else (str(work_ref).strip() if str(work_ref).strip().isdigit() else shortcode_to_media_id(str(work_ref)))
        if not media_id:
            return [], f"无效的 Instagram 作品 ID: {work_ref}"

        collected: List[Dict[str, Any]] = []
        seen: set[str] = set()
        expanded_parents: set[str] = set()
        comments_disabled = False
        next_min_id = ""
        last_err = ""
        max_pages = max(1, min(50, math.ceil(max_count / 14)))
        max_expanded_parents = max(6, min(25, max_pages))

        async def _expand_children_for_batch(raw_items: Sequence[Any]) -> None:
            if not include_replies or len(collected) >= max_count or len(expanded_parents) >= max_expanded_parents:
                return
            for raw_c in raw_items:
                if len(collected) >= max_count or len(expanded_parents) >= max_expanded_parents:
                    break
                if not isinstance(raw_c, Mapping):
                    continue
                cpk = str(raw_c.get("pk") or raw_c.get("id") or "").strip()
                ch_cnt = int(raw_c.get("child_comment_count") or 0)
                prev_ch = raw_c.get("preview_child_comments") or raw_c.get("child_comments") or []
                if not cpk or ch_cnt <= len(prev_ch) or cpk in expanded_parents:
                    continue
                expanded_parents.add(cpk)
                _rc, dc, ec = await self._mobile_request(
                    "GET",
                    f"media/{media_id}/comments/{cpk}/inline_child_comments/",
                    operation="GetChildComments",
                )
                if ec or not isinstance(dc, Mapping):
                    _rc, dc, ec = await self._web_request(
                        "GET",
                        f"media/{media_id}/comments/{cpk}/inline_child_comments/",
                        operation="WebGetChildComments",
                    )
                if not ec and isinstance(dc, Mapping):
                    for ch_raw in (dc.get("child_comments") or []):
                        parsed_ch = parse_ig_comment(ch_raw, reply_to=cpk)
                        if parsed_ch and parsed_ch["comment_id"] not in seen:
                            seen.add(parsed_ch["comment_id"])
                            collected.append(parsed_ch)

        def _ingest_raw_comments(raw_items: Sequence[Any]) -> None:
            flat = flatten_ig_comments(raw_items, include_replies=include_replies)
            for c in flat:
                if c["comment_id"] not in seen:
                    seen.add(c["comment_id"])
                    collected.append(c)

        for _ in range(max_pages):
            q = (
                "analytics_module=comments_v2_feed_contextual_profile"
                "&can_support_threading=true"
                "&is_carousel_bumped_post=false"
                "&feed_position=0"
            )
            if next_min_id:
                q += f"&min_id={quote(next_min_id)}"
            _r, data, err = await self._mobile_request(
                "GET", f"media/{media_id}/stream_comments/?{q}", operation="GetCommentsPage"
            )
            if err or not isinstance(data, Mapping):
                last_err = err or "stream_comments_empty"
                break
            if data.get("comments_disabled"):
                comments_disabled = True
                break
            raw_list = list(data.get("comments") or []) + list(data.get("preview_comments") or [])
            _ingest_raw_comments(raw_list)
            await _expand_children_for_batch(raw_list)
            if len(collected) >= max_count:
                break
            cand_min = str(data.get("next_min_id") or "").strip()
            if not cand_min or cand_min == next_min_id:
                break
            next_min_id = cand_min

        # Mobile 通道无结果且非鉴权挑战时，自动回退 Web 评论分页接口
        if (
            not collected
            and not comments_disabled
            and not last_err.startswith(("logged_out:", "verification_required:"))
        ):
            eb_min_id = ""
            code_hint = ref.shortcode if ref else ""
            referer = f"https://www.instagram.com/p/{code_hint}/" if code_hint else "https://www.instagram.com/"
            for _ in range(max_pages):
                eb_q = "can_support_threading=true&permalink_enabled=false"
                if eb_min_id:
                    eb_q += f"&min_id={quote(eb_min_id)}"
                _r2, data2, err2 = await self._web_request(
                    "GET",
                    f"media/{media_id}/comments/?{eb_q}",
                    referer=referer,
                    operation="WebGetComments",
                )
                if err2 or not isinstance(data2, Mapping):
                    if not last_err or last_err == "stream_comments_empty":
                        last_err = err2
                    break
                if data2.get("comments_disabled"):
                    comments_disabled = True
                    break
                raw_list2 = list(data2.get("comments") or []) + list(data2.get("preview_comments") or [])
                _ingest_raw_comments(raw_list2)
                await _expand_children_for_batch(raw_list2)
                last_err = ""
                if len(collected) >= max_count:
                    break
                cand_eb_min = str(data2.get("next_min_id") or "").strip()
                if not cand_eb_min or cand_eb_min == eb_min_id:
                    break
                eb_min_id = cand_eb_min

        if last_err == "stream_comments_empty":
            last_err = ""
        if not include_replies:
            collected = [c for c in collected if not c.get("reply_to")]
        return collected[:max_count], ("" if collected else last_err)

    async def post_comment(
        self,
        work_ref: str,
        text: str,
        *,
        reply_to: str = "",
        on_submit: Optional[Callable[[], Any]] = None,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """发表作品评论或二级回复（`POST media/{mediaId}/comment/`，附带 `user_breadcrumb` 签名）。"""
        raw_text = str(text or "")
        clean_text = raw_text.replace("\r", "").strip()
        if not clean_text:
            return False, "评论内容不能为空", {}
        if not self.session.is_logged_in:
            return False, "logged_out:未登录 Instagram 账号", {}

        ref = resolve_ig_work_ref(work_ref)
        media_id = ref.media_id if ref else (str(work_ref).strip() if str(work_ref).strip().isdigit() else shortcode_to_media_id(str(work_ref)))
        if not media_id:
            return False, f"target_resolution_failed:无法解析作品 ID {work_ref}", {}

        payload: Dict[str, Any] = {
            "include_media_code": "false",
            "user_breadcrumb": generate_comment_breadcrumb(raw_text),
            "delivery_class": "organic",
            "idempotence_token": str(uuid.uuid4()),
            "include_e2ee_mentioned_user_list": "false",
            "radio_type": "wifi-none",
            "_uid": self.self_uid,
            "_uuid": self.session.unique_id_ex,
            "comment_text": clean_text,
            "is_from_direct_channel": "false",
            "is_carousel_bumped_post": "false",
            "container_module": "comments_v2_feed_contextual_profile",
            "feed_position": 0,
        }
        if self.session.nav_chain:
            payload["nav_chain"] = self.session.nav_chain
        if reply_to:
            payload["replied_to_comment_id"] = str(reply_to).strip()

        await notify_submit(on_submit)
        resp, data, err = await self._mobile_request(
            "POST",
            f"media/{media_id}/comment/",
            operation="AddMediaComment",
            body=signed_dict_content(payload),
            allow_560_retry=False,
        )
        if resp is None or resp.status_code >= 500:
            return False, f"write_uncertain:Instagram 评论请求已发出但未收到确定响应({err or 'timeout'})", {}
        if err:
            return False, err, {}
        comment_obj = (data.get("comment") if isinstance(data, Mapping) else None) or {}
        pk = str(comment_obj.get("pk") or comment_obj.get("id") or "") if isinstance(comment_obj, Mapping) else ""
        if not pk:
            return False, "评论接口未返回有效 comment.pk", {}
        return True, "", {"comment_id": pk}

    # ───────────────────── 点赞与评论点赞 ─────────────────────

    async def like_media(
        self,
        work_ref: str,
        *,
        unlike: bool = False,
        double_tap: bool = True,
        on_submit: Optional[Callable[[], Any]] = None,
    ) -> Tuple[bool, str]:
        """对作品执行点赞或取消点赞（`POST media/{mediaId}/like/` 或 `unlike/`）。"""
        if not self.session.is_logged_in:
            return False, "logged_out:未登录 Instagram 账号"
        ref = resolve_ig_work_ref(work_ref)
        media_id = ref.media_id if ref else (str(work_ref).strip() if str(work_ref).strip().isdigit() else shortcode_to_media_id(str(work_ref)))
        if not media_id:
            return False, f"target_resolution_failed:无法解析作品 ID {work_ref}"

        payload: Dict[str, Any] = {
            "delivery_class": "organic",
            "media_id": media_id,
            "radio_type": "wifi-none",
            "_uid": self.self_uid,
            "_uuid": self.session.unique_id_ex,
            "is_carousel_bumped_post": "false",
            "container_module": "feed_contextual_profile",
            "feed_position": "0",
        }
        if double_tap and not unlike:
            payload["tap_source"] = "double_tap_media"
            payload["source_of_like"] = "double_tap_media"
            payload["content_area_dimension_x"] = "1080"
            payload["content_area_dimension_y"] = "1350"
            payload["normalized_position_x"] = f"{random.uniform(0.1, 0.9):.8f}"
            payload["normalized_position_y"] = f"{random.uniform(0.1, 0.9):.8f}"
        else:
            payload["tap_source"] = "button"

        endpoint = f"media/{media_id}/unlike/?d=0" if unlike else f"media/{media_id}/like/"
        await notify_submit(on_submit)
        resp, _data, err = await self._mobile_request(
            "POST", endpoint, operation="LikeMedia", body=signed_dict_content(payload), allow_560_retry=False
        )
        if resp is None or resp.status_code >= 500:
            return False, f"write_uncertain:点赞请求响应不确定({err or 'timeout'})"
        return (not err), err

    async def like_comment(
        self,
        comment_id: str,
        *,
        unlike: bool = False,
        on_submit: Optional[Callable[[], Any]] = None,
    ) -> Tuple[bool, str]:
        """对单条评论点赞或取消点赞（`POST media/{commentId}/comment_like/`）。"""
        cid = str(comment_id or "").strip()
        if not cid:
            return False, "缺少 comment_id"
        payload: Dict[str, Any] = {
            "delivery_class": "organic",
            "_uid": self.self_uid,
            "_uuid": self.session.unique_id_ex,
            "is_carousel_bumped_post": "false",
            "container_module": "comments_v2_feed_contextual_profile",
            "feed_position": 0,
        }
        action = "comment_unlike" if unlike else "comment_like"
        await notify_submit(on_submit)
        resp, _data, err = await self._mobile_request(
            "POST", f"media/{cid}/{action}/", operation="LikeComment", body=signed_dict_content(payload), allow_560_retry=False
        )
        if resp is None or resp.status_code >= 500:
            return False, f"write_uncertain:评论点赞响应不确定({err or 'timeout'})"
        return (not err), err

    # ───────────────────── 粉丝 / 关注列表与关注 / 取关 ─────────────────────

    async def fetch_followers(
        self,
        user_ref: str = "",
        *,
        max_count: int = 200,
    ) -> Tuple[List[Dict[str, Any]], str]:
        """分页读取粉丝列表（首屏生成并缓存 `rank_token`，翻页复用）。"""
        uid = str(user_ref or self.self_uid).strip()
        if not uid.isdigit():
            uid, _, _, err = await self.resolve_user_id(uid)
            if not uid:
                return [], err

        users: List[Dict[str, Any]] = []
        seen: set[str] = set()
        cursor: Optional[str] = None
        last_err = ""
        for _ in range(max(1, min(10, math.ceil(max_count / 25)))):
            rank_token = self.session.rank_tokens.get(self.self_uid, uid, cursor)
            q = f"search_surface=follow_list_page&enable_groups=true&query=&rank_token={rank_token}"
            if cursor:
                q += f"&max_id={cursor}"
            _r, data, err = await self._mobile_request(
                "GET", f"friendships/{uid}/followers/?{q}", operation="GetFollowers"
            )
            if err or not isinstance(data, Mapping):
                last_err = err
                break
            for u in parse_ig_follow_users(data, default_following=False):
                if u["uid"] not in seen:
                    seen.add(u["uid"])
                    users.append(u)
            next_max = str(data.get("next_max_id") or "").strip()
            if len(users) >= max_count or not next_max or next_max == cursor:
                break
            cursor = next_max
        return users[:max_count], ("" if users else last_err)

    async def fetch_following(
        self,
        user_ref: str = "",
        *,
        max_count: int = 200,
        earliest: bool = False,
    ) -> Tuple[List[Dict[str, Any]], str]:
        """分页读取关注列表（`GET friendships/{userId}/following/`）。"""
        uid = str(user_ref or self.self_uid).strip()
        if not uid.isdigit():
            uid, _, _, err = await self.resolve_user_id(uid)
            if not uid:
                return [], err

        users: List[Dict[str, Any]] = []
        seen: set[str] = set()
        cursor: Optional[str] = None
        last_err = ""
        for _ in range(max(1, min(10, math.ceil(max_count / 25)))):
            rank_token = self.session.rank_tokens.get(self.self_uid, uid, cursor)
            q = (
                f"includes_hashtags=true&search_surface=follow_list_page"
                f"&enable_groups=true&rank_token={rank_token}&query="
            )
            if cursor:
                q += f"&max_id={cursor}"
            elif earliest and uid == self.self_uid:
                q += "&order=date_followed_earliest"
            _r, data, err = await self._mobile_request(
                "GET", f"friendships/{uid}/following/?{q}", operation="GetFollowing"
            )
            if err or not isinstance(data, Mapping):
                last_err = err
                break
            for u in parse_ig_follow_users(data, default_following=True):
                if u["uid"] not in seen:
                    seen.add(u["uid"])
                    users.append(u)
            next_max = str(data.get("next_max_id") or "").strip()
            if len(users) >= max_count or not next_max or next_max == cursor:
                break
            cursor = next_max
        return users[:max_count], ("" if users else last_err)

    async def modify_follow(
        self,
        user_ref: str,
        *,
        follow: bool = True,
        on_submit: Optional[Callable[[], Any]] = None,
    ) -> Tuple[bool, str]:
        """执行关注或取消关注（支持年龄门限自动确认与取关 404 幂等处理）。"""
        if not self.session.is_logged_in:
            return False, "logged_out:未登录 Instagram 账号"
        uid = str(user_ref or "").strip()
        if not uid.isdigit():
            uid, _, _, r_err = await self.resolve_user_id(uid)
            if not uid:
                return False, f"target_resolution_failed:{r_err or '无法解析目标用户 ID'}"

        if follow:
            payload: Dict[str, Any] = {
                "user_id": uid,
                "radio_type": "wifi-none",
                "_uid": self.self_uid,
                "device_id": self.session.device_id,
                "_uuid": self.session.unique_id_ex,
            }
            if self.session.nav_chain:
                payload["nav_chain"] = self.session.nav_chain
            await notify_submit(on_submit)
            resp, data, err = await self._mobile_request(
                "POST",
                f"friendships/create/{uid}/",
                operation="AutoFollow",
                body=signed_dict_content(payload),
                allow_560_retry=False,
            )
            if err.startswith("age_gated:"):
                resp, data, err = await self._mobile_request(
                    "POST",
                    f"friendships/mark_user_overage/{uid}/follow/",
                    operation="AutoFollow",
                    body=signed_dict_content(payload),
                    allow_560_retry=False,
                )
            if resp is None or resp.status_code >= 500:
                return False, f"write_uncertain:关注请求响应不确定({err or 'timeout'})"
            if err:
                return False, err
            fs = (data.get("friendship_status") if isinstance(data, Mapping) else None) or {}
            if isinstance(fs, Mapping) and (fs.get("following") or fs.get("outgoing_request")):
                return True, ""
            if isinstance(data, Mapping) and str(data.get("status") or "").lower() == "ok":
                return True, ""
            return False, "关注接口未返回有效的 friendship_status"

        # 取关若目标已不存在 (404/not_found) 视为幂等成功
        payload_unfollow: Dict[str, Any] = {
            "user_id": uid,
            "radio_type": "wifi-none",
            "_uid": self.self_uid,
            "_uuid": self.session.unique_id_ex,
            "container_module": "self_following",
        }
        if self.session.nav_chain:
            payload_unfollow["nav_chain"] = self.session.nav_chain
        await notify_submit(on_submit)
        resp, _data, err = await self._mobile_request(
            "POST",
            f"friendships/destroy/{uid}/",
            operation="UnfollowUser",
            body=signed_dict_content(payload_unfollow),
            allow_560_retry=False,
        )
        if resp is None or resp.status_code >= 500:
            return False, f"write_uncertain:取关请求响应不确定({err or 'timeout'})"
        if err.startswith("not_found:"):
            return True, ""
        return (not err), err

    # ───────────────────── Direct v2 私信收件箱、历史、发送与已读 ─────────────────────

    async def fetch_dm_conversations(
        self,
        *,
        folder: int = 0,
        cursor: str = "",
        pending: bool = False,
    ) -> Tuple[List[Dict[str, Any]], str]:
        """同步 Direct v2 私信收件箱或待处理请求（移动端优先，Web 会话通道回退）。"""
        if pending:
            path = f"direct_v2/pending_inbox/?visual_message_return_type=unseen&persistentBadging=true&request_session_id={uuid.uuid4()}"
        else:
            q_parts = [
                "visual_message_return_type=unseen",
                "thread_message_limit=10",
                "persistentBadging=true",
                "limit=20",
                "is_prefetching=false",
                f"fetch_reason={'page_scroll' if cursor else 'initial_snapshot'}",
            ]
            if cursor:
                q_parts.append("direction=older")
                q_parts.append(f"cursor={cursor}")
                if self.session.seq_id > 0:
                    q_parts.append(f"seq_id={self.session.seq_id}")
            else:
                q_parts.append(f"folder={int(folder)}")
            path = "direct_v2/inbox/?" + "&".join(q_parts)

        _r, data, err = await self._mobile_request("GET", path, operation="GetDirectMessages")
        if not err and isinstance(data, Mapping):
            if int(data.get("seq_id") or 0) > 0:
                self.session.seq_id = int(data["seq_id"])
            return parse_ig_dm_inbox(data, self_uid=self.self_uid), ""
        if err.startswith(("logged_out:", "verification_required:")):
            return [], err

        # Web 会话 Direct Inbox 回退
        eb_q = f"persistentBadging=true&folder={int(folder)}&limit=20&thread_message_limit=20"
        if cursor:
            eb_q += f"&cursor={quote(cursor)}"
        _r2, data2, err2 = await self._web_request(
            "GET",
            f"https://i.instagram.com/api/v1/direct_v2/inbox/?{eb_q}",
            referer="https://www.instagram.com/direct/inbox/",
            operation="WebGetDirectMessages",
        )
        if not err2 and isinstance(data2, Mapping):
            if int(data2.get("seq_id") or 0) > 0:
                self.session.seq_id = int(data2["seq_id"])
            return parse_ig_dm_inbox(data2, self_uid=self.self_uid), ""
        return [], err or err2 or "读取 Instagram 私信收件箱失败"

    async def fetch_dm_history(
        self,
        thread_id: str,
        *,
        max_pages: int = 2,
    ) -> Tuple[List[Dict[str, Any]], str]:
        """分页拉取指定 Direct v2 会话的消息历史（`GET direct_v2/threads/{tid}/`）。"""
        tid = str(thread_id or "").strip()
        if not tid:
            return [], "缺少 thread_id"

        all_msgs: List[Dict[str, Any]] = []
        seen: set[str] = set()
        cursor = ""
        last_err = ""
        for _ in range(max(1, min(10, int(max_pages)))):
            q = f"seq_id={self.session.seq_id}&limit=20&visual_message_return_type=unseen"
            if cursor:
                q += f"&cursor={cursor}&direction=older"
            _r, data, err = await self._mobile_request(
                "GET", f"direct_v2/threads/{tid}/?{q}", operation="GetThreadMessages"
            )
            if err and not err.startswith(("logged_out:", "verification_required:")):
                _r, data, err = await self._web_request(
                    "GET",
                    f"https://i.instagram.com/api/v1/direct_v2/threads/{tid}/?limit=20",
                    referer=f"https://www.instagram.com/direct/t/{tid}/",
                    operation="WebGetThreadMessages",
                )
            if err or not isinstance(data, Mapping):
                last_err = err
                break
            for m in parse_ig_dm_messages(data, self_uid=self.self_uid):
                if m["msg_id"] not in seen:
                    seen.add(m["msg_id"])
                    all_msgs.append(m)
            th = data.get("thread") if isinstance(data.get("thread"), Mapping) else {}
            oldest = str(th.get("oldest_cursor") or "").strip()
            if not th.get("has_older") or not oldest or oldest == cursor:
                break
            cursor = oldest

        all_msgs.sort(key=lambda m: m["create_time"])
        return all_msgs, ("" if all_msgs else last_err)

    async def send_dm(
        self,
        target_or_thread: str,
        text: str,
        *,
        is_thread: bool = False,
        on_submit: Optional[Callable[[], Any]] = None,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """发送私信消息：自动区分纯文本 (`broadcast/text/`) 与包含链接 (`broadcast/link/`) 两套端点与参数。"""
        msg = str(text or "").strip()
        if not msg:
            return False, "私信内容不能为空", {}
        if not self.session.is_logged_in:
            return False, "logged_out:未登录 Instagram 账号", {}

        target = str(target_or_thread or "").strip()
        if not target:
            return False, "缺少私信接收方或会话 ID", {}

        ctx_id = str(unique_time_id())
        links = _URL_IN_TEXT_RE.findall(msg)
        pairs: List[Tuple[str, Any]] = [
            ("action", "send_item"),
            ("is_x_transport_forward", "false"),
            ("is_shh_mode", "0"),
            ("send_silently", "false"),
            ("btt_dual_send", "false"),
            ("is_ae_dual_send", "false"),
            ("send_attribution", "inbox_search"),
            ("client_context", ctx_id),
            ("device_id", self.session.device_id),
            ("mutation_token", ctx_id),
            ("_uuid", self.session.unique_id_ex),
            ("offline_threading_id", ctx_id),
        ]
        if links:
            endpoint = "direct_v2/threads/broadcast/link/"
            pairs.append(("link_urls", json.dumps(links, ensure_ascii=False, separators=(",", ":"))))
            pairs.append(("link_text", msg))
        else:
            endpoint = "direct_v2/threads/broadcast/text/"
            pairs.append(("text", msg))

        if is_thread or (not target.isdigit() and len(target) >= 18):
            pairs.append(("thread_ids", f"[{target}]"))
        else:
            uid = target
            if not uid.isdigit():
                uid, _, _, r_err = await self.resolve_user_id(uid)
                if not uid:
                    return False, f"target_resolution_failed:{r_err or '无法解析接收者 UID'}", {}
            pairs.append(("recipient_users", f"[[{uid}]]"))

        await notify_submit(on_submit)
        resp, data, err = await self._mobile_request(
            "POST",
            endpoint,
            operation="SendDirectMessage",
            body=unsigned_content(pairs),
            allow_560_retry=False,
        )
        if resp is None or resp.status_code >= 500:
            return False, f"write_uncertain:Instagram 私信已提交但响应不确定({err or 'timeout'})", {}
        if err:
            if "is too long" in err.lower():
                return False, "私信内容超过 Instagram 1000 字符上限", {}
            return False, err, {}
        pl = (data.get("payload") if isinstance(data, Mapping) else None) or {}
        item_id = str(pl.get("item_id") or "") if isinstance(pl, Mapping) else ""
        thread_id = str(pl.get("thread_id") or "") if isinstance(pl, Mapping) else ""
        return True, "", {"item_id": item_id, "thread_id": thread_id}

    async def mark_dm_seen(self, thread_id: str, item_id: str) -> Tuple[bool, str]:
        """标记 Direct v2 会话消息为已读（`POST direct_v2/threads/{tid}/items/{iid}/seen/`）。"""
        tid, iid = str(thread_id or "").strip(), str(item_id or "").strip()
        if not tid or not iid:
            return False, "缺少 thread_id 或 item_id"
        pairs = [
            ("use_unified_inbox", True),
            ("action", "mark_seen"),
            ("thread_id", tid),
            ("item_id", iid),
            ("_uuid", self.session.unique_id_ex),
        ]
        _r, _d, err = await self._mobile_request(
            "POST",
            f"direct_v2/threads/{tid}/items/{iid}/seen/",
            operation="MarkItemSeen",
            body=unsigned_content(pairs),
        )
        return (not err), err

    async def send_dm_photo(
        self,
        target_or_thread: str,
        image_bytes: bytes,
        *,
        width: int = 720,
        height: int = 720,
        is_thread: bool = False,
        on_submit: Optional[Callable[[], Any]] = None,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """发送私信图片：先通过 `rupload_igphoto` 取得 `upload_id`，再提交 `direct_v2/threads/broadcast/configure_photo/`。"""
        if not self.session.is_logged_in:
            return False, "logged_out:未登录 Instagram 账号", {}
        target = str(target_or_thread or "").strip()
        if not target:
            return False, "缺少私信接收方或会话 ID", {}
        upload_id, up_err = await self.rupload_photo_bytes(image_bytes, width=width, height=height)
        if not upload_id:
            return False, up_err or "私信图片上传失败", {}

        ctx_id = str(unique_time_id())
        pairs: List[Tuple[str, Any]] = [
            ("action", "send_item"),
            ("is_x_transport_forward", False),
            ("is_shh_mode", 0),
            ("send_attribution", "inbox_search"),
            ("client_context", ctx_id),
            ("device_id", self.session.device_id),
            ("mutation_token", ctx_id),
            ("_uuid", self.session.unique_id_ex),
            ("allow_full_aspect_ratio", True),
            ("upload_id", upload_id),
            ("offline_threading_id", ctx_id),
            ("btt_dual_send", False),
            ("is_ae_dual_send", False),
        ]
        if is_thread or (not target.isdigit() and len(target) >= 18):
            pairs.append(("thread_ids", f"[{target}]"))
        else:
            uid = target
            if not uid.isdigit():
                uid, _, _, r_err = await self.resolve_user_id(uid)
                if not uid:
                    return False, f"target_resolution_failed:{r_err or '无法解析接收者 UID'}", {}
            pairs.append(("recipient_users", f"[[{uid}]]"))

        await notify_submit(on_submit)
        resp, data, err = await self._mobile_request(
            "POST",
            "direct_v2/threads/broadcast/configure_photo/",
            operation="SendDirectPhoto",
            body=unsigned_content(pairs),
            allow_560_retry=False,
        )
        if resp is None or resp.status_code >= 500:
            return False, f"write_uncertain:私信图片已提交但响应不确定({err or 'timeout'})", {}
        if err:
            return False, err, {}
        pl = (data.get("payload") if isinstance(data, Mapping) else None) or {}
        return True, "", {
            "thread_id": str(pl.get("thread_id") or "") if isinstance(pl, Mapping) else "",
            "item_id": str(pl.get("item_id") or "") if isinstance(pl, Mapping) else "",
        }

    async def send_dm_video(
        self,
        target_or_thread: str,
        video_bytes: bytes,
        *,
        duration_sec: float = 5.0,
        width: int = 720,
        height: int = 1280,
        is_thread: bool = False,
        on_submit: Optional[Callable[[], Any]] = None,
        transcode_sleep_fn: Optional[Callable[[float], Awaitable[None]]] = None,
    ) -> Tuple[bool, str, Dict[str, Any]]:
        """发送私信视频：先通过 `rupload_igvideo` 上传字节流，再提交 `direct_v2/threads/broadcast/configure_video/`。"""
        if not self.session.is_logged_in:
            return False, "logged_out:未登录 Instagram 账号", {}
        target = str(target_or_thread or "").strip()
        if not target:
            return False, "缺少私信接收方或会话 ID", {}
        upload_id, up_err = await self.rupload_video_bytes(
            video_bytes, duration_sec=duration_sec, width=width, height=height, is_dm=True
        )
        if not upload_id:
            return False, up_err or "私信视频上传失败", {}

        ctx_id = str(unique_time_id())
        pairs: List[Tuple[str, Any]] = [
            ("action", "send_item"),
            ("send_attribution", "message_button"),
            ("device_id", self.session.device_id),
            ("offline_threading_id", ctx_id),
            ("client_context", ctx_id),
            ("mutation_token", ctx_id),
            ("upload_id", upload_id),
            ("_uuid", self.session.unique_id_ex),
            ("is_shh_mode", "0"),
            ("video_result", ""),
        ]
        if is_thread or (not target.isdigit() and len(target) >= 18):
            pairs.append(("thread_ids", f"[{target}]"))
        else:
            uid = target
            if not uid.isdigit():
                uid, _, _, r_err = await self.resolve_user_id(uid)
                if not uid:
                    return False, f"target_resolution_failed:{r_err or '无法解析接收者 UID'}", {}
            pairs.append(("recipient_users", f"[[{uid}]]"))

        sleep_fn = transcode_sleep_fn or asyncio.sleep
        await notify_submit(on_submit)
        for counter in range(3):  # 初次 + 最多 2 次转码等待重试
            resp, data, err = await self._mobile_request(
                "POST",
                "direct_v2/threads/broadcast/configure_video/",
                operation="SendDirectVideo",
                body=unsigned_content(pairs),
                allow_560_retry=False,
            )
            if resp is None or resp.status_code >= 500:
                return False, f"write_uncertain:私信视频已提交但响应不确定({err or 'timeout'})", {}
            pl = (data.get("payload") if isinstance(data, Mapping) else None)
            if (err.startswith("transcode_pending:") or (not err and not pl and "transcode" in (resp.text or "").lower())) and counter < 2:
                await sleep_fn(random.uniform(5.0, 15.0))
                continue
            if err:
                return False, err, {}
            if isinstance(pl, Mapping):
                return True, "", {
                    "thread_id": str(pl.get("thread_id") or ""),
                    "item_id": str(pl.get("item_id") or ""),
                }
            return False, "私信视频 configure_video 未返回有效 payload", {}
        return False, "私信视频转码超时", {}

    # ───────────────────── 媒体上传与两阶段 / 三阶段 Configure ─────────────────────

    async def rupload_photo_bytes(
        self,
        image_bytes: bytes,
        *,
        upload_id: str = "",
        width: int = 720,
        height: int = 720,
        is_sidecar: bool = False,
        is_reel: bool = False,
        media_type: str = "1",
        content_type: str = "image/jpeg",
        waterfall_id: str = "",
    ) -> Tuple[str, str]:
        """上传原始图片字节流至 `rupload_igphoto`（参数置于请求头，仅返回 `upload_id`）。"""
        if not image_bytes:
            return "", "图片内容为空"
        uid = str(upload_id or generate_upload_id(use_new=True))
        wf_id = waterfall_id or str(uuid.uuid4())
        quality = str(random.randrange(81, 100))
        img_comp = json.dumps(
            {
                "lib_name": "moz",
                "lib_version": "3.1.m",
                "quality": quality,
                "original_width": int(width),
                "original_height": int(height),
            },
            separators=(",", ":"),
        )
        rupload_params: Dict[str, str] = {
            "upload_id": uid,
            "media_type": str(media_type),
            "image_compression": img_comp,
            "retry_context": RETRY_CONTEXT_DEFAULT,
            "sticker_burnin_params": "[]",
        }
        if is_sidecar:
            rupload_params["is_sidecar"] = "1"
        if is_reel:
            rupload_params["is_clips_video"] = "1"

        entity_name = f"{uid}_0_-{abs(hash((uid, len(image_bytes)))) % 1_000_000_000}"
        headers = {
            "X_FB_PHOTO_WATERFALL_ID": wf_id,
            "X-Instagram-Rupload-Params": json.dumps(rupload_params, separators=(",", ":")),
            "X-Entity-Name": entity_name,
            "X-Entity-Length": str(len(image_bytes)),
            "Offset": "0",
            "X-Entity-Type": content_type,
            "Content-Type": "application/octet-stream",
        }
        _r, data, err = await self._mobile_request(
            "POST",
            f"{RUPLOAD_PHOTO_URL}{entity_name}",
            operation="UploadResumablePicture",
            body=image_bytes,
            extra_headers=headers,
            long_request=True,
        )
        if err:
            return "", err
        ret_id = str((data.get("upload_id") if isinstance(data, Mapping) else "") or uid)
        return ret_id, ""

    async def configure_photo(
        self,
        upload_id: str,
        *,
        width: int = 720,
        height: int = 720,
        caption: str = "",
        disable_comments: bool = False,
        is_story: bool = False,
        on_submit: Optional[Callable[[], Any]] = None,
    ) -> Tuple[bool, str, str]:
        """提交单图或快拍 `configure` 请求（JSON 经 `%20` 百分号编码后拼接 `signed_body=SIGNATURE.`），返回 `(ok, canonical_url, err)`。"""
        cam_id = str(uuid.uuid4())
        payload: Dict[str, Any] = {
            "camera_entry_point": "360",
            "camera_session_id": cam_id,
            "scene_capture_type": "",
            "timezone_offset": str(self.session.timezone_offset),
            "source_type": "4",
            "creation_logger_session_id": cam_id,
            "_uid": self.self_uid,
            "device_id": self.session.device_id,
            "_uuid": self.session.unique_id_ex,
            "audience": "default",
            "upload_id": str(upload_id),
            "device": self.session.device_data(),
            "edits": {
                "crop_original_size": [float(width), float(height)],
                "crop_center": [0.0, -0.0],
                "crop_zoom": 1.0,
            },
            "extra": {"source_width": int(width), "source_height": int(height)},
        }
        if caption:
            payload["caption"] = caption
        if disable_comments:
            payload["disable_comments"] = "1"
        if is_story:
            now_s = int(time.time())
            payload.update({
                "supported_capabilities_new": SUPPORTED_CAPABILITIES_JSON,
                "has_original_sound": "1",
                "original_media_type": "1",
                "composition_id": str(uuid.uuid4()),
                "client_shared_at": str(now_s - random.randint(1, 5)),
                "client_timestamp": str(now_s),
                "media_folder": "Instagram",
                "configure_mode": "1",
                "camera_position": "unknown",
                "creation_surface": "camera",
                "imported_taken_at": str(now_s - random.randint(10, 120)),
                "capture_type": "normal",
            })

        exact_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        endpoint = "media/configure_to_story/" if is_story else "media/configure/"
        extra_hdrs = {} if is_story else {"retry_context": RETRY_CONTEXT_DEFAULT}

        await notify_submit(on_submit)
        resp, data, err = await self._mobile_request(
            "POST",
            endpoint,
            operation="ConfigurePhoto",
            body=ig_configure_content(exact_json),
            extra_headers=extra_hdrs,
            long_request=True,
            allow_560_retry=False,
        )
        if resp is None or resp.status_code >= 500:
            return False, "", f"write_uncertain:图片 configure 已提交但响应不确定({err or 'timeout'})"
        if err:
            return False, "", err
        media = (data.get("media") if isinstance(data, Mapping) else None) or {}
        code = str(media.get("code") or "") if isinstance(media, Mapping) else ""
        pk = str(media.get("pk") or media.get("id") or "") if isinstance(media, Mapping) else ""
        if not code and pk:
            code = media_id_to_shortcode(pk)
        if not code and not pk:
            return False, "", "图片 configure 响应缺少 media 对象"
        url = f"https://www.instagram.com/p/{code}/" if code else f"https://www.instagram.com/"
        return True, url, ""

    async def rupload_video_bytes(
        self,
        video_bytes: bytes,
        *,
        upload_id: str = "",
        duration_sec: float = 5.0,
        width: int = 720,
        height: int = 1280,
        is_reel: bool = False,
        is_sidecar: bool = False,
        is_story: bool = False,
        is_dm: bool = False,
        waterfall_id: str = "",
    ) -> Tuple[str, str]:
        """上传完整视频字节流至 `rupload_igvideo`（GET 预探测 + 单次完整字节流 POST，`Offset=0`）。"""
        if not video_bytes:
            return "", "视频内容为空"
        uid = str(upload_id or generate_upload_id(use_new=False))
        asset_id = str(uuid.uuid4())
        wf_id = waterfall_id or f"{uid}_{asset_id}_{'Mixed' if (is_reel or is_dm) else 'Video'}_0"
        durs = ig_video_duration_values(max(0.1, float(duration_sec)))
        entity_name = f"{uuid.uuid4().hex}-0-{len(video_bytes)}-lessO-{int(time.time()) * 1000}"

        rupload_params: Dict[str, str] = {
            "upload_media_height": str(int(height)),
            "upload_engine_config_enum": "0",
            "xsharing_user_ids": "[]",
            "upload_media_width": str(int(width)),
            "upload_id": uid,
            "media_type": "2",
            "upload_media_duration_ms": str(durs["rupload_upload_media_duration_ms"]),
            "retry_context": RETRY_CONTEXT_DEFAULT,
            "sticker_burnin_params": "[]",
            "hflip": "false",
            "rotate": "0",
            "content_tags": "use_default_cover",
        }
        if is_sidecar:
            rupload_params["for_album"] = "1"
            rupload_params["is_sidecar"] = "1"
        if is_reel:
            rupload_params["is_clips_video"] = "1"
        if is_dm:
            rupload_params["direct_v2"] = "1"

        rupload_json = json.dumps(rupload_params, separators=(",", ":"))
        seg_type = "3" if (is_reel or is_dm) else "2"
        url = f"{RUPLOAD_VIDEO_URL}{entity_name}"

        # 1. GET GetResumableUpload 探测（仅校验非 null，不改变 offset=0）
        probe_headers = {
            "X_fb_video_waterfall_id": wf_id,
            "X-Instagram-Rupload-Params": rupload_json,
            "Segment-Type": seg_type,
        }
        probe_resp, _probe_data, probe_err = await self._mobile_request(
            "GET", url, operation="GetResumableUpload", extra_headers=probe_headers
        )
        if probe_resp is None:
            return "", probe_err or "视频上传预探测失败"

        # 2. 单次完整 POST
        post_headers = {
            "X_fb_video_waterfall_id": wf_id,
            "X-Instagram-Rupload-Params": rupload_json,
            "X-Entity-Length": str(len(video_bytes)),
            "Offset": "0",
            "X-Entity-Type": "video/mp4",
            "X-Entity-Name": entity_name,
            "Segment-Start-Offset": "0",
            "Segment-Type": seg_type,
            "Priority": "u=6, i",
            "Content-Type": "application/octet-stream",
        }
        _r, _data, err = await self._mobile_request(
            "POST",
            url,
            operation="UploadResumableVideo",
            body=video_bytes,
            extra_headers=post_headers,
            long_request=True,
        )
        if err:
            return "", err
        # 返回与封面及后续 configure 共享的本地 upload_id
        return uid, ""

    async def configure_video(
        self,
        upload_id: str,
        *,
        duration_sec: float = 5.0,
        width: int = 720,
        height: int = 1280,
        caption: str = "",
        is_reel: bool = True,
        is_story: bool = False,
        disable_comments: bool = False,
        share_to_feed: bool = True,
        on_submit: Optional[Callable[[], Any]] = None,
        transcode_sleep_fn: Optional[Callable[[float], Awaitable[None]]] = None,
    ) -> Tuple[bool, str, str]:
        """提交视频 / Reels / Story 的最终 `configure` 请求：
        当服务端返回 `transcode not finished` 时，仅针对同 `upload_id` 轮询重试 `configure`（最多初次 + 3 次），不重复上传视频字节。
        """
        dur_len = round(max(0.1, float(duration_sec)), 3)
        dur_ms = ig_video_duration_values(dur_len)["metadata_DurationInMS"]
        now_dt = time.strftime("%Y%m%dT%I%M%S.000Z", time.gmtime())
        cam_id = str(uuid.uuid4())

        payload: Dict[str, Any] = {
            "timezone_offset": str(self.session.timezone_offset),
            "media_folder": "Instagram",
            "source_type": "4",
            "_uid": self.self_uid,
            "device_id": self.session.device_id,
            "_uuid": self.session.unique_id_ex,
            "date_time_original": now_dt,
            "video_result": "",
            "upload_id": str(upload_id),
            "device": self.session.device_data(),
            "length": dur_len,
            "clips": [{"length": dur_len, "source_type": "4", "camera_position": "back"}],
            "extra": {"source_width": int(width), "source_height": int(height)},
            "audio_muted": False,
            "poster_frame_index": 0,
        }
        if caption:
            payload["caption"] = caption
        if disable_comments:
            payload["disable_comments"] = "1"

        extra_hdrs: Dict[str, str] = {"retry_context": RETRY_CONTEXT_DEFAULT}
        if is_story:
            endpoint = "media/configure_to_story/?video=1"
            now_s = int(time.time())
            payload.update({
                "supported_capabilities_new": SUPPORTED_CAPABILITIES_JSON,
                "configure_mode": "1",
                "creation_surface": "camera",
                "camera_position": "unknown",
                "capture_type": "normal",
                "client_shared_at": str(now_s - 2),
                "client_timestamp": str(now_s),
                "imported_taken_at": str(now_s - 30),
                "audience": "default",
                "original_media_type": "2",
            })
        elif is_reel:
            endpoint = "media/configure_to_clips/?video=1"
            extra_hdrs["Is_clips_video"] = "1"
            share_flag = "1" if share_to_feed else "0"
            payload.update({
                "clips_creation_entry_point": "clips",
                "capture_type": "clips_v2",
                "camera_entry_point": "360",
                "clips_share_preview_to_feed": share_flag,
                "is_shared_to_fb": share_flag,
                "creation_logger_session_id": cam_id,
                "camera_session_id": cam_id,
                "composition_id": str(uuid.uuid4()),
                "clips_segments_metadata": {
                    "num_segments": 1,
                    "clips_segments": [
                        {
                            "index": 0,
                            "face_effect_id": None,
                            "speed": 100,
                            "source_type": "0",
                            "duration_ms": dur_ms,
                            "audio_type": "original",
                            "from_draft": "0",
                            "camera_position": -1,
                            "media_folder": "Instagram",
                            "media_type": "video",
                            "original_media_type": "2",
                            "original_segment_hash": str(uuid.uuid4()).lower(),
                        }
                    ],
                },
                "clips_audio_metadata": {"original": {"volume": 1.0}},
                "is_created_with_contextual_music_recs": "0",
                "is_creator_requesting_mashup": "0",
                "is_gifting_enabled": "1",
                "is_created_with_sound_sync": "0",
                "is_clips_edited": "0",
                "third_party_downloads_enabled": "1",
            })
        else:
            endpoint = "media/configure/?video=1"

        exact_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        sleep_fn = transcode_sleep_fn or asyncio.sleep

        await notify_submit(on_submit)
        for counter in range(4):  # 初次 + 最多 3 次转码等待重 configure
            resp, data, err = await self._mobile_request(
                "POST",
                endpoint,
                operation="ConfigureVideo",
                body=ig_configure_content(exact_json),
                extra_headers=extra_hdrs,
                long_request=True,
                allow_560_retry=False,
            )
            if resp is None or resp.status_code >= 500:
                return False, "", f"write_uncertain:视频 configure 已提交但响应不确定({err or 'timeout'})"
            if err.startswith("transcode_pending:") and counter < 3:
                await sleep_fn(random.uniform(30.0, 45.0))
                continue
            if err:
                return False, "", err
            media = (data.get("media") if isinstance(data, Mapping) else None) or {}
            code = str(media.get("code") or "") if isinstance(media, Mapping) else ""
            pk = str(media.get("pk") or media.get("id") or "") if isinstance(media, Mapping) else ""
            if not code and pk:
                code = media_id_to_shortcode(pk)
            if not code and not pk:
                if "transcode not finished" in (resp.text or "").lower() and counter < 3:
                    await sleep_fn(random.uniform(30.0, 45.0))
                    continue
                return False, "", "视频 configure 响应缺少有效 media.code"
            kind = "reel" if is_reel and not is_story else "p"
            url = f"https://www.instagram.com/{kind}/{code}/" if code else "https://www.instagram.com/"
            return True, url, ""

        return False, "", "视频转码超时，多次 configure 仍未就绪"

    async def configure_sidecar(
        self,
        children: Sequence[Mapping[str, Any]],
        *,
        caption: str = "",
        disable_comments: bool = False,
        on_submit: Optional[Callable[[], Any]] = None,
        transcode_sleep_fn: Optional[Callable[[float], Awaitable[None]]] = None,
    ) -> Tuple[bool, str, str]:
        """提交图文/视频混合轮播图集 `media/configure_sidecar/`：
        子项 `device` / `edits` / `extra` / `clips` 按接口规范序列化为独立 JSON 字符串，`nav_chain` 显式置 `None`。
        """
        valid_children = [c for c in children if c.get("upload_id")]
        if len(valid_children) < 2:
            return False, "", "轮播图集至少需要 2 个已上传成功的媒体子项"

        dev_str = json.dumps(self.session.device_data(), separators=(",", ":"))
        now_dt = time.strftime("%Y%m%dT%I%M%S.000Z", time.gmtime())
        children_metadata: List[Dict[str, Any]] = []
        for ch in valid_children:
            w = int(ch.get("width") or 720)
            h = int(ch.get("height") or 720)
            extra_str = json.dumps({"source_width": w, "source_height": h}, separators=(",", ":"))
            if ch.get("kind") == "video":
                dur = round(max(0.1, float(ch.get("duration_sec") or 5.0)), 3)
                clips_str = json.dumps(
                    [{"length": dur, "source_type": "4", "camera_position": "back"}],
                    separators=(",", ":"),
                )
                edits_str = json.dumps({"filter_type": "0", "filter_strength": 1.0}, separators=(",", ":"))
                children_metadata.append({
                    "timezone_offset": str(self.session.timezone_offset),
                    "source_type": "4",
                    "video_result": "",
                    "audience": "default",
                    "date_time_original": now_dt,
                    "filter_type": "0",
                    "video_subtitles_enabled": "1",
                    "upload_id": str(ch["upload_id"]),
                    "device": dev_str,
                    "clips": clips_str,
                    "extra": extra_str,
                    "edits": edits_str,
                    "audio_muted": False,
                    "poster_frame_index": "0",
                    "length": dur,
                    "nav_chain": None,
                })
            else:
                crop_str = json.dumps(
                    {
                        "crop_original_size": [float(w), float(h)],
                        "crop_center": [0.0, -0.0],
                        "crop_zoom": 1.0,
                    },
                    separators=(",", ":"),
                )
                children_metadata.append({
                    "source_type": "4",
                    "timezone_offset": str(self.session.timezone_offset),
                    "upload_id": str(ch["upload_id"]),
                    "scene_capture_type": "",
                    "device": dev_str,
                    "edits": crop_str,
                    "extra": extra_str,
                    "nav_chain": None,
                })

        cam_id = str(uuid.uuid4())
        sidecar_upload_id = generate_upload_id(use_new=False)
        payload: Dict[str, Any] = {
            "camera_entry_point": "360",
            "camera_session_id": cam_id,
            "creation_logger_session_id": cam_id,
            "timezone_offset": str(self.session.timezone_offset),
            "source_type": "4",
            "_uid": self.self_uid,
            "device_id": self.session.device_id,
            "_uuid": self.session.unique_id_ex,
            "caption": caption or "",
            "audience": "default",
            "upload_id": sidecar_upload_id,
            "device": self.session.device_data(),
            "children_metadata": children_metadata,
        }
        if disable_comments:
            payload["disable_comments"] = "1"

        exact_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        sleep_fn = transcode_sleep_fn or asyncio.sleep

        await notify_submit(on_submit)
        for attempt in range(2):  # 转码未完成时等待后重 configure 一次
            resp, data, err = await self._mobile_request(
                "POST",
                "media/configure_sidecar/",
                operation="ConfigureSidecar",
                body=ig_configure_content(exact_json),
                extra_headers={"retry_context": RETRY_CONTEXT_DEFAULT},
                long_request=True,
                allow_560_retry=False,
            )
            if resp is None or resp.status_code >= 500:
                return False, "", f"write_uncertain:轮播 configure_sidecar 已提交但响应不确定({err or 'timeout'})"
            if err.startswith("transcode_pending:") and attempt == 0:
                await sleep_fn(random.uniform(30.0, 45.0))
                continue
            if err:
                return False, "", err
            media = (data.get("media") if isinstance(data, Mapping) else None) or {}
            code = str(media.get("code") or "") if isinstance(media, Mapping) else ""
            pk = str(media.get("pk") or media.get("id") or "") if isinstance(media, Mapping) else ""
            if not code and pk:
                code = media_id_to_shortcode(pk)
            if not code and not pk:
                return False, "", "轮播 configure_sidecar 响应缺少 media 对象"
            url = f"https://www.instagram.com/p/{code}/" if code else "https://www.instagram.com/"
            return True, url, ""

        return False, "", "轮播视频子项转码未完成"
