"""Instagram 移动端 v1 / Web GraphQL / Direct v2 响应规范化提取器。

统一处理多形态响应结构：
- 候选图按分辨率高度降序选取最高清资源；
- 分块流响应（`clips/user/`、`stream_comments/`）支持按换行逐行解析合并，提取尾段分页游标；
- Direct v2 微秒级时间戳统一归一为秒级，完整覆盖 story_share / reel_share / link / action_log / raven_media / profile 等消息体。
"""
from __future__ import annotations

import json
import math
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from ..douyin.extract import Aweme, MediaItem, safe_title  # noqa: F401
from .resolve import media_id_to_shortcode, shortcode_to_media_id


def _domain_is_ig(domain: Any) -> bool:
    if not isinstance(domain, str):
        return False
    if domain == "":
        return True
    clean = domain.lstrip(".").lower()
    return clean in {"instagram.com", "instagr.am"} or clean.endswith(".instagram.com")


def cookie_map_from_state(storage_state: str | Mapping[str, Any] | None) -> Dict[str, str]:
    """从 Playwright `storage_state` JSON 或原始 Cookie 字符串提取有效 `.instagram.com` Cookie。"""
    if storage_state is None:
        return {}
    if isinstance(storage_state, str):
        raw = storage_state.strip()
        if not raw:
            return {}
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, Mapping):
                storage_state = parsed
            else:
                return {}
        else:
            out: Dict[str, str] = {}
            for part in raw.split(";"):
                seg = part.strip()
                if not seg or "=" not in seg:
                    continue
                k, _, v = seg.partition("=")
                k = k.strip()
                if k:
                    out[k] = v.strip()
            return out

    if not isinstance(storage_state, Mapping):
        return {}
    cookies = storage_state.get("cookies")
    if not isinstance(cookies, list):
        return {str(k): str(v) for k, v in storage_state.items() if k and v is not None}

    now = time.time()
    result: Dict[str, str] = {}
    for ck in cookies:
        if not isinstance(ck, Mapping):
            continue
        if not _domain_is_ig(ck.get("domain", "")):
            continue
        name = str(ck.get("name") or "").strip()
        value = str(ck.get("value") or "")
        if not name:
            continue
        expires = ck.get("expires")
        if expires is not None and not isinstance(expires, bool):
            try:
                exp = float(expires)
                if math.isfinite(exp) and exp > 0 and exp <= now:
                    continue
            except (TypeError, ValueError, OverflowError):
                pass
        result[name] = value
    return result


def cookie_str_from_state(storage_state: str | Mapping[str, Any] | None) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookie_map_from_state(storage_state).items())


def has_ig_login_cookies(storage_state: str | Mapping[str, Any] | None) -> bool:
    cks = cookie_map_from_state(storage_state)
    return bool(cks.get("sessionid"))


def _to_int(val: Any) -> int:
    if isinstance(val, bool):
        return int(val)
    if isinstance(val, (int, float)):
        return int(val) if math.isfinite(val) else 0
    if isinstance(val, str):
        s = val.strip().replace(",", "")
        if not s:
            return 0
        try:
            return int(float(s))
        except ValueError:
            return 0
    return 0


def _unix_seconds(val: Any) -> int:
    """把秒 / 毫秒 / Direct v2 微秒时间戳统一归一为 Unix 秒。"""
    if val is None or isinstance(val, bool):
        return 0
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return 0
        # Direct v2 微秒字符串长度 >= 15，截去末尾 6 位得秒
        if s.isdigit() and len(s) >= 15:
            return int(s[:-6])
        try:
            num = float(s)
        except ValueError:
            return 0
    elif isinstance(val, (int, float)):
        num = float(val)
    else:
        return 0
    if not math.isfinite(num) or num <= 0:
        return 0
    n = int(num)
    if n >= 10**14:
        return n // 1_000_000
    if n >= 10**11:
        return n // 1_000
    return n


