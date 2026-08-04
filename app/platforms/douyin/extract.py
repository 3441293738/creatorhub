"""从抖音作品 JSON 提取可下载媒体。对应逆向 engine.ContentChecker.processVideo 的解析部分。"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class MediaItem:
    url: str
    kind: str          # "video" | "image"
    ext: str           # "mp4" | "jpeg" ...
    index: int = 0


@dataclass
class Aweme:
    aweme_id: str
    desc: str
    create_time: int
    author_name: str
    media_type: str             # "video" | "images"
    medias: List[MediaItem] = field(default_factory=list)
    cover: Optional[str] = None
    quality_label: str = ""     # 实际选中的画质,如 "1080p"
    like_count: int = 0
    comment_count: int = 0
    duration: int = 0           # 秒
    avatar: str = ""            # 作者头像
    platform: str = "douyin"    # douyin | xhs(决定下载时的 Referer)


def _first_http(urls) -> Optional[str]:
    return next((u for u in (urls or []) if u.startswith("http")), None)


def _fallback_video_url(video: dict) -> Optional[str]:
    for key in ("play_addr", "play_addr_h264", "download_addr"):
        url = _first_http((video.get(key) or {}).get("url_list"))
        if url:
            return url.replace("playwm", "play")  # 去水印兜底
    return None


def select_video_url(video: dict, quality: str = "highest"):
    """按画质偏好从 bit_rate 多档里挑一档。
    quality: highest(最高/原画级) | lowest(省流) | "1080" | "720" | "540" ...
    返回 (url, 短边分辨率int, 码率int)。挑不到时回退到默认 play_addr。
    """
    cands = []
    for b in (video.get("bit_rate") or []):
        addr = b.get("play_addr") or {}
        url = _first_http(addr.get("url_list"))
        if not url:
            continue
        w, h = addr.get("width") or 0, addr.get("height") or 0
        short = min(w, h) if w and h else 0
        cands.append((url, short, b.get("bit_rate") or 0))

    if not cands:
        return _fallback_video_url(video), 0, 0

    cands.sort(key=lambda c: c[2], reverse=True)  # 按码率从高到低
    if quality == "highest":
        return cands[0]
    if quality == "lowest":
        return cands[-1]
    # 目标分辨率:取不超过目标的最高画质;都超了就取最低那档
    try:
        target = int(quality)
    except (TypeError, ValueError):
        return cands[0]
    within = [c for c in cands if c[1] and c[1] <= target]
    return within[0] if within else cands[-1]


def parse_aweme(item: dict, quality: str = "highest") -> Optional[Aweme]:
    aweme_id = str(item.get("aweme_id") or "")
    if not aweme_id:
        return None
    author = item.get("author") or {}
    desc = (item.get("desc") or "").strip()
    create_time = int(item.get("create_time") or 0)
    aw = Aweme(
        aweme_id=aweme_id,
        desc=desc,
        create_time=create_time,
        author_name=author.get("nickname") or "",
        media_type="video",
    )

    # 作者头像
    av = (author.get("avatar_thumb") or {}).get("url_list") or []
    if av:
        aw.avatar = av[0]

    # 互动数据 / 时长
    stats = item.get("statistics") or {}
    aw.like_count = int(stats.get("digg_count") or 0)
    aw.comment_count = int(stats.get("comment_count") or 0)
    dur_ms = (item.get("video") or {}).get("duration") or 0
    aw.duration = int(dur_ms / 1000) if dur_ms else 0

    # 封面
    cover = (item.get("video") or {}).get("cover") or {}
    cov_urls = cover.get("url_list") or []
    if cov_urls:
        aw.cover = cov_urls[0]

    images = item.get("images")
    if images:  # 图集
        aw.media_type = "images"
        for i, img in enumerate(images):
            urls = img.get("url_list") or []
            url = next((u for u in urls if u.startswith("http")), None)
            if url:
                aw.medias.append(MediaItem(url=url, kind="image", ext="jpeg", index=i))
    else:       # 视频
        url, short, _br = select_video_url(item.get("video") or {}, quality)
        if url:
            aw.medias.append(MediaItem(url=url, kind="video", ext="mp4", index=0))
            aw.quality_label = f"{short}p" if short else (quality or "")

    return aw if aw.medias else None


def parse_comment(raw: dict) -> Optional[dict]:
    """解析一条评论 JSON。返回规范化 dict 或 None。"""
    cid = str(raw.get("cid") or "")
    if not cid:
        return None
    user = raw.get("user") or {}
    return {
        "comment_id": cid,
        "text": (raw.get("text") or "").strip(),
        "user_nickname": user.get("nickname") or "",
        "like_count": int(raw.get("digg_count") or 0),
        "create_time": int(raw.get("create_time") or 0),
        "reply_to": str(raw.get("reply_id") or "") if raw.get("reply_id") not in (None, "0") else "",
    }


def _first_value(d: dict, *keys, default=None):
    for key in keys:
        value = d.get(key)
        if value not in (None, ""):
            return value
    return default


def _number(value, default=0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "blocked"}
    return bool(value)


def danmaku_key(raw: dict) -> str:
    """返回稳定弹幕主键，兼容播放页/创作中心的不同字段名。"""
    value = _first_value(raw, "danmaku_id", "barrage_id", "bullet_id",
                         "id", "cid", "comment_id")
    if value not in (None, ""):
        return str(value)
    stable = {
        "text": _first_value(raw, "content", "text", "danmaku_text", "body", default=""),
        "user": str(_first_value(raw, "user_id", "uid", default="")),
        "time": str(_first_value(raw, "video_time_ms", "offset_time", "time_point", "video_time",
                                 "timepoint", "position", default="")),
    }
    return "hash:" + hashlib.sha1(
        json.dumps(stable, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def parse_danmaku(raw: dict, default_aweme_id: str = "") -> Optional[dict]:
    """解析短视频弹幕 JSON，返回与数据库字段对应的规范化字典。"""
    if not isinstance(raw, dict):
        return None
    text = str(_first_value(raw, "content", "text", "danmaku_text", "body",
                            default="") or "").strip()
    if not text:
        return None
    did = danmaku_key(raw)
    user = (raw.get("user") or raw.get("user_info") or raw.get("author")
            or raw.get("commenter") or {})
    if not isinstance(user, dict):
        user = {}
    user_id = str(_first_value(raw, "user_id", "uid", "user_uid", default="") or
                  _first_value(user, "uid", "user_id", "sec_uid", default="") or "")
    nickname = str(_first_value(raw, "user_nickname", "nickname", "user_name",
                               default="") or
                   _first_value(user, "nickname", "name", "user_name", default="") or "")

    # 播放页当前返回 offset_time(毫秒),不是 time_point(秒)。保留两套字段兼容旧接口。
    ms_value = _first_value(raw, "video_time_ms", "position_ms", "time_ms",
                            "offset_time", "offsetTime", "video_offset")
    if ms_value not in (None, ""):
        video_time_ms = int(max(0, _number(ms_value)))
    else:
        sec_value = _first_value(raw, "time_point", "video_time", "timepoint",
                                 "position", "start_time", "startTime", default=0)
        video_time_ms = int(max(0, _number(sec_value) * 1000))

    create_value = _first_value(raw, "create_time", "createTime", "send_time",
                                "timestamp", "server_time", default=0)
    create_time = int(max(0, _number(create_value)))
    if create_time > 100_000_000_000:
        create_time //= 1000
    aweme_id = str(_first_value(raw, "aweme_id", "item_id", "group_id", "object_id",
                                default=default_aweme_id) or default_aweme_id)
    return {
        "aweme_id": aweme_id,
        "danmaku_id": did,
        "text": text,
        "user_id": user_id,
        "user_nickname": nickname,
        "video_time_ms": video_time_ms,
        "create_time": create_time,
        "like_count": int(max(0, _number(_first_value(
            raw, "digg_count", "like_count", "likes", default=0)))),
        "is_blocked": _as_bool(_first_value(
            raw, "is_blocked", "blocked", "is_block", default=False)),
        "raw_json": json.dumps(raw, ensure_ascii=False, separators=(",", ":")),
    }


def parse_self_user(u: dict) -> dict:
    """把抖音 user 对象归一成账号资料。"""
    avatar = (u.get("avatar_thumb") or u.get("avatar_larger") or {}).get("url_list") or []
    return {
        "nickname": u.get("nickname") or "",
        "sec_uid": u.get("sec_uid") or "",
        "douyin_id": str(u.get("unique_id") or u.get("short_id") or "") or "",
        "avatar": avatar[0] if avatar else "",
        "follower_count": int(u.get("follower_count") or 0),
        "aweme_count": int(u.get("aweme_count") or 0),
    }


def _first(d: dict, *keys, default=None):
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def parse_creator_comment(raw: dict) -> Optional[dict]:
    """⚠️ 实验性:解析创作中心评论(字段名按多种可能兜底)。
    返回含 aweme_id 的规范化 dict 或 None。抖音字段变了就在这里加候选键。"""
    cid = str(_first(raw, "cid", "comment_id", "id") or "")
    if not cid:
        return None
    user = raw.get("user") or raw.get("commenter") or raw.get("user_info") or {}
    aweme_id = str(_first(raw, "aweme_id", "item_id", "group_id", "object_id",
                          default="") or "")
    return {
        "aweme_id": aweme_id,
        "comment_id": cid,
        "text": str(_first(raw, "text", "content", "comment_text", default="")).strip(),
        "user_nickname": _first(user, "nickname", "name", "user_name", default="") or "",
        "like_count": int(_first(raw, "digg_count", "like_count", "diggCount",
                                 default=0) or 0),
        "create_time": int(_first(raw, "create_time", "createTime", "comment_time",
                                  default=0) or 0),
        "reply_to": "",
    }


def safe_title(text: str, limit: int = 60) -> str:
    text = re.sub(r"[\\/:*?\"<>|\r\n\t#]+", "_", text).strip("_ ")
    return (text or "untitled")[:limit]
