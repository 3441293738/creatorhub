"""从快手 Web GraphQL 响应里提取可下载媒体 / 评论 / 账号资料。

快手 PC 站(www.kuaishou.com)所有数据都走 POST /graphql,前端拦截响应即可拿到:
  - visionProfile / visionProfilePhotoList  -> 用户资料 / 作品列表
  - visionVideoDetail                       -> 单条作品详情
  - commentListQuery / visionCommentList    -> 评论列表
复用抖音的 Aweme / MediaItem 数据类(下载器按 aw.platform 决定 Referer)。

⚠️ 快手字段随版本变化较多,这里全部走「多候选键兜底」解析(_first / _dig)。
若某天结构变了,优先在本文件加候选键,不要散落到引擎里。
"""
from __future__ import annotations

import json
from typing import List, Optional

from ..douyin.extract import Aweme, MediaItem, safe_title  # noqa: F401  (复用 & 转出)


def _first(d: dict, *keys, default=None):
    """按顺序取第一个非空字段(防御快手字段改名)。"""
    if not isinstance(d, dict):
        return default
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return default


def _to_int(v) -> int:
    """快手计数常是字符串('1.2万' / '12000' / 12000),尽量转成整数。"""
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        s = v.strip()
        try:
            if s.endswith("万"):
                return int(float(s[:-1]) * 10000)
            if s.endswith("亿"):
                return int(float(s[:-1]) * 100000000)
            return int(float(s))
        except (ValueError, TypeError):
            return 0
    return 0


def _abs_url(host: str, path: str) -> str:
    """拼图集直链:host 可能不带协议,path 以 / 开头。"""
    if not path:
        return ""
    if path.startswith("http"):
        return path
    host = (host or "").strip().rstrip("/")
    if host and not host.startswith("http"):
        host = "https://" + host
    return f"{host}{path}" if host else path


def _video_url(photo: dict) -> Optional[str]:
    """取视频直链。快手 PhotoEntity 的 photoUrl 就是可直接下载的 mp4(实测),
    优先用它;没有再从 DASH manifest 里兜底挑一条。"""
    pu = _first(photo, "photoUrl", "photoH265Url", "mainMvUrl", "srcUrl", default="")
    if isinstance(pu, list):
        pu = pu[0] if pu else ""
    if pu:
        return pu
    # 兜底:manifest(字符串需先 json.loads;dict 直接用)自适应码率挑一条
    manifest = _first(photo, "manifest", "videoResource", default={}) or {}
    if isinstance(manifest, str):
        try:
            manifest = json.loads(manifest)
        except Exception:
            manifest = {}
    best = None
    best_score = -1
    for aset in (manifest.get("adaptationSet") or []):
        for rep in (aset.get("representation") or []):
            url = _first(rep, "url", "backupUrl", default="")
            if isinstance(url, list):
                url = url[0] if url else ""
            if not url:
                continue
            score = _to_int(_first(rep, "avgBitrate", "maxBitrate", "quality", default=0))
            if score > best_score:
                best, best_score = url, score
    return best or None


def _images_from_atlas(photo: dict) -> List[MediaItem]:
    """快手图集:ext_params.atlas / atlas 里 cdn[] + list[] 拼出每张图直链。"""
    atlas = (_first(photo, "atlas", default={})
             or (_first(photo, "ext_params", default={}) or {}).get("atlas")
             or {})
    if not isinstance(atlas, dict):
        return []
    cdns = atlas.get("cdn") or atlas.get("cdnList") or []
    host = cdns[0] if cdns else ""
    paths = atlas.get("list") or []
    out: List[MediaItem] = []
    for i, p in enumerate(paths):
        url = _abs_url(host, p)
        if url:
            out.append(MediaItem(url=url, kind="image", ext="jpeg", index=i))
    return out


