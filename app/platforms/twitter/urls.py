"""X (Twitter) 规范化链接、搜索编码与 GraphQL/REST 响应路由常量。"""
from __future__ import annotations

from urllib.parse import quote

TWITTER_BASE = "https://x.com"
TWITTER_LEGACY_BASE = "https://twitter.com"
HOME_URL = "https://x.com/home"
EXPLORE_URL = "https://x.com/explore"
LOGIN_URL = "https://x.com/i/flow/login"
COMPOSE_URL = "https://x.com/compose/post"
MESSAGES_URL = "https://x.com/messages"
SETTINGS_PROFILE_URL = "https://x.com/settings/profile"

# 浏览器网络层响应拦截提示词（兼容 X 轮换 GraphQL queryId）
GQL_USER_BY_SCREEN_NAME_HINTS = (
    "/UserByScreenName",
    "/UserByRestId",
    "/Viewer",
    "verify_credentials.json",
    "settings.json",
)
GQL_USER_TWEETS_HINTS = (
    "/UserOriginalsTimeline",
    "/UserTweets",
    "/UserTweetsAndReplies",
    "/UserRepliesTimeline",
    "/UserMedia",
    "/UserVideoTimeline",
    "/UserPhotoTimeline",
)
GQL_SEARCH_HINTS = (
    "/SearchTimeline",
    "/GlobalCommunitiesPostSearchTimeline",
    "/GlobalCommunitiesLatestPostSearchTimeline",
    "/ adaptive.json",
    "/search/adaptive.json",
)
GQL_TWEET_DETAIL_HINTS = (
    "/TweetDetail",
    "/TweetResultByRestId",
)
GQL_CREATE_TWEET_HINTS = (
    "/CreateTweet",
    "/CreateNoteTweet",
)
GQL_LIKE_TWEET_HINTS = (
    "/FavoriteTweet",
    "/UnfavoriteTweet",
)
GQL_RETWEET_HINTS = (
    "/CreateRetweet",
    "/DeleteRetweet",
)
GQL_FOLLOW_LIST_HINTS = (
    "/Followers",
    "/Following",
    "/BlueVerifiedFollowers",
    "/FollowersYouKnow",
)
GQL_FOLLOW_MUTATION_HINTS = (
    "friendships/create.json",
    "friendships/destroy.json",
)
REST_DM_INBOX_HINTS = (
    "/dm/inbox_initial_state.json",
    "/dm/user_updates.json",
    "/dm/inbox_timeline/",
)
REST_DM_CONV_HINTS = (
    "/dm/conversation/",
    "/dm/inbox_initial_state.json",
    "/dm/user_updates.json",
)
REST_DM_SEND_HINTS = (
    "/dm/new2.json",
    "/dm/new.json",
    "CreateMessageMutation",
)


def tw_urlencode(text: str) -> str:
    """按 RFC 3986 编码搜索词（空格转 `%20`，安全保留 `-_.~`）。"""
    return quote((text or "").strip(), safe="-_.~")


def profile_url(screen_name: str) -> str:
    handle = (screen_name or "").strip().lstrip("@").strip("/")
    if not handle:
        return HOME_URL
    if handle.isdigit():
        return f"{TWITTER_BASE}/i/user/{handle}"
    return f"{TWITTER_BASE}/{quote(handle, safe='_')}"


def followers_url(screen_name: str) -> str:
    handle = (screen_name or "").strip().lstrip("@").strip("/")
    if not handle or handle.isdigit():
        return HOME_URL
    return f"{TWITTER_BASE}/{quote(handle, safe='_')}/followers"


def following_url(screen_name: str) -> str:
    handle = (screen_name or "").strip().lstrip("@").strip("/")
    if not handle or handle.isdigit():
        return HOME_URL
    return f"{TWITTER_BASE}/{quote(handle, safe='_')}/following"


def tweet_url(tweet_id: str, screen_name: str = "") -> str:
    tid = str(tweet_id or "").strip()
    handle = (screen_name or "").strip().lstrip("@").strip("/")
    if not tid:
        return profile_url(handle)
    if handle and not handle.isdigit():
        return f"{TWITTER_BASE}/{quote(handle, safe='_')}/status/{tid}"
    return f"{TWITTER_BASE}/i/web/status/{tid}"


def tweet_search_url(
    keyword: str,
    *,
    latest: bool = False,
    content_type: str = "all",
) -> str:
    """构造推文搜索 URL（支持综合 Top、最新 Live、纯视频与纯图片过滤）。"""
    encoded = tw_urlencode(keyword)
    url = f"{TWITTER_BASE}/search?q={encoded}&src=typed_query"
    ctype = (content_type or "all").strip().lower()
    if ctype == "video":
        return f"{url}&f=video"
    if ctype == "images":
        return f"{url}&f=image"
    if latest:
        return f"{url}&f=live"
    return url


def user_search_url(keyword: str) -> str:
    """构造用户搜索 URL (`&f=user`)。"""
    encoded = tw_urlencode(keyword)
    return f"{TWITTER_BASE}/search?q={encoded}&src=typed_query&f=user"


def dm_conversation_url(conv_id: str) -> str:
    cid = str(conv_id or "").strip()
    if not cid:
        return MESSAGES_URL
    return f"{TWITTER_BASE}/messages/{quote(cid, safe='-_:')}"