def _best_image_url(obj: Mapping[str, Any]) -> str:
    """按候选图高度降序选择最高清图片直链。"""
    iv2 = obj.get("image_versions2")
    if isinstance(iv2, Mapping):
        cands = iv2.get("candidates")
        if isinstance(cands, list) and cands:
            valid = [c for c in cands if isinstance(c, Mapping) and str(c.get("url") or "").startswith("http")]
            if valid:
                valid.sort(key=lambda c: (_to_int(c.get("height")), _to_int(c.get("width"))), reverse=True)
                return str(valid[0]["url"])
    for key in ("display_url", "thumbnail_src", "thumbnail_url", "media_url"):
        u = str(obj.get(key) or "").strip()
        if u.startswith("http"):
            return u
    disp = obj.get("display_resources")
    if isinstance(disp, list) and disp:
        valid = [r for r in disp if isinstance(r, Mapping) and str(r.get("src") or "").startswith("http")]
        if valid:
            valid.sort(key=lambda r: (_to_int(r.get("config_height")), _to_int(r.get("config_width"))), reverse=True)
            return str(valid[0]["src"])
    return ""


def _best_video_url(obj: Mapping[str, Any]) -> Tuple[str, str]:
    """从 video_versions 或 GraphQL video_url 挑选最高分辨率 MP4 直链与画质标签。"""
    vv = obj.get("video_versions")
    if isinstance(vv, list) and vv:
        valid = [v for v in vv if isinstance(v, Mapping) and str(v.get("url") or "").startswith("http")]
        if valid:
            valid.sort(
                key=lambda v: (_to_int(v.get("height")) * _to_int(v.get("width")), _to_int(v.get("height"))),
                reverse=True,
            )
            best = valid[0]
            h = min(_to_int(best.get("width")) or 0, _to_int(best.get("height")) or 0)
            q = f"{h}p" if h > 0 else ""
            return str(best["url"]), q
    vurl = str(obj.get("video_url") or "").strip()
    if vurl.startswith("http"):
        return vurl, ""
    return "", ""


def _extract_caption(item: Mapping[str, Any]) -> str:
    cap = item.get("caption")
    if isinstance(cap, Mapping):
        return str(cap.get("text") or "").strip()
    if isinstance(cap, str):
        return cap.strip()
    edge_cap = item.get("edge_media_to_caption")
    if isinstance(edge_cap, Mapping):
        edges = edge_cap.get("edges")
        if isinstance(edges, list) and edges:
            node = edges[0].get("node") if isinstance(edges[0], Mapping) else {}
            if isinstance(node, Mapping):
                return str(node.get("text") or "").strip()
    return str(item.get("accessibility_caption") or "").strip()


def _extract_media_items_from_node(node: Mapping[str, Any], index: int = 0) -> Optional[MediaItem]:
    mtype = _to_int(node.get("media_type"))
    typename = str(node.get("__typename") or "")
    is_video = (
        mtype == 2
        or bool(node.get("is_video"))
        or typename in {"GraphVideo", "XDTGraphVideo"}
        or bool(node.get("video_versions"))
    )
    if is_video:
        vurl, _ = _best_video_url(node)
        if vurl:
            return MediaItem(url=vurl, kind="video", ext="mp4", index=index)
    img_url = _best_image_url(node)
    if img_url:
        return MediaItem(url=img_url, kind="image", ext="jpeg", index=index)
    return None


