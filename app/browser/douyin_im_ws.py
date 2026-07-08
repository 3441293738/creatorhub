# -*- coding: utf-8 -*-
"""抖音私信实时接收:frontier-im WebSocket 长连接 + 推送帧解码。

参考 DouYin_Spider/dy_apis/douyin_recv_msg.py。链路:
  wss://frontier-im.douyin.com/ws/v2?...&device_id=&access_key=  (cookie 鉴权)
  收到二进制帧 = PushFrame(field 8=payload=Response);
  Response.body(6).500 = NewMessageNotify; .5 = MessageBody(同历史消息结构)。
access_key = md5(fpid + appKey + device_id + salt),device_id 实测 == 账号自身 uid。
"""
import gzip
import hashlib
from typing import Callable, Dict, List, Optional

from .douyin_im_pb import (_get_fields, _first, _s, _preview_text,
                           _msg_create_ts, peer_uid_from_conv_id,
                           share_video_card)

_APP_KEY = "e1bd35ec9db7b8d846de66ed140b1ad9"
_FPID = "9"
_SALT = "f8a69f1719916z"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")


def compute_access_key(device_id: str) -> str:
    return hashlib.md5(
        (_FPID + _APP_KEY + str(device_id) + _SALT).encode()).hexdigest()


def build_ws_url(device_id: str) -> str:
    ak = compute_access_key(device_id)
    return (f"wss://frontier-im.douyin.com/ws/v2?aid=6383&fpid={_FPID}"
            f"&device_id={device_id}&access_key={ak}"
            f"&device_platform=douyin_pc&version_code=360000"
            f"&xsack=0&xaack=0&xsqos=0&qos_sdk_version=2")


def decode_push_frame(raw: bytes, self_uid: str = "") -> List[dict]:
    """PushFrame -> [新消息 dict]。非消息帧(心跳/其它通知)返回 []。"""
    frame = _get_fields(raw)
    ptype = _s(_first(frame, 7, b""))            # payloadType
    penc = _s(_first(frame, 6, b""))             # payloadEncoding
    payload = _first(frame, 8, b"")
    if not isinstance(payload, bytes) or not payload:
        return []
    if penc == "gzip" or payload[:2] == b"\x1f\x8b":
        try:
            payload = gzip.decompress(payload)
        except Exception:
            pass
    if ptype and ptype not in ("pb", ""):
        return []
    resp = _get_fields(payload)
    body_raw = _first(resp, 6)
    if not isinstance(body_raw, bytes):
        return []
    body = _get_fields(body_raw)
    notify_raw = _first(body, 500)               # new_message_notify
    if not isinstance(notify_raw, bytes):
        return []
    notify = _get_fields(notify_raw)
    mb_raw = _first(notify, 5)                    # MessageBody
    if not isinstance(mb_raw, bytes):
        return []
    m = _get_fields(mb_raw)
    conv_id = _s(_first(m, 1, b"")) or _s(_first(notify, 2, b""))
    if not conv_id:
        return []
    content = _first(m, 8, b"")
    content = content if isinstance(content, bytes) else b""
    sender = _s(_first(m, 7))
    msg_type = _first(m, 6) or 0
    return [{
        "conv_id": conv_id,
        "conv_short_id": _s(_first(m, 5)),
        "server_msg_id": _s(_first(m, 3)),
        "msg_type": msg_type,
        "sender_uid": sender,
        "text": _preview_text(content, msg_type),
        "card": share_video_card(content) if msg_type == 8 else None,
        "create_time": _msg_create_ts(m),
        "peer_uid": peer_uid_from_conv_id(conv_id, self_uid),
        "is_self": bool(self_uid) and sender == self_uid,
    }]


async def receive_messages(cookie_str: str, device_id: str,
                           on_message: Callable[[dict], None],
                           self_uid: str = "",
                           stop_after: Optional[float] = None,
                           should_stop: Optional[Callable[[], bool]] = None,
                           log=print) -> str:
    """连 frontier-im WS,持续解码新消息并回调 on_message。断线自动重连。
    stop_after: 秒(测试用,到点返回);should_stop: 返回 True 则优雅退出。"""
    import asyncio
    import websockets

    url = build_ws_url(device_id)
    headers = {
        "Cookie": cookie_str,
        "User-Agent": _UA,
        "Origin": "https://www.douyin.com",
    }
    start = [0.0]
    frames = [0]
    try:
        import time as _t
        start[0] = _t.monotonic()

        async def _run_once():
            async with websockets.connect(
                    url, additional_headers=headers,
                    subprotocols=["binary", "base64", "pbbp2"],
                    max_size=None, ping_interval=20, ping_timeout=20) as ws:
                log(f"[im-ws] connected device_id={device_id}")
                async for raw in ws:
                    frames[0] += 1
                    if isinstance(raw, (bytes, bytearray)):
                        for msg in decode_push_frame(bytes(raw), self_uid):
                            try:
                                on_message(msg)
                            except Exception as e:
                                log(f"[im-ws] on_message err: {e!r}")
                    if should_stop and should_stop():
                        return
                    if stop_after and (_t.monotonic() - start[0]) > stop_after:
                        return

        while True:
            try:
                await _run_once()
            except Exception as e:
                log(f"[im-ws] conn drop: {e!r}")
            if should_stop and should_stop():
                break
            if stop_after and (_t.monotonic() - start[0]) > stop_after:
                break
            await asyncio.sleep(3)          # 重连退避
        return f"frames={frames[0]}"
    except Exception as e:
        return f"error: {e!r} frames={frames[0]}"