def parse_ks_feed(feed: dict, quality: str = "highest") -> Optional[Aweme]:
    """解析快手一个 feed(visionProfilePhotoList.feeds[] / visionSearchPhoto.feeds[])。
    quality 目前快手 manifest 已按码率优选,保留参数与抖音签名一致(暂未细分档位)。"""
    if not isinstance(feed, dict):
        return None
    photo = _first(feed, "photo", default={}) or {}
    author = _first(feed, "author", default={}) or photo.get("author") or {}
    photo_id = str(_first(photo, "id", "photoId", default="")
                   or _first(feed, "id", default="") or "")
    if not photo_id:
        return None

    desc = (_first(photo, "caption", "captionWithEmojiTrans", default="") or "").strip()
    # 快手时间戳是毫秒
    ts = _to_int(_first(photo, "timestamp", "createTime", default=0))
    create_time = ts // 1000 if ts > 10_000_000_000 else ts

    aw = Aweme(
        aweme_id=photo_id,
        desc=desc,
        create_time=create_time,
        author_name=_first(author, "name", "userName", default="") or "",
        media_type="video",
    )
    aw.platform = "kuaishou"
    aw.cover = _first(photo, "coverUrl", "webpCoverUrl", "coverUrls", default="") or ""
    if isinstance(aw.cover, list):
        aw.cover = (aw.cover[0].get("url") if isinstance(aw.cover[0], dict)
                    else aw.cover[0]) if aw.cover else ""
    aw.avatar = _first(author, "headerUrl", "headurl", "headerUrls", default="") or ""
    if isinstance(aw.avatar, list):
        aw.avatar = (aw.avatar[0].get("url") if isinstance(aw.avatar[0], dict)
                     else aw.avatar[0]) if aw.avatar else ""
    aw.like_count = _to_int(_first(photo, "realLikeCount", "likeCount", default=0))
    aw.comment_count = _to_int(_first(photo, "commentCount", default=0))
    dur_ms = _to_int(_first(photo, "duration", default=0))
    aw.duration = dur_ms // 1000 if dur_ms else 0

    # 视频优先(快手绝大多数是视频,photoUrl 直链最可靠);无 photoUrl 再看图集
    url = _video_url(photo)
    imgs = _images_from_atlas(photo)
    if url:
        aw.media_type = "video"
        aw.medias.append(MediaItem(url=url, kind="video", ext="mp4", index=0))
        aw.quality_label = quality or ""
    elif imgs:
        aw.media_type = "images"
        aw.medias = imgs

    return aw if aw.medias else None


def parse_ks_comment(raw: dict) -> Optional[dict]:
    """解析一条快手评论。返回规范化 dict 或 None。
    兼容两套字段:REST v2(snake_case:comment_id/author_name/headurl)与
    旧 GraphQL(camelCase:commentId/authorName)。"""
    if not isinstance(raw, dict):
        return None
    cid = str(_first(raw, "comment_id", "commentId", "id", default="") or "")
    if not cid:
        return None
    ts = _to_int(_first(raw, "timestamp", "createTime", "create_time", default=0))
    create_time = ts // 1000 if ts > 10_000_000_000 else ts
    return {
        "comment_id": cid,
        "text": (_first(raw, "content", "text", default="") or "").strip(),
        "user_nickname": _first(raw, "author_name", "authorName", "userName", "name",
                                default="") or "",
        "like_count": _to_int(_first(raw, "likedCount", "realLikedCount", "like_count",
                                     "likeCount", default=0)),
        "create_time": create_time,
        "reply_to": str(_first(raw, "replyToCommentId", "reply_to", "replyTo",
                               default="") or ""),
    }


def flatten_ks_comments(root_comments: list) -> list:
    """把快手根评论 + 其 subComments 摊平成一维列表(与小红书 flatten 同角色)。"""
    out: list = []
    for rc in (root_comments or []):
        if not isinstance(rc, dict):
            continue
        out.append(rc)
        for sc in (rc.get("subCommentsV2") or rc.get("subComments")
                   or rc.get("subComment") or []):
            if isinstance(sc, dict):
                out.append(sc)
    return out


def parse_self_user(u: dict) -> dict:
    """把快手 visionProfile.userProfile 归一成账号资料 dict(同抖音 parse_self_user 形状)。
    结构:userProfile = {profile:{user_id,user_name,headurl,...}, ownerCount:{fan,photo,...}}"""
    if not isinstance(u, dict):
        return {"nickname": "", "sec_uid": "", "douyin_id": "", "avatar": "",
                "follower_count": 0, "following_count": 0,
                "aweme_count": 0, "total_favorited": 0, "gender": ""}
    # 兼容直接传 profile 或传整个 userProfile
    profile = _first(u, "profile", default=None) or u
    counts = _first(u, "ownerCount", "counts", default={}) or {}
    avatar = _first(profile, "headurl", "headerUrl", "headUrl", "userHead",
                    "avatar", default="") or ""
    if isinstance(avatar, list):
        avatar = avatar[0] if avatar else ""
    return {
        "nickname": _first(profile, "user_name", "name", "userName", default="") or "",
        "sec_uid": str(_first(profile, "user_id", "eid", "encryptedUserId",
                               "userId", "id", default="") or ""),
        "douyin_id": str(_first(profile, "kwaiId", "userDefineId", default="") or ""),  # 快手号
        "avatar": avatar,
        "follower_count": _to_int(_first(counts, "fan", "fans", "follower",
                                              "fanCount", default=0)),
        "following_count": _to_int(_first(
            counts, "follow", "follows", "following", "followCount", default=0)),
        # 作品数:快手 ownerCount 用 photo_public(实测),兜底 photo
        "aweme_count": _to_int(_first(counts, "photo_public", "photo", "photoCount",
                                      "work", default=0)),
        "total_favorited": _to_int(_first(
            counts, "like", "likes", "liked", "likeCount", default=0)),
        "gender": str(_first(profile, "gender", "sex", default="") or ""),
    }
