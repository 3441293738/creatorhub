"""Facebook 帖子/视频/Reels/群组/主页链接解析与短链展开器。"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Optional, Tuple
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from curl_cffi.requests import AsyncSession

from ...netfp import curl_proxy_url, impersonate_for_ua
from .protocol import DEFAULT_FACEBOOK_UA
from .urls import FACEBOOK_BASE, post_url

FACEBOOK_HOSTS = frozenset({
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "mbasic.facebook.com",
    "web.facebook.com",
    "business.facebook.com",
    "touch.facebook.com",
    "messenger.com",
    "www.messenger.com",
})

SHORT_HOSTS = frozenset({
    "fb.watch",
    "www.fb.watch",
    "fb.me",
    "www.fb.me",
    "fb.com",
    "www.fb.com",
    "l.facebook.com",
})

RESERVED_PATHS = frozenset({
    "",
    "home",
    "home.php",
    "login",
    "login.php",
    "logout",
    "logout.php",
    "reg",
    "signup",
    "recover",
    "checkpoint",
    "two_step_verification",
    "disabled",
    "suspended",
    "watch",
    "reel",
    "reels",
    "videos",
    "video",
    "photo",
    "photo.php",
    "photos",
    "permalink.php",
    "story.php",
    "profile.php",
    "groups",
    "pages",
    "events",
    "marketplace",
    "messages",
    "notifications",
    "friends",
    "search",
    "settings",
    "bookmarks",
    "stories",
    "share",
    "support",
    "help",
    "privacy",
    "policies",
    "legal",
    "about",
    "gaming",
    "saved",
    "memories",
    "fundraisers",
    "live",
    "hashtag",
    "people",
    "me",
})

_URL_RE = re.compile(r"https?://[^\s<>\"'，。！？；：）】》]+", re.IGNORECASE)
_REEL_PATH_RE = re.compile(r"^/reels?/(?P<id>[A-Za-z0-9_.-]+)(?:/.*)?$", re.IGNORECASE)
_WATCH_PATH_RE = re.compile(r"^/watch/(?:live/)?(?P<id>\d{5,25})?(?:/.*)?$", re.IGNORECASE)
_USER_POST_PATH_RE = re.compile(
    r"^/(?P<user>[A-Za-z0-9._-]{2,80})/posts/(?P<id>[A-Za-z0-9_.-]+)(?:/.*)?$",
    re.IGNORECASE,
)
_USER_VIDEO_PATH_RE = re.compile(
    r"^/(?P<user>[A-Za-z0-9._-]{2,80})/videos/(?:[^/]+/)?(?P<id>\d{5,25})(?:/.*)?$",
    re.IGNORECASE,
)
_USER_PHOTO_PATH_RE = re.compile(
    r"^/(?P<user>[A-Za-z0-9._-]{2,80})/photos/(?:[^/]+/)?(?P<id>\d{5,25})(?:/.*)?$",
    re.IGNORECASE,
)
_GROUP_POST_PATH_RE = re.compile(
    r"^/groups/(?P<group>[A-Za-z0-9._-]+)/(?:posts|permalink)/(?P<id>[A-Za-z0-9_.-]+)(?:/.*)?$",
    re.IGNORECASE,
)
_GROUP_ROOT_RE = re.compile(r"^/groups/(?P<group>[A-Za-z0-9._-]+)(?:/.*)?$", re.IGNORECASE)
_SHARE_PATH_RE = re.compile(r"^/share/(?:p|v|r|s)/[A-Za-z0-9._-]+/?$", re.IGNORECASE)
_PEOPLE_PATH_RE = re.compile(r"^/people/[^/]+/(?P<uid>\d{4,25})(?:/.*)?$", re.IGNORECASE)
_UID_RE = re.compile(r"^\d{4,25}$")
_HANDLE_RE = re.compile(r"^[A-Za-z0-9._-]{2,80}$")


@dataclass(frozen=True)
class FacebookPostRef:
    post_id: str
    author_id: str = ""
    kind: str = "post"       # post | video | reel | photo | group
    comment_id: str = ""
    raw_url: str = ""

    @property
    def url(self) -> str:
        if self.raw_url and self.raw_url.startswith(("http://", "https://")):
            return self.raw_url
        return post_url(self.post_id, self.author_id, kind=self.kind)

    @property
    def canonical_url(self) -> str:
        return self.url

    @property
    def owner(self) -> str:
        return self.author_id


def group_post_url_from_photo(raw_url: str) -> str:
    """将群组图片灯箱 URL (`/photo/?fbid=...&set=gm.<pid>&idorvanity=<gid>`) 转为标准群组帖子链接。"""
    u = normalize_facebook_url(raw_url)
    if not u.startswith(("http://", "https://")):
        return ""
    try:
        sp = urlsplit(u)
    except ValueError:
        return ""
    if "/photo" not in (sp.path or "").lower():
        return ""
    q = dict(parse_qsl(sp.query, keep_blank_values=False))
    set_val = str(q.get("set") or "").strip()
    gid = str(q.get("idorvanity") or "").strip()
    m_gm = re.match(r"^(?:gm|pcb)\.(\d{5,25})$", set_val)
    if m_gm and gid:
        return f"https://www.facebook.com/groups/{gid}/posts/{m_gm.group(1)}/"
    return ""


def _first_url_or_raw(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    m = _URL_RE.search(text)
    if m:
        return m.group(0).rstrip(".,;:!?)】》'\"")
    return text


def is_facebook_host(host: str) -> bool:
    h = (host or "").strip().lower()
    if not h:
        return False
    if h in FACEBOOK_HOSTS or h in SHORT_HOSTS:
        return True
    return h.endswith(".facebook.com") or h.endswith(".messenger.com")


def normalize_facebook_url(raw_url: str) -> str:
    """将 `m.facebook.com` / `web.facebook.com` / `mbasic.facebook.com` 规范为 `www.facebook.com` 并清理跳转追踪参数。"""
    candidate = _first_url_or_raw(raw_url)
    if not candidate.startswith(("http://", "https://")):
        return candidate
    try:
        sp = urlsplit(candidate)
    except ValueError:
        return candidate
    host = (sp.hostname or "").lower()
    if host in {"facebook.com", "www.facebook.com", "m.facebook.com", "mbasic.facebook.com", "web.facebook.com", "touch.facebook.com"}:
        q_pairs = [
            (k, v)
            for k, v in parse_qsl(sp.query, keep_blank_values=True)
            if k not in {"_rdc", "_rdr", "__tn__", "rdid", "share_url"} and not k.startswith("__cft__")
        ]
        clean_q = urlencode(q_pairs, doseq=True)
        return urlunsplit(("https", "www.facebook.com", sp.path or "/", clean_q, ""))
    return candidate


def clean_facebook_handle(raw: str) -> str:
    val = str(raw or "").strip().lstrip("@").strip("/")
    if not val:
        return ""
    if val.lower() in RESERVED_PATHS:
        return ""
    if not _HANDLE_RE.match(val):
        return ""
    return val


def looks_like_facebook_post(raw: str) -> bool:
    """判断输入是否为单条 Facebook 帖子 / 视频 / Reels / 照片链接（避免误判 `/<user>/reels/`、`/videos/` 等主页子标签页）。"""
    candidate = _first_url_or_raw(raw)
    if not candidate:
        return False
    if candidate.startswith(("http://", "https://")):
        try:
            sp = urlsplit(candidate)
        except ValueError:
            return False
        host = (sp.hostname or "").lower()
        path = sp.path or ""
        if host in {"fb.watch", "www.fb.watch"} and path.strip("/"):
            return True
        if is_facebook_host(host) and _SHARE_PATH_RE.match(path):
            return True
    return resolve_facebook_post_ref(candidate, allow_numeric=False) is not None


def resolve_facebook_post_ref(
    raw: str,
    *,
    fallback_author: str = "",
    allow_numeric: bool = True,
) -> Optional[FacebookPostRef]:
    """从各类 Facebook 链接解析 `FacebookPostRef`（兼容 posts / videos / reel / watch / permalink / photo / groups）。"""
    candidate = normalize_facebook_url(_first_url_or_raw(raw))
    if not candidate:
        return None
    fb_author = str(fallback_author or "").strip().lstrip("@")

    if candidate.startswith(("http://", "https://")):
        try:
            sp = urlsplit(candidate)
        except ValueError:
            return None
        host = (sp.hostname or "").lower()
        if not is_facebook_host(host):
            return None
        path = sp.path or ""
        qs = parse_qs(sp.query or "")
        comment_id = (qs.get("comment_id") or [""])[0].strip()

        # 1) /reel/<id> 或 /reels/<id>
        m_reel = _REEL_PATH_RE.match(path)
        if m_reel:
            return FacebookPostRef(
                post_id=m_reel.group("id"),
                author_id=fb_author,
                kind="reel",
                comment_id=comment_id,
                raw_url=f"{FACEBOOK_BASE}/reel/{m_reel.group('id')}",
            )

        # 2) /watch/?v=<id> 或 /watch/<id>
        if path.lower().startswith("/watch"):
            vid = (qs.get("v") or [""])[0].strip()
            if not vid:
                m_w = _WATCH_PATH_RE.match(path)
                vid = (m_w.group("id") or "") if m_w else ""
            if vid:
                return FacebookPostRef(
                    post_id=vid,
                    author_id=fb_author,
                    kind="video",
                    comment_id=comment_id,
                    raw_url=f"{FACEBOOK_BASE}/watch/?v={vid}",
                )

        # 3) /permalink.php?story_fbid=...&id=... 或 /story.php?...
        if path.lower() in {"/permalink.php", "/story.php"}:
            sid = (qs.get("story_fbid") or qs.get("fbid") or [""])[0].strip()
            uid = (qs.get("id") or [""])[0].strip() or fb_author
            if sid:
                return FacebookPostRef(
                    post_id=sid,
                    author_id=uid,
                    kind="post",
                    comment_id=comment_id,
                    raw_url=candidate,
                )

        # 4) /photo.php?fbid=... 或 /photo/?fbid=...
        if path.lower() in {"/photo.php", "/photo", "/photo/"}:
            fbid = (qs.get("fbid") or [""])[0].strip()
            if fbid:
                return FacebookPostRef(
                    post_id=fbid,
                    author_id=fb_author,
                    kind="photo",
                    comment_id=comment_id,
                    raw_url=f"{FACEBOOK_BASE}/photo/?fbid={fbid}",
                )

        # 5) /groups/<group_id>/posts/<post_id> 或 /groups/<group_id>/permalink/<post_id>
        m_gp = _GROUP_POST_PATH_RE.match(path)
        if m_gp:
            gid = m_gp.group("group")
            pid = m_gp.group("id")
            return FacebookPostRef(
                post_id=pid,
                author_id=gid,
                kind="group",
                comment_id=comment_id,
                raw_url=f"{FACEBOOK_BASE}/groups/{gid}/posts/{pid}",
            )

        # 6) /groups/<group_id>/?multi_permalinks=<post_id>
        m_gr = _GROUP_ROOT_RE.match(path)
        if m_gr and qs.get("multi_permalinks"):
            gid = m_gr.group("group")
            pid = qs["multi_permalinks"][0].split(",")[0].strip()
            if pid:
                return FacebookPostRef(
                    post_id=pid,
                    author_id=gid,
                    kind="group",
                    comment_id=comment_id,
                    raw_url=f"{FACEBOOK_BASE}/groups/{gid}/posts/{pid}",
                )

        # 7) /<user>/posts/<id>
        m_up = _USER_POST_PATH_RE.match(path)
        if m_up:
            user = m_up.group("user")
            if user.lower() not in RESERVED_PATHS:
                pid = m_up.group("id")
                return FacebookPostRef(
                    post_id=pid,
                    author_id=user,
                    kind="post",
                    comment_id=comment_id,
                    raw_url=f"{FACEBOOK_BASE}/{user}/posts/{pid}",
                )

        # 8) /<user>/videos/<id>
        m_uv = _USER_VIDEO_PATH_RE.match(path)
        if m_uv:
            user = m_uv.group("user")
            pid = m_uv.group("id")
            return FacebookPostRef(
                post_id=pid,
                author_id="" if user.lower() in RESERVED_PATHS else user,
                kind="video",
                comment_id=comment_id,
                raw_url=f"{FACEBOOK_BASE}/{user}/videos/{pid}",
            )

        # 9) /<user>/photos/.../<id>
        m_uph = _USER_PHOTO_PATH_RE.match(path)
        if m_uph:
            user = m_uph.group("user")
            pid = m_uph.group("id")
            return FacebookPostRef(
                post_id=pid,
                author_id="" if user.lower() in RESERVED_PATHS else user,
                kind="photo",
                comment_id=comment_id,
                raw_url=candidate,
            )

        return None

    if candidate.startswith("pfbid") and len(candidate) >= 16:
        return FacebookPostRef(post_id=candidate, author_id=fb_author, kind="post")

    if allow_numeric and _UID_RE.match(candidate) and len(candidate) >= 10:
        return FacebookPostRef(post_id=candidate, author_id=fb_author, kind="post")

    return None


def resolve_facebook_user_target(raw: str) -> str:
    """从 Facebook 个人主页、公共主页或群组链接解析目标标识。

    返回规范目标字符串：
    - 个人/主页数字 ID：纯数字串（如 `61558218339774`）
    - 个人/主页用户名：vanity 名称（如 `zuck`）
    - 群组：`groups/<group_id_or_slug>`
    """
    candidate = normalize_facebook_url(_first_url_or_raw(raw))
    if not candidate:
        return ""

    if candidate.startswith(("http://", "https://")):
        try:
            sp = urlsplit(candidate)
        except ValueError:
            return ""
        host = (sp.hostname or "").lower()
        if not is_facebook_host(host) or host in SHORT_HOSTS:
            return ""
        path = sp.path or ""
        qs = parse_qs(sp.query or "")

        if looks_like_facebook_post(candidate):
            return ""

        # profile.php?id=1000...
        if path.lower().rstrip("/") == "/profile.php":
            uid = (qs.get("id") or [""])[0].strip()
            return uid if _UID_RE.match(uid) else ""

        # /people/<name>/<uid>
        m_ppl = _PEOPLE_PATH_RE.match(path)
        if m_ppl:
            return m_ppl.group("uid")

        # /groups/<group_id>
        m_grp = _GROUP_ROOT_RE.match(path)
        if m_grp:
            grp = m_grp.group("group").strip()
            if grp.lower() not in {"feed", "discover", "joins", "notifications", "create"}:
                return f"groups/{grp}"
            return ""

        segments = [seg for seg in path.split("/") if seg]
        if not segments:
            return ""
        first = segments[0]
        if first.lower() == "pg" and len(segments) >= 2:
            first = segments[1]
        return clean_facebook_handle(first)

    # 非 URL 输入
    low = candidate.lower()
    if low.startswith("groups/"):
        seg = candidate[len("groups/"):].strip().strip("/").split("/")[0]
        return f"groups/{seg}" if seg else ""
    if low.startswith("profile.php?id="):
        uid = candidate.split("id=", 1)[1].split("&", 1)[0].strip()
        return uid if _UID_RE.match(uid) else ""
    if _UID_RE.match(candidate):
        return candidate
    return clean_facebook_handle(candidate)


async def resolve_facebook_short_url(
    raw_url: str,
    *,
    user_agent: str = "",
    proxy: str = "",
    timeout: float = 12.0,
) -> str:
    """展开 `fb.watch`、`fb.me`、`/share/p|v|r/...` 等 Facebook 短链并规范化域名。"""
    candidate = _first_url_or_raw(raw_url)
    if not candidate.startswith(("http://", "https://")):
        return candidate
    try:
        sp = urlsplit(candidate)
    except ValueError:
        return candidate
    host = (sp.hostname or "").lower()
    needs_expand = host in SHORT_HOSTS or bool(_SHARE_PATH_RE.match(sp.path or ""))
    if not needs_expand:
        return normalize_facebook_url(candidate)

    ua = (user_agent or DEFAULT_FACEBOOK_UA).strip()
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
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
            return normalize_facebook_url(final)
    except Exception:
        return normalize_facebook_url(candidate)


async def resolve_facebook_target(
    raw: str,
    *,
    user_agent: str = "",
    proxy: str = "",
) -> Tuple[str, Optional[FacebookPostRef], str]:
    """一站式解析输入（含短链展开），返回 `(expanded_url, post_ref, user_or_group_target)`。"""
    cleaned = _first_url_or_raw(raw)
    if not cleaned:
        return "", None, ""
    expanded = await resolve_facebook_short_url(cleaned, user_agent=user_agent, proxy=proxy)
    p_ref = resolve_facebook_post_ref(expanded, allow_numeric=False)
    if p_ref is not None:
        return expanded, p_ref, p_ref.author_id
    u_target = resolve_facebook_user_target(expanded)
    return expanded, None, u_target
