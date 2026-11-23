"""X (Twitter) 推文链接、t.co 短链与创作者 @username / rest_id 解析器。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import urlsplit

from curl_cffi.requests import AsyncSession

from ...browser.manager import normalize_proxy
from ...netfp import impersonate_for_ua
from .urls import tweet_url

TWITTER_HOSTS = frozenset({
    "x.com",
    "www.x.com",
    "mobile.x.com",
    "twitter.com",
    "www.twitter.com",
    "mobile.twitter.com",
    "vxtwitter.com",
    "fxtwitter.com",
    "fixupx.com",
})
SHORT_HOSTS = frozenset({"t.co", "www.t.co"})

RESERVED_PATHS = frozenset({
    "home",
    "explore",
    "notifications",
    "messages",
    "i",
    "compose",
    "search",
    "settings",
    "login",
    "logout",
    "signup",
    "tos",
    "privacy",
    "hashtag",
    "intent",
    "share",
    "account",
    "suspended",
    "disabled",
    "jobs",
    "lists",
    "bookmarks",
    "communities",
    "grok",
    "premium",
    "about",
    "help",
})

_URL_RE = re.compile(r"https?://[^\s<>\"'，。！？；：）】》]+", re.IGNORECASE)
_STATUS_PATH_RE = re.compile(
    r"^/(?:i/(?:web/)?status|(?P<user>[A-Za-z0-9_]{1,15})/status(?:es)?)/(?P<id>\d{5,25})(?:/.*)?$",
    re.IGNORECASE,
)
_I_USER_PATH_RE = re.compile(r"^/i/user/(?P<uid>\d{4,25})(?:/.*)?$", re.IGNORECASE)
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_SNOWFLAKE_RE = re.compile(r"^\d{10,25}$")
_UID_RE = re.compile(r"^\d{4,25}$")


@dataclass(frozen=True)
class TwitterTweetRef:
    tweet_id: str
    screen_name: str = ""

    @property
    def url(self) -> str:
        return tweet_url(self.tweet_id, self.screen_name)


def _first_url_or_raw(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    m = _URL_RE.search(text)
    if m:
        return m.group(0).rstrip(".,;:!?)】》'\"")
    return text


def is_twitter_host(host: str) -> bool:
    h = (host or "").strip().lower()
    if not h:
        return False
    if h in TWITTER_HOSTS or h in SHORT_HOSTS:
        return True
    return h.endswith(".x.com") or h.endswith(".twitter.com")


def clean_twitter_username(raw: str) -> str:
    """清洗并校验 X `@screen_name`（1~15 位字母数字下划线，排除系统保留路径）。"""
    val = str(raw or "").strip()
    if not val:
        return ""
    if val.startswith("@"):
        val = val[1:].strip()
    if not _HANDLE_RE.match(val):
        return ""
    if val.lower() in RESERVED_PATHS:
        return ""
    return val


def looks_like_twitter_tweet(raw: str) -> bool:
    """判断输入是否为单条推文链接或 `/status/<id>` 形式。"""
    candidate = _first_url_or_raw(raw)
    if not candidate:
        return False
    low = candidate.lower()
    if "/status/" in low or "/statuses/" in low:
        return True
    try:
        parsed = urlsplit(candidate)
        if is_twitter_host(parsed.hostname or "") and _STATUS_PATH_RE.match(parsed.path or ""):
            return True
    except ValueError:
        pass
    return False


def resolve_twitter_tweet_ref(
    raw: str,
    *,
    fallback_screen_name: str = "",
    allow_numeric: bool = True,
) -> Optional[TwitterTweetRef]:
    """从分享文案、x.com/twitter.com 推文链接或纯数字推文 ID 提取 `TwitterTweetRef`。"""
    candidate = _first_url_or_raw(raw)
    if not candidate:
        return None
    fallback_handle = clean_twitter_username(fallback_screen_name)

    if candidate.startswith(("http://", "https://")):
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            return None
        host = (parsed.hostname or "").lower()
        if not is_twitter_host(host):
            return None
        m = _STATUS_PATH_RE.match(parsed.path or "")
        if m:
            tid = m.group("id")
            user = clean_twitter_username(m.group("user") or "") or fallback_handle
            return TwitterTweetRef(tweet_id=tid, screen_name=user)
        return None

    # 兼容 `username/status/1234567890` 无协议头输入
    rel_match = re.search(
        r"(?:^|/)(?:(?P<user>[A-Za-z0-9_]{1,15})/)?status(?:es)?/(?P<id>\d{5,25})",
        candidate,
        re.IGNORECASE,
    )
    if rel_match:
        tid = rel_match.group("id")
        user = clean_twitter_username(rel_match.group("user") or "") or fallback_handle
        return TwitterTweetRef(tweet_id=tid, screen_name=user)

    if allow_numeric and _SNOWFLAKE_RE.match(candidate):
        return TwitterTweetRef(tweet_id=candidate, screen_name=fallback_handle)

    return None


def resolve_twitter_user_target(raw: str) -> str:
    """从 `@username`、主页链接或数字 `rest_id` 解析创作者标识（优先返回不带 `@` 的 `screen_name`）。"""
    candidate = _first_url_or_raw(raw)
    if not candidate:
        return ""

    if candidate.startswith(("http://", "https://")):
        try:
            parsed = urlsplit(candidate)
        except ValueError:
            return ""
        host = (parsed.hostname or "").lower()
        if not is_twitter_host(host) or host in SHORT_HOSTS:
            return ""
        path = parsed.path or ""
        if _STATUS_PATH_RE.match(path):
            return ""
        uid_match = _I_USER_PATH_RE.match(path)
        if uid_match:
            return uid_match.group("uid")
        segments = [seg for seg in path.split("/") if seg]
        if not segments:
            return ""
        first = segments[0]
        handle = clean_twitter_username(first)
        return handle

    # 支持从带分享文案的 `@username` 中提取
    if candidate.startswith("@"):
        token = candidate.split()[0].rstrip(".,;:!?)】》")
        return clean_twitter_username(token)

    if _UID_RE.match(candidate):
        return candidate

    handle = clean_twitter_username(candidate)
    if handle:
        return handle

    at_match = re.search(r"@([A-Za-z0-9_]{1,15})\b", str(raw or ""))
    if at_match:
        return clean_twitter_username(at_match.group(1))
    return ""


async def resolve_twitter_short_url(
    raw_url: str,
    *,
    user_agent: str = "",
    proxy: str = "",
    timeout: float = 12.0,
) -> str:
    """展开 `https://t.co/...` 短链到最终 `x.com` / `twitter.com` 目标地址。"""
    candidate = _first_url_or_raw(raw_url)
    if not candidate.startswith(("http://", "https://")):
        return candidate
    try:
        host = (urlsplit(candidate).hostname or "").lower()
    except ValueError:
        return candidate
    if host not in SHORT_HOSTS:
        return candidate

    ua = (user_agent or "").strip() or (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    from ...netfp import curl_proxy_url
    norm_proxy = curl_proxy_url(proxy) if proxy else ""
    try:
        async with AsyncSession(
            impersonate=impersonate_for_ua(ua),
            trust_env=False,
        ) as cli:
            resp = await cli.get(
                candidate,
                headers=headers,
                proxy=norm_proxy or None,
                timeout=max(3.0, float(timeout)),
                allow_redirects=True,
            )
            final = str(resp.url or candidate)
            return final
    except Exception:
        return candidate


async def resolve_twitter_target(
    raw: str,
    *,
    user_agent: str = "",
    proxy: str = "",
) -> Tuple[str, Optional[TwitterTweetRef], str]:
    """一站式解析输入（含 `t.co` 展开），返回 `(expanded_input, tweet_ref, user_target)`。"""
    cleaned = _first_url_or_raw(raw)
    if not cleaned:
        return "", None, ""
    expanded = await resolve_twitter_short_url(cleaned, user_agent=user_agent, proxy=proxy)
    t_ref = resolve_twitter_tweet_ref(expanded, allow_numeric=False)
    if t_ref is not None:
        return expanded, t_ref, t_ref.screen_name
    u_target = resolve_twitter_user_target(expanded)
    if not u_target and _SNOWFLAKE_RE.match(expanded):
        # 纯长数字默认在单帖场景也可视作 tweet_id
        t_ref = TwitterTweetRef(tweet_id=expanded)
    return expanded, t_ref, u_target
