"""从视频号助手(mmfinderassistant-bin)响应里提取作品 / 评论 / 账号资料。

复用抖音的 Aweme / MediaItem 数据类(与其它平台统一,下载器按 aw.platform 决定 Referer)。

⚠️ 视频号字段名以真实账号抓包为准。这里全部走「多候选键兜底」(_first),
   字段对不上时优先在本文件补候选键,并核对 channels_fetcher 打印的样本。
⚠️ 视频号视频是加密 CDN,拿不到可直接下载的直链是常态 —— 故 parse_channels_feed
   允许返回**无 medias 的 Aweme**(只记元数据+统计,不进下载管线)。
"""
from __future__ import annotations

from typing import List, Optional

from ..douyin.extract import Aweme, MediaItem, safe_title  # noqa: F401  (复用 & 转出)


def _first(d: dict, *keys, default=None):
    if not isinstance(d, dict):
        return default
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return default


def _to_int(v) -> int:
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        s = v.strip()
        try:
            if s.endswith("万"):
                return int(float(s[:-1]) * 10000)
            return int(float(s))
        except (ValueError, TypeError):
            return 0
    return 0


def _media_list(obj: dict) -> list:
    """视频号作品的媒体数组:objectDesc.media[] / media[]。"""
    od = _first(obj, "objectDesc", default={}) or {}
    ml = _first(od, "media", default=None) or _first(obj, "media", default=None) or []
    return ml if isinstance(ml, list) else []


def parse_channels_feed(item: dict, quality: str = "highest") -> Optional[Aweme]:
    """解析视频号助手 post_list 里的一条作品。
    与其它平台不同:即使拿不到可下载媒体也返回 Aweme(视频号视频加密,主要用于
    记录元数据 + 统计 + 作品健康监控),medias 可能为空。"""
    if not isinstance(item, dict):
        return None
    oid = str(_first(item, "objectId", "exportId", "id", default="") or "")
    if not oid:
        return None

    od = _first(item, "objectDesc", default={}) or {}
    desc = (_first(od, "description", default="")
            or _first(item, "desc", "description", "title", default="") or "").strip()
    # 视频号 createtime 多为秒级
    ts = _to_int(_first(item, "createtime", "createTime", "create_time", "postTime",
                        default=0))
    create_time = ts // 1000 if ts > 10_000_000_000 else ts

    medias = _media_list(item)
    first_media = medias[0] if medias and isinstance(medias[0], dict) else {}
    cover = (_first(first_media, "coverUrl", "thumbUrl", "fullCoverUrl", default="")
             or _first(item, "coverUrl", "cover", default="") or "")
    # fileFormat / mediaType 判定图文还是视频
    mtype = "video"
    fmt = str(_first(first_media, "fileFormat", "mediaType", default="")).lower()
    if fmt and ("pic" in fmt or "image" in fmt or "img" in fmt):
        mtype = "images"

    aw = Aweme(
        aweme_id=oid,
        desc=desc,
        create_time=create_time,
        author_name=_first(item, "nickname", "finderNickname", default="") or "",
        media_type=mtype,
    )
    aw.platform = "shipinhao"
    aw.cover = cover
    aw.like_count = _to_int(_first(item, "likeCount", "like_count", "fav", default=0))
    aw.comment_count = _to_int(_first(item, "commentCount", "comment_count", default=0))
    dur = _to_int(_first(first_media, "videoPlayLen", "duration", default=0))
    aw.duration = dur // 1000 if dur > 100000 else dur

    # 尽力取可下载直链(多数情况下拿不到明文 url —— 加密 CDN,允许为空)
    for m in medias:
        if not isinstance(m, dict):
            continue
        url = _first(m, "url", "videoUrl", "originUrl", default="")
        if isinstance(url, str) and url.startswith("http"):
            kind = "image" if mtype == "images" else "video"
            ext = "jpeg" if kind == "image" else "mp4"
            aw.medias.append(MediaItem(url=url, kind=kind, ext=ext, index=len(aw.medias)))
    if aw.medias:
        aw.quality_label = quality or ""
    # 注意:视频号故意「无媒体也返回」,与 ks(无媒体返回 None)不同
    return aw


def parse_channels_comment(raw: dict) -> Optional[dict]:
    """解析一条视频号评论。返回规范化 dict 或 None。"""
    if not isinstance(raw, dict):
        return None
    cid = str(_first(raw, "commentId", "comment_id", "id", default="") or "")
    if not cid:
        return None
    ts = _to_int(_first(raw, "createtime", "createTime", "create_time", default=0))
    create_time = ts // 1000 if ts > 10_000_000_000 else ts
    return {
        "comment_id": cid,
        "text": (_first(raw, "content", "text", "commentContent", default="") or "").strip(),
        "user_nickname": _first(raw, "nickname", "userName", "author_name", default="") or "",
        "like_count": _to_int(_first(raw, "likeCount", "like_count", default=0)),
        "create_time": create_time,
        "reply_to": str(_first(raw, "replyCommentId", "reply_to", "rootCommentId",
                               default="") or ""),
    }


def flatten_channels_comments(root_comments: list) -> list:
    """把视频号根评论 + 其子评论摊平成一维列表。"""
    out: list = []
    for rc in (root_comments or []):
        if not isinstance(rc, dict):
            continue
        out.append(rc)
        for sc in (rc.get("replyList") or rc.get("subComments") or rc.get("children") or []):
            if isinstance(sc, dict):
                out.append(sc)
    return out


def parse_self_user(u: dict) -> dict:
    """把视频号 auth/get_auth_info 里的 finder 信息归一成账号资料 dict
    (同抖音 parse_self_user 形状)。字段以真实抓包为准,多候选键兜底。"""
    if not isinstance(u, dict):
        return {"nickname": "", "sec_uid": "", "douyin_id": "", "avatar": "",
                "follower_count": 0, "aweme_count": 0}
    # 常见形态:{finderUser:{...}} / {finder_info:{...}} / 顶层就是 finder 信息
    finder = (_first(u, "finderUser", "finder_info", "finderInfo", "user", default=None)
              or u)
    avatar = _first(finder, "headImgUrl", "headUrl", "headImgurl", "avatar",
                    "coverImgUrl", default="") or ""
    return {
        "nickname": _first(finder, "nickname", "nickName", "name", default="") or "",
        "sec_uid": str(_first(finder, "username", "finderUsername", "uid", "id",
                              default="") or ""),
        "douyin_id": str(_first(finder, "uniqId", "finderUniqId", "wxNumber",
                                default="") or ""),   # 视频号号
        "avatar": avatar,
        "follower_count": _to_int(_first(finder, "fansCount", "followerCount",
                                         "fans_count", default=0)),
        "aweme_count": _to_int(_first(finder, "feedCount", "postCount", "objectCount",
                                      default=0)),
    }
