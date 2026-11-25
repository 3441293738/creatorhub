"""YouTube 规范化链接、搜索过滤器与 Studio/Innertube 路由常量。"""
from __future__ import annotations

from urllib.parse import quote, quote_plus

YOUTUBE_BASE = "https://www.youtube.com"
YOUTUBE_MOBILE_BASE = "https://m.youtube.com"
YOUTUBE_STUDIO_BASE = "https://studio.youtube.com"
YOUTUBE_SHORT_BASE = "https://youtu.be"

HOME_URL = "https://www.youtube.com/"
LOGIN_URL = "https://accounts.google.com/ServiceLogin?service=youtube&continue=https%3A%2F%2Fwww.youtube.com%2F"
ACCOUNT_URL = "https://www.youtube.com/account"
CHANNEL_SWITCHER_URL = "https://www.youtube.com/channel_switcher"
CHANNEL_SWITCHER_NEXT_URL = "https://www.youtube.com/channel_switcher?next=%2Faccount&feature=settings"
SUBSCRIPTIONS_URL = "https://www.youtube.com/feed/channels"
SUBSCRIPTION_MANAGER_URL = "https://www.youtube.com/subscription_manager"
UPLOAD_URL = "https://www.youtube.com/upload"
STUDIO_CHANNEL_URL = "https://studio.youtube.com/channel/"
STUDIO_VIDEOS_URL = "https://studio.youtube.com/channel/UC/videos/upload"

# YouTube Innertube Web API 端点（仅随页面会话/公开 SSR 配合使用）
INNERTUBE_BASE = "https://www.youtube.com/youtubei/v1"
INNERTUBE_SEARCH_URL = f"{INNERTUBE_BASE}/search"
INNERTUBE_BROWSE_URL = f"{INNERTUBE_BASE}/browse"
INNERTUBE_NEXT_URL = f"{INNERTUBE_BASE}/next"
INNERTUBE_PLAYER_URL = f"{INNERTUBE_BASE}/player"
INNERTUBE_ACCOUNT_MENU_URL = f"{INNERTUBE_BASE}/account/account_menu"

# 浏览器网络层被动拦截提示词
INNERTUBE_ENDPOINT_HINTS = (
    "/youtubei/v1/search",
    "/youtubei/v1/browse",
    "/youtubei/v1/next",
    "/youtubei/v1/player",
    "/youtubei/v1/comment/create_comment",
    "/youtubei/v1/comment/create_comment_reply",
    "/youtubei/v1/like/",
    "/youtubei/v1/subscription/",
)

# 常用 YouTube 搜索 `sp` Protobuf 参数（涵盖上传时间、时长、类型与排序）
SEARCH_SP_TYPE = {
    "video": "EgIQAQ%3D%3D",
    "channel": "EgIQAg%3D%3D",
    "playlist": "EgIQAw%3D%3D",
    "shorts": "EgIYAQ%3D%3D",
}

SEARCH_SP_UPLOAD_DATE = {
    "hour": "EgIIAQ%3D%3D",
    "today": "EgIIAg%3D%3D",
    "week": "EgIIAw%3D%3D",
    "month": "EgIIBA%3D%3D",
    "year": "EgIIBQ%3D%3D",
    # 兼容数值索引映射
    "1": "EgIIAQ%3D%3D",
    "2": "EgIIAg%3D%3D",
    "3": "EgIIAw%3D%3D",
    "4": "EgIIBA%3D%3D",
    "5": "EgIIBQ%3D%3D",
}

SEARCH_SP_DURATION = {
    "short": "EgIYAQ%3D%3D",     # < 4 min
    "medium": "EgIYAw%3D%3D",    # 4 - 20 min
    "long": "EgIYAg%3D%3D",      # > 20 min
    "1": "EgIYAQ%3D%3D",
    "2": "EgIYAg%3D%3D",
    "3": "EgIYAw%3D%3D",
}

SEARCH_SP_SORT_BY = {
    "relevance": "",
    "date": "CAI%3D",
    "upload_date": "CAI%3D",
    "views": "CAMSAhAB",
    "view_count": "CAMSAhAB",
    "rating": "CAE%3D",
    "1": "CAI%3D",
    "2": "CAMSAhAB",
    "3": "CAE%3D",
}


def yt_urlencode(text: str) -> str:
    return quote_plus((text or "").strip(), safe="")


