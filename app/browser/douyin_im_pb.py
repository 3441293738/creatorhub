# -*- coding: utf-8 -*-
"""抖音网页版私信(IM)protobuf 解析。

网页版会话列表**没有**公开 JSON 接口:IM SDK 通过 `imapi.douyin.com/v1/message/get_message_by_init`
(cmd 2043) 一次性下发全部会话 + 每个会话的最后一条消息,响应体是 protobuf 二进制。
本模块**只**按已标定的字段号定向解析(不做类型推断) —— 通用 protobuf 猜测器会把
base64 串/conversation_id 偶发误判成子消息,必须锁定 schema 才稳定。

字段布局(实测标定,与抖音 IM 的 Request/Response.proto 对得上):
  envelope: {1:cmd, 2:seq, 4:"OK", 5:inbox_type, 6:body, 13:自己的 uid}
  body:     {2043: get_message_by_init_body}
  2043:     {1: [会话 wrapper ...]}          # repeated
  会话 wrapper:
    .1 = 会话核心(GetConversationInfoV2Response): {1:conv_id, 2:short_id, 3:type, 4:ticket}
    .2 = 最后一条消息(MessageBody): {6:msg_type, 7:sender_uid, 8:content_json, 14:sender_sec_uid}
  content_json 里文本在 .text;非文本消息(卡片/表情/转账)按 aweType 给占位。
"""
import base64
import json
from typing import Dict, List, Optional


def _read_varint(b: bytes, i: int):
    s = r = 0
    while True:
        x = b[i]; i += 1
        r |= (x & 0x7f) << s
        if not (x & 0x80):
            return r, i
        s += 7


def _get_fields(b: bytes) -> Dict[int, list]:
    """单层解析 protobuf -> {field_no: [values]}。len-delimited 一律保留原始 bytes,
    绝不自动递归(避免字符串被误判成子消息)。上层按需对已知消息字段再调本函数。"""
    out: Dict[int, list] = {}
    i, end = 0, len(b)
    while i < end:
        try:
            tag, i = _read_varint(b, i)
        except IndexError:
            break
        fn, wt = tag >> 3, tag & 7
        if fn == 0:
            break
        try:
            if wt == 0:
                v, i = _read_varint(b, i)
            elif wt == 1:
                v = b[i:i+8]; i += 8
            elif wt == 5:
                v = b[i:i+4]; i += 4
            elif wt == 2:
                ln, i = _read_varint(b, i)
                v = b[i:i+ln]; i += ln
            else:
                break
        except IndexError:
            break
        out.setdefault(fn, []).append(v)
    return out


def _first(d: Dict[int, list], k: int, default=None):
    v = d.get(k)
    return v[0] if v else default


def _s(v) -> str:
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8")
        except Exception:
            return ""
    return "" if v is None else str(v)


# 非文本消息按 aweType 给占位(会话列表预览用)
_AWE_LABEL = {507: "[表情]", 5: "[表情]", 11048: "[小程序卡片]",
              100157: "[系统通知]", 2702: "[小程序]"}

# 消息类型(MessageBody.field6)→ 占位。实测标定见 DouYin_Spider/douyin_recv_msg.py:
#   7=文本 5=表情 17=语音 27=图片 8=分享视频。媒体类消息 content 里没有 .text,
#   只认 .text 会让分享视频/图片/语音等显示成空气泡,故按 msg_type 兜底给占位。
_MSG_TYPE_LABEL = {5: "[表情]", 8: "[视频]", 17: "[语音]", 27: "[图片]"}


def _share_video_text(obj: dict) -> str:
    """分享视频卡片(msg_type=8)预览。实测 content 字段(标定见 fetch_dm_history debug):
      content_title=视频标题(偶尔空,如图集/直播分享) content_name=作者昵称
      cover_url.url_list=封面 itemId=视频ID secUID=作者。
    标题优先;标题空退回 @作者;都没有才纯 [视频],绝不返回空。"""
    title = obj.get("content_title")
    if isinstance(title, str) and title.strip():
        return "[视频] " + title.strip()
    author = obj.get("content_name")
    if isinstance(author, str) and author.strip():
        return "[视频] @" + author.strip()
    return "[视频]"


def _first_url(d) -> str:
    u = d.get("url_list") if isinstance(d, dict) else None
    return u[0] if isinstance(u, list) and u and isinstance(u[0], str) else ""


