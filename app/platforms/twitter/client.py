"""X (Twitter) 异步协议客户端（动态 GraphQL Registry + Syndication + Web REST）。

架构概览：
- 动态 GraphQL 读取：自动从 `https://x.com` 首页发现当前版本 `main.*.js` 中的最新 `queryId` 与
  `featureSwitches`（内存缓存 30 分钟，内置静态兜底），直连读取：
  * 关键词推文搜索：优先尝试 `SearchTimeline`，当账号/出口遇到 X 网关 `SearchTimeline` 404 限制时，
    自动无缝切换至同源 `GlobalCommunitiesPostSearchTimeline`（综合）/ `GlobalCommunitiesLatestPostSearchTimeline`（最新）分页读取；
  * 创作者/本账号推文时间线：`UserOriginalsTimeline` / `UserTweets` 分页读取；
  * 推文详情与完整评论线程：`TweetDetail`（失败时对单帖媒体自动回退免登录 `cdn.syndication.twimg.com/tweet-result`）；
  * 账号资料与健康探测：`UserByScreenName` / `UserByRestId`；
  * 私信收件箱与历史消息：REST v1.1 `dm/inbox_initial_state.json` 与 `dm/conversation/{id}.json`。
- 重度写操作（发推、点赞、转推/引用、关注/取关、发私信、发评论）及协议兼容回退统一由账号隔离浏览器承载。
"""
from __future__ import annotations

from contextlib import asynccontextmanager
import json
import re
import secrets
import time
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlencode

from curl_cffi.requests import AsyncSession

from ...netfp import impersonate_for_ua
from .extract import (
    Aweme,
    extract_graphql_comments,
    extract_graphql_tweets,
    parse_twitter_aweme,
    parse_twitter_dm_inbox,
    parse_twitter_dm_messages,
    parse_twitter_self_user,
    parse_twitter_user,
)
from .protocol import (
    DEFAULT_TWITTER_UA,
    REST_DM_CONVERSATION_TMPL,
    REST_DM_INBOX_URL,
    REST_SETTINGS_URL,
    REST_USERS_SHOW_URL,
    REST_VERIFY_CREDENTIALS_URL,
    SYNDICATION_TWEET_URL,
    build_twitter_web_headers,
    cookie_map_from_state,
    extract_twid_uid,
    syndication_token,
)
from .resolve import (
    resolve_twitter_short_url,
    resolve_twitter_tweet_ref,
    resolve_twitter_user_target,
)