def parse_ig_aweme(item: Mapping[str, Any], *, require_media: bool = False) -> Optional[Aweme]:
    """将 Instagram Mobile API `MediaDataItem`、Reels wrapper 或 Web GraphQL node 统一转为 `Aweme`。"""
    if not isinstance(item, Mapping):
        return None
    if isinstance(item.get("media"), Mapping) and not item.get("pk") and not item.get("code"):
        item = item["media"]
    if isinstance(item.get("node"), Mapping) and not item.get("pk") and not item.get("shortcode"):
        item = item["node"]

    raw_pk = str(item.get("pk") or item.get("id") or "").strip()
    if "_" in raw_pk:
        pk_only = raw_pk.split("_", 1)[0]
    else:
        pk_only = raw_pk

    code = str(item.get("code") or item.get("shortcode") or "").strip()
    if not code and pk_only.isdigit():
        code = media_id_to_shortcode(pk_only)
    if not code:
        return None
    if not pk_only and code:
        pk_only = shortcode_to_media_id(code)

    user = item.get("user") if isinstance(item.get("user"), Mapping) else (
        item.get("owner") if isinstance(item.get("owner"), Mapping) else {}
    )
    username = str(user.get("username") or item.get("author_unique_id") or "").strip()
    author_name = str(user.get("full_name") or username or item.get("author_name") or "").strip()
    avatar = str(user.get("profile_pic_url") or user.get("profile_pic_url_hd") or item.get("avatar") or "").strip()

    mtype = _to_int(item.get("media_type"))
    typename = str(item.get("__typename") or "")
    product_type = str(item.get("product_type") or "").lower()

    medias: List[MediaItem] = []
    quality_label = ""
    cover = _best_image_url(item)

    # 轮播: Mobile API carousel_media 或 GraphQL edge_sidecar_to_children
    carousel = item.get("carousel_media")
    sidecar = item.get("edge_sidecar_to_children")
    if isinstance(carousel, list) and carousel:
        for idx, sub in enumerate(carousel):
            if not isinstance(sub, Mapping):
                continue
            if not cover:
                cover = _best_image_url(sub)
            mi = _extract_media_items_from_node(sub, index=idx)
            if mi is not None:
                medias.append(mi)
    elif isinstance(sidecar, Mapping) and isinstance(sidecar.get("edges"), list):
        for idx, edge in enumerate(sidecar["edges"]):
            node = edge.get("node") if isinstance(edge, Mapping) else None
            if not isinstance(node, Mapping):
                continue
            if not cover:
                cover = _best_image_url(node)
            mi = _extract_media_items_from_node(node, index=idx)
            if mi is not None:
                medias.append(mi)
    else:
        is_vid = (
            mtype == 2
            or bool(item.get("is_video"))
            or typename in {"GraphVideo", "XDTGraphVideo"}
            or product_type in {"clips", "igtv"}
            or bool(item.get("video_versions"))
        )
        if is_vid:
            vurl, quality_label = _best_video_url(item)
            if vurl:
                medias.append(MediaItem(url=vurl, kind="video", ext="mp4", index=0))
        else:
            if cover:
                medias.append(MediaItem(url=cover, kind="image", ext="jpeg", index=0))

    has_any_video = any(m.kind == "video" for m in medias)
    is_carousel = (
        mtype == 8
        or typename in {"GraphSidecar", "XDTGraphSidecar"}
        or len(medias) > 1
    )
    if is_carousel:
        media_type = "images"
    elif has_any_video or mtype == 2 or bool(item.get("is_video")) or product_type in {"clips", "igtv"}:
        media_type = "video"
    else:
        media_type = "images"

    if require_media and not medias:
        return None

    like_count = _to_int(
        item.get("like_count")
        or ((item.get("edge_media_preview_like") or {}).get("count") if isinstance(item.get("edge_media_preview_like"), Mapping) else 0)
        or ((item.get("edge_liked_by") or {}).get("count") if isinstance(item.get("edge_liked_by"), Mapping) else 0)
    )
    comment_count = _to_int(
        item.get("comment_count")
        or ((item.get("edge_media_to_comment") or {}).get("count") if isinstance(item.get("edge_media_to_comment"), Mapping) else 0)
        or ((item.get("edge_media_to_parent_comment") or {}).get("count") if isinstance(item.get("edge_media_to_parent_comment"), Mapping) else 0)
    )
    create_time = _unix_seconds(item.get("taken_at") or item.get("taken_at_timestamp") or item.get("create_time"))
    duration = _to_int(round(float(item.get("video_duration") or item.get("duration") or 0)))

    is_reel = product_type == "clips" or (media_type == "video" and not is_carousel and product_type != "igtv")
    path_kind = "reel" if product_type == "clips" else ("tv" if product_type == "igtv" else ("reel" if is_reel and mtype == 2 and item.get("clips_metadata") else "p"))
    source_url = str(item.get("source_url") or f"https://www.instagram.com/{path_kind}/{code}/")

    return Aweme(
        aweme_id=code,
        desc=_extract_caption(item),
        create_time=create_time,
        author_name=author_name,
        media_type=media_type,
        medias=medias,
        cover=cover or (medias[0].url if medias and medias[0].kind == "image" else ""),
        quality_label=quality_label,
        like_count=like_count,
        comment_count=comment_count,
        duration=max(0, duration),
        avatar=avatar,
        platform="instagram",
        source_url=source_url,
        author_unique_id=username or pk_only,
    )


