"""X (Twitter) 数据提取器：统一归一 GraphQL Timeline、Syndication、REST v1.1 与 DOM 节点。

支持：
- 推文作品 (`Aweme`)：普通图文（最多 4 图 `?name=orig` 原图）、视频/GIF（最高码率 MP4 直链）、纯文本推文及 X Premium 长推文 (`note_tweet`)
- 本账号作品 (`AccountWork` 字典，含曝光量 `views.count`、转推 `retweet_count`、书签 `bookmark_count`)
- 推文评论与嵌套回复线程 (`CommentRecord` / `KeywordCollectionComment`)
- 用户资料、关注/粉丝列表 (`FollowEdge`) 与私信会话/消息 (`DmConversation` / `DmMessage`)
"""
from __future__ import annotations

from datetime import datetime
from email.utils import parsedate_to_datetime
import html
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .urls import tweet_url


def _to_int(val: Any) -> int:
    if val is None or isinstance(val, bool):
        return 0
    if isinstance(val, (int, float)):
        return max(0, int(val))
    s = str(val).strip().replace(",", "")
    if not s:
        return 0
    mult = 1.0
    up = s.upper()
    if up.endswith("K"):
        mult, s = 1_000.0, s[:-1]
    elif up.endswith("M"):
        mult, s = 1_000_000.0, s[:-1]
    elif up.endswith("B"):
        mult, s = 1_000_000_000.0, s[:-1]
    elif s.endswith("万"):
        mult, s = 10_000.0, s[:-1]
    elif s.endswith("亿"):
        mult, s = 100_000_000.0, s[:-1]
    try:
        return max(0, int(float(s) * mult))
    except ValueError:
        return 0


def parse_twitter_timestamp(val: Any) -> int:
    """将 Twitter RFC2822 (`Wed Oct 10 20:19:24 +0000 2018`)、ISO8601 或毫秒/秒时间戳统一转为秒级 Unix 时间戳。"""
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
    try:
        dt = parsedate_to_datetime(s)
        return max(0, int(dt.timestamp()))
    except Exception:
        pass
    try:
        iso = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(iso)
        return max(0, int(dt.timestamp()))
    except Exception:
        return 0


def upgrade_pbs_image_url(url: str, *, orig: bool = True) -> str:
    """将 `pbs.twimg.com/media/...` 缩略图升级为 `?name=orig` 无损原图链接。"""
    u = str(url or "").strip()
    if not u:
        return ""
    if u.startswith("//"):
        u = "https:" + u
    try:
        sp = urlsplit(u)
    except ValueError:
        return u
    host = (sp.hostname or "").lower()
    if "twimg.com" not in host:
        return u
    # 头像 `_normal` -> `_400x400` 提升清晰度
    if "/profile_images/" in sp.path and "_normal." in sp.path:
        return urlunsplit((sp.scheme, sp.netloc, sp.path.replace("_normal.", "_400x400."), sp.query, sp.fragment))
    if not orig or "/media/" not in sp.path:
        return u
    q = dict(parse_qsl(sp.query, keep_blank_values=True))
    # 若路径本身缺扩展名但 query 有 format=jpg/png，保留 format 并升级 name=orig
    q["name"] = "orig"
    return urlunsplit((sp.scheme, sp.netloc, sp.path, urlencode(q), sp.fragment))


def _unwrap_tweet_result(node: Any) -> Optional[Mapping[str, Any]]:
    """解包 GraphQL `tweet_results.result`（兼容 `Tweet` 与 `TweetWithVisibilityResults`）。"""
    if not isinstance(node, Mapping):
        return None
    if isinstance(node.get("tweet_results"), Mapping):
        node = node["tweet_results"].get("result")
    elif isinstance(node.get("tweetResult"), Mapping):
        node = node["tweetResult"].get("result")
    if not isinstance(node, Mapping):
        return None
    typename = str(node.get("__typename") or "")
    if typename == "TweetWithVisibilityResults" and isinstance(node.get("tweet"), Mapping):
        node = node["tweet"]
    elif typename in {"TweetTombstone", "TweetUnavailable"}:
        return None
    return node if isinstance(node, Mapping) else None


def _unwrap_user_result(node: Any) -> Optional[Mapping[str, Any]]:
    if not isinstance(node, Mapping):
        return None
    if isinstance(node.get("user_results"), Mapping):
        node = node["user_results"].get("result")
    elif isinstance(node.get("userResult"), Mapping):
        node = node["userResult"].get("result")
    elif isinstance(node.get("result"), Mapping):
        node = node["result"]
    if not isinstance(node, Mapping):
        return None
    typename = str(node.get("__typename") or "")
    if typename in {"UserUnavailable", "UserSuspended"}:
        return None
    return node