def share_video_card(content) -> Optional[dict]:
    """分享视频(msg_type=8)→ 前端卡片渲染字段。content 可为 JSON 串/bytes/dict。
    抽 {kind, item_id, title, author, author_sec_uid, cover, avatar};非视频卡返回 None。"""
    if isinstance(content, (bytes, bytearray)):
        try:
            content = json.loads(bytes(content).decode("utf-8"))
        except Exception:
            return None
    elif isinstance(content, str):
        try:
            content = json.loads(content)
        except Exception:
            return None
    if not isinstance(content, dict) or not content.get("itemId"):
        return None
    return {
        "kind": "video",
        "item_id": str(content.get("itemId") or ""),
        "title": str(content.get("content_title") or ""),
        "author": str(content.get("content_name") or ""),
        "author_sec_uid": str(content.get("secUID") or ""),
        "cover": _first_url(content.get("cover_url")),
        "avatar": _first_url(content.get("content_thumb")),
    }


def _preview_text(content: bytes, msg_type=None) -> str:
    """从消息 content(JSON 串)取会话列表/气泡预览文本。
    msg_type(可选)= MessageBody.field6,用于给媒体类消息(分享视频/图片/语音/表情)
    兜底占位——这些消息 content 无 .text,不给占位就会显示成空白。"""
    if not isinstance(content, bytes) or not content:
        return _MSG_TYPE_LABEL.get(msg_type, "")
    try:
        obj = json.loads(content.decode("utf-8"))
    except Exception:
        return _MSG_TYPE_LABEL.get(msg_type, "")
    if not isinstance(obj, dict):
        return _MSG_TYPE_LABEL.get(msg_type, "")
    if obj.get("text"):
        return str(obj["text"])
    if msg_type == 8 or obj.get("itemId"):     # 分享视频
        return _share_video_text(obj)
    if obj.get("trans_type") is not None:      # 转账
        return "[转账] " + str(obj.get("title") or "")
    lbl = _AWE_LABEL.get(obj.get("aweType"))
    if lbl:
        return lbl
    if obj.get("push_detail"):
        return str(obj["push_detail"])
    if obj.get("description"):
        return str(obj["description"])
    return _MSG_TYPE_LABEL.get(msg_type, "")   # 按类型兜底,别返回空


def peer_uid_from_conv_id(conv_id: str, self_uid: str) -> str:
    """单聊 conv_id 形如 `0:{type}:{uidA}:{uidB}`,两 uid 顺序不固定,
    对端 = 不是 self_uid 的那个。self_uid 未知则返回空(不猜)。"""
    if not conv_id or not self_uid or ":" not in conv_id:
        return ""
    uids = [x for x in conv_id.split(":") if x.isdigit() and len(x) >= 6]
    if self_uid not in uids:
        return ""
    return next((u for u in uids if u != self_uid), "")


def parse_conversations(raw: bytes) -> List[dict]:
    """解 get_message_by_init 响应体 -> 会话列表(仅单聊 type==1)。
    每项: {conv_id, conv_short_id, peer_uid, peer_sec_uid, ticket,
           last_text, last_msg_type, last_sender_uid, self_uid}。
    昵称/头像不在此包里,由上层用 user/info JSON 水合。"""
    if not raw:
        return []
    env = _get_fields(raw)
    self_uid = _s(_first(env, 13))
    body_raw = _first(env, 6)
    if not isinstance(body_raw, bytes):
        return []
    body = _get_fields(body_raw)
    b2043_raw = _first(body, 2043)
    if not isinstance(b2043_raw, bytes):
        return []
    b2043 = _get_fields(b2043_raw)

    out: List[dict] = []
    for cb in b2043.get(1, []):
        if not isinstance(cb, bytes):
            continue
        conv = _get_fields(cb)
        core = _get_fields(_first(conv, 1, b"") or b"")
        conv_id = _s(_first(core, 1, b""))
        if not conv_id:
            continue
        ctype = _first(core, 3)
        if ctype != 1:                     # 只要单聊,群聊/系统箱先跳过
            continue
        peer_uid = peer_uid_from_conv_id(conv_id, self_uid)
        if not peer_uid:
            continue
        msg = _get_fields(_first(conv, 2, b"") or b"")
        content = _first(msg, 8, b"")
        # peer sec_uid:优先从参与者列表(conv.6.1[]={1:uid,5:sec_uid})取(全会话都有);
        # 退回最后消息的 sender_sec_uid(仅当 peer 是最后发送者)
        peer_sec = ""
        for p in conv.get(6, []):
            if not isinstance(p, bytes):
                continue
            for pp in _get_fields(p).get(1, []):
                if isinstance(pp, bytes):
                    ppf = _get_fields(pp)
                    if _s(_first(ppf, 1)) == peer_uid:
                        peer_sec = _s(_first(ppf, 5, b"")); break
            if peer_sec:
                break
        if not peer_sec and _first(msg, 7) != int(self_uid or 0):
            peer_sec = _s(_first(msg, 14, b""))
        out.append({
            "conv_id": conv_id,
            "conv_short_id": _s(_first(core, 2)),
            "peer_uid": peer_uid,
            "peer_sec_uid": peer_sec,
            "ticket": _s(_first(core, 4, b"")),
            "last_text": _preview_text(
                content if isinstance(content, bytes) else b"", _first(msg, 6)),
            "last_msg_type": _first(msg, 6),
            "last_sender_uid": _s(_first(msg, 7)),
            "last_time": _msg_create_ts(msg),
            "self_uid": self_uid,
        })
    return out


