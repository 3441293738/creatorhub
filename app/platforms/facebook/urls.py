"""Facebook 规范化链接、搜索编码与 GraphQL/DOM 路由常量。"""
from __future__ import annotations

import re
from urllib.parse import quote, quote_plus

FACEBOOK_BASE = "https://www.facebook.com"
FACEBOOK_MOBILE_BASE = "https://m.facebook.com"
MESSENGER_BASE = "https://www.messenger.com"

HOME_URL = "https://www.facebook.com/"
LOGIN_URL = "https://www.facebook.com/login/"
PROFILE_ME_URL = "https://www.facebook.com/profile.php"
FRIENDS_ME_URL = "https://www.facebook.com/me/friends"
MESSAGES_URL = "https://www.facebook.com/messages/t/"
PAGES_SYNC_URL = "https://www.facebook.com/pages/?category=your_pages&ref=bookmarks"
GROUPS_SYNC_URL = "https://www.facebook.com/groups/?category=groups"
GROUPS_JOINS_URL = "https://www.facebook.com/groups/joins/?nav_source=tab&ordering=viewer_added"
STORY_CREATE_URL = "https://www.facebook.com/stories/create"
SUPPORT_URL = "https://www.facebook.com/support"

# Facebook 公开群组搜索过滤器查询参数
PUBLIC_GROUPS_FILTER = (
    "%7B%22name%22%3A%22public_groups%22%2C%22args%22%3A%22%22%7D"
    "&filters=eyJncm91cHNfc2hvd19vbmx5Ijoie1wibmFtZVwiOlwicHVibGljX2dyb3Vwc1wiLFwiYXJnc1wiOlwiXCJ9In0%3D"
)

# 浏览器网络层 GraphQL / 业务接口拦截提示词（仅被动拦截页面响应，不伪造 doc_id）
GQL_ENDPOINT_HINTS = (
    "/api/graphql",
    "/api/graphqlbatch",
)

GQL_CREATE_POST_HINTS = (
    "ComposerStoryCreateMutation",
    "CometComposerStoryCreateMutation",
    "GroupCometComposerStoryCreateMutation",
    "ProfileCometComposerStoryCreateMutation",
    "story_create",
)

GQL_COMMENT_CREATE_HINTS = (
    "CometUFICreateCommentMutation",
    "UFICommentCreate",
    "comment_create",
)

GQL_REACTION_HINTS = (
    "CometUFIFeedbackReactMutation",
    "UFI2FeedbackReact",
    "feedback_react",
)


def fb_urlencode(text: str) -> str:
    """按 Facebook Web 搜索习惯编码关键词（小写十六进制转义）。"""
    encoded = quote_plus((text or "").strip(), safe="")
    return re.sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(), encoded)


def profile_url(user_ref: str) -> str:
    """根据数字 ID、`profile.php?id=...`、`groups/...` 或用户名构造主页 URL。"""
    raw = str(user_ref or "").strip().lstrip("@")
    if not raw:
        return PROFILE_ME_URL
    if raw.startswith(("http://", "https://")):
        return raw
    low = raw.lower()
    if low.startswith("profile.php"):
        return f"{FACEBOOK_BASE}/{raw.lstrip('/')}"
    if low.startswith("groups/"):
        return f"{FACEBOOK_BASE}/{raw.lstrip('/')}"
    if low.startswith("people/"):
        return f"{FACEBOOK_BASE}/{raw.lstrip('/')}"
    if raw.isdigit():
        return f"{FACEBOOK_BASE}/profile.php?id={raw}"
    return f"{FACEBOOK_BASE}/{quote(raw.strip('/'), safe='._-')}"


def group_url(group_ref: str) -> str:
    """构造 Facebook 群组 URL。"""
    raw = str(group_ref or "").strip()
    if not raw:
        return GROUPS_SYNC_URL
    if raw.startswith(("http://", "https://")):
        return raw.rstrip("/")
    cleaned = raw.lstrip("/")
    if cleaned.lower().startswith("groups/"):
        cleaned = cleaned[len("groups/"):]
    return f"{FACEBOOK_BASE}/groups/{cleaned.strip('/')}"


def group_my_posted_url(group_ref: str) -> str:
    """群组「我发布的内容」页面 URL。"""
    return f"{group_url(group_ref).rstrip('/')}/my_posted_content"


def friends_url(user_ref: str = "") -> str:
    raw = str(user_ref or "").strip().lstrip("@")
    if not raw or raw.lower() == "me":
        return FRIENDS_ME_URL
    if raw.isdigit():
        return f"{FACEBOOK_BASE}/profile.php?id={raw}&sk=friends"
    return f"{FACEBOOK_BASE}/{quote(raw.strip('/'), safe='._-')}/friends"


