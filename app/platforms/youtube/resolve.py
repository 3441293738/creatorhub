"""YouTube 链接与标识解析器：视频/Shorts/直播/评论链接与频道句柄/UC ID 统一识别。"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

from .urls import YOUTUBE_BASE, channel_url, video_url

YOUTUBE_HOSTS = frozenset({
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "studio.youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
})

SHORT_HOSTS = frozenset({"youtu.be"})

RESERVED_PATHS = frozenset({
    "",
    "watch",
    "shorts",
    "live",
    "embed",
    "v",
    "results",
    "feed",
    "playlist",
    "upload",
    "account",
    "account_notifications",
    "channel_switcher",
    "subscription_manager",
    "gaming",
    "premium",
    "kids",
    "music",
    "tv",
    "oops",
    "disabled",
    "redirect",
    "logout",
    "signin",
    "api",
    "youtubei",
})

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{16,32}$")
_HANDLE_RE = re.compile(r"^@[^\s/?#@]{2,64}$")
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


def _is_email_address(value: str) -> bool:
    s = str(value or "").strip()
    if "@" not in s or s.startswith("@"):
        return False
    local, _, domain = s.partition("@")
    return bool(local and "." in domain and " " not in s)


@dataclass(frozen=True)
class YouTubeVideoRef:
    video_id: str
    canonical_url: str
    watch_url: str
    kind: str = "video"          # video | shorts | live
    comment_id: str = ""         # lc=... 评论定位 ID
    playlist_id: str = ""        # list=... 播放列表 ID


def is_youtube_host(url_or_host: str) -> bool:
    raw = str(url_or_host or "").strip()
    if not raw:
        return False
    cand = raw if "://" in raw else f"https://{raw}"
    try:
        host = (urlsplit(cand).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return (
        host in YOUTUBE_HOSTS
        or host.endswith(".youtube.com")
        or host.endswith(".youtu.be")
    )


def _extract_first_url(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""
    m = _URL_IN_TEXT_RE.search(raw)
    if m:
        return m.group(0).rstrip("),.;!?'\"》】」』")
    return raw


def normalize_youtube_url(raw: str, *, convert_shorts_to_watch: bool = False) -> str:
    """将各种 YouTube 分享链接（m.youtube.com / youtu.be / shorts / live）归一化为标准 HTTPS 链接。"""
    text = _extract_first_url(raw)
    if not text:
        return ""
    if not text.startswith(("http://", "https://")):
        if _VIDEO_ID_RE.fullmatch(text):
            return video_url(text)
        if _CHANNEL_ID_RE.fullmatch(text) or _HANDLE_RE.fullmatch(text):
            return channel_url(text)
        if is_youtube_host(text):
            text = "https://" + text.lstrip("/")
        else:
            return text

    try:
        parts = urlsplit(text)
    except ValueError:
        return text

    host = (parts.hostname or "").lower().rstrip(".")
    if not is_youtube_host(host):
        return text

    qs = parse_qs(parts.query)
    lc = (qs.get("lc") or [""])[0].strip()
    plist = (qs.get("list") or [""])[0].strip()
    segs = [s for s in (parts.path or "").split("/") if s]

    # youtu.be/<video_id>
    if host in SHORT_HOSTS and segs and _VIDEO_ID_RE.fullmatch(segs[0]):
        return video_url(segs[0], comment_id=lc)

    # /watch?v=<video_id>
    if segs and segs[0].lower() == "watch":
        v = (qs.get("v") or [""])[0].strip()
        if _VIDEO_ID_RE.fullmatch(v):
            return video_url(v, comment_id=lc)

    # /shorts/<id>, /live/<id>, /embed/<id>, /v/<id>
    if len(segs) >= 2 and segs[0].lower() in {"shorts", "live", "embed", "v"}:
        vid = segs[1].strip()
        if _VIDEO_ID_RE.fullmatch(vid):
            is_short = segs[0].lower() == "shorts" and not convert_shorts_to_watch and not lc
            return video_url(vid, is_short=is_short, comment_id=lc)

    # /playlist?list=...
    if segs and segs[0].lower() == "playlist" and plist:
        return f"{YOUTUBE_BASE}/playlist?list={plist}"

    # studio.youtube.com/video/<id>/...
    if host == "studio.youtube.com" and len(segs) >= 2 and segs[0].lower() == "video" and _VIDEO_ID_RE.fullmatch(segs[1]):
        return video_url(segs[1], comment_id=lc)

    # 统一 m.youtube.com -> www.youtube.com
    norm_host = "www.youtube.com" if host in {"youtube.com", "m.youtube.com", "www.youtube.com"} else host
    return urlunsplit(("https", norm_host, parts.path or "/", parts.query, ""))


def extract_video_id(raw: str) -> str:
    ref = resolve_youtube_video_ref(raw, allow_bare_id=True)
    return ref.video_id if ref else ""


def extract_comment_id(raw: str) -> str:
    ref = resolve_youtube_video_ref(raw, allow_bare_id=False)
    return ref.comment_id if ref else ""


def resolve_youtube_video_ref(raw: str, *, allow_bare_id: bool = True) -> Optional[YouTubeVideoRef]:
    """解析 YouTube 视频 / Shorts / 直播 / 评论链接为 `YouTubeVideoRef`。"""
    text = _extract_first_url(raw)
    if not text:
        return None
    if allow_bare_id and _VIDEO_ID_RE.fullmatch(text):
        w = video_url(text)
        return YouTubeVideoRef(video_id=text, canonical_url=w, watch_url=w, kind="video")

    cand = text if "://" in text else (f"https://{text}" if is_youtube_host(text) else "")
    if not cand:
        return None
    try:
        parts = urlsplit(cand)
    except ValueError:
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    if not is_youtube_host(host):
        return None

    qs = parse_qs(parts.query)
    lc = (qs.get("lc") or [""])[0].strip()
    plist = (qs.get("list") or [""])[0].strip()
    segs = [s for s in (parts.path or "").split("/") if s]

    vid = ""
    kind = "video"
    if host in SHORT_HOSTS:
        if segs and _VIDEO_ID_RE.fullmatch(segs[0]):
            vid = segs[0]
    elif segs and segs[0].lower() == "watch":
        v = (qs.get("v") or [""])[0].strip()
        if _VIDEO_ID_RE.fullmatch(v):
            vid = v
    elif len(segs) >= 2 and segs[0].lower() in {"shorts", "live", "embed", "v"}:
        if _VIDEO_ID_RE.fullmatch(segs[1]):
            vid = segs[1]
            kind = "shorts" if segs[0].lower() == "shorts" else ("live" if segs[0].lower() == "live" else "video")
    elif host == "studio.youtube.com" and len(segs) >= 2 and segs[0].lower() == "video":
        if _VIDEO_ID_RE.fullmatch(segs[1]):
            vid = segs[1]
    elif "v" in qs and _VIDEO_ID_RE.fullmatch((qs.get("v") or [""])[0].strip()):
        vid = qs["v"][0].strip()

    if not vid:
        return None

    watch = video_url(vid, is_short=False, comment_id=lc)
    canon = video_url(vid, is_short=(kind == "shorts" and not lc), comment_id=lc)
    return YouTubeVideoRef(
        video_id=vid,
        canonical_url=canon,
        watch_url=watch,
        kind=kind,
        comment_id=lc,
        playlist_id=plist,
    )


def looks_like_youtube_video(raw: str) -> bool:
    """精准识别 YouTube 视频/Shorts/直播链接，排除登录/设置等非作品页面。"""
    return resolve_youtube_video_ref(raw, allow_bare_id=False) is not None


def clean_youtube_handle(raw: str) -> str:
    """清洗 YouTube 频道句柄或 UC 频道 ID（保留 `@handle` 前缀或 `UC...` 原值，自动解码百分号编码并排除邮箱）。"""
    text = str(raw or "").strip()
    if not text or _is_email_address(text):
        return ""
    if _CHANNEL_ID_RE.fullmatch(text):
        return text
    if text.startswith("@"):
        decoded = unquote(text).strip()
        if _HANDLE_RE.fullmatch(decoded):
            return decoded
    if "://" in text or is_youtube_host(text):
        cand = text if "://" in text else f"https://{text}"
        try:
            parts = urlsplit(cand)
        except ValueError:
            return ""
        segs = [unquote(s).strip() for s in (parts.path or "").split("/") if s]
        if not segs:
            return ""
        first = segs[0]
        if first.startswith("@"):
            return first
        if first.lower() == "channel" and len(segs) >= 2:
            return segs[1]
        if first.lower() in {"c", "user"} and len(segs) >= 2:
            return f"{first.lower()}/{segs[1]}"
        if first.lower() not in RESERVED_PATHS and not _VIDEO_ID_RE.fullmatch(first):
            return f"@{first.lstrip('@')}"
        return ""
    if "/" not in text and " " not in text and not text.startswith("@"):
        decoded = unquote(text).strip()
        if _is_email_address(decoded):
            return ""
        return f"@{decoded}"
    return unquote(text).strip()


def resolve_youtube_channel_target(raw: str) -> Tuple[str, str]:
    """解析用户输入的 YouTube 频道目标，返回 `(channel_key, canonical_channel_url)`。

    - 支持 `@handle`、`https://www.youtube.com/@handle(/videos|/shorts)`
    - 支持 `UC...`、`https://www.youtube.com/channel/UC...`
    - 支持 `/c/Name`、`/user/Name`
    - 支持直接粘贴单个视频链接（调用方再按视频详情反查频道）
    """
    text = _extract_first_url(raw)
    if not text or _is_email_address(text):
        return "", ""

    if _CHANNEL_ID_RE.fullmatch(text):
        return text, channel_url(text)
    if text.startswith("@"):
        decoded = unquote(text).strip()
        if _HANDLE_RE.fullmatch(decoded):
            return decoded, channel_url(decoded)

    if "://" in text or is_youtube_host(text):
        # 若输入是视频链接，原样交由上层解析作者频道
        if looks_like_youtube_video(text):
            return "", normalize_youtube_url(text)
        key = clean_youtube_handle(text)
        if key:
            return key, channel_url(key)
        return "", ""

    # 裸句柄文本（如 MrBeast 或中文句柄）
    cleaned = unquote(text).strip().lstrip("@")
    if cleaned and not _is_email_address(cleaned) and re.fullmatch(r"[^\s/?#@]{2,64}", cleaned) and cleaned.lower() not in RESERVED_PATHS:
        if _CHANNEL_ID_RE.fullmatch(cleaned):
            return cleaned, channel_url(cleaned)
        return f"@{cleaned}", channel_url(f"@{cleaned}")
    return "", ""


async def resolve_youtube_target(
    raw: str,
    *,
    client=None,
) -> Tuple[str, str, str]:
    """统一解析 YouTube 监控目标，返回 `(channel_key, nickname, error)`。

    若用户输入的是视频链接且传入了 `client`，自动抓取视频详情页反查作者频道 ID / Handle。
    """
    text = str(raw or "").strip()
    if not text:
        return "", "", "请输入 YouTube 频道链接、@handle 或 UC 频道 ID"

    key, url = resolve_youtube_channel_target(text)
    if key:
        nick = key.lstrip("@")
        if client is not None:
            try:
                prof, _ = await client.fetch_channel_profile(key)
                if prof:
                    resolved_key = str(prof.get("sec_uid") or prof.get("unique_id") or prof.get("uid") or key).strip()
                    resolved_nick = str(prof.get("nickname") or nick).strip()
                    if resolved_key:
                        return resolved_key, resolved_nick, ""
            except Exception:
                pass
        return key, nick, ""

    vref = resolve_youtube_video_ref(text, allow_bare_id=False)
    if vref and client is not None:
        try:
            aw, raw_item, err = await client.fetch_video_detail(vref.watch_url)
            if aw is not None:
                ch_key = str(
                    (raw_item or {}).get("channel_handle")
                    or (raw_item or {}).get("channel_id")
                    or aw.author_unique_id
                    or ""
                ).strip()
                if ch_key:
                    return ch_key, aw.author_name or ch_key.lstrip("@"), ""
            if err:
                return "", "", f"无法从视频链接解析所属频道: {err}"
        except Exception as exc:
            return "", "", f"解析 YouTube 视频所属频道失败: {exc}"

    return "", "", "无法识别该 YouTube 频道地址，请粘贴频道链接（如 https://www.youtube.com/@handle 或 /channel/UC...）"
