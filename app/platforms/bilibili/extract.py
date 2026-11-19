"""从哔哩哔哩 Web / 创作中心 / 私信接口 JSON 与弹幕 XML 提取规范化数据。"""
from __future__ import annotations

import html
import json
import re
import xml.etree.ElementTree as ET
import zlib
from typing import Any, List, Mapping, Optional

from ..douyin.extract import Aweme, MediaItem, safe_title  # noqa: F401
from .resolve import av_to_bv

_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _first(d: Any, *keys: str, default: Any = None) -> Any:
    if not isinstance(d, Mapping):
        return default
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return default


def _to_int(v: Any) -> int:
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        s = v.strip().replace(",", "").replace("+", "")
        if not s or s == "-":
            return 0
        try:
            if s.endswith("万"):
                return int(float(s[:-1]) * 10000)
            if s.endswith("亿"):
                return int(float(s[:-1]) * 100000000)
            return int(float(s))
        except (TypeError, ValueError):
            return 0
    return 0


def _seconds(v: Any) -> int:
    val = _to_int(v)
    while val > 10_000_000_000:
        val //= 1000
    return max(0, val)


def abs_bili_url(url: Any) -> str:
    """把 `//i0.hdslb.com/...` 或 `http://i*.hdslb.com/...` 归一为 `https://` URL。"""
    s = str(url or "").strip()
    if not s:
        return ""
    if s.startswith("//"):
        return "https:" + s
    if s.startswith("http://") and ".hdslb.com/" in s:
        return "https://" + s[len("http://"):]
    return s


def clean_bili_text(value: Any) -> str:
    """剥离 B 站搜索接口返回的 `<em class="keyword">` 等高亮标签并反转义 HTML 实体。"""
    s = str(value or "")
    if not s:
        return ""
    if "<" in s and ">" in s:
        s = _HTML_TAG_RE.sub("", s)
    return html.unescape(s).strip()