# ═══════════ 会话历史消息:imapi/v1/message/get_by_conversation (cmd 301) ═══════════
# 实测(HAR 标定):该接口 URL 无 a_bogus/msToken,纯 cookie 鉴权 + protobuf body,
# 请求里也无 ts_sign/sdk_cert/req_sign —— 读历史不需要任何签名,可无头直接 POST。
# 请求信封绝大多数字段静态(且无用户密钥),故用抓到的真实请求做模板,只替换 body(field 8)。
GET_BY_CONV_URL = "https://imapi.douyin.com/v1/message/get_by_conversation"

# 761 字节真实请求模板(field 8=RequestBody 在 [40,108],其余静态可复用)
_HIST_TEMPLATE_B64 = (
    "CK0CEKZOGgUwLjEuNiIAKAMwADoTZmVmMWE4MDpwL2x6Zy9zdG9yZUJC6hI/CiQwOjE6NTEwNjAx"
    "Mjg5Nzk1NjYyOjE4OTI3MTc3ODU3NzgwMzIQARi/hInwjMXSg2ogASiQjsH7nKT8AjAySgEwWglk"
    "b3V5aW5fcGNyBjM2MDAwMHoTCgtzZXNzaW9uX2FpZBIENjM4M3oQCgtzZXNzaW9uX2RpZBIBMHoV"
    "CghhcHBfbmFtZRIJZG91eWluX3BjehUKD3ByaW9yaXR5X3JlZ2lvbhICY256fQoKdXNlcl9hZ2Vu"
    "dBJvTW96aWxsYS81LjAgKFdpbmRvd3MgTlQgMTAuMDsgV2luNjQ7IHg2NCkgQXBwbGVXZWJLaXQv"
    "NTM3LjM2IChLSFRNTCwgbGlrZSBHZWNrbykgQ2hyb21lLzEzMC4wLjAuMCBTYWZhcmkvNTM3LjM2"
    "ehYKDmNvb2tpZV9lbmFibGVkEgR0cnVlehkKEGJyb3dzZXJfbGFuZ3VhZ2USBXpoLUNOehkKEGJy"
    "b3dzZXJfcGxhdGZvcm0SBVdpbjMyehcKDGJyb3dzZXJfbmFtZRIHTW96aWxsYXp6Cg9icm93c2Vy"
    "X3ZlcnNpb24SZzUuMCAoV2luZG93cyBOVCAxMC4wOyBXaW42NDsgeDY0KSBBcHBsZVdlYktpdC81"
    "MzcuMzYgKEtIVE1MLCBsaWtlIEdlY2tvKSBDaHJvbWUvMTMwLjAuMC4wIFNhZmFyaS81MzcuMzZ6"
    "FgoOYnJvd3Nlcl9vbmxpbmUSBHRydWV6FAoMc2NyZWVuX3dpZHRoEgQxNTM2ehQKDXNjcmVlbl9o"
    "ZWlnaHQSAzg2NHoiCgdyZWZlcmVyEhdodHRwczovL3d3dy5kb3V5aW4uY29tL3oeCg10aW1lem9u"
    "ZV9uYW1lEg1Bc2lhL1NoYW5naGFpeg0KCGRldmljZUlkEgEweg0KCGlzLXJldHJ5EgEwkAEBqgEK"
    "ZG91eWluX3dlYrIBB3dlYl9zZGs=")
