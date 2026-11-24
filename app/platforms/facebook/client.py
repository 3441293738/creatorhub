"""Facebook 异步协议客户端（SSR 内联 Relay + 动态 GraphQL 分页协议）。

架构设计（与本项目 Twitter 动态协议层保持一致，绝不硬编码过期 doc_id）：
1. **首屏 SSR 直取**：使用账号 `.facebook.com` Cookie Jar + 与浏览器 UA 对齐的 `curl_cffi` TLS 指纹直连目标页，
   直接解析服务端注入的 `<script type="application/json">` Relay 数据、会话参数（`fb_dtsg` / `lsd` / `jazoest` / `__rev`）
   与首页翻页游标（`end_cursor` / `has_next_page` / `feedback_id`）；
2. **动态 GraphQL 分页**：通过 `gql_registry` 从 Facebook 官方 `rsrc.php/*.js` 模块与浏览器被动观测到的
   `POST /api/graphql/` 自动维护当前版本 `doc_id` 与特性模板，对关键词搜索、主页/群组动态与帖子评论执行纯 HTTP
   RelayModern 高速翻页；
3. **透明容错与边界**：仅用于读采集（资料体检、公共主页列表、关键词帖子搜索、主页/群组时间线、单帖详情与评论）；
   若 `doc_id` 尚未学习或版本变更失效，`hybrid` 模式自动回退账号隔离浏览器并同步学习最新 `doc_id`；
   所有写操作（发帖/赞评/分享/加群/私信）仍由账号隔离浏览器承载。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import html as html_lib
import json
import random
import re
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple
from urllib.parse import urlencode, urlsplit

from curl_cffi.requests import AsyncSession

from ...netfp import curl_proxies, impersonate_for_ua
from .extract import (
    extract_graphql_comments,
    extract_graphql_posts,
    extract_pages_from_html,
    extract_pagination_info,
    extract_sjs_json_from_html,
    extract_target_post_from_sjs,
    parse_facebook_aweme,
    parse_facebook_user,
    parse_ndjson_payloads,
)
from .gql_registry import (
    extract_doc_ids_from_js,
    extract_rsrc_js_urls_from_html,
    get_slot_operation,
    invalidate_operation,
    record_operation,
)

if TYPE_CHECKING:
    from ..douyin.extract import Aweme
from .protocol import (
    DEFAULT_FACEBOOK_UA,
    build_facebook_web_headers,
    cookie_map_from_state,
    extract_c_user_uid,
    has_facebook_login_cookies,
    parse_members_count,
)
from .resolve import (
    group_post_url_from_photo,
    normalize_facebook_url,
    resolve_facebook_post_ref,
    resolve_facebook_short_url,
)
from .urls import FACEBOOK_BASE, HOME_URL, PAGES_SYNC_URL, PROFILE_ME_URL, post_search_url, profile_url


class FacebookApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 0, category: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.category = category


def classify_facebook_payload_error(status_code: int, final_url: str = "", body: str = "") -> str:
    """将 Facebook HTTP 状态码、重定向落点与页面文案映射为 CreatorHub 统一风控前缀。"""
    url_low = str(final_url or "").lower()
    body_low = str(body or "")[:8000].lower()

    if any(tok in url_low for tok in ("/checkpoint", "/two_step_verification", "/recover/", "/login/identify")):
        return f"verification_required:Facebook 账号触发安全检查或二次验证({final_url})"
    if any(tok in url_low for tok in ("/disabled", "/suspended")):
        return f"verification_required:Facebook 账号已被限制或停用({final_url})"
    if "/login" in url_low or 'name="login"' in body_low and 'type="password"' in body_low:
        return "logged_out:Facebook 登录态已失效，请重新登录"
    if status_code == 401:
        return "logged_out:Facebook 会话未认证(http_401)"
    if status_code == 429 or "temporarily blocked" in body_low or "you’re temporarily blocked" in body_low:
        return f"rate_limit:Facebook 请求触发临时限流(http_{status_code or 429})"
    if status_code == 404:
        return "not_found:目标 Facebook 内容或主页不存在"
    if status_code >= 400:
        return f"http_{status_code}"
    return ""


def _extract_meta(page_html: str, prop_name: str) -> str:
    pat = re.compile(
        rf'<meta[^>]+(?:property|name)=["\']{re.escape(prop_name)}["\'][^>]+content=["\']([^"\']*)["\']',
        re.IGNORECASE,
    )
    m = pat.search(page_html)
    if m:
        return html_lib.unescape(m.group(1)).strip()
    pat2 = re.compile(
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+(?:property|name)=["\']{re.escape(prop_name)}["\']',
        re.IGNORECASE,
    )
    m2 = pat2.search(page_html)
    return html_lib.unescape(m2.group(1)).strip() if m2 else ""


def _unescape_js_url(raw: str) -> str:
    if not raw:
        return ""
    try:
        val = json.loads(f'"{raw}"')
    except Exception:
        val = raw.replace("\\/", "/")
    return html_lib.unescape(val).strip()


def _extract_profile_social_counts(page_html: str) -> Dict[str, Any]:
    """从 Facebook 主页 SSR HTML 的 `profile_social_context` 与结构化字段提取粉丝/关注/好友/点赞计数。"""
    explicit_followers = 0
    explicit_following = 0
    friends_count = 0
    total_favorited = 0

    for m in re.finditer(r'"profile_social_context"\s*:\s*\{\s*"content"\s*:\s*\[', page_html):
        chunk = page_html[m.start():m.start() + 3000]
        # 每个条目包含 {"text":{...,"text":"39 friends"},"uri":"..."}
        for item_m in re.finditer(
            r'"text"\s*:\s*"((?:\\.|[^"\\]){1,120})"\s*\}\s*,\s*"uri"\s*:\s*"((?:\\.|[^"\\]){0,300})"',
            chunk,
        ):
            raw_text = item_m.group(1)
            raw_uri = item_m.group(2)
            try:
                txt = json.loads(f'"{raw_text}"').strip()
            except Exception:
                txt = raw_text.strip()
            uri = _unescape_js_url(raw_uri).lower()
            cnt = parse_members_count(txt)
            if cnt <= 0:
                continue
            low = txt.lower()
            if (
                "/followers" in uri
                or "sk=followers" in uri
                or re.search(r"follower|粉丝|粉絲|追蹤者|关注者", low)
            ):
                explicit_followers = max(explicit_followers, cnt)
            elif (
                "/following" in uri
                or "sk=following" in uri
                or re.search(r"following|正在关注|正在追蹤|追蹤中|已关注", low)
            ):
                explicit_following = max(explicit_following, cnt)
            elif (
                "/friends" in uri
                or "sk=friends" in uri
                or re.search(r"friend|好友|朋友", low)
            ):
                friends_count = max(friends_count, cnt)
            elif (
                "/likes" in uri
                or re.search(r"\blikes?\b|个赞|個讚|次赞|人赞", low)
            ):
                total_favorited = max(total_favorited, cnt)

    if explicit_followers <= 0:
        for pat in (r'"followers_count"\s*:\s*(\d+)', r'"follower_count"\s*:\s*(\d+)'):
            m_f = re.search(pat, page_html)
            if m_f and int(m_f.group(1)) > 0:
                explicit_followers = int(m_f.group(1))
                break
    if explicit_following <= 0:
        m_fg = re.search(r'"following_count"\s*:\s*(\d+)', page_html)
        if m_fg and int(m_fg.group(1)) > 0:
            explicit_following = int(m_fg.group(1))
    if friends_count <= 0:
        m_fr = re.search(r'"friends_count"\s*:\s*(\d+)', page_html)
        if m_fr and int(m_fr.group(1)) > 0:
            friends_count = int(m_fr.group(1))

    return {
        "follower_count": explicit_followers or friends_count,
        "following_count": explicit_following or friends_count,
        "friends_count": friends_count,
        "total_favorited": total_favorited,
        "follower_count_is_friends_fallback": bool(not explicit_followers and friends_count > 0),
        "following_count_is_friends_fallback": bool(not explicit_following and friends_count > 0),
    }


def _extract_self_from_html(page_html: str, fallback_uid: str = "") -> Dict[str, Any]:
    """从 Facebook 登录态首页 / `/profile.php` SSR 脚本抽取当前账号基础资料。"""
    uid = fallback_uid
    m_uid = re.search(r'"USER_ID"\s*:\s*"(\d{4,25})"', page_html)
    if m_uid and m_uid.group(1) != "0":
        uid = m_uid.group(1)
    if not uid:
        m_act = re.search(r'"ACCOUNT_ID"\s*:\s*"(\d{4,25})"', page_html)
        if m_act and m_act.group(1) != "0":
            uid = m_act.group(1)

    name = ""
    for pat in (
        r'"NAME"\s*:\s*"((?:\\.|[^"\\])+)"',
        r'"CurrentUserInitialData"[^}]*"NAME"\s*:\s*"((?:\\.|[^"\\])+)"',
        r'"SHORT_NAME"\s*:\s*"((?:\\.|[^"\\])+)"',
    ):
        m = re.search(pat, page_html)
        if m:
            try:
                cand = json.loads(f'"{m.group(1)}"').strip()
            except Exception:
                cand = m.group(1).strip()
            if cand and cand.lower() not in {"facebook", "log in", "login"}:
                name = cand
                break
    if not name:
        title_m = re.search(r"<title[^>]*>([^<]+)</title>", page_html, re.IGNORECASE)
        if title_m:
            t = html_lib.unescape(title_m.group(1)).strip()
            t = re.sub(r"\s*[|\-–]\s*Facebook\s*$", "", t, flags=re.IGNORECASE).strip()
            if t and t.lower() not in {"facebook", "log in or sign up", "登录或注册"}:
                name = t

    vanity = ""
    m_van = re.search(r'"vanity"\s*:\s*"([A-Za-z0-9._-]{2,80})"', page_html)
    if m_van:
        vanity = m_van.group(1).strip()

    avatar = ""
    for pat in (
        r'"profilePicSmall"\s*:\s*\{[^}]*"uri"\s*:\s*"((?:\\.|[^"\\])+)"',
        r'"profile_picture"\s*:\s*\{[^}]*"uri"\s*:\s*"((?:\\.|[^"\\])+)"',
        r'"profilePicMedium"\s*:\s*\{[^}]*"uri"\s*:\s*"((?:\\.|[^"\\])+)"',
    ):
        m_av = re.search(pat, page_html)
        if m_av:
            avatar = _unescape_js_url(m_av.group(1))
            if avatar.startswith("http"):
                break
    if not avatar:
        avatar = _extract_meta(page_html, "og:image")

    social = _extract_profile_social_counts(page_html)
    return parse_facebook_user({
        "uid": uid,
        "unique_id": vanity or uid,
        "nickname": name or vanity or uid,
        "avatar": avatar,
        **social,
    })


def _extract_post_from_html(page_html: str, page_url: str) -> Optional[Dict[str, Any]]:
    """从 Facebook 帖子/视频/Reels 页面 HTML 提取视频直链、图片、作者与正文。"""
    title = _extract_meta(page_html, "og:title")
    desc = _extract_meta(page_html, "og:description")
    og_image = _extract_meta(page_html, "og:image")
    og_video = _extract_meta(page_html, "og:video") or _extract_meta(page_html, "og:video:url")
    canonical = _extract_meta(page_html, "og:url") or page_url

    hd_url = ""
    sd_url = ""
    for pat in (
        r'"browser_native_hd_url"\s*:\s*"((?:\\.|[^"\\])+)"',
        r'"playable_url_quality_hd"\s*:\s*"((?:\\.|[^"\\])+)"',
        r'"hd_src(?:_no_ratelimit)?"\s*:\s*"((?:\\.|[^"\\])+)"',
    ):
        m = re.search(pat, page_html)
        if m:
            hd_url = _unescape_js_url(m.group(1))
            if hd_url.startswith("http"):
                break
    for pat in (
        r'"browser_native_sd_url"\s*:\s*"((?:\\.|[^"\\])+)"',
        r'"playable_url"\s*:\s*"((?:\\.|[^"\\])+)"',
        r'"sd_src(?:_no_ratelimit)?"\s*:\s*"((?:\\.|[^"\\])+)"',
    ):
        m = re.search(pat, page_html)
        if m:
            sd_url = _unescape_js_url(m.group(1))
            if sd_url.startswith("http"):
                break
    if not sd_url and og_video.startswith("http"):
        sd_url = og_video

    images: List[str] = []
    if not (hd_url or sd_url):
        for pat in (
            r'"viewer_image"\s*:\s*\{[^}]*"uri"\s*:\s*"((?:\\.|[^"\\])+)"',
            r'"photo_image"\s*:\s*\{[^}]*"uri"\s*:\s*"((?:\\.|[^"\\])+)"',
        ):
            for m in re.finditer(pat, page_html):
                u = _unescape_js_url(m.group(1))
                if u.startswith("http") and u not in images:
                    images.append(u)
                if len(images) >= 12:
                    break
        if not images and og_image.startswith("http"):
            images.append(og_image)

    author_name = ""
    m_auth = re.search(r'"owning_profile"\s*:\s*\{[^}]*"name"\s*:\s*"((?:\\.|[^"\\])+)"', page_html)
    if not m_auth:
        m_auth = re.search(r'"actors"\s*:\s*\[\s*\{[^}]*"name"\s*:\s*"((?:\\.|[^"\\])+)"', page_html)
    if m_auth:
        try:
            author_name = json.loads(f'"{m_auth.group(1)}"').strip()
        except Exception:
            author_name = m_auth.group(1).strip()

    create_time = 0
    m_ts = re.search(r'"(?:creation_time|publish_time)"\s*:\s*(\d{9,11})', page_html)
    if m_ts:
        create_time = int(m_ts.group(1))

    duration = 0
    m_dur = re.search(r'"playable_duration_in_ms"\s*:\s*(\d+)', page_html)
    if m_dur:
        duration = max(0, int(int(m_dur.group(1)) / 1000))

    full_text = desc or title
    if title and desc and title not in desc and len(title) < 120:
        full_text = f"{title}\n{desc}".strip()

    if not (hd_url or sd_url or images or full_text):
        return None

    ref = resolve_facebook_post_ref(canonical, allow_numeric=False) or resolve_facebook_post_ref(page_url, allow_numeric=False)
    return {
        "post_id": ref.post_id if ref else "",
        "source_url": normalize_facebook_url(canonical or page_url),
        "author_name": author_name,
        "author_unique_id": ref.author_id if ref else "",
        "desc": full_text,
        "media_type": "video" if (hd_url or sd_url) else "images",
        "browser_native_hd_url": hd_url,
        "browser_native_sd_url": sd_url,
        "images": images,
        "cover": og_image,
        "create_time": create_time,
        "duration": duration,
    }


class FacebookClient:
    """Facebook 轻量异步 HTTP 会话客户端。"""

    def __init__(
        self,
        storage_state_or_cookie: str | Mapping[str, Any] = "",
        user_agent: str = "",
        *,
        proxy: str = "",
        timeout: float = 20.0,
        accept_language: str = "en-US,en;q=0.9",
    ):
        self.cookies: Dict[str, str] = cookie_map_from_state(storage_state_or_cookie)
        self.user_agent = (user_agent or DEFAULT_FACEBOOK_UA).strip()
        self.proxy = str(proxy or "").strip()
        self.timeout = max(5.0, float(timeout))
        self.accept_language = accept_language or "en-US,en;q=0.9"
        self._session: Optional[AsyncSession] = None
        self._cookie_changed: bool = False
        self.base_url: str = FACEBOOK_BASE
        self._gql_ctx: Dict[str, str] = {}
        self._req_seq: int = 1

    @property
    def has_login_cookies(self) -> bool:
        return has_facebook_login_cookies(self.cookies)

    @property
    def self_uid(self) -> str:
        return extract_c_user_uid(self.cookies)

    @property
    def cookie_changed(self) -> bool:
        return self._cookie_changed

    def _update_base_url(self, final_url: str) -> None:
        try:
            sp = urlsplit(str(final_url or ""))
            host = (sp.hostname or "").lower()
            if host in {"www.facebook.com", "web.facebook.com"}:
                self.base_url = f"https://{host}"
        except ValueError:
            pass

    def _rewrite_to_base(self, url: str) -> str:
        if self.base_url != FACEBOOK_BASE and url.startswith(FACEBOOK_BASE):
            return self.base_url + url[len(FACEBOOK_BASE):]
        return url

    def _extract_gql_context_from_html(self, body: str) -> Dict[str, str]:
        """从任意 Facebook Comet 登录态页面 HTML 提取 GraphQL 必需的会话令牌与版本参数。"""
        if not body:
            return self._gql_ctx
        ctx = dict(self._gql_ctx)
        for pat in (
            r'"DTSGInit(?:ial)?Data"[^}]*"token"\s*:\s*"([^"]+)"',
            r'"dtsg"\s*:\s*\{"token"\s*:\s*"([^"]+)"',
            r'name="fb_dtsg"\s+value="([^"]+)"',
        ):
            m = re.search(pat, body)
            if m and m.group(1):
                ctx["fb_dtsg"] = m.group(1).strip()
                break
        if ctx.get("fb_dtsg"):
            ctx["jazoest"] = "2" + "".join(str(ord(c)) for c in ctx["fb_dtsg"])
        for pat in (
            r'"LSD"[^}]*"token"\s*:\s*"([^"]+)"',
            r'name="lsd"\s+value="([^"]+)"',
        ):
            m = re.search(pat, body)
            if m and m.group(1):
                ctx["lsd"] = m.group(1).strip()
                break
        for key, pat in (
            ("__rev", r'"client_revision"\s*:\s*(\d+)'),
            ("__hsi", r'"hsi"\s*:\s*"([^"]+)"'),
            ("__hs", r'"haste_session"\s*:\s*"([^"]+)"'),
            ("__spin_r", r'"__spin_r"\s*:\s*(\d+)'),
            ("__spin_b", r'"__spin_b"\s*:\s*"([^"]+)"'),
            ("__spin_t", r'"__spin_t"\s*:\s*(\d+)'),
        ):
            m = re.search(pat, body)
            if m and m.group(1):
                ctx[key] = m.group(1).strip()
        if not ctx.get("__spin_r") and ctx.get("__rev"):
            ctx["__spin_r"] = ctx["__rev"]
        self._gql_ctx = ctx
        return ctx

    async def ensure_gql_context(self) -> Tuple[Dict[str, str], str]:
        """确保已持有有效的 `fb_dtsg` / `lsd` / `__rev` 会话参数；缺失时拉取首页自动补全。"""
        if self._gql_ctx.get("fb_dtsg") and self._gql_ctx.get("lsd"):
            return self._gql_ctx, ""
        if not self.has_login_cookies:
            return {}, "logged_out:缺少 Facebook 核心登录 Cookie (c_user / xs)"
        try:
            status, final_url, body = await self._get_html(self._rewrite_to_base(HOME_URL))
        except Exception as exc:
            return {}, f"网络请求失败: {exc!r}"
        err = classify_facebook_payload_error(status, final_url, body)
        if err:
            return {}, err
        all_uids = re.findall(r'"USER_ID"\s*:\s*"(\d+)"', body)
        if all_uids and not any(u and u != "0" for u in all_uids):
            return {}, "logged_out:Facebook 会话已失效 (USER_ID=0)"
        ctx = self._extract_gql_context_from_html(body)
        if not ctx.get("fb_dtsg") and not ctx.get("lsd"):
            return ctx, "未能从 Facebook 页面提取 fb_dtsg/lsd 会话令牌"
        return ctx, ""

    async def _discover_doc_ids_from_html(self, html_text: str, target_slots: Sequence[str]) -> int:
        """当逻辑槽位缺少缓存 `doc_id` 时，纯 HTTP 拉取页面 `rsrcMap` 中的 JS 模块自动发现 live `doc_id`。"""
        missing = [s for s in target_slots if get_slot_operation(s) is None]
        if not missing or not html_text:
            return 0
        js_urls = extract_rsrc_js_urls_from_html(html_text, target_slots=missing)
        if not js_urls:
            return 0
        own_session = self._session is None
        cli = self._session or self._build_session()
        learned = 0
        try:
            for ju in js_urls[:12]:
                if all(get_slot_operation(s) is not None for s in missing):
                    break
                try:
                    resp = await cli.get(
                        ju,
                        headers={"User-Agent": self.user_agent, "Accept": "*/*"},
                        timeout=min(self.timeout, 10.0),
                    )
                    if int(resp.status_code) != 200:
                        continue
                    for fname, did in extract_doc_ids_from_js(str(resp.text or "")):
                        if record_operation(fname, did, source="http_rsrc"):
                            learned += 1
                except Exception:
                    continue
        finally:
            if own_session:
                try:
                    await cli.close()
                except Exception:
                    pass
        return learned

    def _next_req_token(self) -> str:
        n = self._req_seq
        self._req_seq += 1
        chars = "0123456789abcdefghijklmnopqrstuvwxyz"
        out = ""
        while n:
            n, r = divmod(n, 36)
            out = chars[r] + out
        return out or "1"

    async def gql_post(
        self,
        friendly_name: str,
        doc_id: str,
        variables: Mapping[str, Any],
        *,
        referer: str = "",
    ) -> Tuple[List[Any], str]:
        """向 `{base_url}/api/graphql/` 发送标准 Comet RelayModern 请求并解析 NDJSON 响应。"""
        ctx, ctx_err = await self.ensure_gql_context()
        if ctx_err:
            return [], ctx_err
        uid = self.self_uid or "0"
        form: Dict[str, str] = {
            "av": uid,
            "__aaid": "0",
            "__user": uid,
            "__a": "1",
            "__req": self._next_req_token(),
            "__hs": ctx.get("__hs") or "",
            "dpr": "1",
            "__ccg": "EXCELLENT",
            "__rev": ctx.get("__rev") or "",
            "__hsi": ctx.get("__hsi") or "",
            "__comet_req": "15",
            "fb_dtsg": ctx.get("fb_dtsg") or "",
            "jazoest": ctx.get("jazoest") or "",
            "lsd": ctx.get("lsd") or "",
            "__spin_r": ctx.get("__spin_r") or ctx.get("__rev") or "",
            "__spin_b": ctx.get("__spin_b") or "trunk",
            "__spin_t": ctx.get("__spin_t") or "",
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": friendly_name,
            "variables": json.dumps(variables, ensure_ascii=False, separators=(",", ":")),
            "server_timestamps": "true",
            "doc_id": str(doc_id),
        }
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Accept-Language": self.accept_language,
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": self.base_url,
            "Referer": referer or (self.base_url + "/"),
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "x-asbd-id": "129477",
            "x-fb-friendly-name": friendly_name,
            "x-fb-lsd": ctx.get("lsd") or "",
        }
        own_session = self._session is None
        cli = self._session or self._build_session()
        if not own_session:
            self._seed_session_cookies(cli)
        gql_url = f"{self.base_url}/api/graphql/"
        try:
            resp = await cli.post(
                gql_url,
                headers=headers,
                data=urlencode(form),
                timeout=self.timeout,
                allow_redirects=False,
            )
            self._merge_response_cookies(resp)
            status = int(resp.status_code)
            text = str(resp.text or "")
            if status in {301, 302, 303, 307, 308}:
                loc = str(resp.headers.get("location") or "")
                return [], classify_facebook_payload_error(status, loc, text) or f"gql_redirect:{loc}"
            if status >= 400:
                return [], classify_facebook_payload_error(status, gql_url, text) or f"http_{status}"
            payloads = parse_ndjson_payloads(text)
            if not payloads:
                return [], "gql_empty_payload"
            # 检查 GraphQL 错误（区分登录失效/频控 vs doc_id 过期）
            for obj in payloads:
                if not isinstance(obj, Mapping):
                    continue
                if obj.get("error") == 1357004 or obj.get("error") == 1357001:
                    return [], "logged_out:Facebook GraphQL 会话已失效，请重新登录"
                errs = obj.get("errors")
                if isinstance(errs, list) and errs and not obj.get("data"):
                    first_e = errs[0] if isinstance(errs[0], Mapping) else {}
                    emsg = str(first_e.get("message") or first_e.get("summary") or "")
                    ecode = first_e.get("code")
                    low = emsg.lower()
                    if ecode in {1357001, 1357004, 190} or "not logged in" in low or "login" in low:
                        return [], f"logged_out:Facebook GraphQL 未登录({emsg or ecode})"
                    if ecode in {1675004, 368} or "rate limit" in low or "temporarily blocked" in low:
                        return [], f"rate_limit:Facebook GraphQL 触发频控({emsg or ecode})"
                    if ecode in {1675030, 1675007} or "invalid" in low or "query" in low or "doc_id" in low or "variable" in low:
                        invalidate_operation(friendly_name)
                        return [], f"gql_schema_changed:{emsg or ecode}"
                    return [], f"gql_error:{emsg or ecode}"
            return payloads, ""
        except Exception as exc:
            return [], f"GraphQL 请求失败: {exc!r}"
        finally:
            if own_session:
                try:
                    await cli.close()
                except Exception:
                    pass

    def _build_session(self) -> AsyncSession:
        proxies = curl_proxies(self.proxy) if self.proxy else None
        cli = AsyncSession(
            impersonate=impersonate_for_ua(self.user_agent),
            proxies=proxies,
            trust_env=False,
        )
        # 必须写入 `.facebook.com` Cookie Jar 而非裸 Cookie 请求头：
        # Facebook 会按地区将 `www.facebook.com` 302 重定向至 `web.facebook.com`，
        # libcurl 跨主机重定向时会安全剥离手写 Cookie 头导致误判未登录（USER_ID=0）。
        self._seed_session_cookies(cli)
        return cli

    def _seed_session_cookies(self, cli: AsyncSession) -> None:
        for k, v in (self.cookies or {}).items():
            ks, vs = str(k or "").strip(), str(v or "").strip()
            if ks and vs:
                try:
                    cli.cookies.set(ks, vs, domain=".facebook.com", path="/")
                except Exception:
                    pass

    @asynccontextmanager
    async def session_scope(self):
        if self._session is not None:
            yield self
            return
        cli = self._build_session()
        self._session = cli
        try:
            yield self
        finally:
            self._session = None
            try:
                await cli.close()
            except Exception:
                pass

    def _merge_response_cookies(self, resp: Any) -> None:
        try:
            jar = getattr(resp, "cookies", None)
            if jar is None:
                return
            items = jar.items() if hasattr(jar, "items") else []
            for k, v in items:
                ks, vs = str(k or "").strip(), str(v or "").strip()
                if ks and vs and self.cookies.get(ks) != vs:
                    self.cookies[ks] = vs
                    self._cookie_changed = True
        except Exception:
            pass

    async def _get_html(self, url: str, *, referer: str = "https://www.facebook.com/") -> Tuple[int, str, str]:
        headers = build_facebook_web_headers(
            self.cookies,
            user_agent=self.user_agent,
            referer=referer,
            accept_language=self.accept_language,
            include_cookie=False,
        )
        own_session = self._session is None
        cli = self._session or self._build_session()
        if not own_session:
            self._seed_session_cookies(cli)
        try:
            resp = await cli.get(
                url,
                headers=headers,
                timeout=self.timeout,
                allow_redirects=True,
            )
            self._merge_response_cookies(resp)
            final_url = str(resp.url or url)
            body = str(resp.text or "")
            self._update_base_url(final_url)
            if int(resp.status_code) == 200 and body:
                self._extract_gql_context_from_html(body)
            return int(resp.status_code), final_url, body
        finally:
            if own_session:
                try:
                    await cli.close()
                except Exception:
                    pass

    async def fetch_self_profile(self) -> Tuple[Optional[Dict[str, Any]], str]:
        """通过 Cookie 请求 `/profile.php` 校验会话并提取本账号资料与已管理的公共主页列表。"""
        if not self.has_login_cookies:
            return None, "logged_out:缺少 Facebook 核心登录 Cookie (c_user / xs)"
        try:
            status, final_url, body = await self._get_html(PROFILE_ME_URL)
        except Exception as exc:
            return None, f"网络请求失败: {exc!r}"

        err = classify_facebook_payload_error(status, final_url, body)
        if err:
            return None, err

        # 检查页面脚本中所有 USER_ID：仅当没有任何非 0 USER_ID 且明确出现 USER_ID="0" 时判定失效
        all_uids = re.findall(r'"USER_ID"\s*:\s*"(\d+)"', body)
        non_zero_uids = [u for u in all_uids if u and u != "0"]
        if all_uids and not non_zero_uids:
            return None, "logged_out:Facebook 会话已失效 (USER_ID=0)"

        prof = _extract_self_from_html(body, fallback_uid=(non_zero_uids[0] if non_zero_uids else self.self_uid))
        if not prof.get("uid"):
            return None, "logged_out:未能确认当前 Facebook 登录账号"

        # 顺带尝试拉取管理的公共主页（失败不影响主资料返回）
        try:
            pages, _ = await self.fetch_managed_pages()
            if pages:
                prof["pages"] = pages
        except Exception:
            pass
        return prof, ""

    async def fetch_managed_pages(self) -> Tuple[List[Dict[str, Any]], str]:
        """读取当前账号名下管理的公共主页列表。"""
        if not self.has_login_cookies:
            return [], "logged_out:缺少 Facebook 登录凭证"
        try:
            status, final_url, body = await self._get_html(PAGES_SYNC_URL)
        except Exception as exc:
            return [], f"网络请求失败: {exc!r}"
        err = classify_facebook_payload_error(status, final_url, body)
        if err:
            return [], err
        return extract_pages_from_html(body), ""

    async def fetch_user_profile(self, user_ref: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """通过 HTTP 读取目标个人主页 / 公共主页 / 群组的基础公开资料。"""
        url = profile_url(user_ref)
        try:
            status, final_url, body = await self._get_html(url)
        except Exception as exc:
            return None, f"网络请求失败: {exc!r}"
        err = classify_facebook_payload_error(status, final_url, body)
        if err:
            return None, err
        title = _extract_meta(body, "og:title")
        title = re.sub(r"\s*[|\-–]\s*Facebook\s*$", "", title, flags=re.IGNORECASE).strip()
        avatar = _extract_meta(body, "og:image")
        desc = _extract_meta(body, "og:description")
        if not title and not avatar:
            return None, "not_found:未能读取目标主页信息"
        social = _extract_profile_social_counts(body)
        return parse_facebook_user({
            "uid": user_ref if str(user_ref).isdigit() else "",
            "unique_id": user_ref,
            "nickname": title or user_ref,
            "avatar": avatar,
            "signature": desc,
            **social,
        }), ""

    async def fetch_post_detail(self, raw_url: str) -> Tuple[Optional[Aweme], str]:
        """解析单条 Facebook 帖子 / 视频 / Reels / Watch 链接并返回 `Aweme`（SSR Relay JSON 优先，OpenGraph 兜底）。"""
        expanded = await resolve_facebook_short_url(
            raw_url,
            user_agent=self.user_agent,
            proxy=self.proxy,
            timeout=self.timeout,
        )
        target_url = group_post_url_from_photo(expanded) or normalize_facebook_url(expanded)
        if not target_url.startswith(("http://", "https://")):
            return None, "无效的 Facebook 链接"
        try:
            status, final_url, body = await self._get_html(self._rewrite_to_base(target_url))
        except Exception as exc:
            return None, f"网络请求失败: {exc!r}"

        err = classify_facebook_payload_error(status, final_url, body)
        if err and not err.startswith("logged_out:"):
            return None, err

        # 1) 优先从 SSR 内联 `<script type="application/json">` 合并目标 Reel/帖子的多块流式 Relay 数据
        ref = resolve_facebook_post_ref(target_url, allow_numeric=False)
        all_sjs = extract_sjs_json_from_html(body, markers=())
        merged_story = extract_target_post_from_sjs(
            all_sjs,
            target_url=final_url or target_url,
            target_id=(ref.post_id if ref else ""),
        )
        if merged_story is not None:
            aw_sjs = parse_facebook_aweme(merged_story)
            if aw_sjs is not None:
                return aw_sjs, ""

        # 2) 兜底使用 OpenGraph / 页面视频直链正则
        raw_node = _extract_post_from_html(body, final_url)
        if raw_node is None:
            if err:
                return None, err
            return None, "未能从页面 SSR 提取 Facebook 帖子或视频数据，需回退隔离浏览器"
        aw = parse_facebook_aweme(raw_node)
        if aw is None:
            return None, "Facebook 帖子结构解析失败"
        return aw, ""

    async def search_posts(
        self,
        keyword: str,
        *,
        max_pages: int = 4,
        max_items: int = 40,
        content_type: str = "all",
        recent: bool = False,
    ) -> Tuple[List[Dict[str, Any]], str]:
        """纯 HTTP 协议关键词搜索 Facebook 帖子/视频（首屏 SSR Relay + 动态 GraphQL 游标翻页）。"""
        kw = str(keyword or "").strip()
        if not kw:
            return [], "关键词不能为空"
        if not self.has_login_cookies:
            return [], "logged_out:缺少 Facebook 核心登录 Cookie (c_user / xs)"

        url = self._rewrite_to_base(post_search_url(kw, content_type=content_type, recent=recent))
        try:
            status, final_url, body = await self._get_html(url)
        except Exception as exc:
            return [], f"网络请求失败: {exc!r}"

        err = classify_facebook_payload_error(status, final_url, body)
        if err:
            return [], err
        all_uids = re.findall(r'"USER_ID"\s*:\s*"(\d+)"', body)
        if all_uids and not any(u and u != "0" for u in all_uids):
            return [], "logged_out:Facebook 会话已失效 (USER_ID=0)"

        items_by_id: Dict[str, Dict[str, Any]] = {}
        cursor = ""
        has_next = False
        bsid = ""

        for obj in extract_sjs_json_from_html(body, markers=("serpResponse", "comet_sections", "permalink_url", '"Story"')):
            for st in extract_graphql_posts(obj):
                aw = parse_facebook_aweme(st)
                if aw and aw.aweme_id and aw.aweme_id not in items_by_id:
                    items_by_id[aw.aweme_id] = st
            pg = extract_pagination_info(obj, kind="posts")
            if pg["end_cursor"] and not cursor:
                cursor = pg["end_cursor"]
                has_next = bool(pg["has_next_page"])
            if pg["bsid"] and not bsid:
                bsid = pg["bsid"]

        # 若只需 1 页或已满足上限，直接零浏览器高速返回
        pages_limit = max(1, min(int(max_pages or 4), 20))
        if max_items and len(items_by_id) >= max_items:
            return list(items_by_id.values())[:max_items], ""

        if pages_limit > 1 and has_next and cursor:
            op = get_slot_operation("search_posts")
            if op is None:
                await self._discover_doc_ids_from_html(body, ("search_posts",))
                op = get_slot_operation("search_posts")
            if op is not None:
                fname = op["friendly_name"]
                did = op["doc_id"]
                tpl = dict(op.get("variables_template") or {})
                for _ in range(pages_limit - 1):
                    if not cursor or not has_next:
                        break
                    if max_items and len(items_by_id) >= max_items:
                        break
                    await asyncio.sleep(random.uniform(0.55, 1.15))
                    args_obj: Dict[str, Any] = {
                        "allow_streaming": False,
                        "args": {
                            "callsite": "COMET_GLOBAL_SEARCH",
                            "config": {
                                "exact_match": False,
                                "high_confidence_config": None,
                                "intercept_config": None,
                                "sts_disambiguation": None,
                                "watch_config": None,
                            },
                            "context": {"bsid": bsid or "", "tsid": None},
                            "experience": {
                                "client_defined_experiences": ["ADS_PARALLEL_FETCH"],
                                "encoded_server_defined_params": None,
                                "fbid": None,
                                "type": "VIDEOS_TAB" if content_type == "video" else "POSTS_TAB",
                            },
                            "filters": [
                                json.dumps({"name": "recent_posts", "args": ""})
                            ] if recent else [],
                            "text": kw,
                        },
                        "count": 5,
                        "cursor": cursor,
                        "feedLocation": "SEARCH",
                        "feedbackSource": 23,
                        "fetch_filters": False,
                        "renderLocation": "search_results_page",
                        "scale": 1,
                        "useDefaultActor": False,
                    }
                    # 合并观测到的非敏感静态开关，保留本次查询的核心动态参数
                    merged_vars = dict(tpl)
                    merged_vars.update(args_obj)
                    payloads, g_err = await self.gql_post(fname, did, merged_vars, referer=final_url)
                    if g_err:
                        if g_err.startswith(("logged_out:", "verification_required:", "rate_limit:")):
                            return list(items_by_id.values()), g_err
                        break
                    added = 0
                    next_cursor = ""
                    next_has = False
                    for pobj in payloads:
                        for st in extract_graphql_posts(pobj):
                            aw = parse_facebook_aweme(st)
                            if aw and aw.aweme_id and aw.aweme_id not in items_by_id:
                                items_by_id[aw.aweme_id] = st
                                added += 1
                        pg2 = extract_pagination_info(pobj, kind="posts")
                        if pg2["end_cursor"]:
                            next_cursor = pg2["end_cursor"]
                            next_has = bool(pg2["has_next_page"])
                    if not next_cursor or next_cursor == cursor or added == 0:
                        break
                    cursor, has_next = next_cursor, next_has

        out = list(items_by_id.values())
        if max_items:
            out = out[:max_items]
        if not out:
            return [], "api_empty_search"
        return out, ""

    async def fetch_user_posts(
        self,
        user_ref: str,
        *,
        known_ids: Optional[Set[str]] = None,
        max_pages: int = 4,
        max_items: int = 40,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any], str]:
        """纯 HTTP 协议抓取个人主页 / 公共主页 / 群组时间线帖子（首屏 SSR + 动态 GraphQL 翻页）。"""
        if not self.has_login_cookies:
            return [], {}, "logged_out:缺少 Facebook 核心登录 Cookie (c_user / xs)"
        known = set(known_ids or ())
        target_url = self._rewrite_to_base(profile_url(user_ref))
        is_group = "/groups/" in target_url.lower()
        try:
            status, final_url, body = await self._get_html(target_url)
        except Exception as exc:
            return [], {}, f"网络请求失败: {exc!r}"

        err = classify_facebook_payload_error(status, final_url, body)
        if err:
            return [], {}, err
        all_uids = re.findall(r'"USER_ID"\s*:\s*"(\d+)"', body)
        if all_uids and not any(u and u != "0" for u in all_uids):
            return [], {}, "logged_out:Facebook 会话已失效 (USER_ID=0)"

        title = _extract_meta(body, "og:title")
        title = re.sub(r"\s*[|\-–]\s*Facebook\s*$", "", title, flags=re.IGNORECASE).strip()
        avatar = _extract_meta(body, "og:image")
        author_info = parse_facebook_user({
            "uid": user_ref if str(user_ref).isdigit() else "",
            "unique_id": user_ref,
            "nickname": title or user_ref,
            "avatar": avatar,
        })

        items_by_id: Dict[str, Dict[str, Any]] = {}
        cursor = ""
        has_next = False
        target_id = user_ref if str(user_ref).isdigit() else ""

        for obj in extract_sjs_json_from_html(body):
            for st in extract_graphql_posts(obj):
                aw = parse_facebook_aweme(st)
                if aw and aw.aweme_id and aw.aweme_id not in items_by_id:
                    items_by_id[aw.aweme_id] = st
            pg = extract_pagination_info(obj, kind="posts")
            if pg["end_cursor"] and not cursor:
                cursor = pg["end_cursor"]
                has_next = bool(pg["has_next_page"])
            if pg["target_id"] and not target_id:
                target_id = pg["target_id"]

        if target_id and not author_info.get("uid"):
            author_info["uid"] = target_id
        for st in items_by_id.values():
            aw_first = parse_facebook_aweme(st)
            if aw_first is None:
                continue
            if aw_first.author_name and (not title or author_info.get("nickname") == user_ref):
                author_info["nickname"] = aw_first.author_name
            if aw_first.avatar and not author_info.get("avatar"):
                author_info["avatar"] = aw_first.avatar
            if author_info.get("nickname") and author_info.get("nickname") != user_ref and author_info.get("avatar"):
                break

        pages_limit = max(1, min(int(max_pages or 4), 20))
        if (known and any(k in items_by_id for k in known)) or (max_items and len(items_by_id) >= max_items):
            out = list(items_by_id.values())
            return (out[:max_items] if max_items else out), author_info, ""

        slot = "group_posts" if is_group else "profile_posts"
        if pages_limit > 1 and has_next and cursor and target_id:
            op = get_slot_operation(slot)
            if op is None:
                await self._discover_doc_ids_from_html(body, (slot,))
                op = get_slot_operation(slot)
            if op is not None:
                fname = op["friendly_name"]
                did = op["doc_id"]
                tpl = dict(op.get("variables_template") or {})
                for _ in range(pages_limit - 1):
                    if not cursor or not has_next:
                        break
                    if (known and any(k in items_by_id for k in known)) or (max_items and len(items_by_id) >= max_items):
                        break
                    await asyncio.sleep(random.uniform(0.55, 1.15))
                    merged_vars = dict(tpl)
                    merged_vars.update({
                        "count": 5,
                        "cursor": cursor,
                        "id": target_id,
                        "scale": 1,
                        "useDefaultActor": False,
                    })
                    if is_group:
                        merged_vars["groupID"] = target_id
                    payloads, g_err = await self.gql_post(fname, did, merged_vars, referer=final_url)
                    if g_err:
                        if g_err.startswith(("logged_out:", "verification_required:", "rate_limit:")):
                            return list(items_by_id.values()), author_info, g_err
                        break
                    added = 0
                    next_cursor = ""
                    next_has = False
                    for pobj in payloads:
                        for st in extract_graphql_posts(pobj):
                            aw = parse_facebook_aweme(st)
                            if aw and aw.aweme_id and aw.aweme_id not in items_by_id:
                                items_by_id[aw.aweme_id] = st
                                added += 1
                        pg2 = extract_pagination_info(pobj, kind="posts")
                        if pg2["end_cursor"]:
                            next_cursor = pg2["end_cursor"]
                            next_has = bool(pg2["has_next_page"])
                    if not next_cursor or next_cursor == cursor or added == 0:
                        break
                    cursor, has_next = next_cursor, next_has

        out = list(items_by_id.values())
        if max_items:
            out = out[:max_items]
        if not out:
            return [], author_info, "api_empty_timeline"
        return out, author_info, ""

    async def fetch_post_comments(
        self,
        post_url_raw: str,
        *,
        max_pages: int = 4,
        max_count: int = 50,
        include_replies: bool = True,
    ) -> Tuple[List[Dict[str, Any]], str]:
        """纯 HTTP 协议抓取 Facebook 帖子/视频/Reels 评论（首屏 SSR + 动态 GraphQL 翻页）。"""
        if not self.has_login_cookies:
            return [], "logged_out:缺少 Facebook 核心登录 Cookie (c_user / xs)"
        target_url = group_post_url_from_photo(post_url_raw) or normalize_facebook_url(post_url_raw)
        if not target_url.startswith(("http://", "https://")):
            return [], "无效的 Facebook 帖子链接"

        try:
            status, final_url, body = await self._get_html(self._rewrite_to_base(target_url))
        except Exception as exc:
            return [], f"网络请求失败: {exc!r}"

        err = classify_facebook_payload_error(status, final_url, body)
        if err:
            return [], err
        all_uids = re.findall(r'"USER_ID"\s*:\s*"(\d+)"', body)
        if all_uids and not any(u and u != "0" for u in all_uids):
            return [], "logged_out:Facebook 会话已失效 (USER_ID=0)"

        comments_by_id: Dict[str, Dict[str, Any]] = {}
        feedback_id = ""
        cursor = ""
        has_next = False

        for obj in extract_sjs_json_from_html(body, markers=('"Comment"', "comment_rendering_instance", "preferred_body", "feedback")):
            for c in extract_graphql_comments(obj):
                cid = c.get("comment_id")
                if cid and cid not in comments_by_id:
                    comments_by_id[cid] = c
            pg = extract_pagination_info(obj, kind="comments")
            if pg["feedback_id"] and not feedback_id:
                feedback_id = pg["feedback_id"]
            if pg["end_cursor"] and not cursor:
                cursor = pg["end_cursor"]
                has_next = bool(pg["has_next_page"])

        # Reels / Watch 视频页首屏 SSR 不内联评论，优先走动态学习的 FBUnifiedVideoFeedbackRightRail 协议接口
        m_vid = re.search(r"/(?:reel|reels|videos)/(\d{5,25})|[?&]v=(\d{5,25})", final_url or target_url, re.IGNORECASE)
        reel_id = (m_vid.group(1) or m_vid.group(2)) if m_vid else ""
        if not comments_by_id and reel_id:
            op_reel = get_slot_operation("reel_comments")
            if op_reel is None:
                await self._discover_doc_ids_from_html(body, ("reel_comments", "post_comments"))
                op_reel = get_slot_operation("reel_comments")
            if op_reel is not None:
                r_fname = op_reel["friendly_name"]
                r_did = op_reel["doc_id"]
                r_tpl = dict(op_reel.get("variables_template") or {})
                r_vars = dict(r_tpl)
                r_vars.update({
                    "feedLocation": r_tpl.get("feedLocation") or "COMET_MEDIA_VIEWER",
                    "feedbackSource": r_tpl.get("feedbackSource") if r_tpl.get("feedbackSource") is not None else 65,
                    "focusCommentID": None,
                    "initial_node_id": reel_id,
                    "scale": 1,
                })
                r_payloads, r_err = await self.gql_post(r_fname, r_did, r_vars, referer=final_url)
                if r_err and r_err.startswith(("logged_out:", "verification_required:", "rate_limit:")):
                    return [], r_err
                for pobj in r_payloads:
                    for c in extract_graphql_comments(pobj):
                        cid = c.get("comment_id")
                        if cid and cid not in comments_by_id:
                            comments_by_id[cid] = c
                    pg_r = extract_pagination_info(pobj, kind="comments")
                    if pg_r["feedback_id"]:
                        feedback_id = pg_r["feedback_id"]
                    if pg_r["end_cursor"]:
                        cursor = pg_r["end_cursor"]
                        has_next = bool(pg_r["has_next_page"])

        pages_limit = max(1, min(int(max_pages or 4), 15))
        if max_count and len(comments_by_id) >= max_count:
            out = list(comments_by_id.values())
            if not include_replies:
                out = [c for c in out if not c.get("reply_to")]
            return out[:max_count], ""

        if feedback_id and ((pages_limit > 1 and has_next and cursor) or not comments_by_id):
            op = get_slot_operation("post_comments")
            if op is None:
                await self._discover_doc_ids_from_html(body, ("post_comments",))
                op = get_slot_operation("post_comments")
            if op is not None:
                fname = op["friendly_name"]
                did = op["doc_id"]
                tpl = dict(op.get("variables_template") or {})
                page_rounds = max(1, pages_limit if not comments_by_id else pages_limit - 1)
                for _ in range(page_rounds):
                    if comments_by_id and (not cursor or not has_next):
                        break
                    if max_count and len(comments_by_id) >= max_count:
                        break
                    await asyncio.sleep(random.uniform(0.5, 1.0))
                    merged_vars = dict(tpl)
                    merged_vars.update({
                        "commentsAfterCount": -1,
                        "commentsAfterCursor": cursor or None,
                        "commentsBeforeCount": None,
                        "commentsBeforeCursor": None,
                        "commentsIntentToken": tpl.get("commentsIntentToken", None),
                        "feedLocation": tpl.get("feedLocation") or "POST_PERMALINK_DIALOG",
                        "focusCommentID": None,
                        "id": feedback_id,
                        "scale": 1,
                        "targetDialect": None,
                        "useDefaultActor": False,
                    })
                    if "CommentsListComponentsPagination" not in fname and "Depth1CommentsListPagination" not in fname:
                        merged_vars["feedbackID"] = feedback_id
                    payloads, g_err = await self.gql_post(fname, did, merged_vars, referer=final_url)
                    if g_err:
                        if g_err.startswith(("logged_out:", "verification_required:", "rate_limit:")):
                            out_err = list(comments_by_id.values())
                            if not include_replies:
                                out_err = [c for c in out_err if not c.get("reply_to")]
                            return out_err[:max_count] if max_count else out_err, g_err
                        break
                    added = 0
                    next_cursor = ""
                    next_has = False
                    for pobj in payloads:
                        for c in extract_graphql_comments(pobj):
                            cid = c.get("comment_id")
                            if cid and cid not in comments_by_id:
                                comments_by_id[cid] = c
                                added += 1
                        pg2 = extract_pagination_info(pobj, kind="comments")
                        if pg2["end_cursor"]:
                            next_cursor = pg2["end_cursor"]
                            next_has = bool(pg2["has_next_page"])
                    if not next_cursor or next_cursor == cursor or added == 0:
                        break
                    cursor, has_next = next_cursor, next_has

        out = list(comments_by_id.values())
        if not include_replies:
            out = [c for c in out if not c.get("reply_to")]
        if max_count:
            out = out[:max_count]
        if not out:
            return [], "api_empty_comments"
        return out, ""

    async def fetch_all_creator_works(
        self,
        user_ref: str,
        *,
        max_pages: int = 6,
        max_items: int = 60,
    ) -> Tuple[List[Dict[str, Any]], str]:
        """纯 HTTP 协议同步本账号/指定主页作品并归一为 `AccountWork` 字典列表。"""
        from .extract import parse_facebook_creator_work

        raw_posts, _author, err = await self.fetch_user_posts(
            user_ref,
            max_pages=max_pages,
            max_items=max_items,
        )
        if err and not raw_posts:
            return [], err
        works = [w for w in (parse_facebook_creator_work(r) for r in raw_posts) if w]
        if not works:
            return [], err or "api_empty_timeline"
        return works, ""
