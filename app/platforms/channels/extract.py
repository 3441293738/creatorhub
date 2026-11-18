"""从视频号助手(mmfinderassistant-bin)、微信原生 Finder(ChannelsObject)及公开预览响应提取作品 / 评论 / 账号资料。

能力要点:
  1. 同时兼容创作者助手 `post_list`、微信原生 `finderGetCommentDetail` / `finderUserPage` / `finderSearch`
     以及公开预览 `get_feed_info` 数据结构;
  2. 自动拼接 CDN `url + urlToken`，按 `spec` 分辨率/码率档位(`X-snsvideoflag=<fileFormat>`)选择画质，
     并保留精确十进制 `decodeKey` 供下载器离线执行 `WxIsaac64` 128KiB 解密 + MP4 结构核验;
  3. 若平台仅返回无直链元数据(如创作者助手部分加密条目)，仍返回 `medias=[]` 的 `Aweme` 保留元数据与统计;
  4. 评论解析完整保留 `ipRegionInfo.regionText`(`ip_label`)，修正根评论 `replyCommentId="0"` 误判，
     并支持展开原生接口的 `levelTwoComment` 二级嵌套列表。
"""
from __future__ import annotations

import html
import re
from typing import Any, List, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..douyin.extract import Aweme, MediaItem, safe_title  # noqa: F401
from .decrypt import ChannelsDecryptError, validate_decode_key
from .resolve import build_finder_feed_url


_HIGHLIGHT_TAG_RE = re.compile(
    r"</?(?:em|mark|span|b|strong|i)\b[^>]*>",
    re.IGNORECASE,
)


def _clean_channels_text(value: Any) -> str:
    """去除视频号搜索结果中的 `<em class=\"highlight\">...</em>` 高亮标签并反转义 HTML 实体。"""
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    if "&" in text:
        text = html.unescape(text)
    if "<" in text:
        text = _HIGHLIGHT_TAG_RE.sub("", text)
    return text.strip()


def _first_positive_int(*values: Any) -> int:
    for val in values:
        num = _to_int(val)
        if num > 0:
            return num
    return 0


def _first(d: Any, *keys: str, default: Any = None) -> Any:
    if not isinstance(d, dict):
        return default
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return default


def _to_int(v: Any) -> int:
    if isinstance(v, bool) or v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "").replace("+", "")
        if not s:
            return 0
        try:
            if s.endswith("亿"):
                return int(float(s[:-1]) * 100_000_000)
            if s.endswith("万"):
                return int(float(s[:-1]) * 10_000)
            if s.lower().endswith("w"):
                return int(float(s[:-1]) * 10_000)
            if s.lower().endswith("k"):
                return int(float(s[:-1]) * 1_000)
            return int(float(s))
        except (ValueError, TypeError):
            return 0
    return 0


def _join_url_token(url: str, url_token: str) -> str:
    clean_url = (url or "").strip()
    token = (url_token or "").strip()
    if not clean_url:
        return ""
    if not token or token in clean_url:
        return clean_url
    if token.startswith(("?", "&")):
        if "?" in clean_url and token.startswith("?"):
            return clean_url + "&" + token[1:]
        if "?" not in clean_url and token.startswith("&"):
            return clean_url + "?" + token[1:]
        return clean_url + token
    sep = "&" if "?" in clean_url else "?"
    return clean_url + sep + token


def _with_sns_video_flag(url: str, file_format: str) -> str:
    fmt = (file_format or "").strip()
    if not url or not fmt:
        return url
    try:
        parts = urlsplit(url)
        pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "X-snsvideoflag"]
        pairs.append(("X-snsvideoflag", fmt))
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(pairs), parts.fragment))
    except Exception:
        return url


def _select_channels_video_spec(
    base_url: str, specs: Any, quality: str = "highest"
) -> tuple[str, str]:
    """根据 `spec` 数组选择对应画质的 `X-snsvideoflag` 与质量标签。"""
    if not isinstance(specs, list) or not specs:
        return base_url, (quality or "")

    candidates: list[tuple[int, int, str, str]] = []
    for sp in specs:
        if not isinstance(sp, dict):
            continue
        fmt = str(_first(sp, "fileFormat", "file_format", default="") or "").strip()
        if not fmt:
            continue
        w = _to_int(_first(sp, "width", default=0))
        h = _to_int(_first(sp, "height", default=0))
        short_edge = min(w, h) if w and h else max(w, h)
        bitrate = _to_int(_first(sp, "videoBitrate", "video_bitrate", "bitRate", "bit_rate", default=0))
        label = f"{short_edge}p" if short_edge else fmt
        candidates.append((bitrate, short_edge, fmt, label))

    if not candidates:
        return base_url, (quality or "")

    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    q = (quality or "highest").strip().lower().rstrip("p")
    if q in ("highest", "best", "原画", ""):
        chosen = candidates[0]
    elif q in ("lowest", "worst", "省流"):
        chosen = candidates[-1]
    elif q.isdigit():
        target = int(q)
        within = [c for c in candidates if c[1] and c[1] <= target]
        chosen = within[0] if within else candidates[-1]
    else:
        chosen = candidates[0]

    return _with_sns_video_flag(base_url, chosen[2]), chosen[3]