def parse_twitter_user(raw: Any) -> Dict[str, Any]:
    """解析 X GraphQL `User`（兼容 `legacy` 与新版 `core` + `avatar` 结构）、REST v1.1 或 DOM 用户字典。"""
    if not isinstance(raw, Mapping):
        return {}
    u = _unwrap_user_result(raw) or raw
    if isinstance(u.get("data"), Mapping) and isinstance(u["data"].get("user"), Mapping):
        inner = _unwrap_user_result(u["data"]["user"]) or u["data"]["user"].get("result")
        if isinstance(inner, Mapping):
            u = inner
    elif isinstance(u.get("user"), Mapping) and not u.get("screen_name"):
        u = _unwrap_user_result(u["user"]) or u["user"]

    legacy = u.get("legacy") if isinstance(u.get("legacy"), Mapping) else {}
    core = u.get("core") if isinstance(u.get("core"), Mapping) else {}
    avatar_node = u.get("avatar") if isinstance(u.get("avatar"), Mapping) else {}

    uid = str(
        u.get("rest_id")
        or u.get("id_str")
        or u.get("uid")
        or (u.get("id") if str(u.get("id") or "").isdigit() else "")
        or ""
    ).strip()
    screen_name = str(
        core.get("screen_name")
        or legacy.get("screen_name")
        or u.get("screen_name")
        or u.get("unique_id")
        or u.get("douyin_id")
        or ""
    ).strip().lstrip("@")
    name = str(
        core.get("name")
        or legacy.get("name")
        or u.get("name")
        or u.get("nickname")
        or screen_name
    ).strip()
    raw_avatar = str(
        avatar_node.get("image_url")
        or legacy.get("profile_image_url_https")
        or u.get("profile_image_url_https")
        or u.get("avatar")
        or ""
    ).strip()
    avatar = upgrade_pbs_image_url(raw_avatar, orig=False)
    bio = str(
        legacy.get("description")
        or (u.get("profile_bio", {}).get("description") if isinstance(u.get("profile_bio"), Mapping) else "")
        or u.get("description")
        or u.get("signature")
        or ""
    ).strip()
    rel_counts = u.get("relationship_counts") if isinstance(u.get("relationship_counts"), Mapping) else {}
    tw_counts = u.get("tweet_counts") if isinstance(u.get("tweet_counts"), Mapping) else {}
    act_counts = u.get("action_counts") if isinstance(u.get("action_counts"), Mapping) else {}
    verif_node = u.get("verification") if isinstance(u.get("verification"), Mapping) else {}

    followers = _to_int(
        rel_counts.get("followers")
        if rel_counts.get("followers") is not None
        else (
            legacy.get("followers_count")
            or u.get("followers_count")
            or u.get("follower_count")
        )
    )
    following = _to_int(
        rel_counts.get("following")
        if rel_counts.get("following") is not None
        else (
            legacy.get("friends_count")
            or u.get("friends_count")
            or u.get("following_count")
        )
    )
    statuses = _to_int(
        tw_counts.get("tweets")
        if tw_counts.get("tweets") is not None
        else (
            legacy.get("statuses_count")
            or u.get("statuses_count")
            or u.get("aweme_count")
        )
    )
    favourites = _to_int(
        act_counts.get("favorites_count")
        if act_counts.get("favorites_count") is not None
        else (
            legacy.get("favourites_count")
            or u.get("favourites_count")
            or u.get("total_favorited")
        )
    )
    rel = u.get("relationship_perspectives") if isinstance(u.get("relationship_perspectives"), Mapping) else {}
    is_following = bool(
        rel.get("following")
        if "following" in rel
        else legacy.get("following", u.get("following", False))
    )
    followed_by = bool(
        rel.get("followed_by")
        if "followed_by" in rel
        else legacy.get("followed_by", u.get("followed_by", False))
    )
    is_verified = bool(
        u.get("is_blue_verified")
        or verif_node.get("verified")
        or legacy.get("verified")
        or u.get("verified")
    )
    return {
        "uid": uid,
        "sec_uid": screen_name or uid,
        "unique_id": screen_name,
        "douyin_id": screen_name,
        "nickname": name or screen_name or uid,
        "avatar": avatar,
        "signature": bio,
        "location": str(
            (u.get("location", {}).get("location") if isinstance(u.get("location"), Mapping) else u.get("location"))
            or legacy.get("location")
            or ""
        ).strip(),
        "follower_count": followers,
        "following_count": following,
        "aweme_count": statuses,
        "total_favorited": favourites,
        "is_following": is_following,
        "followed_by": followed_by,
        "is_mutual": bool(is_following and followed_by),
        "is_verified": is_verified,
    }


