"""哔哩哔哩私信广播 WebSocket Protobuf 编解码与 WatchNotify 实时唤醒接收器。

协议说明：
  - 注册 Broadcast 握手与心跳所需的 8 个基础消息类型；`RpcMeta.id` 字段号为 2 (`uint32`)；
  - `WatchNotify` 仅作为新私信到达唤醒信号，`decode_ws_any` 保留原始 `value` 字节且 `decoded=None`；
  - 连接 `wss://broadcast.chat.bilibili.com:7826/sub`（子协议 `proto`），
    建立连接后依次完成 `AuthReq` -> `TargetPath` (`Subscribe`) -> `Heartbeat`（25 秒周期保活）；
  - 收到 `method` 含 `WatchNotify` 的广播帧时触发 HTTP 增量会话/消息同步。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import struct
import time
from typing import Any, Awaitable, Callable, Mapping

from curl_cffi import CurlWsFlag
from curl_cffi.requests import AsyncSession, WebSocketClosed, WebSocketTimeout

from ...browser.manager import normalize_proxy
from ...netfp import impersonate_for_ua

_DEFAULT_WS_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

BILI_WS_METHODS: dict[str, str] = {
    "AUTH": "/bilibili.broadcast.v1.Broadcast/Auth",
    "SUBSCRIBE": "/bilibili.broadcast.v1.Broadcast/Subscribe",
    "HEARTBEAT": "/bilibili.broadcast.v1.Broadcast/Heartbeat",
}
BILI_WS_DEFAULT_SUBSCRIBE_PATH = "/bilibili.broadcast.message.im.Notify/WatchNotify"
WS_URL = "wss://broadcast.chat.bilibili.com:7826/sub"
TYPE_URL_PREFIX = "type.googleapis.com/"
_NS = "bilibili.broadcast.v1."

SCHEMA: dict[str, tuple[tuple[int, str, str], ...]] = {
    "google.protobuf.Any": (
        (1, "type_url", "string"),
        (2, "value", "bytes"),
    ),
    "google.protobuf.Empty": (),
    f"{_NS}RpcMeta": (
        (2, "id", "uint32"),
    ),
    f"{_NS}RpcFrame": (
        (1, "meta", f"{_NS}RpcMeta"),
        (2, "method", "string"),
        (3, "payload", "google.protobuf.Any"),
    ),
    f"{_NS}AuthReq": (
        (1, "token", "string"),
    ),
    f"{_NS}AuthResp": (),
    f"{_NS}TargetPath": (
        (1, "path", "string"),
    ),
    f"{_NS}HeartbeatResp": (),
}


def _resolve_type(name: str) -> str:
    clean = str(name or "").lstrip(".")
    if clean not in SCHEMA:
        raise KeyError(f"no such type: {clean}")
    return clean


def encode_varint(value: int) -> bytes:
    n = int(value) & 0xFFFFFFFF
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            b |= 0x80
        out.append(b)
        if not n:
            break
    return bytes(out)


def decode_varint(buf: bytes | memoryview, pos: int = 0) -> tuple[int, int]:
    n = 0
    for i in range(10):
        if pos >= len(buf):
            raise ValueError("truncated varint")
        b = buf[pos]
        pos += 1
        n |= (b & 0x7F) << (7 * i)
        if not (b & 0x80):
            return n & 0xFFFFFFFF, pos
    raise ValueError("invalid varint")


def _read_bytes(buf: bytes | memoryview, pos: int) -> tuple[bytes, int]:
    length, pos = decode_varint(buf, pos)
    end = pos + length
    if end > len(buf):
        raise ValueError("truncated bytes")
    return bytes(buf[pos:end]), end


def _skip_field(buf: bytes | memoryview, pos: int, wire: int) -> int:
    if wire == 0:
        for _ in range(10):
            if pos >= len(buf):
                raise ValueError("truncated varint")
            b = buf[pos]
            pos += 1
            if not (b & 0x80):
                return pos
        raise ValueError("invalid varint")
    if wire in (1, 5):
        pos += 8 if wire == 1 else 4
        if pos > len(buf):
            raise ValueError("truncated fixed")
        return pos
    if wire == 2:
        _, pos = _read_bytes(buf, pos)
        return pos
    if wire == 3:
        while pos < len(buf):
            tag, pos = decode_varint(buf, pos)
            if (tag & 7) == 4:
                return pos
            pos = _skip_field(buf, pos, tag & 7)
        raise ValueError("unterminated group")
    raise ValueError(f"invalid wire type {wire}")


def encode_type(type_name: str, obj: Mapping[str, Any] | None = None) -> bytes:
    resolved = _resolve_type(type_name)
    source = obj or {}
    chunks: list[bytes] = []
    for field_id, field_name, field_type in SCHEMA[resolved]:
        if field_name not in source or source[field_name] is None:
            continue
        val = source[field_name]
        if field_type == "uint32":
            chunks.append(encode_varint(field_id * 8))
            chunks.append(encode_varint(int(val)))
        else:
            if field_type == "string":
                raw = str(val).encode("utf-8")
            elif field_type == "bytes":
                raw = bytes(val)
            else:
                raw = encode_type(field_type, val)
            chunks.append(encode_varint(field_id * 8 + 2))
            chunks.append(encode_varint(len(raw)))
            chunks.append(raw)
    return b"".join(chunks)


def decode_type(type_name: str, data: bytes | bytearray | memoryview) -> dict[str, Any]:
    resolved = _resolve_type(type_name)
    buf = bytes(data)
    fields_by_id = {fid: (fname, ftype) for fid, fname, ftype in SCHEMA[resolved]}
    pos = 0
    out: dict[str, Any] = {}
    while pos < len(buf):
        tag, pos = decode_varint(buf, pos)
        field_id = tag >> 3
        wire = tag & 7
        spec = fields_by_id.get(field_id)
        if spec is None:
            pos = _skip_field(buf, pos, wire)
            continue
        fname, ftype = spec
        if ftype == "uint32":
            val, pos = decode_varint(buf, pos)
            out[fname] = val
        else:
            raw, pos = _read_bytes(buf, pos)
            if ftype == "string":
                out[fname] = raw.decode("utf-8")
            elif ftype == "bytes":
                out[fname] = raw
            else:
                out[fname] = decode_type(ftype, raw)
    return out


def normalize_type_name(type_url: str = "") -> str:
    s = str(type_url or "")
    if s.startswith(TYPE_URL_PREFIX):
        return s[len(TYPE_URL_PREFIX):]
    return s


def encode_ws_any(type_name: str, obj: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type_url": TYPE_URL_PREFIX + str(type_name),
        "value": encode_type(type_name, obj),
    }


def decode_ws_any(any_obj: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not any_obj:
        return None
    type_url = str(any_obj.get("type_url") or "")
    type_name = normalize_type_name(type_url)
    value = bytes(any_obj.get("value") or b"")
    decoded = None
    try:
        decoded = decode_type(type_name, value)
    except Exception:
        decoded = None
    return {
        "typeUrl": type_url,
        "typeName": type_name,
        "value": value,
        "decoded": decoded,
    }


def encode_rpc_frame(
    method: str,
    type_name: str,
    payload: Mapping[str, Any] | None = None,
    request_id: int = 0,
) -> dict[str, Any]:
    req_id = int(request_id or 0)
    frame = {
        "meta": {"id": req_id},
        "method": str(method or ""),
        "payload": encode_ws_any(type_name, payload),
    }
    return {
        "requestId": req_id,
        "method": str(method or ""),
        "typeName": str(type_name),
        "buffer": encode_type(f"{_NS}RpcFrame", frame),
    }


def decode_rpc_frame(raw: bytes | bytearray | memoryview) -> dict[str, Any]:
    buf = bytes(raw or b"")
    frame = decode_type(f"{_NS}RpcFrame", buf)
    meta = frame.get("meta") if isinstance(frame.get("meta"), Mapping) else {}
    return {
        "requestId": int(meta.get("id") or 0),
        "method": str(frame.get("method") or ""),
        "payload": decode_ws_any(frame.get("payload")),
        "raw": buf,
    }


def create_uuid_v1_factory(
    *,
    account_seed: str = "",
    now_ms_fn: Callable[[], int] | None = None,
    random_bytes_fn: Callable[[int], bytes] | None = None,
) -> Callable[[], str]:
    """生成单调递增的设备会话 UUIDv1 标识（用于私信 dev_id 与广播鉴权）。"""
    now_fn = now_ms_fn or (lambda: int(time.time() * 1000))
    rand_fn = random_bytes_fn or os.urandom
    last_ms = 0
    last_ns = 0
    seed_str = str(account_seed or "").strip()
    if seed_str:
        digest = hashlib.sha256(f"bili-mac|{seed_str}".encode("utf-8")).digest()
        node_arr = bytearray(digest[:6])
        node_arr[0] |= 0x01
        clock_seq = struct.unpack(">H", digest[6:8])[0] & 0x3FFF
    else:
        clock_seq = struct.unpack(">H", rand_fn(2))[0] & 0x3FFF
        node_arr = bytearray(rand_fn(6))
        node_arr[0] |= 0x01
    node_hex = bytes(node_arr).hex()

    def _next_uuid() -> str:
        nonlocal last_ms, last_ns, clock_seq
        ms = int(now_fn())
        ns = last_ns + 1
        dt = (ms - last_ms) + (ns - last_ns) / 10000.0
        if dt < 0:
            clock_seq = (clock_seq + 1) & 0x3FFF
        if dt < 0 or ms > last_ms:
            ns = 0
        if ns >= 10000:
            ms += 1
            ns = 0
        last_ms = ms
        last_ns = ns
        t = 10000 * (ms + 12219292800000) + ns
        time_low = f"{t & 0xFFFFFFFF:08x}"
        time_mid = f"{(t >> 32) & 0xFFFF:04x}"
        time_hi = f"{((t >> 48) & 0x0FFF) | 0x1000:04x}"
        clk_hi = f"{((clock_seq >> 8) & 0x3F) | 0x80:02x}"
        clk_lo = f"{clock_seq & 0xFF:02x}"
        return f"{time_low}-{time_mid}-{time_hi}-{clk_hi}{clk_lo}-{node_hex}"

    return _next_uuid


create_uuid_v1 = create_uuid_v1_factory()


async def receive_bili_broadcast(
    cookie: str,
    user_agent: str,
    on_watch_notify: Callable[[], Awaitable[None]],
    *,
    auth_token: str = "",
    subscribe_path: str = BILI_WS_DEFAULT_SUBSCRIBE_PATH,
    should_stop: Callable[[], bool] | None = None,
    proxy: str = "",
    account_seed: str = "",
    strict_proxy: bool = False,
) -> None:
    """维持 B 站 Broadcast WebSocket 连接，收到 WatchNotify 时触发增量 HTTP 同步。"""
    from ...netfp import curl_proxy_url

    norm_proxy = curl_proxy_url(proxy)
    _im_log = logging.getLogger("creatorhub.im.bili")
    if not norm_proxy:
        _im_log.debug("B站实时私信当前未配置代理，使用本机直连")

    ua = str(user_agent or "").strip() or _DEFAULT_WS_UA
    eff_seed = str(account_seed or "").strip()
    if not eff_seed and cookie:
        eff_seed = hashlib.sha256(cookie.encode("utf-8")).hexdigest()[:24]
    uuid_gen = create_uuid_v1_factory(account_seed=eff_seed) if eff_seed else create_uuid_v1
    token = auth_token or uuid_gen()
    stop_check = should_stop or (lambda: False)
    sync_lock = asyncio.Lock()
    attempt = 0

    async def _trigger_sync() -> None:
        if sync_lock.locked():
            return
        async with sync_lock:
            try:
                await on_watch_notify()
            except Exception as exc:
                _im_log.warning("B站 WatchNotify 增量同步失败: %r", exc)
            await asyncio.sleep(random.uniform(0.85, 1.35))

    while not stop_check():
        req_id = 1

        def _next_id() -> int:
            nonlocal req_id
            req_id += 1
            return req_id

        heartbeat_task: asyncio.Task | None = None
        try:
            headers = {
                "User-Agent": ua,
                "Cookie": cookie,
                "Origin": "https://message.bilibili.com",
                "Sec-WebSocket-Protocol": "proto",
            }
            async with AsyncSession(
                impersonate=impersonate_for_ua(ua), trust_env=False
            ) as session:
                async with session.ws_connect(
                    WS_URL,
                    proxy=(norm_proxy or None),
                    headers=headers,
                    timeout=15,
                ) as ws:
                    subscribed = False
                    auth_frame = encode_rpc_frame(
                        BILI_WS_METHODS["AUTH"],
                        f"{_NS}AuthReq",
                        {"token": token},
                        _next_id(),
                    )
                    await ws.send_bytes(auth_frame["buffer"])

                    async def _heartbeat_loop() -> None:
                        while not stop_check():
                            await asyncio.sleep(random.uniform(22.0, 28.0))
                            hb = encode_rpc_frame(
                                BILI_WS_METHODS["HEARTBEAT"],
                                f"{_NS}HeartbeatResp",
                                {},
                                _next_id(),
                            )
                            await ws.send_bytes(hb["buffer"])

                    while not stop_check():
                        try:
                            raw, flags = await ws.recv(timeout=30.0)
                        except (WebSocketTimeout, asyncio.TimeoutError):
                            continue
                        if flags & CurlWsFlag.CLOSE:
                            raise WebSocketClosed("Server closed broadcast WebSocket")
                        if (
                            not (flags & CurlWsFlag.BINARY)
                            or not isinstance(raw, (bytes, bytearray))
                            or not raw
                        ):
                            continue
                        try:
                            frame = decode_rpc_frame(raw)
                        except Exception:
                            continue
                        method = frame.get("method") or ""
                        if method == BILI_WS_METHODS["AUTH"]:
                            if not subscribed:
                                sub_frame = encode_rpc_frame(
                                    BILI_WS_METHODS["SUBSCRIBE"],
                                    f"{_NS}TargetPath",
                                    {"path": subscribe_path},
                                    _next_id(),
                                )
                                await ws.send_bytes(sub_frame["buffer"])
                        elif method == BILI_WS_METHODS["SUBSCRIBE"]:
                            subscribed = True
                            attempt = 0
                            hb = encode_rpc_frame(
                                BILI_WS_METHODS["HEARTBEAT"],
                                f"{_NS}HeartbeatResp",
                                {},
                                _next_id(),
                            )
                            await ws.send_bytes(hb["buffer"])
                            if heartbeat_task is None or heartbeat_task.done():
                                heartbeat_task = asyncio.create_task(_heartbeat_loop())
                        elif "WatchNotify" in method:
                            asyncio.create_task(_trigger_sync())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if stop_check():
                break
            backoff = min(60.0, 4.0 * (2 ** min(attempt, 4))) * random.uniform(0.85, 1.25)
            attempt += 1
            _im_log.warning("B站私信长连接断开(%r)，%.1fs 后重连", exc, backoff)
            await asyncio.sleep(backoff)
        finally:
            if heartbeat_task is not None and not heartbeat_task.done():
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