def _extract_decode_key(media_obj: dict, item_obj: dict | None = None) -> str:
    raw_key = _first(media_obj, "decodeKey", "decode_key", "encfilekey", "key", default=None)
    if raw_key in (None, "", 0, "0"):
        raw_key = _first(item_obj or {}, "decodeKey", "decode_key", "key", default=None)
    if raw_key in (None, "", 0, "0"):
        return ""
    try:
        normalized = validate_decode_key(raw_key)
        return "" if normalized == "0" else normalized
    except ChannelsDecryptError:
        return ""


def _media_list(obj: dict) -> list[dict]:
    od = _first(obj, "objectDesc", "desc", "feedInfo", default={}) or {}
    if not isinstance(od, dict):
        od = {}
    ml = (
        _first(od, "media", "picInfo", default=None)
        or _first(obj, "media", "files", "pic_info", default=None)
        or []
    )
    return [m for m in ml if isinstance(m, dict)] if isinstance(ml, list) else []


def parse_channels_feed(item: dict, quality: str = "highest") -> Optional[Aweme]:
    """解析视频号助手 / 原生 Finder / 公开预览的一条作品。"""
    if not isinstance(item, dict):
        return None
    # 若传入的是 `{data: {object: {...}}}` 包装结构，自动解包
    if "object" in item and isinstance(item["object"], dict) and not _first(item, "objectId", "id", "exportId", "share_id"):
        item = item["object"]

    oid = str(
        _first(item, "objectId", "id", "exportId", "export_id", "share_id", default="") or ""
    ).strip()
    if not oid:
        return None

    nonce_id = str(
        _first(item, "objectNonceId", "object_nonce_id", "nonceId", "nonce_id", default="") or ""
    ).strip()

    od = _first(item, "objectDesc", "desc", "feedInfo", default={}) or {}
    if not isinstance(od, dict):
        od = {}

    desc = _clean_channels_text(
        _first(od, "description", "shortTitle", default="")
        or _first(item, "description", "title", default="")
        or ""
    )

    ts = _to_int(
        _first(
            item, "createtime", "createTime", "create_time", "postTime",
            default=_first(od, "createtime", "createTime", default=0),
        )
    )
    create_time = ts // 1000 if ts > 10_000_000_000 else ts

    contact = (
        _first(item, "contact", "author", "finderUser", "authorInfo", default=None)
        or {}
    )
    if not isinstance(contact, dict):
        contact = {}
    author_name = _clean_channels_text(
        _first(contact, "nickname", "nickName", "highlightNickname", "name", default="")
        or _first(
            item,
            "nickname", "finderNickname", "highlightNickname", "author", "author_nickname",
            default="",
        )
        or ""
    )
    author_uid = str(
        _first(contact, "username", "finderUsername", "id", default="")
        or _first(item, "username", "finderUsername", default="")
        or ""
    )
    avatar = str(
        _first(contact, "headUrl", "headImgUrl", "avatar_url", "avatar", default="")
        or _first(item, "author_avatar", default="")
        or ""
    )

    medias = _media_list(item)
    first_media = medias[0] if medias else {}
    cover = str(
        _first(first_media, "coverUrl", "thumbUrl", "fullCoverUrl", default="")
        or _first(od, "coverUrl", default="")
        or _first(item, "coverUrl", "cover_url", "cover", default="")
        or ""
    )

    fmt = str(_first(first_media, "fileFormat", "mediaType", default="")).lower()
    media_type_code = _first(od, "mediaType", default=_first(item, "media_type", "type", default=None))
    has_pic_only = bool(
        (isinstance(od.get("picInfo"), list) and od.get("picInfo"))
        or (isinstance(item.get("pic_info"), list) and item.get("pic_info"))
    ) and not any(
        _first(item, "video_url", "h265_video_url", "h264_video_url")
        or _first(od, "videoUrl", "h265VideoInfo", "h264VideoInfo")
    )

    mtype = "video"
    if (
        media_type_code in (2, "2", "picture", "images")
        or fmt == "2"
        or has_pic_only
        or any(tok in fmt for tok in ("pic", "image", "img"))
    ):
        mtype = "images"

    direct_ci = item.get("countInfo") if isinstance(item.get("countInfo"), dict) else {}
    mono = item.get("monotonicData") if isinstance(item.get("monotonicData"), dict) else {}
    mono_ci = mono.get("countInfo") if isinstance(mono.get("countInfo"), dict) else {}
    mono_comment_raw = mono.get("commentCount")
    mono_comment = (
        mono_comment_raw.get("commentCount")
        if isinstance(mono_comment_raw, dict)
        else mono_comment_raw
    )
    ext = item.get("objectExtend") if isinstance(item.get("objectExtend"), dict) else {}
    ext_mono = ext.get("monotonicData") if isinstance(ext.get("monotonicData"), dict) else {}
    ext_ci = ext_mono.get("countInfo") if isinstance(ext_mono.get("countInfo"), dict) else {}
    ext_comment_raw = ext_mono.get("commentCount")
    ext_comment = (
        ext_comment_raw.get("commentCount")
        if isinstance(ext_comment_raw, dict)
        else ext_comment_raw
    )
    counts_display = item.get("counts_display") if isinstance(item.get("counts_display"), dict) else {}

    like_count = _first_positive_int(
        item.get("likeCount"),
        item.get("like_count"),
        direct_ci.get("likeCount"),
        mono_ci.get("likeCount"),
        ext_ci.get("likeCount"),
        od.get("likeCountFmt"),
        counts_display.get("likeCountFmt"),
        item.get("favCount"),
        item.get("fav"),
        direct_ci.get("favCount"),
        mono_ci.get("favCount"),
        ext_ci.get("favCount"),
    )
    comment_count = _first_positive_int(
        item.get("commentCount"),
        item.get("comment_count"),
        direct_ci.get("commentCount"),
        mono_ci.get("commentCount"),
        mono_comment,
        ext_ci.get("commentCount"),
        ext_comment,
        od.get("commentCountFmt"),
        counts_display.get("commentCountFmt"),
    )
    dur = _to_int(_first(first_media, "videoPlayLen", "duration", "durationMs", default=0))
    duration = dur // 1000 if dur > 100_000 else dur

    source_url = str(_first(item, "source_url", "share_url", "pageurl", default="") or "").strip()
    if not source_url and oid.isdecimal() and nonce_id:
        source_url = build_finder_feed_url(oid, nonce_id)

    aw = Aweme(
        aweme_id=oid,
        desc=desc,
        create_time=create_time,
        author_name=author_name,
        media_type=mtype,
        cover=cover,
        like_count=like_count,
        comment_count=comment_count,
        duration=duration,
        avatar=avatar,
        platform="shipinhao",
        source_url=source_url,
        author_unique_id=author_uid,
        object_nonce_id=nonce_id,
    )

    selected_label = ""
    for m in medias:
        raw_url = str(_first(m, "url", "videoUrl", "originUrl", default="") or "").strip()
        url_token = str(_first(m, "urlToken", "url_token", default="") or "").strip()
        full_url = _join_url_token(raw_url, url_token)
        if not full_url.startswith("http"):
            continue
        if mtype == "images":
            aw.medias.append(MediaItem(url=full_url, kind="image", ext="jpeg", index=len(aw.medias)))
        else:
            specs = _first(m, "spec", default=None) or _first(item, "spec", default=None)
            final_url, q_label = _select_channels_video_spec(full_url, specs, quality)
            if q_label and not selected_label:
                selected_label = q_label
            dec_key = _extract_decode_key(m, item)
            aw.medias.append(
                MediaItem(
                    url=final_url,
                    kind="video",
                    ext="mp4",
                    index=len(aw.medias),
                    decode_key=dec_key,
                )
            )

    # 兼容公开预览 `get_feed_info` 顶层 videoUrl / h265VideoInfo / h264VideoInfo
    if not aw.medias and mtype == "video":
        preview_candidates = [
            str(item.get("h265_video_url") or ""),
            str(((od.get("h265VideoInfo") or {}) if isinstance(od.get("h265VideoInfo"), dict) else {}).get("videoUrl") or ""),
            str(item.get("h264_video_url") or ""),
            str(((od.get("h264VideoInfo") or {}) if isinstance(od.get("h264VideoInfo"), dict) else {}).get("videoUrl") or ""),
            str(_first(item, "video_url", "url", default=_first(od, "videoUrl", default="")) or ""),
        ]
        if (quality or "").strip().lower() in ("lowest", "worst", "省流"):
            preview_candidates = list(reversed(preview_candidates))
        chosen_preview = next((u.strip() for u in preview_candidates if u.strip().startswith("http")), "")
        if chosen_preview:
            dec_key = _extract_decode_key({}, item)
            aw.medias.append(
                MediaItem(url=chosen_preview, kind="video", ext="mp4", index=0, decode_key=dec_key)
            )

    if aw.medias:
        aw.quality_label = selected_label or quality or ""
    return aw