def video_url(video_id: str, *, is_short: bool = False, comment_id: str = "") -> str:
    """构造 YouTube 视频 / Shorts / 定位评论规范 URL。"""
    vid = str(video_id or "").strip()
    if not vid:
        return HOME_URL
    if vid.startswith(("http://", "https://")):
        return vid
    if is_short and not comment_id:
        return f"{YOUTUBE_BASE}/shorts/{quote(vid, safe='_-')}"
    base = f"{YOUTUBE_BASE}/watch?v={quote(vid, safe='_-')}"
    if comment_id:
        base += f"&lc={quote(str(comment_id).strip(), safe='._-')}"
    return base


def shorts_url(video_id: str) -> str:
    vid = str(video_id or "").strip()
    if not vid:
        return HOME_URL
    if vid.startswith(("http://", "https://")):
        return vid
    return f"{YOUTUBE_BASE}/shorts/{quote(vid, safe='_-')}"


def channel_url(channel_ref: str, *, tab: str = "") -> str:
    """根据 `UC...` 频道 ID、`@handle`、`/c/...`、`/user/...` 或完整 URL 构造频道链接。"""
    raw = str(channel_ref or "").strip()
    if not raw:
        return HOME_URL
    if raw.startswith(("http://", "https://")):
        base = raw.rstrip("/")
    elif raw.startswith("@"):
        base = f"{YOUTUBE_BASE}/{quote(raw, safe='@._-')}"
    elif raw.startswith("UC") and len(raw) >= 16:
        base = f"{YOUTUBE_BASE}/channel/{quote(raw, safe='_-')}"
    elif raw.startswith(("channel/", "c/", "user/")):
        base = f"{YOUTUBE_BASE}/{raw.strip('/')}"
    else:
        base = f"{YOUTUBE_BASE}/@{quote(raw.lstrip('@'), safe='._-')}"

    clean_tab = str(tab or "").strip().lower().lstrip("/")
    if clean_tab in {"videos", "shorts", "streams", "live", "playlists", "community", "about", "featured"}:
        if base.rstrip("/").endswith(f"/{clean_tab}"):
            return base
        # 去掉末尾已有 tab 再拼接
        for existing in ("/videos", "/shorts", "/streams", "/live", "/playlists", "/community", "/about", "/featured"):
            if base.lower().endswith(existing):
                base = base[: -len(existing)]
                break
        return f"{base.rstrip('/')}/{clean_tab}"
    return base


def search_url(
    keyword: str,
    *,
    search_type: str = "video",
    upload_date: str = "",
    duration: str = "",
    sort_by: str = "",
    raw_query_concat: bool = False,
) -> str:
    """构造 YouTube 搜索页 URL（支持类型/上传时间/时长/排序过滤）。"""
    q = (keyword or "").strip()
    if raw_query_concat:
        base = f"{YOUTUBE_BASE}/results?search_query={q}"
    else:
        base = f"{YOUTUBE_BASE}/results?search_query={yt_urlencode(q)}"

    sp = ""
    st = str(search_type or "").strip().lower()
    ud = str(upload_date or "").strip().lower()
    du = str(duration or "").strip().lower()
    sb = str(sort_by or "").strip().lower()

    if st == "channel":
        sp = SEARCH_SP_TYPE["channel"]
    elif st == "playlist":
        sp = SEARCH_SP_TYPE["playlist"]
    elif ud and ud in SEARCH_SP_UPLOAD_DATE:
        sp = SEARCH_SP_UPLOAD_DATE[ud]
    elif du and du in SEARCH_SP_DURATION:
        sp = SEARCH_SP_DURATION[du]
    elif sb and sb in SEARCH_SP_SORT_BY and SEARCH_SP_SORT_BY[sb]:
        sp = SEARCH_SP_SORT_BY[sb]
    elif st in SEARCH_SP_TYPE:
        sp = SEARCH_SP_TYPE[st]

    return f"{base}&sp={sp}" if sp else base


def channel_search_url(keyword: str) -> str:
    return search_url(keyword, search_type="channel")


def playlist_url(playlist_id: str) -> str:
    pid = str(playlist_id or "").strip()
    if not pid:
        return HOME_URL
    if pid.startswith(("http://", "https://")):
        return pid
    return f"{YOUTUBE_BASE}/playlist?list={quote(pid, safe='_-')}"
