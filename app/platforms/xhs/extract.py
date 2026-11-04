"""把小红书的接口 JSON 提取成与抖音一致的 Aweme(供下载器/通知复用)。
小红书改版时,字段名容错都集中在这里改。
"""
from __future__ import annotations

import json
import time
from typing import List, Optional
from urllib.parse import urlsplit

from ..douyin.extract import Aweme, MediaItem


def _first(d: dict, *keys, default=None):
    if not isinstance(d, dict):
        return default
    for k in keys:
        v = d.get(k)
        if v not in (None, "", 0, []):
            return v
    return default


def _object(value) -> dict:
    return value if isinstance(value, dict) else {}


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _num(v) -> int:
    """小红书互动数可能是 "1.2万" / "999+" 这类字符串。"""
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v or "").strip().replace("+", "")
    try:
        if "万" in s:
            return int(float(s.replace("万", "")) * 10000)
        if "亿" in s:
            return int(float(s.replace("亿", "")) * 100000000)
        return int(float(s))
    except (ValueError, TypeError):
        return 0


# ── 列表项(user_posted / search 的精简卡片) ──
def parse_note_brief(item: dict) -> Optional[dict]:
    """Read a note card, preserving its token/source pair across list shapes."""
    if not isinstance(item, dict):
        return None
    # Search also returns users/topics/query suggestions with unrelated IDs.
    model_type = item.get("model_type") or item.get("modelType")
    if model_type and (not isinstance(model_type, str) or model_type not in {"note", "normal", "video"}):
        return None
    card = item.get("note_card") or item.get("noteCard") or item
    if not isinstance(card, dict):
        return None
    note_id = str(_first(card, "note_id", "noteId", default="")
                  or _first(item, "note_id", "noteId", default="")
                  or _first(card, "id", default="")
                  or _first(item, "id", default="") or "")
    if not note_id:
        return None
    outer_token = _text(_first(item, "xsec_token", "xsecToken"))
    inner_token = _text(_first(card, "xsec_token", "xsecToken"))
    owner, other = (item, card) if outer_token else (card, item)
    xsec_token = outer_token or inner_token
    xsec_source = _text(_first(owner, "xsec_source", "xsecSource"))
    if not xsec_source and _text(_first(other, "xsec_token", "xsecToken")) in {"", xsec_token}:
        xsec_source = _text(_first(other, "xsec_source", "xsecSource"))
    cover = card.get("cover") or {}
    if not isinstance(cover, dict):
        cover = {}
    cov_url = _first(cover, "url_default", "urlDefault", "url_pre", "urlPre", "url") or ""
    cover_info = _first(cover, "info_list", "infoList")
    if not cov_url and isinstance(cover_info, list):
        cov_url = next((entry.get("url", "") for entry in cover_info
                        if isinstance(entry, dict) and entry.get("url")), "")
    create_time = _num(_first(card, "time", "create_time", "createTime", "timestamp",
                              default=0))
    if create_time > 10_000_000_000:
        create_time //= 1000
    if not create_time and len(note_id) >= 8:
        # 小红书 ObjectId 风格 note_id 的前 8 位通常为发布时间戳。
        try:
            candidate = int(note_id[:8], 16)
            if 946684800 <= candidate <= int(time.time()) + 86400:
                create_time = candidate
        except ValueError:
            pass
    user = _object(card.get("user"))
    return {
        "note_id": note_id,
        "xsec_token": xsec_token,
        "xsec_source": xsec_source,
        "type": card.get("type") or "normal",       # normal | video
        "title": _first(card, "display_title", "displayTitle", "title", "desc", default="") or "",
        "cover": cov_url,
        "create_time": create_time,
        "author_id": str(_first(user, "user_id", "userId", "userid", "id", default="") or ""),
        "author_name": _text(_first(user, "nickname", "nick_name", "nickName", "name")),
        "avatar": _text(_first(user, "avatar", "images")),
    }