def _normalize_proxy_url(proxy: str) -> str:
    raw = str(proxy or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        return f"http://{raw}"
    return raw


class TwitterApiError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 0, category: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.category = category


def classify_twitter_payload_error(status_code: int, payload: Any, fallback_text: str = "") -> str:
    """将 X HTTP 状态码与 `errors[]` 结构映射为 CreatorHub 统一风控前缀。"""
    codes: List[int] = []
    messages: List[str] = []
    if isinstance(payload, Mapping):
        errs = payload.get("errors")
        if isinstance(errs, list):
            for e in errs:
                if isinstance(e, Mapping):
                    if e.get("code") is not None:
                        try:
                            codes.append(int(e["code"]))
                        except (TypeError, ValueError):
                            pass
                    if e.get("message"):
                        messages.append(str(e["message"]).strip())
        elif isinstance(payload.get("error"), str):
            messages.append(str(payload["error"]).strip())

    msg = "; ".join(m for m in messages if m) or fallback_text[:180].strip()
    low = msg.lower()

    # Twitter 经典错误码：32/89/99/215 登录失效；64 封禁；326 锁定需验证；88/429 频控
    if status_code == 401 or any(c in {32, 89, 99, 215} for c in codes) or "could not authenticate" in low:
        return f"logged_out:X (Twitter) 登录态已失效({msg or status_code})"
    if 326 in codes or "locked" in low or "account_access" in low or "arkose" in low or "bounce" in low:
        return f"verification_required:X 账号触发安全锁定或挑战验证({msg or 'code_326'})"
    if 64 in codes or "suspended" in low:
        return f"verification_required:X 账号已被冻结/封禁({msg or 'code_64'})"
    if status_code == 429 or 88 in codes or "rate limit" in low or "too many requests" in low:
        return f"rate_limit:X 接口触发频率限制({msg or status_code})"
    if status_code == 404 or any(c in {34, 50, 63, 144} for c in codes) or "not found" in low:
        return f"not_found:{msg or '目标推文或用户不存在'}"
    if status_code == 403:
        return f"api_forbidden:{msg or 'http_403'}"
    return msg or f"http_{status_code}"


# ── 动态 GraphQL Operation 规格缓存（自动随 main.*.js 热更新，内置静态兜底） ──
_COMMON_TWEET_FEATURES: Dict[str, bool] = {
    "rweb_video_screen_enabled": False,
    "rweb_cashtags_enabled": True,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_profile_redirect_enabled": False,
    "rweb_tipjar_consumption_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "premium_content_api_read_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": True,
    "responsive_web_jetfuel_frame": True,
    "responsive_web_grok_share_attachment_enabled": True,
    "responsive_web_grok_annotations_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "responsive_web_grok_show_grok_translated_post": False,
    "responsive_web_grok_analysis_button_from_backend": True,
    "post_ctas_fetch_enabled": True,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_grok_image_annotation_enabled": True,
    "responsive_web_grok_imagine_annotation_enabled": True,
    "responsive_web_grok_community_note_auto_translation_is_enabled": False,
    "responsive_web_enhance_cards_enabled": False,
}

_COMMON_USER_FEATURES: Dict[str, bool] = {
    "hidden_profile_subscriptions_enabled": True,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_profile_redirect_enabled": False,
    "rweb_tipjar_consumption_enabled": True,
    "verified_phone_label_enabled": False,
    "subscriptions_verification_info_is_identity_verified_enabled": True,
    "subscriptions_verification_info_verified_since_enabled": True,
    "highlights_tweets_tab_ui_enabled": True,
    "responsive_web_twitter_article_notes_tab_enabled": True,
    "subscriptions_feature_can_gift_premium": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "responsive_web_graphql_timeline_navigation_enabled": True,
}

_FALLBACK_GQL_SPECS: Dict[str, Tuple[str, Dict[str, bool]]] = {
    "UserByScreenName": ("AMIBMjtxEEATh4z8V9GtRg", _COMMON_USER_FEATURES),
    "UserByRestId": ("mxaYWwt52eAb6OAzYHMVzg", _COMMON_USER_FEATURES),
    "UserOriginalsTimeline": ("ZFKu1DFS63hgrkLzR3Hsdw", _COMMON_TWEET_FEATURES),
    "UserTweets": ("D6z-FJI2pKuwEeYYxVIGbg", _COMMON_TWEET_FEATURES),
    "TweetDetail": ("dQDdKtt882nl-Km98FJGSQ", _COMMON_TWEET_FEATURES),
    "TweetResultByRestId": ("U8lc5aomF5krJzZ9vJ9wxg", _COMMON_TWEET_FEATURES),
    "SearchTimeline": ("vLHjqbDv7TPFMBkIIO1nOA", _COMMON_TWEET_FEATURES),
    "GlobalCommunitiesPostSearchTimeline": ("Tm8OZBTJ5F_dQNdP1KEoug", _COMMON_TWEET_FEATURES),
    "GlobalCommunitiesLatestPostSearchTimeline": ("60lEf-uOZXGMD_KQoO1WSw", _COMMON_TWEET_FEATURES),
}

_GQL_CACHE: Dict[str, Tuple[str, Dict[str, bool]]] = {}
_GQL_CACHE_AT: float = 0.0
_GQL_CACHE_TTL: float = 1800.0
_MAIN_JS_RE = re.compile(r"https://abs\.twimg\.com/responsive-web/client-web/main\.[0-9a-zA-Z_-]+\.js")
_GQL_SPEC_RE = re.compile(
    r'queryId:"([^"]+)",operationName:"([^"]+)",operationType:"query",metadata:\{featureSwitches:\[([^\]]*)\]'
)


class TwitterClient:
    """按账号会话与代理隔离的 X (Twitter) 异步轻量协议客户端。"""

    def __init__(
        self,
        storage_state: str | Mapping[str, Any] | None = None,
        user_agent: str = "",
        *,
        proxy: str = "",
        timeout: float = 25.0,
    ):
        self.cookies = cookie_map_from_state(storage_state)
        self.user_agent = (user_agent or DEFAULT_TWITTER_UA).strip()
        self.proxy = _normalize_proxy_url(proxy)
        self.timeout = max(5.0, float(timeout or 25.0))
        self.self_uid = extract_twid_uid(self.cookies)
        self._session: Optional[AsyncSession] = None
        self._session_bootstrapped = False

    @property
    def has_auth(self) -> bool:
        return bool(self.cookies.get("auth_token"))

    @asynccontextmanager
    async def session_scope(self):
        imp = impersonate_for_ua(self.user_agent)
        session = AsyncSession(
            impersonate=imp,
            proxies={"http": self.proxy, "https": self.proxy} if self.proxy else None,
            timeout=self.timeout,
        )
        self._session = session
        try:
            yield self
        finally:
            self._session = None
            try:
                await session.close()
            except Exception:
                pass

    async def _raw_get_text(self, url: str, *, with_cookies: bool = False) -> Tuple[int, str]:
        headers = {"User-Agent": self.user_agent, "Accept": "text/html,application/javascript,*/*"}
        own_session = self._session is None
        sess = self._session
        if own_session:
            sess = AsyncSession(
                impersonate=impersonate_for_ua(self.user_agent),
                proxies={"http": self.proxy, "https": self.proxy} if self.proxy else None,
                timeout=self.timeout,
            )
        assert sess is not None
        try:
            resp = await sess.get(
                url,
                headers=headers,
                cookies=(self.cookies if with_cookies else None),
            )
            if with_cookies:
                try:
                    for ck_name, ck_val in resp.cookies.items():
                        if ck_name and ck_val:
                            self.cookies[str(ck_name)] = str(ck_val)
                    if not self.self_uid:
                        self.self_uid = extract_twid_uid(self.cookies)
                except Exception:
                    pass
            return int(resp.status_code), (resp.text or "")
        except Exception:
            return 0, ""
        finally:
            if own_session:
                try:
                    await sess.close()
                except Exception:
                    pass

    async def _ensure_graphql_specs(self, *, force_refresh: bool = False) -> Dict[str, Tuple[str, Dict[str, bool]]]:
        global _GQL_CACHE, _GQL_CACHE_AT
        now = time.monotonic()
        if not force_refresh and _GQL_CACHE and (now - _GQL_CACHE_AT) < _GQL_CACHE_TTL:
            return _GQL_CACHE

        st, html = await self._raw_get_text("https://x.com/home", with_cookies=self.has_auth)
        if st == 200 and html:
            m_js = _MAIN_JS_RE.search(html)
            if m_js:
                st_js, js_text = await self._raw_get_text(m_js.group(0), with_cookies=False)
                if st_js == 200 and js_text:
                    discovered: Dict[str, Tuple[str, Dict[str, bool]]] = dict(_FALLBACK_GQL_SPECS)
                    for m in _GQL_SPEC_RE.finditer(js_text):
                        qid, op_name, raw_switches = m.group(1), m.group(2), m.group(3)
                        switches = re.findall(r'"([^"]+)"', raw_switches)
                        feats = {k: True for k in switches}
                        discovered[op_name] = (qid, feats)
                    _GQL_CACHE = discovered
                    _GQL_CACHE_AT = now
                    return _GQL_CACHE

        if not _GQL_CACHE:
            _GQL_CACHE = dict(_FALLBACK_GQL_SPECS)
            _GQL_CACHE_AT = now
        return _GQL_CACHE

    async def ensure_session_cookies(self) -> Dict[str, str]:
        """当仅提供 `auth_token` 而缺少 `ct0` / `twid` 时，自动向 `x.com/home` 换取完整会话 Cookie。"""
        if not self.has_auth:
            return self.cookies
        if (not self.cookies.get("ct0") or not self.self_uid) and not self._session_bootstrapped:
            self._session_bootstrapped = True
            if not _GQL_CACHE:
                await self._ensure_graphql_specs(force_refresh=True)
            else:
                await self._raw_get_text("https://x.com/home", with_cookies=True)
        if not self.cookies.get("ct0"):
            self.cookies["ct0"] = secrets.token_hex(16)
        if not self.self_uid:
            self.self_uid = extract_twid_uid(self.cookies)
        return self.cookies

    async def _get_json(
        self,
        url: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        with_auth: bool = True,
        referer: str = "https://x.com/",
    ) -> Tuple[int, Any, str]:
        full_url = f"{url}?{urlencode(params)}" if params else url
        if with_auth:
            if self.has_auth and not self.cookies.get("ct0"):
                await self.ensure_session_cookies()
            headers = build_twitter_web_headers(
                self.cookies, user_agent=self.user_agent, referer=referer
            )
        else:
            headers = {
                "User-Agent": self.user_agent,
                "Accept": "application/json,text/plain,*/*",
                "Origin": "https://platform.twitter.com",
                "Referer": "https://platform.twitter.com/",
            }

        own_session = self._session is None
        sess = self._session
        if own_session:
            imp = impersonate_for_ua(self.user_agent)
            sess = AsyncSession(
                impersonate=imp,
                proxies={"http": self.proxy, "https": self.proxy} if self.proxy else None,
                timeout=self.timeout,
            )
        assert sess is not None
        try:
            resp = await sess.get(full_url, headers=headers)
            status = int(resp.status_code)
            raw_text = resp.text or ""
            try:
                for ck_name, ck_val in resp.cookies.items():
                    if ck_name and ck_val:
                        self.cookies[str(ck_name)] = str(ck_val)
                if not self.self_uid:
                    self.self_uid = extract_twid_uid(self.cookies)
            except Exception:
                pass
        except Exception as exc:
            return 0, None, f"network_error:{type(exc).__name__}: {exc}"
        finally:
            if own_session:
                try:
                    await sess.close()
                except Exception:
                    pass

        payload: Any = None
        if raw_text.lstrip().startswith(("{", "[")):
            try:
                payload = json.loads(raw_text)
            except ValueError:
                payload = None

        if status >= 400:
            return status, payload, classify_twitter_payload_error(status, payload, raw_text)
        if isinstance(payload, Mapping) and payload.get("errors") and not payload.get("data"):
            return status, payload, classify_twitter_payload_error(status, payload, raw_text)
        return status, payload, ""

    async def _graphql_get(
        self,
        operation_name: str,
        variables: Mapping[str, Any],
        *,
        field_toggles: Optional[Mapping[str, Any]] = None,
        referer: str = "https://x.com/",
    ) -> Tuple[int, Any, str]:
        if not self.has_auth:
            return 401, None, "logged_out:缺少 X auth_token 登录态"
        specs = await self._ensure_graphql_specs()
        spec = specs.get(operation_name) or _FALLBACK_GQL_SPECS.get(operation_name)
        if not spec:
            return 0, None, f"graphql_unknown_op:{operation_name}"

        qid, feats = spec
        params: Dict[str, str] = {
            "variables": json.dumps(dict(variables), separators=(",", ":"), ensure_ascii=False),
            "features": json.dumps(feats, separators=(",", ":"), ensure_ascii=False),
        }
        if field_toggles:
            params["fieldToggles"] = json.dumps(dict(field_toggles), separators=(",", ":"), ensure_ascii=False)

        url = f"https://x.com/i/api/graphql/{qid}/{operation_name}"
        status, payload, err = await self._get_json(url, params=params, with_auth=True, referer=referer)
        # 若因 X 部署新前端包导致 queryId 轮换返回 404/400，强制刷新一次 main.*.js 重试
        if status in {400, 404} and payload is None and operation_name != "SearchTimeline":
            specs = await self._ensure_graphql_specs(force_refresh=True)
            new_spec = specs.get(operation_name)
            if new_spec and new_spec[0] != qid:
                new_qid, new_feats = new_spec
                params["features"] = json.dumps(new_feats, separators=(",", ":"), ensure_ascii=False)
                status, payload, err = await self._get_json(
                    f"https://x.com/i/api/graphql/{new_qid}/{operation_name}",
                    params=params,
                    with_auth=True,
                    referer=referer,
                )
        return status, payload, err

    async def fetch_tweet_syndication(
        self,
        tweet_or_url: str,
    ) -> Tuple[Optional[Aweme], Dict[str, Any], str]:
        """使用单帖详情接口（优先已登录 GraphQL `TweetDetail`，回退公开 `Syndication`）拉取推文与高清媒体直链。"""
        expanded = await resolve_twitter_short_url(
            tweet_or_url,
            user_agent=self.user_agent,
            proxy=self.proxy,
            timeout=min(12.0, self.timeout),
        )
        ref = resolve_twitter_tweet_ref(expanded, allow_numeric=True)
        if ref is None or not ref.tweet_id:
            return None, {}, "not_found:无法从输入解析推文 ID"

        if self.has_auth:
            raw_node, g_err = await self.fetch_tweet_detail(ref.tweet_id)
            if isinstance(raw_node, Mapping):
                aw_g = parse_twitter_aweme(raw_node)
                if aw_g is not None:
                    if ref.screen_name and not aw_g.author_unique_id:
                        aw_g.author_unique_id = ref.screen_name
                    return aw_g, dict(raw_node), ""
            elif g_err.startswith(("logged_out:", "verification_required:")):
                return None, {}, g_err

        token = syndication_token(ref.tweet_id)
        status, payload, err = await self._get_json(
            SYNDICATION_TWEET_URL,
            params={"id": ref.tweet_id, "lang": "en", "token": token},
            with_auth=False,
        )
        if not isinstance(payload, Mapping) or not (
            payload.get("id_str")
            or payload.get("text")
            or payload.get("mediaDetails")
            or payload.get("video")
            or payload.get("photos")
        ):
            return None, {}, err or f"syndication_empty:{status}"

        typename = str(payload.get("__typename") or "")
        if typename in {"TweetTombstone", "TweetUnavailable"}:
            return None, dict(payload), "not_found:推文不可见或已被删除"

        aw = parse_twitter_aweme(payload)
        if aw is not None and ref.screen_name and not aw.author_unique_id:
            aw.author_unique_id = ref.screen_name
        return aw, dict(payload), ""

    async def fetch_tweet_detail(self, tweet_or_url: str) -> Tuple[Optional[Dict[str, Any]], str]:
        """通过 GraphQL `TweetDetail` 读取目标推文原始节点。"""
        ref = resolve_twitter_tweet_ref(tweet_or_url, allow_numeric=True)
        tid = ref.tweet_id if ref else str(tweet_or_url or "").strip()
        if not tid:
            return None, "missing_tweet_id"
        vars_d = {
            "focalTweetId": tid,
            "with_rux_injections": False,
            "rankingMode": "Relevance",
            "includePromotedContent": True,
            "withCommunity": True,
            "withQuickPromoteEligibilityTweetFields": True,
            "withBirdwatchNotes": True,
            "withVoice": True,
        }
        _st, payload, err = await self._graphql_get(
            "TweetDetail",
            vars_d,
            referer=f"https://x.com/i/web/status/{tid}",
        )
        if not payload:
            return None, err or "empty"
        tweets, _ = extract_graphql_tweets(payload)
        for t in tweets:
            if str(t.get("rest_id") or "") == tid:
                return t, ""
        return (tweets[0], "") if tweets else (None, err or "empty")

    async def fetch_comments(
        self,
        tweet_or_url: str,
        *,
        max_count: int = 60,
        include_replies: bool = True,
    ) -> Tuple[List[Dict[str, Any]], str]:
        """通过 GraphQL `TweetDetail` 分页读取推文下的评论及嵌套回复。"""
        ref = resolve_twitter_tweet_ref(tweet_or_url, allow_numeric=True)
        tid = ref.tweet_id if ref else str(tweet_or_url or "").strip()
        if not tid:
            return [], "missing_tweet_id"
        collected: Dict[str, Dict[str, Any]] = {}
        cursor = ""
        last_err = ""
        max_pages = max(1, min(8, (max(1, int(max_count)) // 20) + 2))
        for _ in range(max_pages):
            vars_d: Dict[str, Any] = {
                "focalTweetId": tid,
                "with_rux_injections": False,
                "rankingMode": "Relevance",
                "includePromotedContent": True,
                "withCommunity": True,
                "withQuickPromoteEligibilityTweetFields": True,
                "withBirdwatchNotes": True,
                "withVoice": True,
            }
            if cursor:
                vars_d["cursor"] = cursor
                vars_d["referrer"] = "tweet"
            _st, payload, err = await self._graphql_get(
                "TweetDetail",
                vars_d,
                referer=f"https://x.com/i/web/status/{tid}",
            )
            if err and not payload:
                last_err = err
                break
            batch, next_cursor = extract_graphql_comments(
                payload, target_tweet_id=tid, include_replies=include_replies
            )
            before = len(collected)
            for c in batch:
                cid = str(c.get("comment_id") or "")
                if cid:
                    collected.setdefault(cid, c)
            if len(collected) >= max_count or not next_cursor or next_cursor == cursor or len(collected) == before:
                break
            cursor = next_cursor
        return list(collected.values())[:max_count], ("" if collected else last_err)

    async def search_posts(
        self,
        keyword: str,
        *,
        max_pages: int = 3,
        latest: bool = False,
        content_type: str = "all",
    ) -> Tuple[List[Dict[str, Any]], str]:
        """按关键词分页搜索推文。

        优先尝试 `SearchTimeline`；当账号/出口触发 X 网关 `SearchTimeline` 404 空包限制时，
        自动无缝回退至同源 `GlobalCommunitiesLatestPostSearchTimeline` / `GlobalCommunitiesPostSearchTimeline`。
        """
        kw = str(keyword or "").strip()
        if not kw:
            return [], "搜索关键词不能为空"
        if not self.has_auth:
            return [], "logged_out:缺少 X auth_token 登录态"

        ctype = str(content_type or "all").strip().lower()
        query_expr = kw
        if ctype == "video" and "filter:" not in kw:
            query_expr = f"{kw} filter:videos"
        elif ctype == "images" and "filter:" not in kw:
            query_expr = f"{kw} filter:images"

        collected: Dict[str, Dict[str, Any]] = {}
        pages = max(1, min(15, int(max_pages or 3)))

        # 1) 尝试标准 SearchTimeline
        product = "Latest" if latest else ("Videos" if ctype == "video" else ("Photos" if ctype == "images" else "Top"))
        st_search, payload_search, err_search = await self._graphql_get(
            "SearchTimeline",
            {
                "rawQuery": kw,
                "count": 20,
                "querySource": "typed_query",
                "product": product,
            },
            referer="https://x.com/explore",
        )
        if err_search.startswith(("logged_out:", "verification_required:", "rate_limit:")):
            return [], err_search

        active_op = "SearchTimeline"
        cursor = ""
        if st_search == 200 and payload_search:
            batch, cursor = extract_graphql_tweets(payload_search)
            for t in batch:
                tid = str(t.get("rest_id") or "")
                if tid:
                    collected.setdefault(tid, t)

        # 2) 若 SearchTimeline 返回 404 空包或无数据，自动切换至高可用 GlobalCommunities*SearchTimeline
        if not collected:
            active_op = (
                "GlobalCommunitiesLatestPostSearchTimeline"
                if latest
                else "GlobalCommunitiesPostSearchTimeline"
            )
            cursor = ""
            st_fb, payload_fb, err_fb = await self._graphql_get(
                active_op,
                {"rawQuery": query_expr, "count": 20},
                referer="https://x.com/explore",
            )
            if err_fb and not payload_fb:
                return [], err_fb
            batch, cursor = extract_graphql_tweets(payload_fb)
            for t in batch:
                tid = str(t.get("rest_id") or "")
                if tid:
                    collected.setdefault(tid, t)

        # 后续分页（第 2..pages 页）
        stagnant = 0
        for _ in range(1, pages):
            if not cursor:
                break
            if active_op == "SearchTimeline":
                vars_page: Dict[str, Any] = {
                    "rawQuery": kw,
                    "count": 20,
                    "cursor": cursor,
                    "querySource": "typed_query",
                    "product": product,
                }
            else:
                vars_page = {
                    "rawQuery": query_expr,
                    "count": 20,
                    "cursor": cursor,
                }
            st_p, payload_p, err_p = await self._graphql_get(
                active_op,
                vars_page,
                referer="https://x.com/explore",
            )
            if err_p and not payload_p:
                break
            batch, next_cursor = extract_graphql_tweets(payload_p)
            before = len(collected)
            for t in batch:
                tid = str(t.get("rest_id") or "")
                if tid:
                    collected.setdefault(tid, t)
            if len(collected) == before:
                stagnant += 1
                if stagnant >= 2:
                    break
            else:
                stagnant = 0
            if not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor

        return list(collected.values()), ("" if collected else "empty")

    async def fetch_user_tweets(
        self,
        user_target: str,
        *,
        max_pages: int = 3,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any], str]:
        """按 `@screen_name` 或数字 `user_id` 分页读取创作者时间线 (`UserOriginalsTimeline` / `UserTweets`)。"""
        user_info, u_err = await self.fetch_user_info(user_target)
        uid = str(user_info.get("uid") or "").strip()
        if not uid:
            target = resolve_twitter_user_target(user_target)
            if target and target.isdigit():
                uid = target
            else:
                return [], user_info, (u_err or "not_found:未找到目标 X 用户 UID")

        collected: Dict[str, Dict[str, Any]] = {}
        pages = max(1, min(15, int(max_pages or 3)))
        active_op = "UserOriginalsTimeline"
        cursor = ""
        last_err = ""

        for page_idx in range(pages):
            vars_d: Dict[str, Any] = {
                "userId": uid,
                "count": 20,
                "includePromotedContent": True,
                "withCommunity": True,
                "withVoice": True,
            }
            if cursor:
                vars_d["cursor"] = cursor
            st, payload, err = await self._graphql_get(
                active_op,
                vars_d,
                referer=f"https://x.com/{user_info.get('unique_id') or 'home'}",
            )
            if page_idx == 0 and (st >= 400 or not payload) and active_op == "UserOriginalsTimeline":
                active_op = "UserTweets"
                st, payload, err = await self._graphql_get(
                    active_op,
                    vars_d,
                    referer=f"https://x.com/{user_info.get('unique_id') or 'home'}",
                )
            if err and not payload:
                last_err = err
                break
            batch, next_cursor = extract_graphql_tweets(payload)
            before = len(collected)
            for t in batch:
                tid = str(t.get("rest_id") or "")
                if tid:
                    collected.setdefault(tid, t)
            if not next_cursor or next_cursor == cursor or len(collected) == before:
                break
            cursor = next_cursor

        return list(collected.values()), user_info, ("" if collected else (last_err or "empty"))

    async def fetch_self_profile(self, *, hint_screen_name: str = "") -> Tuple[Dict[str, Any], str]:
        """探测当前会话登录态并获取本账号资料（支持仅 `auth_token` 自动换取 `ct0`/`twid`，优先 GraphQL `UserByRestId`）。"""
        if not self.has_auth:
            return {}, "logged_out:缺少 X auth_token 登录态"

        await self.ensure_session_cookies()

        # 1) 若 Cookie 含 twid (`u=<uid>`)，直接通过 UserByRestId 查询最新完整资料
        if self.self_uid:
            prof, err = await self.fetch_user_info(self.self_uid)
            if prof.get("uid") or prof.get("unique_id"):
                return prof, ""
            if err.startswith(("logged_out:", "verification_required:")):
                return {}, err

        # 2) 若调用方已知账号 screen_name，走 UserByScreenName
        clean_hint = resolve_twitter_user_target(hint_screen_name)
        if clean_hint:
            prof, err = await self.fetch_user_info(clean_hint)
            if prof.get("uid") or prof.get("unique_id"):
                if prof.get("uid"):
                    self.self_uid = str(prof["uid"])
                return prof, ""
            if err.startswith(("logged_out:", "verification_required:")):
                return {}, err

        # 3) 兼容旧版 REST settings.json / verify_credentials.json
        _st, settings_data, err = await self._get_json(REST_SETTINGS_URL)
        if isinstance(settings_data, Mapping) and settings_data.get("screen_name"):
            sn = str(settings_data["screen_name"]).strip()
            prof, _ = await self.fetch_user_info(sn)
            if prof.get("uid") or prof.get("unique_id"):
                return prof, ""
            return {
                "uid": self.self_uid,
                "sec_uid": sn,
                "unique_id": sn,
                "douyin_id": sn,
                "nickname": sn,
                "avatar": "",
                "follower_count": 0,
                "following_count": 0,
                "aweme_count": 0,
                "total_favorited": 0,
            }, ""

        _st3, cred_data, c_err = await self._get_json(REST_VERIFY_CREDENTIALS_URL)
        if isinstance(cred_data, Mapping) and (cred_data.get("id_str") or cred_data.get("screen_name")):
            parsed = parse_twitter_self_user(cred_data)
            if parsed.get("uid"):
                self.self_uid = str(parsed["uid"])
            return parsed, ""

        return {}, err or c_err or "api_unavailable"

    async def fetch_user_info(self, user_target: str) -> Tuple[Dict[str, Any], str]:
        """按 `@screen_name` 或数字 `user_id` 查询用户公开资料（优先 GraphQL `UserByScreenName` / `UserByRestId`）。"""
        target = resolve_twitter_user_target(user_target)
        if not target:
            return {}, "not_found:无效的 X 用户标识"

        if self.has_auth:
            if target.isdigit():
                _st, g_data, g_err = await self._graphql_get(
                    "UserByRestId",
                    {"userId": target, "withSafetyModeUserFields": True},
                    referer=f"https://x.com/i/user/{target}",
                )
            else:
                _st, g_data, g_err = await self._graphql_get(
                    "UserByScreenName",
                    {"screen_name": target, "withGrokTranslatedBio": False},
                    referer=f"https://x.com/{target}",
                )
            if isinstance(g_data, Mapping):
                parsed = parse_twitter_user(g_data)
                if parsed.get("uid") or parsed.get("unique_id"):
                    return parsed, ""
            if g_err.startswith(("logged_out:", "verification_required:", "rate_limit:")):
                return {}, g_err

        params: Dict[str, Any] = {"include_profile_interstitial_type": "1"}
        if target.isdigit():
            params["user_id"] = target
        else:
            params["screen_name"] = target
        _st, data, err = await self._get_json(REST_USERS_SHOW_URL, params=params)
        if isinstance(data, Mapping) and (data.get("id_str") or data.get("screen_name")):
            return parse_twitter_user(data), ""
        return {}, err or "api_unavailable"

    async def fetch_dm_inbox(self) -> Tuple[List[Dict[str, Any]], str]:
        """直连读取 `/i/api/1.1/dm/inbox_initial_state.json` 收件箱会话列表。"""
        if not self.has_auth:
            return [], "logged_out:缺少 X auth_token 登录态"
        params = {
            "nsfw_filtering_enabled": "false",
            "filter_low_quality": "false",
            "include_quality": "all",
            "include_profile_interstitial_type": "1",
            "include_blocking": "1",
            "include_blocked_by": "1",
            "dm_users": "true",
            "include_groups": "true",
            "include_inbox_timelines": "true",
            "supports_reactions": "true",
            "ext": "mediaColor,altText,mediaStats,highlightedLabel",
        }
        _st, payload, err = await self._get_json(
            REST_DM_INBOX_URL,
            params=params,
            referer="https://x.com/messages",
        )
        if err and not payload:
            return [], err
        rows = parse_twitter_dm_inbox(payload, self_uid=self.self_uid)
        return rows, ""

    async def fetch_dm_messages(self, conv_id: str) -> Tuple[List[Dict[str, Any]], str]:
        """直连读取单条 DM 会话历史消息 `/i/api/1.1/dm/conversation/{conv_id}.json`。"""
        cid = str(conv_id or "").strip()
        if not cid:
            return [], "missing_conv_id"
        if not self.has_auth:
            return [], "logged_out:缺少 X auth_token 登录态"
        url = REST_DM_CONVERSATION_TMPL.format(conv_id=cid)
        params = {
            "context": "FETCH_DM_CONVERSATION",
            "include_profile_interstitial_type": "1",
            "dm_users": "false",
            "supports_reactions": "true",
        }
        _st, payload, err = await self._get_json(
            url,
            params=params,
            referer=f"https://x.com/messages/{cid}",
        )
        if err and not payload:
            return [], err
        msgs = parse_twitter_dm_messages(payload, conv_id=cid, self_uid=self.self_uid)
        return msgs, ""