def parse_channels_comment(raw: dict) -> Optional[dict]:
    """解析一条视频号评论(兼容助手响应与原生 `pc_findergetcommentlist` 记录)。"""
    if not isinstance(raw, dict):
        return None
    cid = str(_first(raw, "commentId", "comment_id", "id", default="") or "").strip()
    if not cid:
        return None
    ts = _to_int(_first(raw, "createtime", "createTime", "create_time", "create_time_raw", default=0))
    create_time = ts // 1000 if ts > 10_000_000_000 else ts

    author_contact = raw.get("authorContact") if isinstance(raw.get("authorContact"), dict) else {}
    sec_uid = str(
        _first(raw, "username", "finderUsername", "userId", "user_id", default="")
        or _first(author_contact, "username", default="")
        or ""
    )
    nickname = str(
        _first(raw, "nickname", "userName", "author_name", default="")
        or _first(author_contact, "nickname", default="")
        or ""
    )
    ip_info = raw.get("ipRegionInfo") if isinstance(raw.get("ipRegionInfo"), dict) else {}
    ip_label = str(
        _first(ip_info, "regionText", default="")
        or _first(raw, "ip_region", "ip_label", "ipLabel", default="")
        or ""
    ).strip()

    reply_raw = str(
        _first(raw, "replyCommentId", "parent_comment_id", "reply_to", default="") or ""
    ).strip()
    if reply_raw == "0":
        reply_raw = ""
    if not reply_raw:
        root_raw = str(_first(raw, "rootCommentId", "root_comment_id", default="") or "").strip()
        if root_raw and root_raw != "0" and root_raw != cid:
            reply_raw = root_raw

    return {
        "comment_id": cid,
        "text": str(_first(raw, "content", "text", "commentContent", default="") or "").strip(),
        "user_nickname": nickname,
        "user_sec_uid": sec_uid,
        "ip_label": ip_label,
        "like_count": _to_int(_first(raw, "likeCount", "like_count", default=0)),
        "create_time": create_time,
        "reply_to": reply_raw,
    }


