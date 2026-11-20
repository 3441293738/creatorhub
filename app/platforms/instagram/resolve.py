"""Instagram 链接、@用户名、Shortcode 与数字 Media ID 互转解析器。

支持零网络调用完成 base64url Shortcode 与十进制 `mediaId` (`pk`) 的双向精确换算，
可将帖子 / Reels / IGTV / Story 链接直接解析为标准作品引用。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_CHAR_TO_INDEX = {ch: idx for idx, ch in enumerate(SHORTCODE_ALPHABET)}

RESERVED_PATHS = frozenset({
    "p",
    "reel",
    "reels",
    "tv",
    "stories",
    "explore",
    "direct",
    "accounts",
    "challenge",
    "about",
    "legal",
    "developer",
    "directory",
    "api",
    "graphql",
    "oauth",
    "privacy",
    "terms",
    "session",
    "web",
})

_WORK_PATH_RE = re.compile(
    r"/(?P<kind>p|reel|reels|tv)/(?P<code>[A-Za-z0-9_-]{5,32})(?:[/?#]|$)",
    re.IGNORECASE,
)
_STORY_PATH_RE = re.compile(
    r"/stories/(?P<user>[A-Za-z0-9._]{1,30})/(?P<pk>\d{6,25})(?:[/?#]|$)",
    re.IGNORECASE,
)
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]{6,28}$")
_MEDIA_PK_RE = re.compile(r"^(?P<pk>\d{6,25})(?:_(?P<uid>\d+))?$")


@dataclass(frozen=True)
class InstagramWorkRef:
    shortcode: str
    media_id: str
    kind: str = "p"  # "p" | "reel" | "tv" | "story"
    canonical_url: str = ""


def media_id_to_shortcode(media_id: str | int) -> str:
    """将十进制 `mediaId`（若含 `_userId` 后缀则取前半段）按 64 进制映射为 Instagram Shortcode。"""
    raw = str(media_id or "").strip()
    if not raw:
        return ""
    if "_" in raw:
        raw = raw.split("_", 1)[0]
    if not raw.isdigit():
        return ""
    num = int(raw)
    if num <= 0:
        return SHORTCODE_ALPHABET[0] if raw == "0" else ""
    chars: list[str] = []
    while num > 0:
        num, rem = divmod(num, 64)
        chars.append(SHORTCODE_ALPHABET[rem])
    chars.reverse()
    return "".join(chars)


def shortcode_to_media_id(shortcode: str) -> str:
    """将 base64url Shortcode 还原为十进制 `mediaId` (`pk`) 字符串。"""
    code = str(shortcode or "").strip()
    if not code:
        return ""
    val = 0
    for ch in code:
        idx = _CHAR_TO_INDEX.get(ch)
        if idx is None:
            return ""
        val = val * 64 + idx
    return str(val) if val > 0 else ""


def _is_ig_host(host: str) -> bool:
    h = (host or "").lower().strip()
    if ":" in h:
        h = h.split(":", 1)[0]
    return (
        h in {"instagram.com", "instagr.am"}
        or h.endswith(".instagram.com")
    )


def looks_like_ig_work(raw: str) -> bool:
    text = str(raw or "").strip()
    if not text:
        return False
    if "instagram.com/" in text.lower() or "instagr.am/" in text.lower():
        return bool(_WORK_PATH_RE.search(text) or _STORY_PATH_RE.search(text))
    return bool(_MEDIA_PK_RE.match(text))


def resolve_ig_work_ref(raw: str) -> Optional[InstagramWorkRef]:
    """解析帖子 / Reel / IGTV / Story 链接、纯数字 media_id (`pk` 或 `pk_uid`) 或裸 shortcode。"""
    text = str(raw or "").strip()
    if not text:
        return None

    if "://" in text or text.lower().startswith(("www.instagram.com/", "instagram.com/", "instagr.am/")):
        candidate = text if "://" in text else f"https://{text}"
        parsed = urlparse(candidate)
        if parsed.netloc and not _is_ig_host(parsed.netloc):
            return None
        path = parsed.path or ""
        m = _WORK_PATH_RE.search(path)
        if m:
            kind_raw = m.group("kind").lower()
            kind = "reel" if kind_raw in {"reel", "reels"} else ("tv" if kind_raw == "tv" else "p")
            code = m.group("code")
            pk = shortcode_to_media_id(code)
            return InstagramWorkRef(
                shortcode=code,
                media_id=pk,
                kind=kind,
                canonical_url=f"https://www.instagram.com/{'reel' if kind == 'reel' else kind}/{code}/",
            )
        ms = _STORY_PATH_RE.search(path)
        if ms:
            pk = ms.group("pk")
            code = media_id_to_shortcode(pk)
            user = ms.group("user")
            return InstagramWorkRef(
                shortcode=code or pk,
                media_id=pk,
                kind="story",
                canonical_url=f"https://www.instagram.com/stories/{user}/{pk}/",
            )
        return None

    mpk = _MEDIA_PK_RE.match(text)
    if mpk:
        pk = mpk.group("pk")
        code = media_id_to_shortcode(pk)
        if not code:
            return None
        return InstagramWorkRef(
            shortcode=code,
            media_id=pk,
            kind="p",
            canonical_url=f"https://www.instagram.com/p/{code}/",
        )

    if _SHORTCODE_RE.match(text) and not text.startswith("@"):
        pk = shortcode_to_media_id(text)
        if pk:
            return InstagramWorkRef(
                shortcode=text,
                media_id=pk,
                kind="p",
                canonical_url=f"https://www.instagram.com/p/{text}/",
            )
    return None


def resolve_ig_work(raw: str) -> str:
    """提取规范化 shortcode（作为 CreatorHub `aweme_id`）。"""
    ref = resolve_ig_work_ref(raw)
    return ref.shortcode if ref else ""


def clean_ig_username(raw: str) -> str:
    """从 `@username` 或 `https://www.instagram.com/{username}/` 提取合法 Instagram 用户名。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    if looks_like_ig_work(text):
        return ""
    if "://" in text or text.lower().startswith(("www.instagram.com/", "instagram.com/", "instagr.am/")):
        candidate = text if "://" in text else f"https://{text}"
        parsed = urlparse(candidate)
        if parsed.netloc and not _is_ig_host(parsed.netloc):
            return ""
        segs = [s for s in (parsed.path or "").split("/") if s]
        if not segs:
            return ""
        first = segs[0].lstrip("@").strip()
        if first.lower() in RESERVED_PATHS:
            return ""
        return first if _USERNAME_RE.match(first) else ""

    handle = text.lstrip("@").strip().strip("/")
    if handle.lower() in RESERVED_PATHS:
        return ""
    if _USERNAME_RE.match(handle) and not handle.isdigit():
        return handle
    return ""


def resolve_ig_user_target(raw: str) -> str:
    """解析创作者输入：支持 `@username`、主页链接或纯数字 `user_id` (`pk`)。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    if text.isdigit() and 4 <= len(text) <= 25:
        return text
    return clean_ig_username(text)