def _img_url(img: dict) -> Optional[str]:
    """从一张图的多档里取一个可用直链(优先无水印大图)。"""
    info = _first(img, "info_list", "infoList", default=[])
    info = [it for it in info if isinstance(it, dict)] if isinstance(info, list) else []
    # 优先 WB_DFT(默认)/ WB_PRV 之外的高清场景
    for scene in ("WB_DFT", "WB_PRV"):
        for it in info:
            if _first(it, "image_scene", "imageScene") == scene and it.get("url"):
                return it["url"]
    for it in info:
        if it.get("url"):
            return it["url"]
    return _first(img, "url_default", "urlDefault", "url_pre", "urlPre", "url")


def _http_media_url(value) -> Optional[str]:
    """Keep signed URLs intact; malformed entries must not shadow backups."""
    url = _text(value)
    if not url:
        return None
    try:
        parsed = urlsplit(url)
        if (parsed.scheme in {"http", "https"} and parsed.hostname
                and not parsed.username and not parsed.password):
            return url
    except ValueError:
        pass
    return None


def _video_streams(video: dict) -> list[dict]:
    candidates = []
    for field in ("media", "media_v2", "mediaV2"):
        media = video.get(field)
        # media_v2 can be a JSON string even inside the page's normalized state.
        if field != "media" and isinstance(media, str):
            try:
                media = json.loads(media)
            except (ValueError, RecursionError):
                continue
        streams = _object(_object(media).get("stream"))
        # Codec group names are not stable (EF4/EF5, or e_f4/e_f5 after
        # browser decamelization). Inspect every group, not only known names.
        for group, entries in streams.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                codec = _text(_first(entry, "video_codec", "videoCodec")) or str(group)
                codec = codec.lower().split(".", 1)[0]
                priority = {
                    "h264": 0, "avc": 0, "avc1": 0,
                    "h265": 1, "hevc": 1, "hev1": 1, "hvc1": 1,
                    "av1": 2, "av01": 2, "h266": 3, "vvc": 3,
                }.get(codec, 4)
                candidates.append((priority, entry))
    # Stable sort retains source/quality order within a codec preference.
    return [entry for _, entry in sorted(candidates, key=lambda pair: pair[0])]


def _video_url(note_card: dict) -> Optional[str]:
    video = _object(note_card.get("video"))
    for stream in _video_streams(video):
        urls = [stream.get(key) for key in ("master_url", "masterUrl", "url")]
        for key in ("backup_urls", "backupUrls"):
            backups = stream.get(key)
            if isinstance(backups, list):
                urls.extend(backups)
        for value in urls:
            url = _http_media_url(value)
            if url:
                return url
    # 老结构兜底:consumer.origin_video_key
    key = _text(_first(video.get("consumer"), "origin_video_key", "originVideoKey"))
    if key:
        return f"https://sns-video-bd.xhscdn.com/{key}"
    return None


def parse_note_detail(note_card: dict, brief: dict | None = None, *,
                      require_media: bool = True) -> Optional[Aweme]:
    """Parse detail independently of media availability when explicitly requested.

    Download/monitor callers keep the strict default. Collections may preserve
    metadata with require_media=False, but must still report missing media.
    """
    if not isinstance(note_card, dict):
        return None
    note_id = str(_first(note_card, "note_id", "noteId", "id",
                         default=(brief or {}).get("note_id", "")) or "")
    if not note_id:
        return None
    user = _object(note_card.get("user"))
    title = _text(_first(note_card, "title", "display_title", "displayTitle"))
    desc = _text(note_card.get("desc"))
    full_desc = (title + ("\n" + desc if desc and desc != title else "")).strip() \
        or (brief or {}).get("title", "")
    interact = _object(_first(note_card, "interact_info", "interactInfo"))
    ntype = note_card.get("type") or (brief or {}).get("type") or "normal"
    created = _num(_first(note_card, "time", "create_time", "createTime",
                         default=(brief or {}).get("create_time", 0)))
    if created > 10_000_000_000:
        created //= 1000

    aw = Aweme(
        aweme_id=note_id,
        desc=full_desc,
        create_time=created,
        author_name=(_text(_first(user, "nickname", "nick_name", "nickName", "name"))
                     or (brief or {}).get("author_name", "")),
        media_type="video" if ntype == "video" else "images",
    )
    aw.platform = "xhs"
    aw.avatar = _text(_first(user, "avatar", "images")) or (brief or {}).get("avatar", "")
    aw.like_count = _num(_first(interact, "liked_count", "likedCount", default=0))
    aw.comment_count = _num(_first(interact, "comment_count", "commentCount", default=0))

    imgs = _first(note_card, "image_list", "imageList", default=[])
    imgs = [img for img in imgs if isinstance(img, dict)] if isinstance(imgs, list) else []
    if ntype == "video":
        url = _video_url(note_card)
        if url:
            aw.medias.append(MediaItem(url=url, kind="video", ext="mp4", index=0))
        # 视频也带封面图列表,用第一张做 cover
        if imgs:
            aw.cover = _img_url(imgs[0])
    else:
        for i, img in enumerate(imgs):
            url = _img_url(img)
            if url:
                aw.medias.append(MediaItem(url=url, kind="image", ext="jpeg", index=i))
        if aw.medias:
            aw.cover = aw.medias[0].url
    if not aw.cover:
        aw.cover = (brief or {}).get("cover") or ""
    return aw if aw.medias or not require_media else None