def followers_url(user_ref: str = "") -> str:
    raw = str(user_ref or "").strip().lstrip("@")
    if not raw or raw.lower() == "me":
        return f"{FACEBOOK_BASE}/me/followers"
    if raw.isdigit():
        return f"{FACEBOOK_BASE}/profile.php?id={raw}&sk=followers"
    return f"{FACEBOOK_BASE}/{quote(raw.strip('/'), safe='._-')}/followers"


def following_url(user_ref: str = "") -> str:
    raw = str(user_ref or "").strip().lstrip("@")
    if not raw or raw.lower() == "me":
        return f"{FACEBOOK_BASE}/me/following"
    if raw.isdigit():
        return f"{FACEBOOK_BASE}/profile.php?id={raw}&sk=following"
    return f"{FACEBOOK_BASE}/{quote(raw.strip('/'), safe='._-')}/following"


def post_url(post_id: str, author_id: str = "", *, kind: str = "post") -> str:
    """构造 Facebook 帖子/视频/Reels 规范链接。"""
    pid = str(post_id or "").strip()
    aid = str(author_id or "").strip().lstrip("@")
    k = str(kind or "post").strip().lower()
    if not pid:
        return profile_url(aid)
    if pid.startswith(("http://", "https://")):
        return pid
    if k == "reel":
        return f"{FACEBOOK_BASE}/reel/{pid}"
    if k == "video":
        if aid and not aid.isdigit():
            return f"{FACEBOOK_BASE}/{quote(aid, safe='._-')}/videos/{pid}"
        return f"{FACEBOOK_BASE}/watch/?v={pid}"
    if k == "photo":
        return f"{FACEBOOK_BASE}/photo/?fbid={pid}"
    if k == "group" and aid:
        return f"{FACEBOOK_BASE}/groups/{aid}/posts/{pid}"
    if aid:
        if aid.isdigit() and not pid.startswith("pfbid"):
            return f"{FACEBOOK_BASE}/permalink.php?story_fbid={pid}&id={aid}"
        if not aid.isdigit():
            return f"{FACEBOOK_BASE}/{quote(aid, safe='._-')}/posts/{pid}"
    if pid.startswith("pfbid"):
        return f"{FACEBOOK_BASE}/{pid}"
    return f"{FACEBOOK_BASE}/permalink.php?story_fbid={pid}"


def post_search_url(
    keyword: str,
    *,
    content_type: str = "all",
    recent: bool = False,
) -> str:
    """构造 Facebook 帖子 / 视频 / Reels 搜索 URL。"""
    encoded = fb_urlencode(keyword)
    ctype = (content_type or "all").strip().lower()
    if ctype == "video":
        return f"{FACEBOOK_BASE}/search/videos/?q={encoded}"
    if ctype == "images":
        return f"{FACEBOOK_BASE}/search/photos/?q={encoded}"
    base = f"{FACEBOOK_BASE}/search/posts/?q={encoded}"
    if recent:
        # recent_posts 过滤器编码（对应 Facebook Web「最新帖子」筛选）
        return base + "&filters=eyJyZWNlbnRfcG9zdHM6MCI6IntcIm5hbWVcIjpcInJlY2VudF9wb3N0c1wiLFwiYXJnc1wiOlwiXCJ9In0%3D"
    return base


def people_search_url(keyword: str) -> str:
    """构造用户搜索 URL。"""
    return f"{FACEBOOK_BASE}/search/people/?q={fb_urlencode(keyword)}"


def page_find_url(keyword: str) -> str:
    """构造公共主页搜索 URL。"""
    return f"{FACEBOOK_BASE}/search/pages/?q={fb_urlencode(keyword)}"


def group_find_url(
    keyword: str,
    *,
    public_only: bool = False,
    use_new_search: bool = True,
) -> str:
    """构造群组搜索 URL（支持仅筛选公开群组）。"""
    encoded = fb_urlencode(keyword)
    if use_new_search:
        if not public_only:
            return f"{FACEBOOK_BASE}/search/groups/?q={encoded}"
        return f"{FACEBOOK_BASE}/search/groups/?q={encoded}&filters_groups_show_only={PUBLIC_GROUPS_FILTER}"
    if not public_only:
        return f"{FACEBOOK_BASE}/search/groups/?q={encoded}"
    return f"{FACEBOOK_BASE}/search/open/groups-privacy/str/{encoded}/groups-named/intersect"


def dm_thread_url(thread_id: str) -> str:
    """构造 Messenger 会话 URL（`/messages/t/<thread_id>`）。"""
    tid = str(thread_id or "").strip().strip("/")
    if not tid:
        return MESSAGES_URL
    if tid.startswith(("http://", "https://")):
        return tid
    return f"{FACEBOOK_BASE}/messages/t/{quote(tid, safe='._-:')}"