_HIST_BODY_SPAN = (40, 108)   # field 8 在模板里的字节区间


def _enc_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7f
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _enc_tag(fn: int, wt: int) -> bytes:
    return _enc_varint((fn << 3) | wt)


def _enc_v(fn: int, n: int) -> bytes:       # varint 字段
    return _enc_tag(fn, 0) + _enc_varint(n)


def _enc_ld(fn: int, data: bytes) -> bytes:  # 长度分隔字段(string/message)
    return _enc_tag(fn, 2) + _enc_varint(len(data)) + data


def _rebuild_envelope(cmd: int, request_body: bytes) -> bytes:
    """用真实请求模板重建信封,只改 cmd(field 1)+ body(field 8),其余字段原样保留。
    (mark_read/get_by_conversation/send 的信封字段集一致,仅 cmd+body 不同。)"""
    tmpl = base64.b64decode(_HIST_TEMPLATE_B64)
    out = bytearray()
    i, n = 0, len(tmpl)
    while i < n:
        tag, j = _read_varint(tmpl, i)
        fn, wt = tag >> 3, tag & 7
        start = i
        if wt == 0:
            _, j = _read_varint(tmpl, j)
        elif wt == 2:
            ln, j = _read_varint(tmpl, j); j += ln
        elif wt == 1:
            j += 8
        elif wt == 5:
            j += 4
        if fn == 1:
            out += _enc_v(1, cmd)
        elif fn == 8:
            out += _enc_ld(8, request_body)
        else:
            out += tmpl[start:j]
        i = j
    return bytes(out)


def build_history_request(conv_id: str, conv_type: int, conv_short_id: int,
                          cursor: int, count: int = 50) -> bytes:
    """构造 get_by_conversation 请求(cmd 301)。body(field 8→301)字段:
    {1:conv_id, 2:type, 3:short_id, 4:1(方向), 5:cursor, 6:count}。
    cursor 传上一页返回的 next_cursor;首拉传 0(服务端给最新)或会话当前 index。"""
    body301 = (
        _enc_ld(1, conv_id.encode("utf-8"))
        + _enc_v(2, int(conv_type))
        + _enc_v(3, int(conv_short_id))
        + _enc_v(4, 1)
        + _enc_v(5, int(cursor))
        + _enc_v(6, int(count))
    )
    return _rebuild_envelope(301, _enc_ld(301, body301))


def build_send_request(conv_id: str, conv_type: int, conv_short_id: int,
                       ticket: str, text: str, client_msg_id: str,
                       stime_ms: int) -> bytes:
    """构造 send 请求(cmd 100)。body(field 8→100)= SendMessageRequestBody:
    {1:conv_id, 2:type, 3:short_id, 4:content_json, 5:[ext...], 6:msg_type=7, 7:ticket, 8:client_msg_id}。
    content = {"text":..,"aweType":700,"mention_users":[],"richTextInfos":[]}。
    client_msg_id/stime_ms 由调用方传(模块内不取时间/随机,保持纯函数)。"""
    content = json.dumps(
        {"mention_users": [], "aweType": 700, "richTextInfos": [], "text": text},
        ensure_ascii=False, separators=(",", ":"))

    def _ext(k: str, v: str) -> bytes:
        return _enc_ld(5, _enc_ld(1, k.encode()) + _enc_ld(2, v.encode()))

    body100 = (
        _enc_ld(1, conv_id.encode("utf-8"))
        + _enc_v(2, int(conv_type))
        + _enc_v(3, int(conv_short_id))
        + _enc_ld(4, content.encode("utf-8"))
        + _ext("s:client_message_id", client_msg_id)
        + _ext("s:stime", str(stime_ms))
        + _ext("s:mentioned_users", "")
        + _enc_v(6, 7)
        + _enc_ld(7, ticket.encode("utf-8"))
        + _enc_ld(8, client_msg_id.encode("utf-8"))
    )
    return _rebuild_envelope(100, _enc_ld(100, body100))