parse_twitter_self_user = parse_twitter_user


def _expand_tweet_text(text: str, entities: Optional[Mapping[str, Any]]) -> str:
    """将推文正文中的 HTML 实体解码，并把 `t.co` 链接还原为可读的 `expanded_url`（移除末尾媒体自链）。"""
    out = html.unescape(str(text or ""))
    if not isinstance(entities, Mapping):
        return out.strip()
    # 移除附着在正文末尾的图片/视频自身 t.co 链接
    for m in (entities.get("media") or []):
        if isinstance(m, Mapping) and m.get("url"):
            out = out.replace(str(m["url"]), "")
    for u in (entities.get("urls") or []):
        if isinstance(u, Mapping) and u.get("url") and u.get("expanded_url"):
            out = out.replace(str(u["url"]), str(u["expanded_url"]))
    return out.strip()


def _best_mp4_variant(variants: Iterable[Any]) -> Tuple[str, int]:
    best_url = ""
    best_bitrate = -1
    for v in variants or []:
        if not isinstance(v, Mapping):
            continue
        ctype = str(v.get("content_type") or v.get("type") or "").lower()
        vurl = str(v.get("url") or v.get("src") or "").strip()
        if not vurl:
            continue
        if "mp4" in ctype or ".mp4" in vurl.lower():
            br = _to_int(v.get("bitrate"))
            if br >= best_bitrate:
                best_bitrate = br
                best_url = vurl
    return best_url, max(0, best_bitrate)


def _extract_media_items(tweet_node: Mapping[str, Any], legacy: Mapping[str, Any]) -> Tuple[str, List[MediaItem], str, int, str]:
    """从 GraphQL `extended_entities`、Syndication `mediaDetails`/`video`/`photos` 或 DOM 提取媒体项。

    返回 `(media_type, medias, cover_url, duration_sec, quality_label)`。
    """
    medias: List[MediaItem] = []
    cover = ""
    duration = 0
    quality_label = ""
    has_video = False

    # 1) GraphQL / REST v1.1 extended_entities.media
    ext = legacy.get("extended_entities") if isinstance(legacy.get("extended_entities"), Mapping) else {}
    ent = legacy.get("entities") if isinstance(legacy.get("entities"), Mapping) else {}
    raw_media_list = (
        ext.get("media")
        or ent.get("media")
        or tweet_node.get("mediaDetails")
        or []
    )
    if isinstance(raw_media_list, list) and raw_media_list:
        for idx, m in enumerate(raw_media_list, start=1):
            if not isinstance(m, Mapping):
                continue
            mtype = str(m.get("type") or "").lower()
            thumb = str(m.get("media_url_https") or m.get("media_url") or "").strip()
            if thumb and not cover:
                cover = upgrade_pbs_image_url(thumb, orig=False)
            if mtype in {"video", "animated_gif"}:
                vinfo = m.get("video_info") if isinstance(m.get("video_info"), Mapping) else {}
                mp4_url, bitrate = _best_mp4_variant(vinfo.get("variants") or [])
                if mp4_url:
                    has_video = True
                    dur_ms = _to_int(vinfo.get("duration_millis"))
                    if dur_ms > 0:
                        duration = max(duration, max(1, round(dur_ms / 1000)))
                    if bitrate >= 2_000_000:
                        quality_label = "1080P"
                    elif bitrate >= 800_000 and not quality_label:
                        quality_label = "720P"
                    medias.append(MediaItem(url=mp4_url, kind="video", ext="mp4", index=len(medias) + 1))
            else:
                img_url = upgrade_pbs_image_url(thumb, orig=True)
                if img_url:
                    ext_name = "png" if "format=png" in img_url.lower() or img_url.lower().endswith(".png") else "jpg"
                    medias.append(MediaItem(url=img_url, kind="image", ext=ext_name, index=len(medias) + 1))

    # 2) Syndication fallback (`video.variants` + `photos`)
    if not medias:
        syn_video = tweet_node.get("video")
        if isinstance(syn_video, Mapping):
            mp4_url, _ = _best_mp4_variant(syn_video.get("variants") or [])
            if not cover and syn_video.get("poster"):
                cover = str(syn_video.get("poster") or "")
            dur_ms = _to_int(syn_video.get("durationMs"))
            if dur_ms > 0:
                duration = max(1, round(dur_ms / 1000))
            if mp4_url:
                has_video = True
                medias.append(MediaItem(url=mp4_url, kind="video", ext="mp4", index=1))
        syn_photos = tweet_node.get("photos")
        if not medias and isinstance(syn_photos, list):
            for p in syn_photos:
                if isinstance(p, Mapping) and p.get("url"):
                    img_url = upgrade_pbs_image_url(str(p["url"]), orig=True)
                    if not cover:
                        cover = upgrade_pbs_image_url(str(p["url"]), orig=False)
                    medias.append(MediaItem(url=img_url, kind="image", ext="jpg", index=len(medias) + 1))

    # 3) DOM fallback (`dom_medias` / `media_urls`)
    if not medias and isinstance(tweet_node.get("dom_medias"), list):
        for dm in tweet_node["dom_medias"]:
            if not isinstance(dm, Mapping) or not dm.get("url"):
                continue
            u = str(dm["url"]).strip()
            if u.startswith("blob:"):
                continue
            kind = "video" if str(dm.get("kind") or "") == "video" else "image"
            if kind == "video":
                has_video = True
                medias.append(MediaItem(url=u, kind="video", ext="mp4", index=len(medias) + 1))
            else:
                img_u = upgrade_pbs_image_url(u, orig=True)
                if not cover:
                    cover = upgrade_pbs_image_url(u, orig=False)
                medias.append(MediaItem(url=img_u, kind="image", ext="jpg", index=len(medias) + 1))

    if not cover and isinstance(tweet_node.get("cover_url"), str):
        cover = str(tweet_node["cover_url"]).strip()

    media_type = "video" if has_video else "images"
    return media_type, medias, cover, duration, quality_label