def flatten_channels_comments(root_comments: list) -> list:
    """把视频号根评论 + 其二级评论(`levelTwoComment` / `replyList` / `subComments`)摊平成一维列表。"""
    out: list[dict] = []
    for rc in root_comments or []:
        if not isinstance(rc, dict):
            continue
        out.append(rc)
        root_id = str(_first(rc, "commentId", "comment_id", "id", default="") or "").strip()
        nested = (
            rc.get("levelTwoComment")
            or rc.get("replyList")
            or rc.get("subComments")
            or rc.get("children")
            or []
        )
        if not isinstance(nested, list):
            continue
        for sc in nested:
            if not isinstance(sc, dict):
                continue
            child = dict(sc)
            if root_id and not child.get("rootCommentId"):
                child["rootCommentId"] = root_id
            out.append(child)
    return out


def parse_self_user(u: dict) -> dict:
    """把视频号 auth/get_auth_info 里的 finder 信息归一成账号资料 dict。"""
    if not isinstance(u, dict):
        return {
            "nickname": "", "sec_uid": "", "douyin_id": "", "avatar": "",
            "follower_count": 0, "aweme_count": 0,
        }
    finder = (
        _first(u, "finderUser", "finder_info", "finderInfo", "contact", "user", default=None)
        or u
    )
    if not isinstance(finder, dict):
        finder = u
    avatar = (
        _first(finder, "headImgUrl", "headUrl", "headImgurl", "avatar", "coverImgUrl", default="")
        or ""
    )
    return {
        "nickname": _first(finder, "nickname", "nickName", "name", default="") or "",
        "sec_uid": str(_first(finder, "username", "finderUsername", "uid", "id", default="") or ""),
        "douyin_id": str(_first(finder, "uniqId", "finderUniqId", "wxNumber", default="") or ""),
        "avatar": avatar,
        "follower_count": _to_int(_first(finder, "fansCount", "followerCount", "fans_count", default=0)),
        "aweme_count": _to_int(_first(finder, "feedCount", "postCount", "objectCount", default=0)),
    }
