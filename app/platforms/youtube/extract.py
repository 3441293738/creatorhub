"""YouTube 数据提取与归一化模块：解析 SSR `ytInitialData` / `ytInitialPlayerResponse` 与 Innertube JSON。

支持归一化：
- 视频 / Shorts / 直播 (`Aweme`)
- 本账号作品 (`AccountWork` 字典)
- 频道资料、多频道切换列表 (`channel_switcher`) 与订阅列表 (`/feed/channels`)
- 评论与回复（兼容现代 `commentEntityPayload` 与旧版 `commentRenderer`）
- 关键词搜索（视频与频道）与 `continuation` 翻页令牌提取
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import html
import json
import re
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .protocol import parse_duration_seconds, parse_youtube_count
from .resolve import clean_youtube_handle, normalize_youtube_url, resolve_youtube_video_ref
from .urls import YOUTUBE_BASE, channel_url, video_url

if TYPE_CHECKING:
    from ..douyin.extract import Aweme, MediaItem

_RELATIVE_TIME_PATTERNS: Tuple[Tuple[re.Pattern[str], int], ...] = (
    (re.compile(r"(\d+)\s*(?:seconds?|secs?|s(?=\s+ago\b)|秒)", re.I), 1),
    (re.compile(r"(\d+)\s*(?:minutes?|mins?|m(?=\s+ago\b)|分钟|分)", re.I), 60),
    (re.compile(r"(\d+)\s*(?:hours?|hrs?|h(?=\s+ago\b)|小时|小時)", re.I), 3600),
    (re.compile(r"(\d+)\s*(?:days?|d(?=\s+ago\b)|天|日)", re.I), 86400),
    (re.compile(r"(\d+)\s*(?:weeks?|wks?|w(?=\s+ago\b)|周|週)", re.I), 7 * 86400),
    (re.compile(r"(\d+)\s*(?:months?|mos?|mo(?=\s+ago\b)|个月|個月|月)", re.I), 30 * 86400),
    (re.compile(r"(\d+)\s*(?:years?|yrs?|y(?=\s+ago\b)|年)", re.I), 365 * 86400),
)


def _to_int(val: Any) -> int:
    return parse_youtube_count(val)


def _text_of(node: Any) -> str:
    """从 YouTube `simpleText` / `runs` / `content` 文本对象安全提取纯文本。"""
    if node is None:
        return ""
    if isinstance(node, str):
        return node.strip()
    if isinstance(node, Mapping):
        if isinstance(node.get("simpleText"), str):
            return node["simpleText"].strip()
        if isinstance(node.get("content"), str):
            return node["content"].strip()
        if isinstance(node.get("text"), str):
            return node["text"].strip()
        runs = node.get("runs")
        if isinstance(runs, list):
            return "".join(str(r.get("text") or "") for r in runs if isinstance(r, Mapping)).strip()
    return ""


def _pick_thumbnail(node: Any) -> str:
    """从 YouTube `thumbnails` / `sources` 列表挑选分辨率最高的一张封面 URL。"""
    if isinstance(node, str):
        u = node.strip()
        if u.startswith("//"):
            return "https:" + u
        return u
    if not isinstance(node, Mapping):
        return ""
    items = (
        node.get("thumbnails")
        or node.get("sources")
        or (node.get("thumbnail") or {}).get("thumbnails")
        or (node.get("image") or {}).get("sources")
    )
    if isinstance(items, list) and items:
        best_url = ""
        best_w = -1
        for it in items:
            if not isinstance(it, Mapping):
                continue
            u = str(it.get("url") or "").strip()
            if not u:
                continue
            if u.startswith("//"):
                u = "https:" + u
            w = int(it.get("width") or 0)
            if w >= best_w:
                best_w = w
                best_url = u
        if best_url:
            return best_url
    for key in ("url", "src"):
        u = str(node.get(key) or "").strip()
        if u:
            return ("https:" + u) if u.startswith("//") else u
    return ""


def _extract_balanced_json(text: str, start_idx: int) -> Optional[Any]:
    """从字符串中 `start_idx` 处的 `{` 或 `[` 平衡解析一段 JSON 对象。"""
    n = len(text)
    if start_idx < 0 or start_idx >= n or text[start_idx] not in "{[":
        return None
    open_ch = text[start_idx]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_str = False
    esc = False
    for i in range(start_idx, n):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                blob = text[start_idx : i + 1]
                try:
                    return json.loads(blob)
                except Exception:
                    return None
    return None


def _extract_js_var_json(html_text: str, markers: Tuple[str, ...]) -> Optional[Dict[str, Any]]:
    raw = str(html_text or "")
    if not raw:
        return None
    for marker in markers:
        pos = 0
        while True:
            idx = raw.find(marker, pos)
            if idx < 0:
                break
            brace = raw.find("{", idx + len(marker))
            if brace >= 0 and brace - (idx + len(marker)) < 64:
                parsed = _extract_balanced_json(raw, brace)
                if isinstance(parsed, dict):
                    return parsed
            # 某些版本将 JSON 放在单引号字符串里 `var ytInitialData = '...';`
            quote_idx = idx + len(marker)
            while quote_idx < len(raw) and raw[quote_idx] in " \t\r\n=":
                quote_idx += 1
            if quote_idx < len(raw) and raw[quote_idx] in ("'", '"'):
                q = raw[quote_idx]
                end_q = quote_idx + 1
                esc = False
                while end_q < len(raw):
                    c = raw[end_q]
                    if esc:
                        esc = False
                    elif c == "\\":
                        esc = True
                    elif c == q:
                        break
                    end_q += 1
                if end_q < len(raw):
                    s = raw[quote_idx + 1 : end_q]
                    try:
                        decoded = bytes(s, "utf-8").decode("unicode_escape")
                        parsed = json.loads(decoded)
                        if isinstance(parsed, dict):
                            return parsed
                    except Exception:
                        pass
            pos = idx + len(marker)
    return None


def extract_yt_initial_data(html_text: str) -> Dict[str, Any]:
    """从 YouTube SSR HTML 提取 `ytInitialData` JSON 对象。"""
    data = _extract_js_var_json(
        html_text,
        (
            "var ytInitialData =",
            'window["ytInitialData"] =',
            "window['ytInitialData'] =",
            "ytInitialData =",
        ),
    )
    return data if isinstance(data, dict) else {}


def extract_yt_initial_player_response(html_text: str) -> Dict[str, Any]:
    """从 YouTube SSR HTML 提取 `ytInitialPlayerResponse` JSON 对象。"""
    data = _extract_js_var_json(
        html_text,
        (
            "var ytInitialPlayerResponse =",
            'window["ytInitialPlayerResponse"] =',
            "window['ytInitialPlayerResponse'] =",
            "ytInitialPlayerResponse =",
        ),
    )
    return data if isinstance(data, dict) else {}


def extract_ytcfg(html_text: str) -> Dict[str, Any]:
    """从 YouTube SSR HTML 提取 `ytcfg.set({...})` 配置（含 `INNERTUBE_API_KEY`、`INNERTUBE_CONTEXT`、`VISITOR_DATA`）。"""
    raw = str(html_text or "")
    out: Dict[str, Any] = {}
    pos = 0
    while True:
        idx = raw.find("ytcfg.set(", pos)
        if idx < 0:
            break
        brace = raw.find("{", idx + 10)
        if brace >= 0 and brace - idx < 24:
            obj = _extract_balanced_json(raw, brace)
            if isinstance(obj, dict):
                out.update(obj)
        pos = idx + 10
    return out


def parse_youtube_timestamp(val: Any) -> int:
    """将 YouTube Unix 时间戳、ISO8601 日期、英文绝对日期或相对时间字符串转为秒级时间戳。"""
    if val is None or val == "":
        return 0
    if isinstance(val, (int, float)):
        n = int(val)
        return n // 1000 if n > 100_000_000_000 else max(0, n)
    s = str(val).strip()
    if not s:
        return 0
    if s.isdigit():
        n = int(s)
        return n // 1000 if n > 100_000_000_000 else max(0, n)
    # ISO8601: 2025-11-01 or 2025-11-01T12:00:00Z
    iso_clean = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso_clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max(0, int(dt.timestamp()))
    except Exception:
        pass
    # 英文日期如 "Nov 15, 2025" / "Premiered Nov 15, 2025"
    m_date = re.search(r"([A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4})", s)
    if m_date:
        for fmt in ("%b %d, %Y", "%B %d, %Y"):
            try:
                dt = datetime.strptime(m_date.group(1), fmt).replace(tzinfo=timezone.utc)
                return max(0, int(dt.timestamp()))
            except ValueError:
                continue
    # 相对时间如 "2 days ago" / "3小时前" / "1 month ago"
    now = datetime.now(timezone.utc)
    for pat, unit_sec in _RELATIVE_TIME_PATTERNS:
        m = pat.search(s)
        if m:
            num = int(m.group(1))
            dt = now - timedelta(seconds=num * unit_sec)
            return max(0, int(dt.timestamp()))
    return 0


def _extract_continuation_tokens(payload: Any, *, prefer_comment: bool = False) -> List[str]:
    """递归提取 `continuationCommand.token` / `nextContinuationData.continuation`。"""
    tokens: List[str] = []
    seen: set[str] = set()
    stack: List[Any] = [payload]
    while stack:
        cur = stack.pop()
        if isinstance(cur, list):
            stack.extend(reversed(cur))
            continue
        if not isinstance(cur, Mapping):
            continue
        # 优先精确识别评论区 section
        if prefer_comment and cur.get("targetId") == "comments-section":
            sub_toks = _extract_continuation_tokens(cur, prefer_comment=False)
            for t in sub_toks:
                if t not in seen:
                    seen.add(t)
                    tokens.append(t)
            continue

        cc = cur.get("continuationCommand")
        if isinstance(cc, Mapping) and isinstance(cc.get("token"), str):
            tok = cc["token"].strip()
            if tok and tok not in seen:
                seen.add(tok)
                tokens.append(tok)
        ncd = cur.get("nextContinuationData")
        if isinstance(ncd, Mapping) and isinstance(ncd.get("continuation"), str):
            tok = ncd["continuation"].strip()
            if tok and tok not in seen:
                seen.add(tok)
                tokens.append(tok)
        for v in reversed(list(cur.values())):
            if isinstance(v, (Mapping, list)):
                stack.append(v)
    return tokens


def _normalize_video_renderer(node: Mapping[str, Any], *, default_author: str = "", default_channel_id: str = "", default_handle: str = "", default_avatar: str = "") -> Optional[Dict[str, Any]]:
    """将 `videoRenderer` / `gridVideoRenderer` / `playlistVideoRenderer` / `reelItemRenderer` / `shortsLockupViewModel` 转为统一扁平字典。"""
    # 1) 现代 Shorts 卡片 shortsLockupViewModel
    if "onTap" in node and ("overlayMetadata" in node or "entityId" in node):
        nav = (node.get("onTap") or {}).get("innertubeCommand") or {}
        reel_ep = nav.get("reelWatchEndpoint") or {}
        vid = str(reel_ep.get("videoId") or "").strip()
        if not vid:
            web_url = str((nav.get("commandMetadata") or {}).get("webCommandMetadata", {}).get("url") or "")
            ref = resolve_youtube_video_ref(web_url, allow_bare_id=False)
            vid = ref.video_id if ref else ""
        if not vid:
            return None
        om = node.get("overlayMetadata") or {}
        title = _text_of(om.get("primaryText")) or str(node.get("accessibilityText") or "").strip()
        views_text = _text_of(om.get("secondaryText"))
        cover = _pick_thumbnail(node.get("thumbnail")) or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
        return {
            "video_id": vid,
            "title": title,
            "desc": title,
            "cover": cover,
            "play_count": _to_int(views_text),
            "like_count": 0,
            "comment_count": 0,
            "duration": 0,
            "create_time": 0,
            "author_name": default_author,
            "channel_id": default_channel_id,
            "channel_handle": default_handle,
            "avatar": default_avatar,
            "is_short": True,
            "source_url": video_url(vid, is_short=True),
        }

    # 2) 现代频道/列表视频卡片 lockupViewModel (YouTube 2025-2026)
    if "contentImage" in node and ("contentId" in node or "contentType" in node):
        ctype = str(node.get("contentType") or "").upper()
        if ctype and ctype not in {"LOCKUP_CONTENT_TYPE_VIDEO", "LOCKUP_CONTENT_TYPE_SHORTS"}:
            return None
        on_tap = (((node.get("rendererContext") or {}).get("commandContext") or {}).get("onTap") or {}).get("innertubeCommand") or {}
        watch_ep = on_tap.get("watchEndpoint") or {}
        reel_ep = on_tap.get("reelWatchEndpoint") or {}
        vid = str(
            node.get("contentId")
            or watch_ep.get("videoId")
            or reel_ep.get("videoId")
            or ""
        ).strip()
        if not vid or len(vid) < 6:
            return None

        lmeta = (node.get("metadata") or {}).get("lockupMetadataViewModel") or {}
        title = _text_of(lmeta.get("title"))
        thumb_vm = (node.get("contentImage") or {}).get("thumbnailViewModel") or {}
        cover = _pick_thumbnail(thumb_vm.get("image")) or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"

        duration = 0
        for ov in (thumb_vm.get("overlays") or []):
            if not isinstance(ov, Mapping):
                continue
            bot = ov.get("thumbnailBottomOverlayViewModel") or {}
            for badge in (bot.get("badges") or []):
                tb = (badge or {}).get("thumbnailBadgeViewModel") or {}
                dur_txt = str(tb.get("text") or "").strip()
                if dur_txt and ":" in dur_txt:
                    duration = parse_duration_seconds(dur_txt)
                    if duration > 0:
                        break
            if duration > 0:
                break

        views = 0
        published_txt = ""
        create_time = 0
        author_name = default_author
        channel_id = default_channel_id
        channel_handle = default_handle
        avatar = default_avatar

        # 头像与频道链接（多频道混排流中存在）
        dec_av = ((lmeta.get("image") or {}).get("decoratedAvatarViewModel")) or {}
        if isinstance(dec_av, Mapping):
            av_url = _pick_thumbnail((dec_av.get("avatar") or {}).get("avatarViewModel"))
            if av_url and not avatar:
                avatar = av_url
            av_cmd = (((dec_av.get("rendererContext") or {}).get("commandContext") or {}).get("onTap") or {}).get("innertubeCommand") or {}
            be = av_cmd.get("browseEndpoint") or {}
            if not channel_id and be.get("browseId"):
                channel_id = str(be["browseId"]).strip()
            canon = str(be.get("canonicalBaseUrl") or "").strip()
            if canon and not channel_handle:
                channel_handle = clean_youtube_handle(f"{YOUTUBE_BASE}{canon}")

        rows = ((lmeta.get("metadata") or {}).get("contentMetadataViewModel") or {}).get("metadataRows") or []
        for row_idx, row in enumerate(rows):
            if not isinstance(row, Mapping):
                continue
            for part in (row.get("metadataParts") or []):
                if not isinstance(part, Mapping):
                    continue
                txt = _text_of(part.get("text"))
                acc_lbl = str(part.get("accessibilityLabel") or "").strip()
                icon_name = str(((part.get("leadingIcon") or {}).get("name")) or "").upper()
                combo = f"{acc_lbl} {txt}".lower()
                if not (txt or acc_lbl):
                    continue
                if icon_name == "PLAY_ARROW_OUTLINED" or any(k in combo for k in ("view", "观看", "觀看", "次播放")):
                    views = max(views, _to_int(txt or acc_lbl))
                    continue
                ts_cand = parse_youtube_timestamp(acc_lbl or txt) or parse_youtube_timestamp(txt)
                if ts_cand > 0:
                    create_time = ts_cand
                    published_txt = acc_lbl or txt
                    continue
                if row_idx == 0 and len(rows) > 1 and not author_name and txt:
                    author_name = txt

        nav_url = str(((on_tap.get("commandMetadata") or {}).get("webCommandMetadata") or {}).get("url") or "")
        is_short = ctype == "LOCKUP_CONTENT_TYPE_SHORTS" or "/shorts/" in nav_url or bool(reel_ep)
        return {
            "video_id": vid,
            "title": title,
            "desc": title or vid,
            "cover": cover,
            "play_count": views,
            "like_count": 0,
            "comment_count": 0,
            "duration": duration,
            "create_time": create_time,
            "published_text": published_txt,
            "author_name": author_name,
            "channel_id": channel_id,
            "channel_handle": channel_handle,
            "avatar": avatar,
            "is_short": is_short,
            "source_url": video_url(vid, is_short=is_short),
        }

    vid = str(node.get("videoId") or node.get("video_id") or "").strip()
    if not vid:
        nav = node.get("navigationEndpoint") or {}
        if isinstance(nav, Mapping):
            vid = str(
                (nav.get("watchEndpoint") or {}).get("videoId")
                or (nav.get("reelWatchEndpoint") or {}).get("videoId")
                or ""
            ).strip()
    if not vid or len(vid) < 6:
        return None

    title = _text_of(node.get("title")) or _text_of(node.get("headline"))
    desc_snippet = ""
    detailed = node.get("detailedMetadataSnippets")
    if isinstance(detailed, list) and detailed:
        desc_snippet = _text_of((detailed[0] or {}).get("snippetText"))
    if not desc_snippet:
        desc_snippet = _text_of(node.get("descriptionSnippet"))
    full_desc = title
    if desc_snippet and desc_snippet not in title:
        full_desc = f"{title}\n{desc_snippet}" if title else desc_snippet

    cover = _pick_thumbnail(node.get("thumbnail")) or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    duration = parse_duration_seconds(
        _text_of(node.get("lengthText"))
        or node.get("lengthSeconds")
        or node.get("duration")
    )
    views = _to_int(
        _text_of(node.get("viewCountText"))
        or _text_of(node.get("shortViewCountText"))
        or node.get("view_count")
        or node.get("play_count")
    )
    published_txt = _text_of(node.get("publishedTimeText")) or str(node.get("publish_date") or "")
    create_time = parse_youtube_timestamp(node.get("create_time") or published_txt)

    # 作者与频道信息
    owner_text = node.get("ownerText") or node.get("longBylineText") or node.get("shortBylineText") or {}
    author_name = _text_of(owner_text) or str(node.get("author_name") or default_author).strip()
    channel_id = str(node.get("channel_id") or default_channel_id).strip()
    channel_handle = str(node.get("channel_handle") or default_handle).strip()
    if isinstance(owner_text, Mapping) and isinstance(owner_text.get("runs"), list):
        for r in owner_text["runs"]:
            if not isinstance(r, Mapping):
                continue
            be = ((r.get("navigationEndpoint") or {}).get("browseEndpoint")) or {}
            if not channel_id and be.get("browseId"):
                channel_id = str(be["browseId"]).strip()
            canon = str(be.get("canonicalBaseUrl") or "").strip()
            if canon and not channel_handle:
                channel_handle = clean_youtube_handle(f"{YOUTUBE_BASE}{canon}")

    avatar = str(node.get("avatar") or default_avatar).strip()
    if not avatar:
        ch_thumb = node.get("channelThumbnailSupportedRenderers") or {}
        if isinstance(ch_thumb, Mapping):
            with_link = ch_thumb.get("channelThumbnailWithLinkRenderer") or {}
            avatar = _pick_thumbnail(with_link.get("thumbnail"))
        if not avatar:
            avatar = _pick_thumbnail(node.get("channelThumbnail"))

    nav_url = str(((node.get("navigationEndpoint") or {}).get("commandMetadata") or {}).get("webCommandMetadata", {}).get("url") or "")
    is_short = bool(node.get("is_short")) or "/shorts/" in nav_url or bool((node.get("navigationEndpoint") or {}).get("reelWatchEndpoint"))
    src_url = str(node.get("source_url") or "").strip() or video_url(vid, is_short=is_short)

    return {
        "video_id": vid,
        "title": title,
        "desc": full_desc or title or vid,
        "cover": cover,
        "play_count": views,
        "like_count": _to_int(node.get("like_count")),
        "comment_count": _to_int(node.get("comment_count")),
        "duration": duration,
        "create_time": create_time,
        "published_text": published_txt,
        "author_name": author_name,
        "channel_id": channel_id,
        "channel_handle": channel_handle,
        "avatar": avatar,
        "is_short": is_short,
        "source_url": src_url,
    }


def extract_channel_profile(initial_data: Mapping[str, Any]) -> Dict[str, Any]:
    """从频道主页 `ytInitialData` 提取频道 ID、@handle、标题、头像、订阅数、视频数与简介。"""
    if not isinstance(initial_data, Mapping):
        return {}
    meta = ((initial_data.get("metadata") or {}).get("channelMetadataRenderer")) or {}
    micro = ((initial_data.get("microformat") or {}).get("microformatDataRenderer")) or {}
    header = initial_data.get("header") or {}

    channel_id = str(meta.get("externalId") or "").strip()
    title = str(meta.get("title") or micro.get("title") or "").strip()
    bio = str(meta.get("description") or micro.get("description") or "").strip()
    avatar = _pick_thumbnail(meta.get("avatar")) or _pick_thumbnail(micro.get("thumbnail"))

    handle = ""
    vanity = str(meta.get("vanityChannelUrl") or micro.get("urlCanonical") or "").strip()
    if vanity:
        handle = clean_youtube_handle(vanity)

    subscribers = 0
    video_count = 0

    # 1) 旧版 c4TabbedHeaderRenderer
    c4 = header.get("c4TabbedHeaderRenderer")
    if isinstance(c4, Mapping):
        channel_id = channel_id or str(c4.get("channelId") or "").strip()
        title = title or str(c4.get("title") or "").strip()
        handle = handle or clean_youtube_handle(_text_of(c4.get("channelHandleText")))
        avatar = avatar or _pick_thumbnail(c4.get("avatar"))
        subscribers = subscribers or _to_int(_text_of(c4.get("subscriberCountText")))
        video_count = video_count or _to_int(_text_of(c4.get("videosCountText")))

    # 2) 现代 pageHeaderRenderer (2024-2026)
    phr = header.get("pageHeaderRenderer")
    if isinstance(phr, Mapping):
        title = title or str(phr.get("pageTitle") or "").strip()
        vm = (phr.get("content") or {}).get("pageHeaderViewModel") or {}
        if isinstance(vm, Mapping):
            t_vm = ((vm.get("title") or {}).get("dynamicTextViewModel") or {}).get("text")
            title = title or _text_of(t_vm)
            img_vm = vm.get("image") or {}
            dec = img_vm.get("decoratedAvatarViewModel") or {}
            av_vm = dec.get("avatar") or {}
            avatar = avatar or _pick_thumbnail(av_vm.get("avatarViewModel"))

            # metadataRows 包含 @handle、subscribers、videos 三段元数据
            meta_vm = (vm.get("metadata") or {}).get("contentMetadataViewModel") or {}
            for row in (meta_vm.get("metadataRows") or []):
                if not isinstance(row, Mapping):
                    continue
                for part in (row.get("metadataParts") or []):
                    txt = _text_of((part or {}).get("text"))
                    low = txt.lower()
                    if not txt:
                        continue
                    if txt.startswith("@") and not handle:
                        handle = txt.split()[0]
                    elif any(k in low for k in ("subscriber", "订阅", "訂閱", "位订阅者", "abonné", "suscriptor")):
                        subscribers = max(subscribers, _to_int(txt))
                    elif any(k in low for k in ("video", "视频", "影片", "個视频", "个视频")):
                        video_count = max(video_count, _to_int(txt))

            desc_vm = (vm.get("description") or {}).get("descriptionPreviewViewModel") or {}
            bio = bio or _text_of(desc_vm.get("description"))

    sec_uid = handle or channel_id
    if not sec_uid and not title:
        return {}
    return {
        "uid": channel_id,
        "sec_uid": sec_uid,
        "unique_id": handle,
        "douyin_id": handle or channel_id,
        "nickname": title or sec_uid.lstrip("@"),
        "avatar": avatar,
        "signature": bio,
        "follower_count": subscribers,
        "following_count": 0,
        "aweme_count": video_count,
        "total_favorited": 0,
    }


def extract_channel_videos(
    payload: Mapping[str, Any],
    *,
    default_author: str = "",
    default_channel_id: str = "",
    default_handle: str = "",
    default_avatar: str = "",
) -> Tuple[List[Dict[str, Any]], str]:
    """从频道 `/videos` 或 `/shorts` 页的 `ytInitialData` 或 `/youtubei/v1/browse` 续页响应提取视频列表与下一页 token。"""
    if not isinstance(payload, Mapping):
        return [], ""

    prof = extract_channel_profile(payload)
    author = default_author or str(prof.get("nickname") or "")
    cid = default_channel_id or str(prof.get("uid") or "")
    handle = default_handle or str(prof.get("unique_id") or "")
    avatar = default_avatar or str(prof.get("avatar") or "")

    items: List[Dict[str, Any]] = []
    seen: set[str] = set()
    stack: List[Any] = [payload]

    while stack:
        cur = stack.pop()
        if isinstance(cur, list):
            stack.extend(reversed(cur))
            continue
        if not isinstance(cur, Mapping):
            continue

        for key in ("videoRenderer", "gridVideoRenderer", "playlistVideoRenderer", "reelItemRenderer", "shortsLockupViewModel", "lockupViewModel"):
            sub = cur.get(key)
            if isinstance(sub, Mapping):
                row = _normalize_video_renderer(
                    sub,
                    default_author=author,
                    default_channel_id=cid,
                    default_handle=handle,
                    default_avatar=avatar,
                )
                if row and row["video_id"] not in seen:
                    seen.add(row["video_id"])
                    items.append(row)

        for v in reversed(list(cur.values())):
            if isinstance(v, (Mapping, list)):
                stack.append(v)

    toks = _extract_continuation_tokens(payload)
    next_token = toks[0] if toks else ""
    return items, next_token


def extract_search_videos(payload: Mapping[str, Any]) -> Tuple[List[Dict[str, Any]], str]:
    """从 `/results?search_query=...` 的 `ytInitialData` 或 `/youtubei/v1/search` 响应提取视频与续页 token。"""
    return extract_channel_videos(payload)


def extract_search_channels(payload: Mapping[str, Any]) -> Tuple[List[Dict[str, Any]], str]:
    """从搜索或订阅响应提取频道条目 (`channelRenderer`) 与续页 token。"""
    if not isinstance(payload, Mapping):
        return [], ""
    items: List[Dict[str, Any]] = []
    seen: set[str] = set()
    stack: List[Any] = [payload]

    while stack:
        cur = stack.pop()
        if isinstance(cur, list):
            stack.extend(reversed(cur))
            continue
        if not isinstance(cur, Mapping):
            continue

        cr = cur.get("channelRenderer") or cur.get("gridChannelRenderer")
        if isinstance(cr, Mapping):
            cid = str(cr.get("channelId") or "").strip()
            title = _text_of(cr.get("title"))
            canon = str(((cr.get("navigationEndpoint") or {}).get("browseEndpoint") or {}).get("canonicalBaseUrl") or "").strip()
            handle = clean_youtube_handle(f"{YOUTUBE_BASE}{canon}") if canon else ""
            key = cid or handle
            if key and key not in seen:
                seen.add(key)
                # YouTube 2024+ 把 @handle 放进 subscriberCountText，把订阅数放进 videoCountText
                sub_txt = _text_of(cr.get("subscriberCountText"))
                vid_txt = _text_of(cr.get("videoCountText"))
                if sub_txt.startswith("@") and not handle:
                    handle = sub_txt.split()[0]
                    subscribers = _to_int(vid_txt)
                    videos = 0
                else:
                    subscribers = _to_int(sub_txt)
                    videos = _to_int(vid_txt)
                items.append({
                    "uid": cid,
                    "sec_uid": handle or cid,
                    "unique_id": handle,
                    "douyin_id": handle or cid,
                    "nickname": title or handle.lstrip("@") or cid,
                    "avatar": _pick_thumbnail(cr.get("thumbnail")),
                    "signature": _text_of(cr.get("descriptionSnippet")),
                    "follower_count": subscribers,
                    "aweme_count": videos,
                    "url": channel_url(handle or cid),
                })

        for v in reversed(list(cur.values())):
            if isinstance(v, (Mapping, list)):
                stack.append(v)

    toks = _extract_continuation_tokens(payload)
    next_token = toks[0] if toks else ""
    return items, next_token


extract_subscriptions = extract_search_channels


def extract_video_detail(
    initial_data: Mapping[str, Any],
    player_response: Mapping[str, Any],
    *,
    source_url: str = "",
) -> Tuple[Optional[Dict[str, Any]], str]:
    """合并 `/watch?v=...` 的 `ytInitialData` 与 `ytInitialPlayerResponse`，返回 `(video_dict, comments_continuation)`。"""
    vd = (player_response.get("videoDetails") or {}) if isinstance(player_response, Mapping) else {}
    mf = ((player_response.get("microformat") or {}).get("playerMicroformatRenderer") or {}) if isinstance(player_response, Mapping) else {}
    streaming = (player_response.get("streamingData") or {}) if isinstance(player_response, Mapping) else {}

    vid = str(vd.get("videoId") or "").strip()
    if not vid and source_url:
        ref = resolve_youtube_video_ref(source_url, allow_bare_id=True)
        vid = ref.video_id if ref else ""
    if not vid:
        return None, ""

    title = str(vd.get("title") or _text_of(mf.get("title")) or "").strip()
    short_desc = str(vd.get("shortDescription") or _text_of(mf.get("description")) or "").strip()
    author_name = str(vd.get("author") or mf.get("ownerChannelName") or "").strip()
    channel_id = str(vd.get("channelId") or mf.get("externalChannelId") or "").strip()
    channel_handle = ""
    owner_url = str(mf.get("ownerProfileUrl") or "").strip()
    if owner_url:
        channel_handle = clean_youtube_handle(owner_url)

    duration = parse_duration_seconds(vd.get("lengthSeconds") or mf.get("lengthSeconds") or 0)
    views = _to_int(vd.get("viewCount") or mf.get("viewCount") or 0)
    cover = _pick_thumbnail(vd.get("thumbnail")) or _pick_thumbnail(mf.get("thumbnail")) or f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg"
    create_time = parse_youtube_timestamp(mf.get("publishDate") or mf.get("uploadDate") or 0)

    like_count = 0
    comment_count = 0
    avatar = ""

    # 从 initial_data 提取精确点赞数、评论数、作者头像与 handle
    if isinstance(initial_data, Mapping):
        stack: List[Any] = [initial_data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, list):
                stack.extend(cur)
                continue
            if not isinstance(cur, Mapping):
                continue
            if "videoOwnerRenderer" in cur and isinstance(cur["videoOwnerRenderer"], Mapping):
                vor = cur["videoOwnerRenderer"]
                avatar = avatar or _pick_thumbnail(vor.get("thumbnail"))
                canon = str(((vor.get("navigationEndpoint") or {}).get("browseEndpoint") or {}).get("canonicalBaseUrl") or "")
                if canon and not channel_handle:
                    channel_handle = clean_youtube_handle(f"{YOUTUBE_BASE}{canon}")
            if "likeButtonViewModel" in cur and isinstance(cur["likeButtonViewModel"], Mapping):
                lb = cur["likeButtonViewModel"]
                toggled = ((lb.get("toggleButtonViewModel") or {}).get("toggleButtonViewModel") or {})
                def_btn = ((toggled.get("defaultButtonViewModel") or {}).get("buttonViewModel") or {})
                like_count = max(like_count, _to_int(def_btn.get("title") or def_btn.get("accessibilityText")))
            if "segmentedLikeDislikeButtonRenderer" in cur and isinstance(cur["segmentedLikeDislikeButtonRenderer"], Mapping):
                sl = cur["segmentedLikeDislikeButtonRenderer"]
                lbr = (sl.get("likeButton") or {}).get("toggleButtonRenderer") or {}
                like_count = max(like_count, _to_int(_text_of(lbr.get("defaultText")) or ((lbr.get("accessibilityData") or {}).get("accessibilityData") or {}).get("label")))
            if "commentsEntryPointHeaderRenderer" in cur and isinstance(cur["commentsEntryPointHeaderRenderer"], Mapping):
                ce = cur["commentsEntryPointHeaderRenderer"]
                comment_count = max(comment_count, _to_int(_text_of(ce.get("commentCount"))))
            if cur.get("panelIdentifier") == "engagement-panel-comments-section":
                hdr = ((cur.get("header") or {}).get("engagementPanelTitleHeaderRenderer")) or {}
                if isinstance(hdr, Mapping):
                    comment_count = max(comment_count, _to_int(_text_of(hdr.get("contextualInfo"))))
            for v in cur.values():
                if isinstance(v, (Mapping, list)):
                    stack.append(v)

    # 提取可用直链流（若 playerResponse 返回了未加密 URL；加密流交由 yt-dlp 无损下载）
    media_urls: List[Dict[str, Any]] = []
    formats = streaming.get("formats") or []
    if isinstance(formats, list):
        for fmt in formats:
            if not isinstance(fmt, Mapping):
                continue
            u = str(fmt.get("url") or "").strip()
            if u.startswith("http"):
                media_urls.append({
                    "url": u,
                    "quality": str(fmt.get("qualityLabel") or fmt.get("quality") or ""),
                    "mime": str(fmt.get("mimeType") or ""),
                    "height": int(fmt.get("height") or 0),
                })

    comments_toks = _extract_continuation_tokens(initial_data, prefer_comment=True) if isinstance(initial_data, Mapping) else []
    comments_token = comments_toks[0] if comments_toks else ""

    desc = title
    if short_desc and short_desc != title:
        desc = f"{title}\n\n{short_desc}" if title else short_desc

    is_short = "/shorts/" in str(source_url or "").lower()
    canon_url = video_url(vid, is_short=is_short)
    return {
        "video_id": vid,
        "title": title,
        "desc": desc or vid,
        "cover": cover,
        "play_count": views,
        "like_count": like_count,
        "comment_count": comment_count,
        "duration": duration,
        "create_time": create_time,
        "author_name": author_name,
        "channel_id": channel_id,
        "channel_handle": channel_handle,
        "avatar": avatar,
        "is_short": is_short,
        "source_url": canon_url,
        "direct_formats": media_urls,
        "keywords": list(vd.get("keywords") or []) if isinstance(vd.get("keywords"), list) else [],
    }, comments_token


def extract_comments(payload: Mapping[str, Any], *, video_id: str = "") -> Tuple[List[Dict[str, Any]], str]:
    """从 `/youtubei/v1/next` 或页面 `ytInitialData` 提取评论列表与下一页 continuation token。

    同时支持：
    1. 2024-2026 现代结构：`frameworkUpdates.entityBatchUpdate.mutations[].payload.commentEntityPayload`
    2. 传统结构：`commentThreadRenderer.comment.commentRenderer`
    """
    if not isinstance(payload, Mapping):
        return [], ""

    comments: List[Dict[str, Any]] = []
    seen: set[str] = set()

    # 1) 现代 commentEntityPayload
    mutations = (((payload.get("frameworkUpdates") or {}).get("entityBatchUpdate") or {}).get("mutations")) or []
    if isinstance(mutations, list):
        for mut in mutations:
            if not isinstance(mut, Mapping):
                continue
            cep = (mut.get("payload") or {}).get("commentEntityPayload")
            if not isinstance(cep, Mapping):
                continue
            props = cep.get("properties") or {}
            author = cep.get("author") or {}
            toolbar = cep.get("toolbar") or {}
            cid = str(props.get("commentId") or "").strip()
            text = _text_of(props.get("content"))
            if not cid or not text or cid in seen:
                continue
            seen.add(cid)
            nick = str(author.get("displayName") or "").strip()
            uid = str(author.get("channelId") or nick).strip()
            parent = cid.split(".", 1)[0] if "." in cid else ""
            comments.append({
                "comment_id": cid,
                "text": html.unescape(text),
                "user_nickname": nick or uid or cid,
                "user_sec_uid": uid,
                "ip_label": "",
                "like_count": _to_int(toolbar.get("likeCountNotliked") or toolbar.get("likeCountLiked") or toolbar.get("likeCountA11y")),
                "create_time": parse_youtube_timestamp(props.get("publishedTime")),
                "reply_to": parent,
                "comment_url": video_url(video_id, comment_id=cid) if video_id else "",
            })

    # 2) 传统 commentRenderer 兜底
    stack: List[Any] = [payload]
    while stack:
        cur = stack.pop()
        if isinstance(cur, list):
            stack.extend(reversed(cur))
            continue
        if not isinstance(cur, Mapping):
            continue
        cr = cur.get("commentRenderer")
        if isinstance(cr, Mapping):
            cid = str(cr.get("commentId") or "").strip()
            text = _text_of(cr.get("contentText"))
            if cid and text and cid not in seen:
                seen.add(cid)
                nick = _text_of(cr.get("authorText"))
                uid = str(((cr.get("authorEndpoint") or {}).get("browseEndpoint") or {}).get("browseId") or nick).strip()
                parent = cid.split(".", 1)[0] if "." in cid else ""
                comments.append({
                    "comment_id": cid,
                    "text": html.unescape(text),
                    "user_nickname": nick or uid or cid,
                    "user_sec_uid": uid,
                    "ip_label": "",
                    "like_count": _to_int(cr.get("voteCount") and _text_of(cr.get("voteCount")) or cr.get("likeCount")),
                    "create_time": parse_youtube_timestamp(_text_of(cr.get("publishedTimeText"))),
                    "reply_to": parent,
                    "comment_url": video_url(video_id, comment_id=cid) if video_id else "",
                })
        for v in reversed(list(cur.values())):
            if isinstance(v, (Mapping, list)):
                stack.append(v)

    toks = _extract_continuation_tokens(payload)
    next_token = toks[0] if toks else ""
    return comments, next_token


def extract_managed_channels(payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """从 `/channel_switcher` `ytInitialData` 或 `account_menu` 提取当前 Google 账号下的所有可选频道。"""
    if not isinstance(payload, Mapping):
        return []
    channels: List[Dict[str, Any]] = []
    seen: set[str] = set()
    stack: List[Any] = [payload]

    while stack:
        cur = stack.pop()
        if isinstance(cur, list):
            stack.extend(reversed(cur))
            continue
        if not isinstance(cur, Mapping):
            continue

        air = cur.get("accountItemRenderer") or cur.get("accountItem")
        if isinstance(air, Mapping):
            name = _text_of(air.get("accountName"))
            handle = _text_of(air.get("channelHandle"))
            byline = ""
            bylines = air.get("accountByline") or []
            if isinstance(bylines, list):
                byline = " ".join(_text_of(b) for b in bylines if _text_of(b)).strip()
            ep = air.get("serviceEndpoint") or air.get("navigationEndpoint") or {}
            ss = ep.get("selectActiveIdentityEndpoint") or {}
            channel_id = ""
            for tok in (ss.get("supportedTokens") or []):
                if not isinstance(tok, Mapping):
                    continue
                obf = (tok.get("accountSigninToken") or {}).get("signinUrl") or ""
                if obf and not channel_id:
                    channel_id = str(obf)
                pg = (tok.get("pageIdToken") or {}).get("pageId") or ""
                if pg:
                    channel_id = str(pg)
            avatar = _pick_thumbnail(air.get("accountPhoto"))
            is_selected = bool(air.get("isSelected", False))
            key = handle or name or channel_id
            if name and key and key not in seen:
                seen.add(key)
                channels.append({
                    "name": name,
                    "handle": handle,
                    "channel_id": channel_id,
                    "link": channel_url(handle or channel_id) if (handle or channel_id) else YOUTUBE_BASE,
                    "subscribers_text": byline,
                    "subscribers_count": _to_int(byline),
                    "avatar": avatar,
                    "is_selected": is_selected,
                })

        for v in reversed(list(cur.values())):
            if isinstance(v, (Mapping, list)):
                stack.append(v)
    return channels


def extract_account_menu_self(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """从 `/youtubei/v1/account/account_menu` 响应提取当前登录账号资料。"""
    if not isinstance(payload, Mapping):
        return {}
    name = ""
    handle = ""
    email = ""
    avatar = ""
    channel_id = ""

    stack: List[Any] = [payload]
    while stack:
        cur = stack.pop()
        if isinstance(cur, list):
            stack.extend(reversed(cur))
            continue
        if not isinstance(cur, Mapping):
            continue
        hdr = cur.get("activeAccountHeaderRenderer")
        if isinstance(hdr, Mapping):
            name = name or _text_of(hdr.get("accountName"))
            raw_h = _text_of(hdr.get("channelHandle"))
            if raw_h and not ("@" in raw_h and not raw_h.startswith("@") and "." in raw_h.split("@", 1)[1]):
                handle = handle or clean_youtube_handle(raw_h)
            email = email or _text_of(hdr.get("email"))
            avatar = avatar or _pick_thumbnail(hdr.get("accountPhoto"))
            mc = hdr.get("manageAccountTitle") or {}
            # 某些结构中 browseId 在 channel 链接里
            for v in hdr.values():
                if isinstance(v, Mapping):
                    be = (v.get("navigationEndpoint") or {}).get("browseEndpoint") or {}
                    if be.get("browseId") and str(be["browseId"]).startswith("UC"):
                        channel_id = str(be["browseId"])
        # 也从「查看你的频道」菜单项读取 UC 频道 ID / @handle
        if "compactLinkRenderer" in cur and isinstance(cur["compactLinkRenderer"], Mapping):
            clr = cur["compactLinkRenderer"]
            be = (clr.get("navigationEndpoint") or {}).get("browseEndpoint") or {}
            bid = str(be.get("browseId") or "").strip()
            canon = str(be.get("canonicalBaseUrl") or "").strip()
            if bid.startswith("UC") and not channel_id:
                channel_id = bid
            if canon.startswith("/@") and not handle:
                handle = clean_youtube_handle(canon)
        for v in reversed(list(cur.values())):
            if isinstance(v, (Mapping, list)):
                stack.append(v)

    if not (name or handle or channel_id or email):
        return {}
    return {
        "uid": channel_id,
        "sec_uid": handle or channel_id,
        "unique_id": handle,
        "douyin_id": handle or channel_id,
        "nickname": name or handle.lstrip("@") or email,
        "avatar": avatar,
        "email": email,
        "follower_count": 0,
        "following_count": 0,
        "aweme_count": 0,
        "total_favorited": 0,
    }


def parse_youtube_aweme(raw: Any, quality: str = "highest") -> Optional["Aweme"]:
    """将归一化后的 YouTube 视频字典转换为 CreatorHub `Aweme` 对象。"""
    from ..douyin.extract import Aweme, MediaItem

    if not isinstance(raw, Mapping):
        return None
    vid = str(raw.get("video_id") or raw.get("aweme_id") or raw.get("item_id") or "").strip()
    if not vid:
        src = str(raw.get("source_url") or raw.get("url") or "").strip()
        ref = resolve_youtube_video_ref(src, allow_bare_id=False)
        vid = ref.video_id if ref else ""
    if not vid:
        return None

    is_short = bool(raw.get("is_short", False))
    src_url = str(raw.get("source_url") or raw.get("url") or "").strip() or video_url(vid, is_short=is_short)
    cover = str(raw.get("cover") or raw.get("cover_url") or "").strip() or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    desc = str(raw.get("desc") or raw.get("title") or vid).strip()
    author_name = str(raw.get("author_name") or raw.get("nickname") or "").strip()
    author_uid = str(raw.get("channel_handle") or raw.get("channel_id") or raw.get("author_unique_id") or "").strip()

    medias: List[MediaItem] = []
    q_label = ""
    direct_formats = raw.get("direct_formats") or []
    if isinstance(direct_formats, list) and direct_formats:
        # 按画质偏好挑选直链（通常为 360p/720p progressive；高清自适应流交由 download_jobs 的 yt-dlp 无损合并）
        sorted_fmts = sorted(
            [f for f in direct_formats if isinstance(f, Mapping) and str(f.get("url") or "").startswith("http")],
            key=lambda x: int(x.get("height") or 0),
            reverse=(quality != "lowest"),
        )
        if sorted_fmts:
            picked = sorted_fmts[0]
            q_label = str(picked.get("quality") or "")
            medias.append(MediaItem(url=str(picked["url"]), kind="video", ext="mp4", index=0))

    # 若无直接流 URL，仍以 watch URL 占位（download_jobs / share_downloader 会用 yt-dlp 解析最高清无水印原流）
    if not medias:
        medias.append(MediaItem(url=src_url, kind="video", ext="mp4", index=0))

    return Aweme(
        aweme_id=vid,
        desc=desc,
        create_time=parse_youtube_timestamp(raw.get("create_time")),
        author_name=author_name,
        media_type="video",
        medias=medias,
        cover=cover,
        quality_label=q_label or ("shorts" if is_short else ""),
        like_count=_to_int(raw.get("like_count")),
        comment_count=_to_int(raw.get("comment_count")),
        duration=parse_duration_seconds(raw.get("duration")),
        avatar=str(raw.get("avatar") or ""),
        platform="youtube",
        source_url=src_url,
        author_unique_id=author_uid,
    )


def parse_youtube_creator_work(raw: Any) -> Optional[Dict[str, Any]]:
    """转换为本账号作品 (`AccountWork`) 扁平字典。"""
    aw = parse_youtube_aweme(raw)
    if aw is None:
        return None
    node = raw if isinstance(raw, Mapping) else {}
    return {
        "item_id": aw.aweme_id,
        "desc": aw.desc,
        "media_type": "video",
        "cover_url": aw.cover or "",
        "create_time": aw.create_time,
        "like_count": _to_int(node.get("like_count")) or aw.like_count,
        "comment_count": _to_int(node.get("comment_count")) or aw.comment_count,
        "collect_count": 0,
        "share_count": 0,
        "play_count": _to_int(node.get("play_count") or node.get("view_count")),
        "status": str(node.get("status") or "公开"),
    }


def parse_youtube_comment(raw: Any, *, reply_to: str = "") -> Optional[Dict[str, Any]]:
    """将 YouTube 评论字典归一为 `CommentRecord` 兼容格式。"""
    if not isinstance(raw, Mapping):
        return None
    cid = str(raw.get("comment_id") or raw.get("id") or "").strip()
    text = str(raw.get("text") or "").strip()
    if not text and not cid:
        return None
    if not cid:
        cid = hashlib.md5(f"{raw.get('user_nickname', '')}:{text}".encode("utf-8")).hexdigest()[:16]
    nick = str(raw.get("user_nickname") or raw.get("author") or "").strip()
    uid = str(raw.get("user_sec_uid") or raw.get("channel_id") or nick).strip()
    parent = str(reply_to or raw.get("reply_to") or (cid.split(".", 1)[0] if "." in cid else "")).strip()
    return {
        "comment_id": cid,
        "text": html.unescape(text),
        "user_nickname": nick or uid or cid,
        "user_sec_uid": uid,
        "ip_label": str(raw.get("ip_label") or "")[:40],
        "like_count": _to_int(raw.get("like_count")),
        "create_time": parse_youtube_timestamp(raw.get("create_time")),
        "reply_to": parent,
    }


def parse_youtube_user(raw: Any) -> Dict[str, Any]:
    """将 YouTube 频道 / 账号资料归一为 CreatorHub 账号/创作者资料字典。"""
    if not isinstance(raw, Mapping):
        return {}
    uid = str(raw.get("uid") or raw.get("channel_id") or "").strip()
    handle = str(raw.get("unique_id") or raw.get("handle") or raw.get("channel_handle") or "").strip()
    if handle and not handle.startswith("@") and not handle.startswith("UC"):
        handle = f"@{handle}"
    sec_uid = str(raw.get("sec_uid") or handle or uid).strip()
    name = str(raw.get("nickname") or raw.get("name") or raw.get("title") or handle.lstrip("@") or uid).strip()
    return {
        "uid": uid,
        "sec_uid": sec_uid,
        "unique_id": handle,
        "douyin_id": handle or uid or sec_uid,
        "nickname": name,
        "avatar": str(raw.get("avatar") or "").strip(),
        "signature": str(raw.get("signature") or raw.get("bio") or "").strip(),
        "follower_count": _to_int(raw.get("follower_count") or raw.get("subscribers") or raw.get("subscribers_count")),
        "following_count": _to_int(raw.get("following_count") or raw.get("subscriptions_count")),
        "aweme_count": _to_int(raw.get("aweme_count") or raw.get("video_count")),
        "total_favorited": _to_int(raw.get("total_favorited")),
        "channels": list(raw.get("channels") or []) if isinstance(raw.get("channels"), list) else [],
    }


parse_youtube_self_user = parse_youtube_user