def parse_twitter_aweme(
    raw: Any,
    quality: str = "highest",
    *,
    skip_retweets: bool = False,
) -> Optional[Aweme]:
    """将单条 X GraphQL / Syndication / DOM 推文节点标准化为 CreatorHub `Aweme`。"""
    if not isinstance(raw, Mapping):
        return None
    node = _unwrap_tweet_result(raw) or raw
    legacy = node.get("legacy") if isinstance(node.get("legacy"), Mapping) else node

    # 如果是纯转推 (`retweeted_status_result`)，可选跳过或解包取决于场景
    if skip_retweets and (
        isinstance(legacy.get("retweeted_status_result"), Mapping)
        or bool(node.get("is_retweet"))
    ):
        return None

    tid = str(
        node.get("rest_id")
        or legacy.get("id_str")
        or node.get("id_str")
        or node.get("aweme_id")
        or (node.get("id") if str(node.get("id") or "").isdigit() else "")
        or ""
    ).strip()
    if not tid:
        return None

    # 作者信息
    core_user = None
    if isinstance(node.get("core"), Mapping):
        core_user = _unwrap_user_result(node["core"])
    if core_user is None and isinstance(node.get("user"), Mapping):
        core_user = node["user"]
    user_info = parse_twitter_user(core_user) if core_user else {}

    screen_name = str(
        user_info.get("unique_id")
        or node.get("author_unique_id")
        or node.get("screen_name")
        or ""
    ).strip().lstrip("@")
    author_name = str(
        user_info.get("nickname")
        or node.get("author_name")
        or screen_name
    ).strip()
    author_uid = str(user_info.get("uid") or screen_name).strip()
    avatar = str(user_info.get("avatar") or node.get("avatar") or "").strip()

    # 正文提取（优先 X Premium `note_tweet` 完整长文）
    note_text = ""
    if isinstance(node.get("note_tweet"), Mapping):
        nt_res = (
            (node["note_tweet"].get("note_tweet_results") or {}).get("result")
            if isinstance(node["note_tweet"].get("note_tweet_results"), Mapping)
            else None
        )
        if isinstance(nt_res, Mapping):
            note_text = _expand_tweet_text(
                str(nt_res.get("text") or ""),
                nt_res.get("entity_set") if isinstance(nt_res.get("entity_set"), Mapping) else None,
            )

    raw_text = (
        note_text
        or str(legacy.get("full_text") or node.get("text") or node.get("desc") or "")
    )
    desc = _expand_tweet_text(
        raw_text,
        legacy.get("entities") if isinstance(legacy.get("entities"), Mapping) else None,
    )

    media_type, medias, cover, duration, q_label = _extract_media_items(node, legacy)
    create_time = parse_twitter_timestamp(
        legacy.get("created_at") or node.get("created_at") or node.get("create_time")
    )
    like_count = _to_int(
        legacy.get("favorite_count")
        if legacy.get("favorite_count") is not None
        else node.get("favorite_count", node.get("like_count", 0))
    )
    comment_count = _to_int(
        legacy.get("reply_count")
        if legacy.get("reply_count") is not None
        else node.get("conversation_count", node.get("comment_count", 0))
    )

    canonical = str(node.get("source_url") or "").strip() or tweet_url(tid, screen_name)
    return Aweme(
        aweme_id=tid,
        desc=desc,
        create_time=create_time,
        author_name=author_name,
        author_unique_id=screen_name or author_uid,
        media_type=media_type,
        medias=medias,
        quality_label=q_label or ("原图" if medias and media_type == "images" else ""),
        cover=cover,
        avatar=avatar,
        like_count=like_count,
        comment_count=comment_count,
        duration=duration,
        platform="twitter",
        source_url=canonical,
    )


