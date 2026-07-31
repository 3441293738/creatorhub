"""把小红书的接口 JSON 提取成与抖音一致的 Aweme(供下载器/通知复用)。
小红书改版时,字段名容错都集中在这里改。
"""
from __future__ import annotations

import time
from typing import List, Optional

from ..douyin.extract import Aweme, MediaItem


def _first(d: dict, *keys, default=None):
    for k in keys:
        v = (d or {}).get(k)
        if v not in (None, "", 0, []):
            return v
    return default


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
    """从 user_posted.notes[] 或 search.items[] 取 {note_id, xsec_token, type, title, cover}。"""
    if not isinstance(item, dict):
        return None
    note_id = str(_first(item, "note_id", "id", default="") or "")
    xsec_token = str(item.get("xsec_token") or "")
    card = item.get("note_card") or item            # search 把卡片放在 note_card 里
    if not note_id:
        note_id = str(_first(card, "note_id", "id", default="") or "")
    if not note_id:
        return None
    cover = card.get("cover") or {}
    cov_url = _first(cover, "url_default", "url_pre", "url") or ""
    if not cov_url and isinstance(cover.get("info_list"), list) and cover["info_list"]:
        cov_url = cover["info_list"][0].get("url", "")
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
    return {
        "note_id": note_id,
        "xsec_token": xsec_token,
        "type": card.get("type") or "normal",       # normal | video
        "title": _first(card, "display_title", "title", "desc", default="") or "",
        "cover": cov_url,
        "create_time": create_time,
    }


def _img_url(img: dict) -> Optional[str]:
    """从一张图的多档里取一个可用直链(优先无水印大图)。"""
    info = img.get("info_list") or []
    # 优先 WB_DFT(默认)/ WB_PRV 之外的高清场景
    for scene in ("WB_DFT", "WB_PRV"):
        for it in info:
            if it.get("image_scene") == scene and it.get("url"):
                return it["url"]
    for it in info:
        if it.get("url"):
            return it["url"]
    return _first(img, "url_default", "url_pre", "url")


def _video_url(note_card: dict) -> Optional[str]:
    video = note_card.get("video") or {}
    media = video.get("media") or {}
    stream = media.get("stream") or {}
    for codec in ("h264", "h265", "av1", "h266"):
        arr = stream.get(codec) or []
        for s in arr:
            url = s.get("master_url") or (s.get("backup_urls") or [None])[0]
            if url:
                return url
    # 老结构兜底:consumer.origin_video_key
    key = ((video.get("consumer") or {}).get("origin_video_key")
           or (video.get("consumer") or {}).get("originVideoKey"))
    if key:
        return f"https://sns-video-bd.xhscdn.com/{key}"
    return None


def parse_note_detail(note_card: dict, brief: dict | None = None) -> Optional[Aweme]:
    """把 feed 接口的 note_card 解析成 Aweme(含媒体直链)。"""
    if not isinstance(note_card, dict):
        return None
    note_id = str(_first(note_card, "note_id", "id",
                         default=(brief or {}).get("note_id", "")) or "")
    if not note_id:
        return None
    user = note_card.get("user") or {}
    title = _first(note_card, "title", "display_title", default="") or ""
    desc = (note_card.get("desc") or "").strip()
    full_desc = (title + ("\n" + desc if desc and desc != title else "")).strip() \
        or (brief or {}).get("title", "")
    interact = note_card.get("interact_info") or {}
    ntype = note_card.get("type") or (brief or {}).get("type") or "normal"
    created = int(_first(note_card, "time", "create_time",
                         default=(brief or {}).get("create_time", 0)) or 0)
    if created > 10_000_000_000:
        created //= 1000

    aw = Aweme(
        aweme_id=note_id,
        desc=full_desc,
        create_time=created,
        author_name=_first(user, "nickname", "nick_name", "name", default="") or "",
        media_type="video" if ntype == "video" else "images",
    )
    aw.platform = "xhs"
    aw.avatar = _first(user, "avatar", "images", default="") or ""
    aw.like_count = _num(_first(interact, "liked_count", "likedCount", default=0))
    aw.comment_count = _num(_first(interact, "comment_count", "commentCount", default=0))

    if ntype == "video":
        url = _video_url(note_card)
        if url:
            aw.medias.append(MediaItem(url=url, kind="video", ext="mp4", index=0))
        # 视频也带封面图列表,用第一张做 cover
        imgs = note_card.get("image_list") or []
        if imgs:
            aw.cover = _img_url(imgs[0])
    else:
        for i, img in enumerate(note_card.get("image_list") or []):
            url = _img_url(img)
            if url:
                aw.medias.append(MediaItem(url=url, kind="image", ext="jpeg", index=i))
        if aw.medias:
            aw.cover = aw.medias[0].url
    if not aw.cover:
        aw.cover = (brief or {}).get("cover") or ""
    return aw if aw.medias else None


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
    """把含 sub_comments 的评论树拍平成一维(一级 + 子评论)。"""
    out = []
    for c in raw_list or []:
        out.append(c)
        for sub in (c.get("sub_comments") or c.get("subComments") or []):
            out.append(sub)
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