def parse_ig_creator_work(item: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """把本账号帖子/Reel 归一为 `AccountWork` 入库字典。"""
    aw = parse_ig_aweme(item, require_media=False)
    if aw is None:
        return None
    play_count = _to_int(
        item.get("play_count")
        or item.get("ig_play_count")
        or item.get("view_count")
        or item.get("video_view_count")
        or ((item.get("media") or {}).get("play_count") if isinstance(item.get("media"), Mapping) else 0)
    )
    return {
        "item_id": aw.aweme_id,
        "desc": aw.desc,
        "media_type": aw.media_type,
        "cover_url": aw.cover or "",
        "create_time": aw.create_time,
        "like_count": aw.like_count,
        "comment_count": aw.comment_count,
        "collect_count": 0,
        "share_count": _to_int(item.get("reshare_count") or 0),
        "play_count": play_count,
        "status": "公开",
        "duration": aw.duration,
        "source_url": aw.source_url,
    }


_NDJSON_MERGE_LIST_KEYS = (
    "comments",
    "preview_comments",
    "child_comments",
    "fb_comments",
    "items",
    "users",
    "sections",
    "threads",
)


def parse_ig_json_or_ndjson(raw_text: str) -> Any:
    """解析标准单对象 JSON 或 Instagram 分块多行 NDJSON (`stream_comments/`、`clips/user/` 等)。"""
    text = str(raw_text or "").strip()
    if not text or not text.startswith(("{", "[")):
        return None
    try:
        return json.loads(text)
    except ValueError:
        if "\n" not in text:
            return None

    merged: Dict[str, Any] = {}
    parsed_any = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, Mapping):
            continue
        parsed_any = True
        for k, v in obj.items():
            if k in _NDJSON_MERGE_LIST_KEYS and isinstance(v, list):
                existing = merged.get(k)
                if isinstance(existing, list):
                    existing.extend(v)
                else:
                    merged[k] = list(v)
            elif v is not None and v != "":
                merged[k] = v
            elif k not in merged:
                merged[k] = v
    return merged if parsed_any else None