def parse_twitter_creator_work(raw: Any) -> Optional[Dict[str, Any]]:
    """将单条推文节点转换为 `AccountWork` 落库结构（含曝光量 `views.count`、转推与书签计数）。"""
    if not isinstance(raw, Mapping):
        return None
    node = _unwrap_tweet_result(raw) or raw
    legacy = node.get("legacy") if isinstance(node.get("legacy"), Mapping) else node
    aw = parse_twitter_aweme(node, skip_retweets=False)
    if aw is None:
        return None

    views_node = node.get("views") if isinstance(node.get("views"), Mapping) else {}
    play_count = _to_int(
        views_node.get("count")
        or node.get("view_count")
        or node.get("play_count")
    )
    retweet_count = _to_int(legacy.get("retweet_count") or node.get("retweet_count"))
    quote_count = _to_int(legacy.get("quote_count") or node.get("quote_count"))
    bookmark_count = _to_int(legacy.get("bookmark_count") or node.get("bookmark_count"))
    is_rt = isinstance(legacy.get("retweeted_status_result"), Mapping) or bool(node.get("is_retweet"))

    return {
        "item_id": aw.aweme_id,
        "desc": aw.desc,
        "media_type": aw.media_type,
        "cover_url": aw.cover or "",
        "create_time": aw.create_time,
        "like_count": aw.like_count,
        "comment_count": aw.comment_count,
        "collect_count": bookmark_count,
        "share_count": retweet_count + quote_count,
        "play_count": play_count,
        "status": "转推" if is_rt else "公开",
        "duration": aw.duration,
        "source_url": aw.source_url,
    }


def parse_twitter_comment(
    raw: Any,
    *,
    target_tweet_id: str = "",
    reply_to: str = "",
) -> Optional[Dict[str, Any]]:
    """把一条回复推文转换为 `CommentRecord` / `KeywordCollectionComment` 兼容字典。"""
    if not isinstance(raw, Mapping):
        return None
    node = _unwrap_tweet_result(raw) or raw
    legacy = node.get("legacy") if isinstance(node.get("legacy"), Mapping) else node

    cid = str(
        node.get("rest_id")
        or legacy.get("id_str")
        or node.get("comment_id")
        or ""
    ).strip()
    if not cid or (target_tweet_id and cid == str(target_tweet_id)):
        return None

    core_user = _unwrap_user_result(node.get("core")) if isinstance(node.get("core"), Mapping) else (
        node.get("user") if isinstance(node.get("user"), Mapping) else None
    )
    uinfo = parse_twitter_user(core_user) if core_user else {}
    screen_name = str(
        uinfo.get("unique_id")
        or node.get("user_sec_uid")
        or ""
    ).strip().lstrip("@")
    nickname = str(
        uinfo.get("nickname")
        or node.get("user_nickname")
        or screen_name
    ).strip()

    raw_text = str(legacy.get("full_text") or node.get("text") or "").strip()
    text = _expand_tweet_text(
        raw_text,
        legacy.get("entities") if isinstance(legacy.get("entities"), Mapping) else None,
    )
    # 剥离 Twitter 回复开头自动带上的 `@author ` 前缀以保持评论干净可读
    cleaned_text = re.sub(r"^(?:@[A-Za-z0-9_]{1,15}\s+)+", "", text).strip() or text

    in_reply_to = str(
        reply_to
        or legacy.get("in_reply_to_status_id_str")
        or node.get("reply_to")
        or ""
    ).strip()
    if target_tweet_id and in_reply_to == str(target_tweet_id):
        in_reply_to = ""

    return {
        "comment_id": cid,
        "text": cleaned_text,
        "user_nickname": nickname or screen_name or cid,
        "user_sec_uid": screen_name or str(uinfo.get("uid") or ""),
        "ip_label": str(uinfo.get("location") or node.get("ip_label") or "")[:40],
        "like_count": _to_int(
            legacy.get("favorite_count")
            if legacy.get("favorite_count") is not None
            else node.get("like_count", 0)
        ),
        "create_time": parse_twitter_timestamp(
            legacy.get("created_at") or node.get("create_time")
        ),
        "reply_to": in_reply_to,
    }