# ── 评论 ──
def parse_comment(raw: dict) -> Optional[dict]:
    cid = str(_first(raw, "id", "comment_id", default="") or "")
    if not cid:
        return None
    user = raw.get("user_info") or raw.get("user") or {}
    target = raw.get("target_comment") or {}
    return {
        "comment_id": cid,
        "text": (_first(raw, "content", "text", default="") or "").strip(),
        "user_nickname": _first(user, "nickname", "nick_name", "name", default="") or "",
        "like_count": _num(_first(raw, "like_count", "liked_count", default=0)),
        "create_time": int(_first(raw, "create_time", "createTime", default=0) or 0) // 1000
        if int(_first(raw, "create_time", "createTime", default=0) or 0) > 10_000_000_000
        else int(_first(raw, "create_time", "createTime", default=0) or 0),
        "reply_to": str(_first(target, "id", "comment_id", default="") or ""),
    }


def flatten_comments(raw_list: list) -> list:
    """Flatten once, preserving a reply's parent without mutating snapshots."""
    out = []
    seen = set()

    def append(raw, parent=""):
        if not isinstance(raw, dict):
            return
        cid = str(raw.get("id") or raw.get("comment_id") or "")
        if not cid or cid in seen:
            return
        value = dict(raw)
        if parent:
            target = value.get("target_comment") or {}
            if not (target.get("id") or target.get("comment_id")):
                value["target_comment"] = {**target, "id": parent}
        seen.add(cid)
        out.append(value)

    for c in raw_list or []:
        if not isinstance(c, dict):
            continue
        append(c)
        parent = str(c.get("id") or c.get("comment_id") or "")
        for sub in (c.get("sub_comments") or c.get("subComments") or []):
            append(sub, parent)
    return out


# ── 账号资料 ──
def _interaction(u: dict, *types) -> int:
    """从 otherinfo 的 interactions:[{type,count}] 里取某类计数(如 fans)。"""
    for it in (u.get("interactions") or []):
        if str(it.get("type") or it.get("name") or "").lower() in types:
            return _num(it.get("count"))
    return 0


def parse_self_user(u: dict) -> dict:
    """小红书 user/me + otherinfo 归一成账号资料。"""
    basic = u.get("basic_info") or u
    fans = _num(_first(u, "fans", "follower_count", default=0)) or \
        _interaction(u, "fans", "follower")
    notes = _num(_first(u, "notes", "note_count", "ndiscovery", default=0)) or \
        _interaction(u, "notes", "note")
    return {
        "nickname": _first(basic, "nickname", "nick_name", "name", default="") or "",
        "sec_uid": str(_first(u, "user_id", "userId", default="")
                       or _first(basic, "red_id", default="") or ""),
        "douyin_id": str(_first(u, "red_id", "redId", default="")
                         or _first(basic, "red_id", default="") or ""),
        "avatar": _first(basic, "images", "imageb", "avatar", default="") or "",
        "follower_count": fans,
        "aweme_count": notes,
    }
