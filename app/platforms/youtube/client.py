"""YouTube 异步协议客户端（SSR `ytInitialData` + Innertube Web 分页协议）。

架构设计：
1. **首屏 SSR 直取**：使用账号 `.youtube.com` / `.google.com` Cookie Jar + 与浏览器 UA 对齐的 `curl_cffi` TLS 指纹直连目标页，
   直接解析服务端注入的 `ytInitialData`、`ytInitialPlayerResponse` 与 `ytcfg`；
2. **Innertube Web 分页**：复用首屏动态下发的 `INNERTUBE_API_KEY`、`INNERTUBE_CONTEXT`、`VISITOR_DATA` 与 `SAPISIDHASH`
   认证头，调用 `/youtubei/v1/search`、`/youtubei/v1/browse`、`/youtubei/v1/next` 与 `/youtubei/v1/account/account_menu`
   实现频道视频/Shorts、关键词搜索、评论区与多频道列表高速翻页；
3. **透明容错与边界**：仅用于读采集（资料体检、频道切换列表、关键词搜索、频道视频/Shorts、视频详情、评论与订阅列表）；
   `hybrid` 模式下遇到页面结构变更或登录态校验不足时无缝回退账号隔离浏览器；
   所有写操作（Studio 上传发布、评论/回复、点赞/点踩、订阅/取关、观看/养号）均由账号隔离浏览器承载。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import random
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Tuple

from curl_cffi.requests import AsyncSession

from ...netfp import curl_proxies, impersonate_for_ua
from .extract import (
    extract_account_menu_self,
    extract_channel_profile,
    extract_channel_videos,
    extract_comments,
    extract_managed_channels,
    extract_search_channels,
    extract_search_videos,
    extract_subscriptions,
    extract_video_detail,
    extract_yt_initial_data,
    extract_yt_initial_player_response,
    extract_ytcfg,
    parse_youtube_aweme,
    parse_youtube_creator_work,
    parse_youtube_user,
)
from .protocol import (
    DEFAULT_YOUTUBE_UA,
    build_innertube_context,
    build_youtube_web_headers,
    compute_sapisidhash,
    cookie_map_from_state,
    has_youtube_login_cookies,
)
from .resolve import resolve_youtube_channel_target, resolve_youtube_video_ref
from .urls import (
    ACCOUNT_URL,
    CHANNEL_SWITCHER_URL,
    HOME_URL,
    INNERTUBE_ACCOUNT_MENU_URL,
    INNERTUBE_BROWSE_URL,
    INNERTUBE_NEXT_URL,
    INNERTUBE_SEARCH_URL,
    SUBSCRIPTIONS_URL,
    YOUTUBE_BASE,
    channel_search_url,
    channel_url,
    search_url,
)

if TYPE_CHECKING:
    from ..douyin.extract import Aweme


class YouTubeApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 0, category: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.category = category


def classify_youtube_payload_error(status_code: int, final_url: str = "", body: str = "") -> str:
    """将 YouTube HTTP 状态码、重定向落点与页面文案映射为 CreatorHub 统一风控前缀。"""
    url_low = str(final_url or "").lower()
    body_low = str(body or "")[:8000].lower()

    if "youtube.com/oops" in url_low or "/disabled/" in url_low:
        return f"verification_required:YouTube 账号已被限制或停用({final_url})"
    if "accounts.google.com/signin/challenge" in url_low or "accounts.google.com/v3/signin/challenge" in url_low:
        return f"verification_required:Google 账号触发安全验证或二次校验({final_url})"
    if "accounts.google.com/cookiemismatch" in url_low:
        return "logged_out:YouTube Cookie 状态不匹配或已过期，请重新登录"
    if "accounts.google.com" in url_low and ("servicelogin" in url_low or "/signin" in url_low):
        return "logged_out:YouTube / Google 登录态已失效，请重新登录"
    if "consent.youtube.com" in url_low or "consent.google.com" in url_low:
        return "verification_required:当前代理地区触发 YouTube Consent 页面，请使用浏览器完成一次确认"
    if status_code == 401:
        return "logged_out:YouTube 会话未认证(http_401)"
    if status_code == 429 or "too many requests" in body_low or "unusual traffic from your computer network" in body_low:
        return f"rate_limit:YouTube 请求触发限流或异常流量检测(http_{status_code or 429})"
    if status_code == 404:
        return "not_found:目标 YouTube 频道或视频不存在"
    if status_code >= 400:
        return f"http_{status_code}"
    return ""


class YouTubeClient:
    """YouTube SSR + Innertube 轻量异步读客户端。"""

    def __init__(
        self,
        storage_state: str | Mapping[str, Any] | None = "",
        user_agent: str = "",
        *,
        proxy: str = "",
        timeout: float = 20.0,
        locale: str = "en-US",
    ):
        self.cookies: Dict[str, str] = cookie_map_from_state(storage_state)
        self.cookies.setdefault("SOCS", "CAI")
        self.ua = (user_agent or DEFAULT_YOUTUBE_UA).strip()
        self.proxy = (proxy or "").strip()
        self.timeout = max(5.0, float(timeout or 20.0))
        self.locale = (locale or "en-US").strip()
        self._accept_lang = f"{self.locale},en;q=0.9" if not self.locale.startswith("en") else "en-US,en;q=0.9"
        self._session: Optional[AsyncSession] = None
        self._api_key: str = ""
        self._client_version: str = "2.20250115.01.00"
        self._visitor_data: str = ""
        self._hl: str = "en"
        self._gl: str = "US"

    def _build_session(self, *, anonymous: bool = False) -> AsyncSession:
        proxies = curl_proxies(self.proxy) or None
        sess = AsyncSession(
            timeout=self.timeout,
            allow_redirects=True,
            impersonate=impersonate_for_ua(self.ua),
            proxies=proxies,
            trust_env=False,
        )
        # 将干净的 .youtube.com Cookie 仅注入 session cookie jar，避免同时拼手动 Cookie 头导致重复/冲突引发 CookieMismatch
        if anonymous:
            sess.cookies.set("SOCS", "CAI", domain=".youtube.com", path="/")
        else:
            for k, v in (self.cookies or {}).items():
                if k and v:
                    try:
                        sess.cookies.set(str(k), str(v), domain=".youtube.com", path="/")
                    except Exception:
                        pass
            sess.cookies.set("SOCS", str(self.cookies.get("SOCS") or "CAI"), domain=".youtube.com", path="/")
        return sess

    @asynccontextmanager
    async def session_scope(self):
        sess = self._build_session()
        self._session = sess
        try:
            yield self
        finally:
            self._session = None
            try:
                await sess.close()
            except Exception:
                pass

    def _update_ytcfg(self, html_text: str) -> Dict[str, Any]:
        cfg = extract_ytcfg(html_text)
        if not cfg:
            return {}
        if cfg.get("INNERTUBE_API_KEY"):
            self._api_key = str(cfg["INNERTUBE_API_KEY"])
        if cfg.get("INNERTUBE_CLIENT_VERSION"):
            self._client_version = str(cfg["INNERTUBE_CLIENT_VERSION"])
        if cfg.get("VISITOR_DATA"):
            self._visitor_data = str(cfg["VISITOR_DATA"])
        ctx_client = ((cfg.get("INNERTUBE_CONTEXT") or {}).get("client")) or {}
        if isinstance(ctx_client, Mapping):
            if ctx_client.get("hl"):
                self._hl = str(ctx_client["hl"])
            if ctx_client.get("gl"):
                self._gl = str(ctx_client["gl"])
            if ctx_client.get("visitorData") and not self._visitor_data:
                self._visitor_data = str(ctx_client["visitorData"])
        return cfg

    def _merge_response_cookies(self, resp: Any) -> None:
        from .protocol import _is_forbidden_yt_cookie_name
        try:
            rc = getattr(resp, "cookies", None)
            if rc:
                for k, v in dict(rc).items():
                    ks = str(k or "").strip()
                    vs = str(v or "").strip()
                    if ks and vs and not _is_forbidden_yt_cookie_name(ks):
                        self.cookies[ks] = vs
        except Exception:
            pass

    async def _get_html(self, url: str, *, referer: str = HOME_URL, allow_anonymous_fallback: bool = False) -> Tuple[str, str, str]:
        """GET YouTube HTML 页面，返回 `(html, final_url, error)`。"""
        headers = build_youtube_web_headers(
            self.cookies,
            user_agent=self.ua,
            referer=referer,
            accept_language=self._accept_lang,
            include_cookie=False,
        )
        own = self._session is None
        sess = self._session or self._build_session()
        try:
            resp = await sess.get(url, headers=headers, timeout=self.timeout)
            self._merge_response_cookies(resp)
            final_url = str(getattr(resp, "url", url) or url)
            text = resp.text or ""
            err = classify_youtube_payload_error(resp.status_code, final_url, text)
            if err and allow_anonymous_fallback and err.startswith("logged_out:") and self.cookies:
                # 公开读接口遇到账号 Cookie 过期/CookieMismatch 时，自动用干净匿名 SOCS=CAI 会话重试，不影响公开采集与评论抓取
                anon_sess = self._build_session(anonymous=True)
                try:
                    r2 = await anon_sess.get(url, headers=headers, timeout=self.timeout)
                    f2 = str(getattr(r2, "url", url) or url)
                    t2 = r2.text or ""
                    e2 = classify_youtube_payload_error(r2.status_code, f2, t2)
                    if not e2 and t2:
                        self._update_ytcfg(t2)
                        return t2, f2, ""
                except Exception:
                    pass
                finally:
                    try:
                        await anon_sess.close()
                    except Exception:
                        pass
            if err:
                return "", final_url, err
            self._update_ytcfg(text)
            return text, final_url, ""
        except Exception as exc:
            return "", url, f"network_error:{exc}"
        finally:
            if own:
                try:
                    await sess.close()
                except Exception:
                    pass

    async def _post_innertube(
        self,
        endpoint_url: str,
        body: Mapping[str, Any],
        *,
        referer: str = HOME_URL,
        require_auth: bool = False,
    ) -> Tuple[Dict[str, Any], str]:
        """调用 YouTube Web `/youtubei/v1/*` 接口。"""
        url = f"{endpoint_url}?key={self._api_key}" if self._api_key else endpoint_url
        payload = dict(body)
        if "context" not in payload:
            payload["context"] = build_innertube_context(
                client_version=self._client_version,
                hl=self._hl,
                gl=self._gl,
                visitor_data=self._visitor_data,
            )

        headers: Dict[str, str] = {
            "User-Agent": self.ua,
            "Accept": "*/*",
            "Accept-Language": self._accept_lang,
            "Content-Type": "application/json",
            "Origin": YOUTUBE_BASE,
            "Referer": referer or HOME_URL,
            "X-YouTube-Client-Name": "1",
            "X-YouTube-Client-Version": self._client_version,
            "X-Origin": YOUTUBE_BASE,
        }
        if self._visitor_data:
            headers["X-Goog-Visitor-Id"] = self._visitor_data
        auth = compute_sapisidhash(self.cookies, origin=YOUTUBE_BASE)
        if auth:
            headers["Authorization"] = auth
            headers["X-Goog-AuthUser"] = "0"
        elif require_auth:
            return {}, "logged_out:缺少 SAPISID 凭证，无法执行已登录 Innertube 校验"

        own = self._session is None
        sess = self._session or self._build_session()
        try:
            resp = await sess.post(
                url,
                headers=headers,
                data=json.dumps(payload, ensure_ascii=False),
                timeout=self.timeout,
            )
            self._merge_response_cookies(resp)
            text = resp.text or ""
            err = classify_youtube_payload_error(resp.status_code, str(getattr(resp, "url", url)), text)
            if err and not require_auth and err.startswith("logged_out:") and self.cookies:
                anon_hdrs = {k: v for k, v in headers.items() if k not in {"Authorization", "X-Goog-AuthUser"}}
                anon_sess = self._build_session(anonymous=True)
                try:
                    r2 = await anon_sess.post(
                        url,
                        headers=anon_hdrs,
                        data=json.dumps(payload, ensure_ascii=False),
                        timeout=self.timeout,
                    )
                    t2 = r2.text or ""
                    e2 = classify_youtube_payload_error(r2.status_code, str(getattr(r2, "url", url)), t2)
                    if not e2 and t2:
                        text, err = t2, ""
                except Exception:
                    pass
                finally:
                    try:
                        await anon_sess.close()
                    except Exception:
                        pass
            if err:
                return {}, err
            try:
                data = json.loads(text)
            except Exception:
                return {}, "invalid_json:YouTube Innertube 返回非 JSON 数据"
            if isinstance(data, dict) and isinstance(data.get("error"), Mapping):
                msg = str(data["error"].get("message") or data["error"].get("status") or "innertube_error")
                code = int(data["error"].get("code") or 0)
                if code == 401:
                    return {}, f"logged_out:{msg}"
                if code == 429:
                    return {}, f"rate_limit:{msg}"
                return {}, f"api_error:{msg}"
            return data if isinstance(data, dict) else {}, ""
        except Exception as exc:
            return {}, f"network_error:{exc}"
        finally:
            if own:
                try:
                    await sess.close()
                except Exception:
                    pass

    async def _ensure_ytcfg(self) -> str:
        if self._api_key:
            return ""
        _, _, err = await self._get_html(HOME_URL)
        return err

    async def fetch_self_profile(self) -> Tuple[Optional[Dict[str, Any]], str]:
        """校验 YouTube 登录态并读取当前账号/频道资料、订阅数与可选频道列表。"""
        if not has_youtube_login_cookies(self.cookies):
            return None, "logged_out:缺少 YouTube / Google 登录态 Cookie"

        # 1) 访问首页或 account 页刷新 ytcfg 与校验是否被踢回登录页
        html_text, final_url, err = await self._get_html(ACCOUNT_URL, referer=HOME_URL)
        if err:
            return None, err

        cfg = extract_ytcfg(html_text)
        logged_in_flag = cfg.get("LOGGED_IN")
        if logged_in_flag is False:
            return None, "logged_out:YouTube 页面返回未登录态(LOGGED_IN=false)"

        profile: Dict[str, Any] = {}
        # 2) 调用 account_menu 获取最准确的当前账号昵称、@handle、头像与 UC 频道 ID
        menu_data, menu_err = await self._post_innertube(
            INNERTUBE_ACCOUNT_MENU_URL,
            {"deviceTheme": "DEVICE_THEME_SUPPORTED"},
            referer=ACCOUNT_URL,
            require_auth=False,
        )
        if menu_data:
            profile = extract_account_menu_self(menu_data)

        # 3) 补充 channel_switcher 获取多频道列表与默认选中频道
        channels, _ = await self.fetch_managed_channels()
        if channels:
            selected = next((c for c in channels if c.get("is_selected")), channels[0])
            if not profile.get("nickname"):
                profile["nickname"] = selected.get("name") or ""
            if not profile.get("unique_id") and selected.get("handle"):
                profile["unique_id"] = selected["handle"]
            if not profile.get("uid") and selected.get("channel_id"):
                profile["uid"] = selected["channel_id"]
            if not profile.get("avatar") and selected.get("avatar"):
                profile["avatar"] = selected["avatar"]
            if selected.get("subscribers_count"):
                profile["follower_count"] = max(int(profile.get("follower_count") or 0), int(selected["subscribers_count"]))
            profile["channels"] = channels

        # 4) 若拿到了 @handle 或 UC 频道 ID，再顺手拉一次频道主页 SSR 补齐订阅数与作品数
        ch_ref = str(profile.get("unique_id") or profile.get("uid") or "").strip()
        if ch_ref:
            ch_prof, _ = await self.fetch_channel_profile(ch_ref)
            if ch_prof:
                for k in ("uid", "sec_uid", "unique_id", "douyin_id", "nickname", "avatar", "signature"):
                    if ch_prof.get(k) and not profile.get(k):
                        profile[k] = ch_prof[k]
                profile["follower_count"] = max(int(profile.get("follower_count") or 0), int(ch_prof.get("follower_count") or 0))
                profile["aweme_count"] = max(int(profile.get("aweme_count") or 0), int(ch_prof.get("aweme_count") or 0))

        # 5) 若尚未拿到任何身份字段且 logged_in_flag 未明确为 True，判定会话失效
        if not (profile.get("nickname") or profile.get("unique_id") or profile.get("uid")):
            if logged_in_flag is True:
                return None, "profile_incomplete:已登录但暂未解析到频道资料"
            return None, menu_err or "logged_out:无法读取 YouTube 登录账号信息，会话可能已失效"

        return parse_youtube_user(profile), ""

    async def fetch_managed_channels(self) -> Tuple[List[Dict[str, Any]], str]:
        """从 `/channel_switcher` 拉取当前 Google 账号可切换的全部 YouTube 频道。"""
        html_text, _, err = await self._get_html(CHANNEL_SWITCHER_URL, referer=ACCOUNT_URL)
        if err:
            return [], err
        initial = extract_yt_initial_data(html_text)
        channels = extract_managed_channels(initial)
        return channels, ("" if channels else "empty")

    async def fetch_channel_profile(self, channel_ref: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """拉取任意 YouTube 频道资料。"""
        key, url = resolve_youtube_channel_target(channel_ref)
        target_url = url or channel_url(channel_ref)
        html_text, _, err = await self._get_html(target_url, referer=HOME_URL, allow_anonymous_fallback=True)
        if err:
            return None, err
        initial = extract_yt_initial_data(html_text)
        prof = extract_channel_profile(initial)
        if not prof:
            return None, "empty"
        return parse_youtube_user(prof), ""

    async def fetch_channel_videos(
        self,
        channel_ref: str,
        *,
        tab: str = "videos",
        max_items: int = 30,
        max_pages: int = 5,
        include_shorts: bool = False,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any], str]:
        """拉取指定频道的视频列表（支持 Innertube `/browse` 续页与可选 Shorts 合并）。"""
        key, base_url = resolve_youtube_channel_target(channel_ref)
        if not (key or base_url):
            return [], {}, "empty"
        target_url = channel_url(key or base_url, tab=tab or "videos")
        html_text, _, err = await self._get_html(target_url, referer=HOME_URL, allow_anonymous_fallback=True)
        if err:
            return [], {}, err

        initial = extract_yt_initial_data(html_text)
        profile = extract_channel_profile(initial)
        items, cont = extract_channel_videos(
            initial,
            default_author=str(profile.get("nickname") or ""),
            default_channel_id=str(profile.get("uid") or ""),
            default_handle=str(profile.get("unique_id") or ""),
            default_avatar=str(profile.get("avatar") or ""),
        )
        seen = {row["video_id"] for row in items}

        pages = 1
        while cont and len(items) < max_items and pages < max(1, max_pages):
            await asyncio.sleep(random.uniform(0.35, 0.8))
            data, p_err = await self._post_innertube(
                INNERTUBE_BROWSE_URL,
                {"continuation": cont},
                referer=target_url,
            )
            if p_err or not data:
                break
            more, next_cont = extract_channel_videos(
                data,
                default_author=str(profile.get("nickname") or ""),
                default_channel_id=str(profile.get("uid") or ""),
                default_handle=str(profile.get("unique_id") or ""),
                default_avatar=str(profile.get("avatar") or ""),
            )
            added = 0
            for row in more:
                if row["video_id"] not in seen:
                    seen.add(row["video_id"])
                    items.append(row)
                    added += 1
            if not added or not next_cont or next_cont == cont:
                break
            cont = next_cont
            pages += 1

        # 可选追加抓取 Shorts 标签页，补全创作者短视频
        if include_shorts and tab == "videos" and len(items) < max_items:
            shorts_u = channel_url(key or base_url or channel_ref, tab="shorts")
            s_html, _, s_err = await self._get_html(shorts_u, referer=target_url, allow_anonymous_fallback=True)
            if not s_err and s_html:
                s_init = extract_yt_initial_data(s_html)
                s_items, _ = extract_channel_videos(
                    s_init,
                    default_author=str(profile.get("nickname") or ""),
                    default_channel_id=str(profile.get("uid") or ""),
                    default_handle=str(profile.get("unique_id") or ""),
                    default_avatar=str(profile.get("avatar") or ""),
                )
                for row in s_items:
                    if row["video_id"] not in seen:
                        seen.add(row["video_id"])
                        items.append(row)
                        if len(items) >= max_items:
                            break

        return items[:max_items], profile, ("" if items else "empty")

    async def search_videos(
        self,
        keyword: str,
        *,
        max_items: int = 30,
        max_pages: int = 4,
        upload_date: str = "",
        duration: str = "",
        sort_by: str = "",
        search_type: str = "video",
    ) -> Tuple[List[Dict[str, Any]], str]:
        """按关键词搜索 YouTube 视频 / Shorts（支持上传时间/时长/排序过滤与 Innertube `/search` 翻页）。"""
        kw = (keyword or "").strip()
        if not kw:
            return [], "empty_keyword"
        target_url = search_url(
            kw,
            search_type=search_type,
            upload_date=upload_date,
            duration=duration,
            sort_by=sort_by,
        )
        html_text, _, err = await self._get_html(target_url, referer=HOME_URL, allow_anonymous_fallback=True)
        if err:
            return [], err
        initial = extract_yt_initial_data(html_text)
        items, cont = extract_search_videos(initial)
        seen = {row["video_id"] for row in items}

        pages = 1
        while cont and len(items) < max_items and pages < max(1, max_pages):
            await asyncio.sleep(random.uniform(0.4, 0.85))
            data, p_err = await self._post_innertube(
                INNERTUBE_SEARCH_URL,
                {"continuation": cont},
                referer=target_url,
            )
            if p_err or not data:
                break
            more, next_cont = extract_search_videos(data)
            added = 0
            for row in more:
                if row["video_id"] not in seen:
                    seen.add(row["video_id"])
                    items.append(row)
                    added += 1
            if not added or not next_cont or next_cont == cont:
                break
            cont = next_cont
            pages += 1

        return items[:max_items], ("" if items else "empty")

    async def search_channels(
        self,
        keyword: str,
        *,
        max_items: int = 30,
        max_pages: int = 3,
    ) -> Tuple[List[Dict[str, Any]], str]:
        """按关键词搜索 YouTube 频道（可用于自动订阅前候选采集）。"""
        kw = (keyword or "").strip()
        if not kw:
            return [], "empty_keyword"
        target_url = channel_search_url(kw)
        html_text, _, err = await self._get_html(target_url, referer=HOME_URL, allow_anonymous_fallback=True)
        if err:
            return [], err
        initial = extract_yt_initial_data(html_text)
        items, cont = extract_search_channels(initial)
        seen = {row["sec_uid"] for row in items}

        pages = 1
        while cont and len(items) < max_items and pages < max(1, max_pages):
            await asyncio.sleep(random.uniform(0.35, 0.75))
            data, p_err = await self._post_innertube(
                INNERTUBE_SEARCH_URL,
                {"continuation": cont},
                referer=target_url,
            )
            if p_err or not data:
                break
            more, next_cont = extract_search_channels(data)
            added = 0
            for row in more:
                if row["sec_uid"] not in seen:
                    seen.add(row["sec_uid"])
                    items.append(row)
                    added += 1
            if not added or not next_cont or next_cont == cont:
                break
            cont = next_cont
            pages += 1

        return items[:max_items], ("" if items else "empty")

    async def fetch_video_detail(
        self,
        video_ref: str,
        *,
        quality: str = "highest",
    ) -> Tuple[Optional["Aweme"], Optional[Dict[str, Any]], str]:
        """抓取单条 YouTube 视频 / Shorts 完整元数据，返回 `(Aweme, raw_dict, error)`。"""
        ref = resolve_youtube_video_ref(video_ref, allow_bare_id=True)
        if ref is None:
            return None, None, "invalid_video_ref:请输入有效的 YouTube 视频链接或 11 位视频 ID"

        html_text, _, err = await self._get_html(ref.watch_url, referer=HOME_URL, allow_anonymous_fallback=True)
        if err:
            return None, None, err
        initial = extract_yt_initial_data(html_text)
        player = extract_yt_initial_player_response(html_text)

        # 检查 playabilityStatus（私密/已删除/地区不可用）
        ps = player.get("playabilityStatus") or {}
        if isinstance(ps, Mapping):
            st = str(ps.get("status") or "").upper()
            reason = str(ps.get("reason") or "")
            if st == "LOGIN_REQUIRED":
                return None, None, f"logged_out:该视频需要登录查看({reason})"
            if st in {"ERROR", "UNPLAYABLE"} and not (player.get("videoDetails") or {}).get("videoId"):
                return None, None, f"not_found:{reason or '视频不可播放或已删除'}"

        detail, comments_token = extract_video_detail(initial, player, source_url=ref.canonical_url)
        if detail is None:
            return None, None, "empty"
        detail["comments_continuation"] = comments_token
        aweme = parse_youtube_aweme(detail, quality=quality)
        return aweme, detail, ""

    async def fetch_video_comments(
        self,
        video_ref: str,
        *,
        max_items: int = 100,
        max_pages: int = 5,
    ) -> Tuple[List[Dict[str, Any]], str]:
        """抓取单条 YouTube 视频评论（先从 watch 页拿 comments continuation，再调用 `/youtubei/v1/next` 分页）。"""
        ref = resolve_youtube_video_ref(video_ref, allow_bare_id=True)
        if ref is None:
            return [], "invalid_video_ref:请输入有效的 YouTube 视频链接或 11 位视频 ID"

        _, detail, err = await self.fetch_video_detail(ref.watch_url)
        if err and not detail:
            return [], err
        cont = str((detail or {}).get("comments_continuation") or "").strip()
        if not cont:
            return [], "empty"

        comments: List[Dict[str, Any]] = []
        seen: set[str] = set()
        pages = 0
        while cont and len(comments) < max_items and pages < max(1, max_pages):
            if pages > 0:
                await asyncio.sleep(random.uniform(0.35, 0.8))
            data, c_err = await self._post_innertube(
                INNERTUBE_NEXT_URL,
                {"continuation": cont},
                referer=ref.watch_url,
            )
            if c_err:
                if comments:
                    break
                return [], c_err
            batch, next_cont = extract_comments(data, video_id=ref.video_id)
            added = 0
            for row in batch:
                if row["comment_id"] not in seen:
                    seen.add(row["comment_id"])
                    comments.append(row)
                    added += 1
            if not added or not next_cont or next_cont == cont:
                break
            cont = next_cont
            pages += 1

        return comments[:max_items], ("" if comments else "empty")

    async def fetch_subscriptions(self, *, max_items: int = 200, max_pages: int = 5) -> Tuple[List[Dict[str, Any]], str]:
        """拉取当前登录账号的已订阅频道列表 (`/feed/channels`)。"""
        if not has_youtube_login_cookies(self.cookies):
            return [], "logged_out:缺少 YouTube 登录态，无法读取订阅列表"
        html_text, _, err = await self._get_html(SUBSCRIPTIONS_URL, referer=HOME_URL)
        if err:
            return [], err
        initial = extract_yt_initial_data(html_text)
        items, cont = extract_subscriptions(initial)
        seen = {row["sec_uid"] for row in items}

        pages = 1
        while cont and len(items) < max_items and pages < max(1, max_pages):
            await asyncio.sleep(random.uniform(0.35, 0.75))
            data, p_err = await self._post_innertube(
                INNERTUBE_BROWSE_URL,
                {"continuation": cont},
                referer=SUBSCRIPTIONS_URL,
            )
            if p_err or not data:
                break
            more, next_cont = extract_subscriptions(data)
            added = 0
            for row in more:
                if row["sec_uid"] not in seen:
                    seen.add(row["sec_uid"])
                    items.append(row)
                    added += 1
            if not added or not next_cont or next_cont == cont:
                break
            cont = next_cont
            pages += 1

        return items[:max_items], ("" if items else "empty")

    async def fetch_all_creator_works(
        self,
        channel_ref: str,
        *,
        max_pages: int = 6,
        max_items: int = 120,
        include_shorts: bool = True,
    ) -> Tuple[List[Dict[str, Any]], str]:
        """抓取本账号频道的视频 + Shorts 作品并标准化为 AccountWork 字典列表。"""
        raw_items, _author, err = await self.fetch_channel_videos(
            channel_ref,
            tab="videos",
            max_items=max_items,
            max_pages=max_pages,
            include_shorts=include_shorts,
        )
        works = [w for w in (parse_youtube_creator_work(x) for x in raw_items) if w]
        if works:
            return works, ""
        return [], err or "empty"