def _iter_timeline_instructions(payload: Any) -> Iterable[Mapping[str, Any]]:
    """递归遍历任意深度 GraphQL 响应中的 `instructions[]` 列表。"""
    stack = [payload]
    while stack:
        cur = stack.pop()
        if isinstance(cur, Mapping):
            instrs = cur.get("instructions")
            if isinstance(instrs, list):
                for ins in instrs:
                    if isinstance(ins, Mapping):
                        yield ins
            for k, v in cur.items():
                if k != "instructions" and isinstance(v, (Mapping, list)):
                    stack.append(v)
        elif isinstance(cur, list):
            for el in cur:
                if isinstance(el, (Mapping, list)):
                    stack.append(el)


def extract_graphql_tweets(payload: Any) -> Tuple[List[Dict[str, Any]], str]:
    """从 X GraphQL Timeline / SearchTimeline / TweetDetail 响应提取所有推文节点与下一页 `bottom_cursor`。"""
    tweets: List[Dict[str, Any]] = []
    seen_ids: set[str] = set()
    bottom_cursor = ""

    def _add_tweet(candidate: Any) -> None:
        unwrapped = _unwrap_tweet_result(candidate)
        if not isinstance(unwrapped, Mapping):
            return
        tid = str(
            unwrapped.get("rest_id")
            or ((unwrapped.get("legacy") or {}).get("id_str") if isinstance(unwrapped.get("legacy"), Mapping) else "")
            or ""
        ).strip()
        if not tid or tid in seen_ids:
            return
        # 跳过纯广告 PromotedTweet（若有 promotedMetadata）
        seen_ids.add(tid)
        tweets.append(dict(unwrapped))

    # 单条 TweetResultByRestId / CreateTweet 响应直接解包
    if isinstance(payload, Mapping):
        data = payload.get("data")
        if isinstance(data, Mapping):
            if isinstance(data.get("create_tweet"), Mapping):
                _add_tweet(data["create_tweet"].get("tweet_results"))
            if isinstance(data.get("tweetResult"), Mapping):
                _add_tweet(data["tweetResult"])

    for ins in _iter_timeline_instructions(payload):
        entries = list(ins.get("entries") or [])
        if isinstance(ins.get("entry"), Mapping):
            entries.append(ins["entry"])
        module_items = ins.get("moduleItems")
        if isinstance(module_items, list):
            for mi in module_items:
                if isinstance(mi, Mapping) and isinstance(mi.get("item"), Mapping):
                    ic = mi["item"].get("itemContent")
                    if isinstance(ic, Mapping) and not ic.get("promotedMetadata"):
                        _add_tweet(ic)

        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            entry_id = str(entry.get("entryId") or "")
            content = entry.get("content")
            if not isinstance(content, Mapping):
                continue
            # 识别底部翻页游标
            if (
                str(content.get("cursorType") or "").lower() == "bottom"
                or entry_id.startswith("cursor-bottom-")
            ) and content.get("value"):
                bottom_cursor = str(content["value"])
                continue
            if entry_id.startswith("promoted-tweet"):
                continue

            ic = content.get("itemContent")
            if isinstance(ic, Mapping) and not ic.get("promotedMetadata"):
                _add_tweet(ic)

            items = content.get("items")
            if isinstance(items, list):
                for sub in items:
                    if not isinstance(sub, Mapping):
                        continue
                    sub_item = sub.get("item")
                    if isinstance(sub_item, Mapping):
                        sub_ic = sub_item.get("itemContent")
                        if isinstance(sub_ic, Mapping) and not sub_ic.get("promotedMetadata"):
                            _add_tweet(sub_ic)

    return tweets, bottom_cursor


def extract_graphql_comments(
    payload: Any,
    *,
    target_tweet_id: str = "",
    include_replies: bool = True,
) -> Tuple[List[Dict[str, Any]], str]:
    """从 `TweetDetail` 响应提取主推文下的评论及子回复线程。"""
    raw_tweets, cursor = extract_graphql_tweets(payload)
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for t in raw_tweets:
        c = parse_twitter_comment(t, target_tweet_id=target_tweet_id)
        if not c or c["comment_id"] in seen:
            continue
        if not include_replies and c.get("reply_to"):
            continue
        seen.add(c["comment_id"])
        out.append(c)
    return out, cursor