def parse_ig_reels_chunked_response(raw_text: str) -> Tuple[List[Dict[str, Any]], str, bool]:
    """解析 `clips/user/` 分块多行 JSON 响应（`X-Ig-Transfer-Encoding: chunked`），
    逐行合并 `items[].media`，并从尾段 `paging_info` 提取 `max_id` 与 `more_available`。
    """
    medias: List[Dict[str, Any]] = []
    next_max_id = ""
    more_available = False
    for raw_line in str(raw_text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if not isinstance(obj, Mapping):
            continue
        for entry in (obj.get("items") or []):
            if not isinstance(entry, Mapping):
                continue
            media = entry.get("media") if isinstance(entry.get("media"), Mapping) else entry
            if isinstance(media, Mapping) and (media.get("pk") or media.get("id") or media.get("code")):
                medias.append(dict(media))
        pinfo = obj.get("paging_info")
        if isinstance(pinfo, Mapping):
            if pinfo.get("max_id") is not None:
                next_max_id = str(pinfo.get("max_id") or "")
            if "more_available" in pinfo:
                more_available = bool(pinfo.get("more_available"))
    return medias, next_max_id, more_available


def parse_ig_self_user(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """解析 `users/{id}/info/`、`users/web_profile_info/` 或 `accounts/current_user/` 用户资料。"""
    if not isinstance(payload, Mapping):
        return {}
    user = payload.get("user") if isinstance(payload.get("user"), Mapping) else (
        ((payload.get("data") or {}).get("user") if isinstance(payload.get("data"), Mapping) else None)
    )
    u: Mapping[str, Any] = user if isinstance(user, Mapping) else payload
    uid = str(u.get("pk") or u.get("pk_id") or u.get("id") or "").strip()
    username = str(u.get("username") or "").strip()
    full_name = str(u.get("full_name") or "").strip()
    avatar = str(u.get("profile_pic_url_hd") or u.get("profile_pic_url") or "").strip()
    follower_count = _to_int(
        u.get("follower_count")
        or ((u.get("edge_followed_by") or {}).get("count") if isinstance(u.get("edge_followed_by"), Mapping) else 0)
    )
    following_count = _to_int(
        u.get("following_count")
        or ((u.get("edge_follow") or {}).get("count") if isinstance(u.get("edge_follow"), Mapping) else 0)
    )
    aweme_count = _to_int(
        u.get("media_count")
        or ((u.get("edge_owner_to_timeline_media") or {}).get("count") if isinstance(u.get("edge_owner_to_timeline_media"), Mapping) else 0)
    )
    return {
        "uid": uid,
        "sec_uid": uid or username,
        "unique_id": username,
        "douyin_id": username,
        "nickname": full_name or username,
        "avatar": avatar,
        "signature": str(u.get("biography") or "").strip(),
        "follower_count": follower_count,
        "following_count": following_count,
        "aweme_count": aweme_count,
        "total_favorited": 0,
        "is_private": bool(u.get("is_private")),
        "is_verified": bool(u.get("is_verified")),
    }


parse_ig_user = parse_ig_self_user


def parse_ig_comment(raw: Mapping[str, Any], *, reply_to: str = "") -> Optional[Dict[str, Any]]:
    """提取规范化单条评论结构（支持顶层评论与二级回复）。"""
    if not isinstance(raw, Mapping):
        return None
    cid = str(raw.get("pk") or raw.get("id") or "").strip()
    if not cid:
        return None
    user = raw.get("user") if isinstance(raw.get("user"), Mapping) else (
        raw.get("owner") if isinstance(raw.get("owner"), Mapping) else {}
    )
    parent_id = str(reply_to or raw.get("parent_comment_id") or "").strip()
    like_cnt = raw.get("comment_like_count")
    if like_cnt is None:
        like_cnt = raw.get("like_count")
        if like_cnt is None and isinstance(raw.get("edge_liked_by"), Mapping):
            like_cnt = raw["edge_liked_by"].get("count")
    username = str(user.get("username") or "").strip()
    uid = str(user.get("pk") or user.get("id") or username).strip()
    return {
        "comment_id": cid,
        "text": str(raw.get("text") or "").strip(),
        "user_nickname": str(user.get("full_name") or username or "").strip(),
        "user_sec_uid": username or uid,
        "user_uid": uid,
        "avatar": str(user.get("profile_pic_url") or "").strip(),
        "ip_label": "",
        "like_count": _to_int(like_cnt),
        "create_time": _unix_seconds(raw.get("created_at_utc") or raw.get("created_at")),
        "reply_to": parent_id,
    }


def flatten_ig_comments(
    comments: Iterable[Mapping[str, Any]],
    *,
    include_replies: bool = True,
) -> List[Dict[str, Any]]:
    """展平顶层评论及其内嵌 `preview_child_comments` / `child_comments`。"""
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for c in comments or []:
        if not isinstance(c, Mapping):
            continue
        node = c.get("node") if isinstance(c.get("node"), Mapping) else c
        parsed = parse_ig_comment(node)
        if not parsed or parsed["comment_id"] in seen:
            continue
        seen.add(parsed["comment_id"])
        out.append(parsed)
        if not include_replies:
            continue
        children: List[Any] = []
        for child_key in ("preview_child_comments", "child_comments"):
            if isinstance(node.get(child_key), list):
                children.extend(node[child_key])
        threaded = node.get("edge_threaded_comments")
        if isinstance(threaded, Mapping) and isinstance(threaded.get("edges"), list):
            children.extend(threaded["edges"])
        for sub in children:
            sub_node = sub.get("node") if isinstance(sub, Mapping) and isinstance(sub.get("node"), Mapping) else sub
            if not isinstance(sub_node, Mapping):
                continue
            child_parsed = parse_ig_comment(sub_node, reply_to=parsed["comment_id"])
            if child_parsed and child_parsed["comment_id"] not in seen:
                seen.add(child_parsed["comment_id"])
                out.append(child_parsed)
    return out


def parse_ig_follow_users(payload: Mapping[str, Any], *, default_following: bool = True) -> List[Dict[str, Any]]:
    """把 `friendships/{userId}/followers/` 或 `/following/` 的 `users[]` 转为 `FollowEdge` 结构。"""
    if not isinstance(payload, Mapping):
        return []
    users = payload.get("users")
    if not isinstance(users, list):
        return []
    out: List[Dict[str, Any]] = []
    for u in users:
        if not isinstance(u, Mapping):
            continue
        pk = str(u.get("pk") or u.get("pk_id") or u.get("id") or "").strip()
        username = str(u.get("username") or "").strip()
        if not pk and not username:
            continue
        fs = u.get("friendship_status") if isinstance(u.get("friendship_status"), Mapping) else {}
        is_following = bool(fs.get("following")) if "following" in fs else default_following
        followed_by = bool(fs.get("followed_by"))
        out.append({
            "uid": pk or username,
            "sec_uid": username or pk,
            "nickname": str(u.get("full_name") or username or pk).strip(),
            "avatar": str(u.get("profile_pic_url") or "").strip(),
            "signature": str(u.get("biography") or "").strip(),
            "is_following": is_following,
            "is_mutual": bool(is_following and followed_by),
        })
    return out


def format_dm_item_text(item: Mapping[str, Any]) -> Tuple[str, str]:
    """将 Direct v2 消息节点解析为 `(msg_type, text)` 摘要文本。"""
    item_type = str(item.get("item_type") or item.get("type") or "text").strip()
    if isinstance(item.get("story_share"), Mapping):
        st = str(item["story_share"].get("text") or "").strip()
        return "story_share", f"Replied to story :{st}" if st else "Replied to story"
    if isinstance(item.get("reel_share"), Mapping):
        rs = item["reel_share"]
        rt = str(rs.get("type") or "reply").strip()
        txt = str(rs.get("text") or "").strip()
        return "reel_share", f"{rt} from story:{txt}" if txt else f"{rt} from story"
    if item_type == "action_log" and isinstance(item.get("action_log"), Mapping):
        return "action_log", str(item["action_log"].get("description") or "").strip()
    if item_type == "link" and isinstance(item.get("link"), Mapping):
        lk = item["link"]
        ltxt = str(lk.get("text") or "").strip()
        return "link", f"link:{ltxt}" if ltxt else str(item.get("text") or "")
    if item_type == "raven_media" or isinstance(item.get("visual_media"), Mapping):
        return "image", "One time view(disappearing image/video)"
    if item_type == "profile" and isinstance(item.get("profile"), Mapping):
        uname = str(item["profile"].get("username") or "").strip()
        return "profile", f"Shared profile :{uname}"
    if item_type in {"media", "media_share", "clip"}:
        txt = str(item.get("text") or "").strip()
        return "media", txt or "[媒体分享]"
    return ("text" if item_type == "text" else item_type), str(item.get("text") or "").strip()


def parse_ig_dm_inbox(payload: Mapping[str, Any], *, self_uid: str = "") -> List[Dict[str, Any]]:
    """解析 `direct_v2/inbox/` 或 `pending_inbox/` 为 `DmConversation` 兼容字典列表。"""
    if not isinstance(payload, Mapping):
        return []
    inbox = payload.get("inbox") if isinstance(payload.get("inbox"), Mapping) else payload
    threads = inbox.get("threads")
    if not isinstance(threads, list):
        return []
    out: List[Dict[str, Any]] = []
    for th in threads:
        if not isinstance(th, Mapping):
            continue
        thread_id = str(th.get("thread_id") or th.get("thread_v2_id") or "").strip()
        if not thread_id:
            continue
        users = [u for u in (th.get("users") or []) if isinstance(u, Mapping)]
        peer = None
        for u in users:
            upk = str(u.get("pk") or u.get("id") or "")
            if self_uid and upk == str(self_uid):
                continue
            peer = u
            break
        if peer is None and users:
            peer = users[0]
        if peer is None and isinstance(th.get("inviter"), Mapping):
            peer = th["inviter"]

        peer_uid = str((peer or {}).get("pk") or (peer or {}).get("id") or "").strip()
        peer_username = str((peer or {}).get("username") or "").strip()
        peer_nick = str(
            th.get("thread_title")
            or (peer or {}).get("full_name")
            or peer_username
            or peer_uid
        ).strip()
        peer_avatar = str((peer or {}).get("profile_pic_url") or "").strip()

        items = [i for i in (th.get("items") or []) if isinstance(i, Mapping)]
        last_text = ""
        last_time = _unix_seconds(th.get("last_activity_at"))
        if items:
            items_sorted = sorted(items, key=lambda x: _unix_seconds(x.get("timestamp")), reverse=True)
            _, last_text = format_dm_item_text(items_sorted[0])
            last_time = max(last_time, _unix_seconds(items_sorted[0].get("timestamp")))

        read_state = _to_int(th.get("read_state"))
        unread = _to_int(th.get("unread_count")) or (1 if read_state > 0 else 0)
        out.append({
            "conv_id": thread_id,
            "peer_uid": peer_uid,
            "peer_sec_uid": peer_username or peer_uid,
            "peer_nickname": peer_nick,
            "peer_avatar": peer_avatar,
            "last_text": last_text,
            "last_time": last_time,
            "unread_count": unread,
        })
    return out


def parse_ig_dm_messages(payload: Mapping[str, Any], *, self_uid: str = "") -> List[Dict[str, Any]]:
    """解析 `direct_v2/threads/{threadId}/` 为按时间升序排列的 `DmMessage` 列表。"""
    if not isinstance(payload, Mapping):
        return []
    thread = payload.get("thread") if isinstance(payload.get("thread"), Mapping) else payload
    items = thread.get("items")
    if not isinstance(items, list):
        return []
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for it in items:
        if not isinstance(it, Mapping):
            continue
        msg_id = str(it.get("item_id") or it.get("client_context") or "").strip()
        if not msg_id or msg_id in seen:
            continue
        seen.add(msg_id)
        sender_id = str(it.get("user_id") or "").strip()
        direction = "out" if (self_uid and sender_id == str(self_uid)) else "in"
        msg_type, text = format_dm_item_text(it)
        out.append({
            "msg_id": msg_id,
            "server_msg_id": msg_id,
            "sender_uid": sender_id,
            "direction": direction,
            "msg_type": msg_type,
            "text": text,
            "create_time": _unix_seconds(it.get("timestamp")),
        })
    out.sort(key=lambda m: m["create_time"])
    return out