def parse_send_response(resp: bytes) -> dict:
    """解 send 响应信封 {1:cmd,3:error_code?,4:msg('OK'/错误),6:body}。
    成功 msg=='OK';失败带错误码/文案。"""
    if not resp:
        return {"ok": False, "msg": "空响应", "cmd": 0}
    env = _get_fields(resp)
    msg = _s(_first(env, 4, b""))
    cmd = _first(env, 1) or 0
    err = _first(env, 3) or 0
    return {"ok": (msg == "OK"), "msg": msg, "cmd": cmd, "error_code": err}


def _ext_map(m: Dict[int, list]) -> Dict[str, str]:
    """MessageBody.ext(field 9)= repeated {1:key,2:value} -> dict。"""
    out: Dict[str, str] = {}
    for e in m.get(9, []):
        if isinstance(e, bytes):
            kv = _get_fields(e)
            k = _s(_first(kv, 1, b""))
            if k:
                out[k] = _s(_first(kv, 2, b""))
    return out


def _msg_create_ts(m: Dict[int, list]) -> int:
    """消息发送时间(unix 秒)。真实时间在 ext 的 s:server_message_create_time(毫秒);
    退回 field 5(微秒级排序索引)。前端 fmtTime 吃秒。"""
    ext = _ext_map(m)
    ms = ext.get("s:server_message_create_time") or ext.get("a:im_client_send_msg_time")
    if ms:
        try:
            return int(float(ms)) // 1000
        except Exception:
            pass
    v5 = _first(m, 5)
    if isinstance(v5, int) and v5 > 1_000_000_000_000_000:   # 微秒
        return v5 // 1_000_000
    return 0


def _msg_from_body(mb: bytes) -> Optional[dict]:
    """把一条 MessageBody(protobuf) 解成 {msg_id, index, msg_type, sender_uid,
    content, text, create_time(秒)}。字段同 get_message_by_init 里的最后一条消息。"""
    m = _get_fields(mb)
    conv_id = _s(_first(m, 1, b""))
    if not conv_id:
        return None
    content = _first(m, 8, b"")
    content = content if isinstance(content, bytes) else b""
    msg_type = _first(m, 6) or 0
    return {
        "conv_id": conv_id,
        "server_msg_id": _s(_first(m, 3)),
        "index": _first(m, 4) or 0,
        "msg_type": msg_type,
        "sender_uid": _s(_first(m, 7)),
        "content": content.decode("utf-8", "ignore"),
        "text": _preview_text(content, msg_type),
        "card": share_video_card(content) if msg_type == 8 else None,
        "create_time": _msg_create_ts(m),
        "sender_sec_uid": _s(_first(m, 14, b"")),
    }


def parse_messages(resp: bytes) -> dict:
    """解 get_by_conversation 响应 -> {messages:[...], next_cursor, has_more, self_uid}。
    响应信封 {6:body, 13:self_uid};body 里 field 301 = {1:[MessageBody...], 2:cursor, 3:has_more}。
    注:MessageBody 在 body301 的具体字段号待真实非空响应确认,这里对 1 及兜底全字段扫描。"""
    if not resp:
        return {"messages": [], "next_cursor": 0, "has_more": False, "self_uid": ""}
    env = _get_fields(resp)
    self_uid = _s(_first(env, 13))
    body_raw = _first(env, 6)
    if not isinstance(body_raw, bytes):
        return {"messages": [], "next_cursor": 0, "has_more": False, "self_uid": self_uid}
    body = _get_fields(body_raw)
    inner_raw = _first(body, 301)
    inner = _get_fields(inner_raw) if isinstance(inner_raw, bytes) else {}
    msgs: List[dict] = []
    # 优先 field 1(repeated MessageBody);兜底:扫描所有 bytes 字段里像 MessageBody 的
    cands = list(inner.get(1, []))
    if not cands:
        for k, vs in inner.items():
            for v in vs:
                if isinstance(v, bytes) and len(v) > 8:
                    cands.append(v)
    for v in cands:
        if isinstance(v, bytes):
            mm = _msg_from_body(v)
            if mm:
                msgs.append(mm)
    # 游标/has_more:body301 里的 varint 字段(实测空响应见到 2=cursor,3=has_more)
    next_cursor = 0
    for k in (2, 3, 4, 5):
        val = _first(inner, k)
        if isinstance(val, int) and val > 1000:
            next_cursor = val; break
    has_more = bool(_first(inner, 3) or 0)
    return {"messages": msgs, "next_cursor": next_cursor,
            "has_more": has_more, "self_uid": self_uid}