def extract_graphql_users(payload: Any) -> Tuple[List[Dict[str, Any]], str]:
    """从 `SearchTimeline` (`&f=user`)、`Followers` 或 `Following` GraphQL 响应提取用户列表与底部游标。"""
    users: List[Dict[str, Any]] = []
    seen: set[str] = set()
    bottom_cursor = ""

    def _add_user(candidate: Any) -> None:
        u = _unwrap_user_result(candidate)
        if not isinstance(u, Mapping):
            return
        parsed = parse_twitter_user(u)
        key = str(parsed.get("uid") or parsed.get("unique_id") or "")
        if not key or key in seen:
            return
        seen.add(key)
        users.append(parsed)

    for ins in _iter_timeline_instructions(payload):
        for entry in (ins.get("entries") or []):
            if not isinstance(entry, Mapping):
                continue
            entry_id = str(entry.get("entryId") or "")
            content = entry.get("content")
            if not isinstance(content, Mapping):
                continue
            if (
                str(content.get("cursorType") or "").lower() == "bottom"
                or entry_id.startswith("cursor-bottom-")
            ) and content.get("value"):
                bottom_cursor = str(content["value"])
                continue
            ic = content.get("itemContent")
            if isinstance(ic, Mapping) and not ic.get("promotedMetadata"):
                _add_user(ic)
            for sub in (content.get("items") or []):
                if isinstance(sub, Mapping) and isinstance(sub.get("item"), Mapping):
                    sub_ic = sub["item"].get("itemContent")
                    if isinstance(sub_ic, Mapping) and not sub_ic.get("promotedMetadata"):
                        _add_user(sub_ic)

    return users, bottom_cursor


def parse_twitter_follow_users(
    payload: Any,
    *,
    default_following: bool = True,
) -> Tuple[List[Dict[str, Any]], str]:
    """将关注/粉丝 GraphQL 响应转换为 `FollowEdge` 兼容列表。"""
    users, cursor = extract_graphql_users(payload)
    out: List[Dict[str, Any]] = []
    for u in users:
        uid = str(u.get("uid") or u.get("sec_uid") or "").strip()
        handle = str(u.get("unique_id") or u.get("sec_uid") or uid).strip()
        if not uid and not handle:
            continue
        is_f = bool(u.get("is_following")) if u.get("is_following") is not None else default_following
        f_by = bool(u.get("followed_by"))
        out.append({
            "uid": uid or handle,
            "sec_uid": handle or uid,
            "nickname": str(u.get("nickname") or handle or uid).strip(),
            "avatar": str(u.get("avatar") or "").strip(),
            "signature": str(u.get("signature") or "").strip(),
            "is_following": is_f,
            "is_mutual": bool(is_f and f_by),
        })
    return out, cursor