def parse_bili_duration(value: Any) -> int:
    """支持整数秒与 `MM:SS` / `HH:MM:SS` 字符串时长。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0, int(value))
    s = str(value or "").strip()
    if not s:
        return 0
    if ":" in s:
        parts = s.split(":")
        try:
            acc = 0
            for part in parts:
                acc = acc * 60 + int(part)
            return max(0, acc)
        except ValueError:
            return 0
    return _to_int(s)


def _extract_opus_aweme(item: Mapping[str, Any], *, require_media: bool = False) -> Optional[Aweme]:
    """解析 B 站图文动态（opus / dynamic item）。"""
    opus_id = str(_first(item, "id_str", "dynamic_id_str", "opus_id", "id", default="") or "")
    modules = item.get("modules") if isinstance(item.get("modules"), Mapping) else {}
    if not opus_id or not modules:
        return None
    author_mod = modules.get("module_author") if isinstance(modules.get("module_author"), Mapping) else {}
    dyn_mod = modules.get("module_dynamic") if isinstance(modules.get("module_dynamic"), Mapping) else {}
    stat_mod = modules.get("module_stat") if isinstance(modules.get("module_stat"), Mapping) else {}

    major = dyn_mod.get("major") if isinstance(dyn_mod.get("major"), Mapping) else {}
    desc_obj = dyn_mod.get("desc") if isinstance(dyn_mod.get("desc"), Mapping) else {}
    opus = major.get("opus") if isinstance(major.get("opus"), Mapping) else {}
    draw = major.get("draw") if isinstance(major.get("draw"), Mapping) else {}
    archive = major.get("archive") if isinstance(major.get("archive"), Mapping) else {}

    if archive and _first(archive, "bvid"):
        merged = dict(archive)
        merged.setdefault("owner", {
            "mid": _first(author_mod, "mid", default=""),
            "name": _first(author_mod, "name", default=""),
            "face": _first(author_mod, "face", default=""),
        })
        merged.setdefault("pubdate", _first(author_mod, "pub_ts", default=0))
        return parse_bili_aweme(merged, require_media=require_media)

    title = clean_bili_text(_first(opus, "title", default=""))
    summary_obj = opus.get("summary") if isinstance(opus.get("summary"), Mapping) else {}
    body = clean_bili_text(_first(summary_obj, "text", default="") or _first(desc_obj, "text", default=""))
    full_desc = f"{title}\n{body}".strip() if (title and body and body != title) else (title or body)

    pics_raw = _first(opus, "pics", default=None) or _first(draw, "items", default=[]) or []
    medias: List[MediaItem] = []
    if isinstance(pics_raw, list):
        for idx, pic in enumerate(pics_raw):
            if not isinstance(pic, Mapping):
                continue
            u = abs_bili_url(_first(pic, "url", "src", default=""))
            if u:
                medias.append(MediaItem(url=u, kind="image", ext="jpeg", index=idx))

    like_obj = stat_mod.get("like") if isinstance(stat_mod.get("like"), Mapping) else {}
    comment_obj = stat_mod.get("comment") if isinstance(stat_mod.get("comment"), Mapping) else {}
    aw = Aweme(
        aweme_id=opus_id,
        desc=full_desc,
        create_time=_seconds(_first(author_mod, "pub_ts", "ptime", default=0)),
        author_name=clean_bili_text(_first(author_mod, "name", default="")),
        media_type="images",
        medias=medias,
        cover=medias[0].url if medias else "",
        like_count=_to_int(_first(like_obj, "count", default=0)),
        comment_count=_to_int(_first(comment_obj, "count", default=0)),
        avatar=abs_bili_url(_first(author_mod, "face", default="")),
        platform="bilibili",
        source_url=f"https://www.bilibili.com/opus/{opus_id}",
        author_unique_id=str(_first(author_mod, "mid", default="") or ""),
    )
    if require_media and not aw.medias:
        return None
    return aw


def parse_bili_aweme(
    item: Mapping[str, Any],
    *,
    play_url: str = "",
    audio_url: str = "",
    quality_label: str = "",
    require_media: bool = False,
) -> Optional[Aweme]:
    """解析 B 站公开视频（详情/空间投稿列表/关键词搜索）或图文动态为统一 `Aweme`。"""
    if not isinstance(item, Mapping):
        return None
    if isinstance(item.get("modules"), Mapping):
        return _extract_opus_aweme(item, require_media=require_media)

    result_type = str(item.get("type") or "").strip().lower()
    if result_type and result_type not in {"video", "ketang", ""}:
        # 搜索综合结果里排除纯用户/番剧建议卡片
        if not _first(item, "bvid", "aid"):
            return None

    bvid = str(_first(item, "bvid", default="") or "").strip()
    aid = _to_int(_first(item, "aid", "id", default=0))
    if not bvid and aid > 0:
        try:
            bvid = av_to_bv(aid)
        except ValueError:
            bvid = ""
    if not bvid:
        return None

    owner = item.get("owner") if isinstance(item.get("owner"), Mapping) else {}
    stat = item.get("stat") if isinstance(item.get("stat"), Mapping) else {}

    title = clean_bili_text(_first(item, "title", default=""))
    desc = clean_bili_text(_first(item, "desc", "description", default=""))
    if desc == "-":
        desc = ""
    full_desc = f"{title}\n{desc}".strip() if (title and desc and desc != title) else (title or desc)

    author_name = clean_bili_text(
        _first(owner, "name", default="") or _first(item, "author", "uname", default="")
    )
    mid = str(_first(owner, "mid", default="") or _first(item, "mid", default="") or "")
    avatar = abs_bili_url(
        _first(owner, "face", default="") or _first(item, "upic", "face", "avatar", default="")
    )
    cover = abs_bili_url(_first(item, "pic", "cover", default=""))
    create_time = _seconds(_first(item, "pubdate", "created", "senddate", "ctime", default=0))
    duration = parse_bili_duration(_first(item, "duration", "length", default=0))

    like_count = _to_int(_first(stat, "like", default=None) or _first(item, "like", default=0))
    comment_count = _to_int(
        _first(stat, "reply", default=None)
        or _first(item, "comment", "review", "video_review", default=0)
    )

    aw = Aweme(
        aweme_id=bvid,
        desc=full_desc,
        create_time=create_time,
        author_name=author_name,
        media_type="video",
        cover=cover,
        quality_label=quality_label,
        like_count=like_count,
        comment_count=comment_count,
        duration=duration,
        avatar=avatar,
        platform="bilibili",
        source_url=f"https://www.bilibili.com/video/{bvid}",
        author_unique_id=mid,
    )
    direct_url = str(play_url or _first(item, "play_url", default="") or "").strip()
    direct_audio = str(audio_url or _first(item, "audio_url", default="") or "").strip()
    if direct_url:
        aw.medias.append(
            MediaItem(url=direct_url, kind="video", ext="mp4", index=0, audio_url=direct_audio)
        )
    if require_media and not aw.medias:
        return None
    return aw


def parse_bili_creator_work(arc_audit: Mapping[str, Any]) -> Optional[dict]:
    """解析创作中心 `member.bilibili.com/x/web/archives` 单项 `arc_audits[]` 为 `AccountWork` 字典。"""
    if not isinstance(arc_audit, Mapping):
        return None
    archive = (
        arc_audit.get("Archive")
        if isinstance(arc_audit.get("Archive"), Mapping)
        else arc_audit.get("archive")
        if isinstance(arc_audit.get("archive"), Mapping)
        else arc_audit
    )
    stat = (
        arc_audit.get("stat")
        if isinstance(arc_audit.get("stat"), Mapping)
        else arc_audit.get("Stat")
        if isinstance(arc_audit.get("Stat"), Mapping)
        else {}
    )

    bvid = str(_first(archive, "bvid", default="") or "").strip()
    aid = _to_int(_first(archive, "aid", default=0))
    if not bvid and aid > 0:
        try:
            bvid = av_to_bv(aid)
        except ValueError:
            bvid = ""
    if not bvid:
        return None

    title = clean_bili_text(_first(archive, "title", "desc", default=""))
    state = _to_int(_first(archive, "state", default=0))
    state_desc = str(_first(archive, "state_desc", default="") or "").strip()
    reject_reason = str(_first(archive, "reject_reason", default="") or "").strip()
    if state < 0 and reject_reason and reject_reason not in state_desc:
        status = f"{state_desc}: {reject_reason}" if state_desc else reject_reason
    else:
        status = state_desc

    return {
        "item_id": bvid,
        "aid": aid,
        "desc": title,
        "media_type": "video",
        "cover_url": abs_bili_url(_first(archive, "cover", "pic", default="")),
        "create_time": _seconds(_first(archive, "ptime", "ctime", "pubdate", default=0)),
        "like_count": _to_int(_first(stat, "like", default=0)),
        "comment_count": _to_int(_first(stat, "reply", default=0)),
        "collect_count": _to_int(_first(stat, "fav", "favorite", default=0)),
        "share_count": _to_int(_first(stat, "share", default=0)),
        "play_count": _to_int(_first(stat, "view", default=0)),
        "danmaku_count": _to_int(_first(stat, "danmaku", default=0)),
        "coin_count": _to_int(_first(stat, "coin", default=0)),
        "duration": parse_bili_duration(_first(archive, "duration", default=0)),
        "is_only_self": _to_int(_first(archive, "is_only_self", default=0)),
        "status": status,
    }


def _strip_ip_prefix(loc: Any) -> str:
    s = str(loc or "").strip()
    for prefix in ("IP属地：", "IP属地:"):
        if s.startswith(prefix):
            return s[len(prefix):].strip()
    return s


def parse_bili_comment(raw: Mapping[str, Any], default_aweme_id: str = "") -> Optional[dict]:
    """解析公开评论 `/x/v2/reply/wbi/main` 单条评论，返回与 `CommentRecord` 一致的 8 字段字典。"""
    _ = default_aweme_id
    if not isinstance(raw, Mapping):
        return None
    cid = str(_first(raw, "rpid_str", "id_str", "rpid", "id", "comment_id", default="") or "").strip()
    if not cid or cid == "0":
        return None

    content = raw.get("content") if isinstance(raw.get("content"), Mapping) else {}
    member = raw.get("member") if isinstance(raw.get("member"), Mapping) else {}
    reply_ctrl = raw.get("reply_control") if isinstance(raw.get("reply_control"), Mapping) else {}

    text = str(_first(content, "message", default="") or _first(raw, "message", "text", default="") or "").strip()
    parent = str(_first(raw, "parent_str", "parent", default="") or "").strip()
    reply_to = parent if parent and parent not in {"0", cid} else ""

    return {
        "comment_id": cid,
        "text": text,
        "user_nickname": str(
            _first(member, "uname", "name", default="")
            or _first(raw, "replier_name", "uname", default="")
            or ""
        ),
        "user_sec_uid": str(
            _first(member, "mid", default="")
            or _first(raw, "mid", "replier_mid", default="")
            or ""
        ),
        "ip_label": _strip_ip_prefix(_first(reply_ctrl, "location", default="")),
        "like_count": _to_int(_first(raw, "like", "like_count", default=0)),
        "create_time": _seconds(_first(raw, "ctime", "create_time", default=0)),
        "reply_to": reply_to,
    }


def parse_bili_creator_comment(raw: Mapping[str, Any], default_aweme_id: str = "") -> Optional[dict]:
    """解析创作中心 `/x/v2/reply/up/fulllist` 评论项（额外附带 `aweme_id`）。"""
    base = parse_bili_comment(raw)
    if base is None:
        return None
    bvid = str(_first(raw, "bvid", default="") or default_aweme_id or "").strip()
    oid = _to_int(_first(raw, "oid", default=0))
    ctype = _to_int(_first(raw, "type", default=1))
    if not bvid and ctype == 1 and oid > 0:
        try:
            bvid = av_to_bv(oid)
        except ValueError:
            bvid = ""
    return {"aweme_id": bvid, **base}


def flatten_bili_comments(replies: Any, top_replies: Any = None) -> List[dict]:
    """将根评论及其内嵌 `replies[]` 二级楼中楼摊平为一维列表。"""
    out: List[dict] = []
    seen: set[str] = set()

    def _push(entry: Any) -> None:
        if not isinstance(entry, Mapping):
            return
        cid = str(_first(entry, "rpid_str", "id_str", "rpid", "id", default="") or "")
        if cid and cid not in seen:
            seen.add(cid)
            out.append(dict(entry))
        sub = entry.get("replies")
        if isinstance(sub, list):
            for child in sub:
                _push(child)

    if isinstance(top_replies, list):
        for item in top_replies:
            _push(item)
    if isinstance(replies, list):
        for item in replies:
            _push(item)
    return out


def parse_bili_danmaku_xml(xml_or_bytes: str | bytes, default_aweme_id: str = "") -> List[dict]:
    """解析 B 站 `/x/v1/dm/list.so?oid={cid}` XML 弹幕流（自动兼容 raw deflate / gzip / UTF-8 文本）。"""
    if isinstance(xml_or_bytes, (bytes, bytearray)):
        raw_bytes = bytes(xml_or_bytes)
        if not raw_bytes:
            return []
        if not raw_bytes.lstrip().startswith(b"<"):
            for wbits in (-zlib.MAX_WBITS, zlib.MAX_WBITS | 32):
                try:
                    raw_bytes = zlib.decompress(raw_bytes, wbits)
                    break
                except zlib.error:
                    continue
        xml_text = raw_bytes.decode("utf-8", errors="replace")
    else:
        xml_text = str(xml_or_bytes or "")

    xml_text = xml_text.strip()
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    records: List[dict] = []
    seen: set[str] = set()
    for elem in root.findall("d"):
        text = (elem.text or "").strip()
        p_attr = (elem.attrib.get("p") or "").strip()
        if not text or not p_attr:
            continue
        parts = p_attr.split(",")
        if len(parts) < 8:
            continue
        try:
            offset_sec = float(parts[0] or 0)
        except ValueError:
            offset_sec = 0.0
        video_time_ms = int(max(0.0, offset_sec * 1000))
        create_time = _seconds(parts[4])
        mid_hash = str(parts[6] or "").strip()
        dmid = str(parts[7] or "").strip()
        if not dmid or dmid in seen:
            continue
        seen.add(dmid)
        raw_obj = {
            "dmid": dmid,
            "p": p_attr,
            "text": text,
            "mid_hash": mid_hash,
        }
        records.append({
            "aweme_id": default_aweme_id,
            "danmaku_id": dmid,
            "text": text,
            "user_id": mid_hash,
            "user_nickname": "",
            "video_time_ms": video_time_ms,
            "create_time": create_time,
            "like_count": 0,
            "is_blocked": False,
            "raw_json": json.dumps(raw_obj, ensure_ascii=False, separators=(",", ":")),
        })
    return records


def parse_bili_self_user(
    nav_data: Mapping[str, Any],
    stat_data: Optional[Mapping[str, Any]] = None,
    account_data: Optional[Mapping[str, Any]] = None,
    archive_count: int = 0,
) -> dict:
    """将 `/x/web-interface/nav` + `/x/relation/stat` + `/x/member/web/account` 归一为 CreatorHub 账号资料。"""
    if not isinstance(nav_data, Mapping):
        return {
            "nickname": "",
            "sec_uid": "",
            "douyin_id": "",
            "avatar": "",
            "follower_count": 0,
            "following_count": 0,
            "aweme_count": 0,
            "total_favorited": 0,
            "gender": "",
        }
    stat = stat_data if isinstance(stat_data, Mapping) else {}
    acct = account_data if isinstance(account_data, Mapping) else {}
    mid = str(_first(nav_data, "mid", default="") or _first(acct, "mid", default="") or "")
    uname = str(_first(nav_data, "uname", default="") or _first(acct, "uname", default="") or "")
    userid = str(_first(acct, "userid", default="") or mid)
    sex = str(_first(acct, "sex", default="") or "")
    return {
        "nickname": uname,
        "uid": mid,
        "sec_uid": mid,
        "douyin_id": userid or mid,
        "avatar": abs_bili_url(_first(nav_data, "face", default="")),
        "follower_count": _to_int(_first(stat, "follower", default=0)),
        "following_count": _to_int(_first(stat, "following", default=0)),
        "aweme_count": max(0, int(archive_count or 0)),
        "total_favorited": 0,
        "gender": sex,
    }


def _decode_bili_dm_content(content: Any, msg_type: int) -> tuple[str, str, Any]:
    parsed: Any = content
    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except Exception:
            parsed = content
    if msg_type == 5:
        return "[消息已撤回]", "recall", parsed
    if isinstance(parsed, Mapping):
        if msg_type == 1 or "content" in parsed:
            txt = str(parsed.get("content") or "").strip()
            if txt:
                return txt, "text", parsed
        if msg_type == 2 or "url" in parsed:
            url = abs_bili_url(parsed.get("url") or "")
            return "[图片]", "image", {"url": url, **dict(parsed)}
        title = str(_first(parsed, "title", "text", "reply_content", default="") or "").strip()
        if title:
            return title, "video" if msg_type == 7 else str(msg_type or "card"), parsed
    if isinstance(parsed, str) and parsed.strip():
        return parsed.strip(), "text", parsed
    return "", str(msg_type or "unknown"), parsed


def parse_bili_dm_sessions(
    payload: Any,
    user_cards: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> List[dict]:
    """将 `session_svr/v1/session_svr/get_sessions` 响应归一为 `DmConversation` 列表。"""
    root = payload if isinstance(payload, Mapping) else {}
    data = root.get("data") if isinstance(root.get("data"), Mapping) else root
    sessions = data.get("session_list") if isinstance(data.get("session_list"), list) else []
    cards = user_cards if isinstance(user_cards, Mapping) else {}

    out: List[dict] = []
    seen: set[str] = set()
    for raw in sessions:
        if not isinstance(raw, Mapping):
            continue
        session_type = _to_int(_first(raw, "session_type", default=1))
        talker_id = str(_first(raw, "talker_id", default="") or "").strip()
        if not talker_id or talker_id == "0" or session_type not in {1, 0}:
            continue
        if talker_id in seen:
            continue
        seen.add(talker_id)

        last_msg = raw.get("last_msg") if isinstance(raw.get("last_msg"), Mapping) else {}
        msg_type = _to_int(_first(last_msg, "msg_type", default=1))
        last_text, _, _ = _decode_bili_dm_content(last_msg.get("content"), msg_type)
        last_time = _seconds(
            _first(last_msg, "timestamp", default=None) or _first(raw, "session_ts", default=0)
        )

        card = cards.get(talker_id) if isinstance(cards.get(talker_id), Mapping) else {}
        acct_info = raw.get("account_info") if isinstance(raw.get("account_info"), Mapping) else {}
        nickname = str(
            _first(card, "name", "uname", default="")
            or _first(acct_info, "name", default="")
            or ""
        )
        avatar = abs_bili_url(
            _first(card, "face", default="")
            or _first(acct_info, "pic_url", "face", default="")
        )
        meta = {
            "session_type": session_type,
            "session_ts": _to_int(_first(raw, "session_ts", default=0)),
            "max_seqno": _to_int(_first(raw, "max_seqno", default=0)),
            "ack_seqno": _to_int(_first(raw, "ack_seqno", default=0)),
            "is_follow": _to_int(_first(raw, "is_follow", default=0)),
            "is_dnd": _to_int(_first(raw, "is_dnd", default=0)),
        }
        out.append({
            "conv_id": talker_id,
            "peer_uid": talker_id,
            "peer_sec_uid": talker_id,
            "peer_nickname": nickname,
            "peer_avatar": avatar,
            "last_text": last_text,
            "last_time": last_time,
            "unread_count": max(0, _to_int(_first(raw, "unread_count", default=0))),
            "conv_short_id": talker_id,
            "ticket": "",
            "raw_json": json.dumps(meta, ensure_ascii=False, separators=(",", ":")),
        })
    return out


def parse_bili_dm_messages(
    payload: Any,
    *,
    peer_uid: str = "",
    self_uid: str = "",
    size: int = 20,
) -> dict:
    """将 `svr_sync/v1/svr_sync/fetch_session_msgs` 归一为 CreatorHub 私信历史结构。"""
    root = payload if isinstance(payload, Mapping) else {}
    data = root.get("data") if isinstance(root.get("data"), Mapping) else root
    rows = data.get("messages") if isinstance(data.get("messages"), list) else []

    messages: List[dict] = []
    seqnos: List[int] = []
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        sender = str(_first(raw, "sender_uid", default="") or "")
        receiver = str(_first(raw, "receiver_id", default="") or "")
        msg_type = _to_int(_first(raw, "msg_type", default=1))
        text, kind, parsed_content = _decode_bili_dm_content(raw.get("content"), msg_type)
        seqno = _to_int(_first(raw, "msg_seqno", default=0))
        if seqno > 0:
            seqnos.append(seqno)
        msg_key = str(_first(raw, "msg_key", default="") or seqno or "")
        if self_uid:
            direction = "out" if sender == str(self_uid) else "in"
        elif peer_uid:
            direction = "in" if sender == str(peer_uid) else "out"
        else:
            direction = "in"
        messages.append({
            "server_msg_id": msg_key,
            "sender_uid": sender,
            "receiver_uid": receiver,
            "direction": direction,
            "text": text,
            "msg_type": kind,
            "create_time": _seconds(_first(raw, "timestamp", default=0)),
            "store_id": seqno,
            "content": parsed_content,
            "raw": dict(raw),
        })

    messages.sort(key=lambda r: (int(r.get("create_time") or 0), int(r.get("store_id") or 0)))
    min_seqno = _to_int(_first(data, "min_seqno", default=0)) or (min(seqnos) if seqnos else 0)
    has_more = bool(_first(data, "has_more", default=0)) or (len(rows) >= max(1, int(size or 20)))
    return {
        "messages": messages,
        "next_cursor": min_seqno,
        "has_more": has_more,
        "min_seqno": min_seqno,
        "max_seqno": _to_int(_first(data, "max_seqno", default=0)),
    }