def _dm_state_root(payload: Any) -> Optional[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        return None
    for key in ("inbox_initial_state", "conversation_timeline", "user_events"):
        sub = payload.get(key)
        if isinstance(sub, Mapping):
            return sub
    if "conversations" in payload or "entries" in payload:
        return payload
    return None


def parse_twitter_dm_inbox(payload: Any, *, self_uid: str = "") -> List[Dict[str, Any]]:
    """将 X `/i/api/1.1/dm/inbox_initial_state.json` 解析为 `DmConversation` 字典列表。"""
    root = _dm_state_root(payload)
    if not isinstance(root, Mapping):
        return []
    users_map = root.get("users") if isinstance(root.get("users"), Mapping) else {}
    convs_map = root.get("conversations") if isinstance(root.get("conversations"), Mapping) else {}
    entries = root.get("entries") if isinstance(root.get("entries"), list) else []

    # 聚合每个会话的最新消息文本与时间戳
    latest_by_conv: Dict[str, Tuple[int, str]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        msg = entry.get("message") if isinstance(entry.get("message"), Mapping) else None
        if not isinstance(msg, Mapping):
            continue
        cid = str(msg.get("conversation_id") or "").strip()
        mdata = msg.get("message_data") if isinstance(msg.get("message_data"), Mapping) else {}
        ts = parse_twitter_timestamp(mdata.get("time") or msg.get("time"))
        txt = _expand_tweet_text(
            str(mdata.get("text") or ""),
            mdata.get("entities") if isinstance(mdata.get("entities"), Mapping) else None,
        )
        if not txt and isinstance(mdata.get("attachment"), Mapping):
            txt = "[媒体附件]"
        if cid and ts >= latest_by_conv.get(cid, (0, ""))[0]:
            latest_by_conv[cid] = (ts, txt)

    out: List[Dict[str, Any]] = []
    for conv_id, cobj in convs_map.items():
        if not isinstance(cobj, Mapping):
            continue
        cid = str(cobj.get("conversation_id") or conv_id or "").strip()
        if not cid:
            continue
        participants = [p for p in (cobj.get("participants") or []) if isinstance(p, Mapping)]
        peer_uid = ""
        for p in participants:
            puid = str(p.get("user_id") or "").strip()
            if self_uid and puid == str(self_uid):
                continue
            if puid:
                peer_uid = puid
                break
        if not peer_uid and participants:
            peer_uid = str(participants[0].get("user_id") or "").strip()

        u_raw = users_map.get(peer_uid) if peer_uid else None
        u_info = parse_twitter_user(u_raw) if isinstance(u_raw, Mapping) else {}
        peer_handle = str(u_info.get("unique_id") or "").strip()
        peer_nick = str(
            cobj.get("name")
            or u_info.get("nickname")
            or peer_handle
            or peer_uid
            or cid
        ).strip()
        peer_avatar = str(
            cobj.get("avatar_image_https")
            or u_info.get("avatar")
            or ""
        ).strip()

        sort_ts = parse_twitter_timestamp(cobj.get("sort_timestamp"))
        msg_ts, msg_txt = latest_by_conv.get(cid, (0, ""))
        last_time = max(sort_ts, msg_ts)

        # 未读判断：比较 sort_event_id 与自己的 last_read_event_id
        unread = _to_int(cobj.get("unread_count"))
        if not unread and self_uid:
            my_part = next(
                (p for p in participants if str(p.get("user_id") or "") == str(self_uid)),
                None,
            )
            if isinstance(my_part, Mapping):
                last_read = _to_int(my_part.get("last_read_event_id"))
                max_entry = _to_int(cobj.get("max_entry_id") or cobj.get("sort_event_id"))
                if last_read and max_entry and max_entry > last_read:
                    unread = 1

        out.append({
            "conv_id": cid,
            "peer_uid": peer_uid,
            "peer_sec_uid": peer_handle or peer_uid,
            "peer_nickname": peer_nick,
            "peer_avatar": upgrade_pbs_image_url(peer_avatar, orig=False),
            "last_text": msg_txt,
            "last_time": last_time,
            "unread_count": unread,
        })

    out.sort(key=lambda r: int(r.get("last_time") or 0), reverse=True)
    return out


def parse_twitter_dm_messages(
    payload: Any,
    *,
    conv_id: str = "",
    self_uid: str = "",
) -> List[Dict[str, Any]]:
    """解析 X `/i/api/1.1/dm/conversation/{id}.json` 或 `inbox_initial_state` 为升序 `DmMessage` 列表。"""
    root = _dm_state_root(payload)
    if not isinstance(root, Mapping):
        return []
    entries = root.get("entries") if isinstance(root.get("entries"), list) else []
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        msg = entry.get("message") if isinstance(entry.get("message"), Mapping) else None
        if not isinstance(msg, Mapping):
            continue
        m_cid = str(msg.get("conversation_id") or "").strip()
        if conv_id and m_cid and m_cid != str(conv_id):
            continue
        mdata = msg.get("message_data") if isinstance(msg.get("message_data"), Mapping) else {}
        mid = str(mdata.get("id") or msg.get("id") or "").strip()
        if not mid or mid in seen:
            continue
        seen.add(mid)
        sender_id = str(mdata.get("sender_id") or "").strip()
        direction = "out" if (self_uid and sender_id == str(self_uid)) else "in"
        txt = _expand_tweet_text(
            str(mdata.get("text") or ""),
            mdata.get("entities") if isinstance(mdata.get("entities"), Mapping) else None,
        )
        msg_type = "text"
        att = mdata.get("attachment")
        if isinstance(att, Mapping):
            if "photo" in att:
                msg_type = "image"
                txt = txt or "[图片]"
            elif "video" in att or "animated_gif" in att:
                msg_type = "video"
                txt = txt or "[视频]"
            elif "tweet" in att:
                msg_type = "share"
                txt = txt or "[分享推文]"
        out.append({
            "msg_id": mid,
            "server_msg_id": mid,
            "sender_uid": sender_id,
            "direction": direction,
            "msg_type": msg_type,
            "text": txt,
            "create_time": parse_twitter_timestamp(mdata.get("time") or msg.get("time")),
        })
    out.sort(key=lambda m: int(m.get("create_time") or 0))
    return out


from ..douyin.extract import Aweme, MediaItem  # noqa: E402
