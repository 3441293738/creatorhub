"""WMPF Chrome DevTools Protobuf/Zlib 编解码与单会话本地回环 CDP 转发桥。

特性与安全边界:
  1. 纯标准库 Protobuf varint / length-delimited 编解码与有界(16 MiB) RFC1950 zlib 解压。
  2. 回环桥严格绑定 `127.0.0.1`，校验对端本地进程 PID 白名单(`allowed_pids`)，
     且 CDP WebSocket 路径使用随机 `secrets.token_urlsafe(24)` 控制口令保护，单会话独占。
  3. 零进程注入、零二进制补丁、零凭据/Cookie/原始包日志落盘。
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os
import secrets
import socket
import sys
import zlib
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

MAX_MESSAGE_BYTES = 16 * 1024 * 1024
CDP_REQUEST = "chromeDevtools"
CDP_RESULT = "chromeDevtoolsResult"
CDP_CATEGORIES = frozenset((CDP_REQUEST, CDP_RESULT))


class CodecError(ValueError):
    """非法的 WMPF Protobuf 或 zlib 报文(异常文本绝不包含业务载荷)。"""


@dataclass(frozen=True)
class Envelope:
    seq: int = 0
    after: int = 0
    category: str = ""
    data: bytes = field(default=b"", repr=False)
    compress_algo: int = 0
    original_size: int = 0


@dataclass(frozen=True)
class CdpMessage:
    seq: int
    category: str
    op_id: int = 0
    payload: str = field(default="", repr=False)
    jscontext_id: str = field(default="", repr=False)
    after: int = 0
    compress_algo: int = 0
    original_size: int = 0

    def json(self) -> Any:
        try:
            return json.loads(self.payload)
        except (ValueError, TypeError):
            raise CodecError("CDP payload is not valid JSON") from None


def _uint(value: Any, bits: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < (1 << bits):
        raise CodecError(f"Expected unsigned {bits}-bit integer")
    return value


def _varint(value: int) -> bytes:
    _uint(value, 64)
    out = bytearray()
    while value >= 128:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    for index in range(10):
        if pos >= len(data):
            raise CodecError("Truncated protobuf varint")
        byte = data[pos]
        pos += 1
        if index == 9 and byte > 1:
            raise CodecError("Protobuf varint exceeds uint64")
        result |= (byte & 127) << (7 * index)
        if not (byte & 128):
            return result, pos
    raise CodecError("Overlong protobuf varint")


def _scalar(number: int, value: int) -> bytes:
    return _varint(number << 3) + _varint(value)


def _blob(number: int, value: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(value)) + value


def _text(value: Any) -> bytes:
    if not isinstance(value, str):
        raise CodecError("Expected a string")
    try:
        return value.encode("utf-8")
    except UnicodeError:
        raise CodecError("Invalid UTF-8 string") from None


def _parse_fields(raw: bytes | bytearray | memoryview, maximum: int = MAX_MESSAGE_BYTES):
    if not isinstance(raw, (bytes, bytearray, memoryview)):
        raise CodecError("Expected binary protobuf data")
    data = bytes(raw)
    if len(data) > maximum:
        raise CodecError("Message exceeds configured size limit")
    pos = 0
    while pos < len(data):
        tag, pos = _read_varint(data, pos)
        number, wire = tag >> 3, tag & 7
        if number == 0 or number > 0x1FFFFFFF:
            raise CodecError("Invalid protobuf field number")
        if wire == 0:
            value: Any
            value, pos = _read_varint(data, pos)
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            if pos + size > len(data):
                raise CodecError("Truncated protobuf fixed-width field")
            value = data[pos : pos + size]
            pos += size
        elif wire == 2:
            size, pos = _read_varint(data, pos)
            if size > maximum or pos + size > len(data):
                raise CodecError("Truncated or oversized length-delimited field")
            value = data[pos : pos + size]
            pos += size
        else:
            raise CodecError("Unsupported protobuf wire type")
        yield number, wire, value


def _decode_utf8(value: bytes) -> str:
    try:
        return value.decode("utf-8")
    except UnicodeError:
        raise CodecError("Invalid UTF-8 field") from None


def encode_envelope(
    envelope: Envelope, *, include_after: bool = False, maximum: int = MAX_MESSAGE_BYTES
) -> bytes:
    _uint(envelope.seq, 32)
    _uint(envelope.after, 32)
    _uint(envelope.compress_algo, 32)
    _uint(envelope.original_size, 32)
    if not isinstance(envelope.data, bytes):
        raise CodecError("Envelope data must be bytes")
    parts = [_scalar(1, envelope.seq)]
    if include_after or envelope.after:
        parts.append(_scalar(2, envelope.after))
    parts.extend((
        _blob(3, _text(envelope.category)),
        _blob(4, envelope.data),
        _scalar(5, envelope.compress_algo),
        _scalar(6, envelope.original_size),
    ))
    out = b"".join(parts)
    if len(out) > maximum:
        raise CodecError("Envelope exceeds configured size limit")
    return out


def decode_envelope(raw: bytes | bytearray | memoryview, *, maximum: int = MAX_MESSAGE_BYTES) -> Envelope:
    values: dict[str, Any] = dict(
        seq=0, after=0, category="", data=b"", compress_algo=0, original_size=0
    )
    schema = {
        1: ("seq", 0),
        2: ("after", 0),
        3: ("category", 2),
        4: ("data", 2),
        5: ("compress_algo", 0),
        6: ("original_size", 0),
    }
    for number, wire, value in _parse_fields(raw, maximum):
        if number not in schema:
            continue
        name, expected = schema[number]
        if wire != expected:
            raise CodecError("Wrong wire type for envelope field")
        values[name] = _decode_utf8(value) if name == "category" else value
        if expected == 0:
            _uint(value, 32)
    return Envelope(**values)


def encode_cdp_body(payload: str, *, op_id: int = 0, jscontext_id: str = "") -> bytes:
    return (
        _scalar(1, _uint(op_id, 64))
        + _blob(2, _text(payload))
        + _blob(3, _text(jscontext_id))
    )


def decode_cdp_body(raw: bytes, *, maximum: int = MAX_MESSAGE_BYTES) -> dict[str, Any]:
    values: dict[str, Any] = {"op_id": 0, "payload": "", "jscontext_id": ""}
    schema = {1: ("op_id", 0), 2: ("payload", 2), 3: ("jscontext_id", 2)}
    for number, wire, value in _parse_fields(raw, maximum):
        if number not in schema:
            continue
        name, expected = schema[number]
        if wire != expected:
            raise CodecError("Wrong wire type for CDP body field")
        values[name] = value if wire == 0 else _decode_utf8(value)
    return values


def _inflate_bounded(raw: bytes, maximum: int) -> bytes:
    try:
        decoder = zlib.decompressobj()
        out = decoder.decompress(raw, maximum + 1)
        if len(out) > maximum or decoder.unconsumed_tail:
            raise CodecError("Inflated body exceeds configured size limit")
        out += decoder.flush(maximum - len(out) + 1)
        if len(out) > maximum:
            raise CodecError("Inflated body exceeds configured size limit")
        if not decoder.eof or decoder.unused_data:
            raise CodecError("Truncated or trailing zlib stream")
        return out
    except zlib.error:
        raise CodecError("Invalid zlib-compressed body") from None


def encode_cdp(
    payload: str,
    *,
    seq: int,
    op_id: int = 0,
    jscontext_id: str = "",
    category: str = CDP_REQUEST,
    after: int = 0,
    compress: bool = False,
    maximum: int = MAX_MESSAGE_BYTES,
) -> bytes:
    if category not in CDP_CATEGORIES:
        raise CodecError("Unsupported CDP category")
    body = encode_cdp_body(payload, op_id=op_id, jscontext_id=jscontext_id)
    if len(body) > maximum:
        raise CodecError("CDP body exceeds configured size limit")
    original_size = len(body) if compress else 0
    data = zlib.compress(body) if compress else body
    return encode_envelope(
        Envelope(seq, after, category, data, 1 if compress else 0, original_size),
        maximum=maximum,
    )


def decode_cdp(
    raw: bytes | bytearray | memoryview,
    *,
    maximum: int = MAX_MESSAGE_BYTES,
    validate_original_size: bool = True,
) -> CdpMessage:
    env = decode_envelope(raw, maximum=maximum)
    if env.category not in CDP_CATEGORIES:
        raise CodecError("Envelope is not a CDP request/result")
    if env.compress_algo not in (0, 1):
        raise CodecError("Unsupported compression algorithm bits")
    if env.original_size > maximum:
        raise CodecError("Declared original size exceeds configured limit")
    body = _inflate_bounded(env.data, maximum) if env.compress_algo else env.data
    if (
        validate_original_size
        and env.compress_algo
        and env.original_size
        and len(body) != env.original_size
    ):
        raise CodecError("Inflated size disagrees with originalSize")
    values = decode_cdp_body(body, maximum=maximum)
    return CdpMessage(
        env.seq,
        env.category,
        values["op_id"],
        values["payload"],
        values["jscontext_id"],
        env.after,
        env.compress_algo,
        env.original_size,
    )


def unwrap_cdp_result(
    raw: bytes | bytearray | memoryview, *, maximum: int = MAX_MESSAGE_BYTES
) -> str | None:
    env = decode_envelope(raw, maximum=maximum)
    if env.category != CDP_RESULT:
        return None
    return decode_cdp(raw, maximum=maximum).payload


_WECHAT_EXE_NAMES = frozenset({
    "wechat.exe",
    "weixin.exe",
    "wechatappex.exe",
    "wmpfruntime.exe",
    "wechatplayer.exe",
})


def _win32_owner_pid(peer_port: int, server_port: int) -> int | None:
    """Windows stdlib fallback using iphlpapi.GetExtendedTcpTable."""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
    TCP_TABLE_OWNER_PID_ALL = 5
    MIB_TCP_STATE_ESTAB = 5
    ERROR_INSUFFICIENT_BUFFER = 122

    class MIB_TCPROW_OWNER_PID(ctypes.Structure):
        _fields_ = [
            ("dwState", wintypes.DWORD),
            ("dwLocalAddr", wintypes.DWORD),
            ("dwLocalPort", wintypes.DWORD),
            ("dwRemoteAddr", wintypes.DWORD),
            ("dwRemotePort", wintypes.DWORD),
            ("dwOwningPid", wintypes.DWORD),
        ]

    class MIB_TCP6ROW_OWNER_PID(ctypes.Structure):
        _fields_ = [
            ("ucLocalAddr", ctypes.c_ubyte * 16),
            ("dwLocalScopeId", wintypes.DWORD),
            ("dwLocalPort", wintypes.DWORD),
            ("ucRemoteAddr", ctypes.c_ubyte * 16),
            ("dwRemoteScopeId", wintypes.DWORD),
            ("dwRemotePort", wintypes.DWORD),
            ("dwState", wintypes.DWORD),
            ("dwOwningPid", wintypes.DWORD),
        ]

    def _query_table(af: int, row_cls: type[ctypes.Structure]):
        size = wintypes.DWORD(0)
        res = iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False, af, TCP_TABLE_OWNER_PID_ALL, 0)
        if res not in (0, ERROR_INSUFFICIENT_BUFFER) or size.value <= 4:
            return []
        buf = ctypes.create_string_buffer(size.value)
        if iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False, af, TCP_TABLE_OWNER_PID_ALL, 0) != 0:
            return []
        num = wintypes.DWORD.from_buffer(buf).value
        offset = ctypes.sizeof(wintypes.DWORD)
        row_size = ctypes.sizeof(row_cls)
        rows = []
        for i in range(num):
            if offset + (i + 1) * row_size > len(buf):
                break
            rows.append(row_cls.from_buffer_copy(buf, offset + i * row_size))
        return rows

    loopback_v4 = int.from_bytes(socket.inet_aton("127.0.0.1"), sys.byteorder)
    for row in _query_table(socket.AF_INET, MIB_TCPROW_OWNER_PID):
        if (
            row.dwState == MIB_TCP_STATE_ESTAB
            and row.dwLocalAddr == loopback_v4
            and row.dwRemoteAddr == loopback_v4
            and socket.ntohs(row.dwLocalPort & 0xFFFF) == peer_port
            and socket.ntohs(row.dwRemotePort & 0xFFFF) == server_port
        ):
            return int(row.dwOwningPid)

    loopback_v6 = (0,) * 15 + (1,)
    for row in _query_table(socket.AF_INET6, MIB_TCP6ROW_OWNER_PID):
        if (
            row.dwState == MIB_TCP_STATE_ESTAB
            and tuple(row.ucLocalAddr) == loopback_v6
            and tuple(row.ucRemoteAddr) == loopback_v6
            and socket.ntohs(row.dwLocalPort & 0xFFFF) == peer_port
            and socket.ntohs(row.dwRemotePort & 0xFFFF) == server_port
        ):
            return int(row.dwOwningPid)
    return None


def owner_pid(peer_port: int, server_port: int) -> int | None:
    try:
        import psutil
    except ImportError:
        return _win32_owner_pid(peer_port, server_port)

    for conn in psutil.net_connections(kind="tcp"):
        if (
            conn.laddr
            and conn.raddr
            and conn.laddr.ip in ("127.0.0.1", "::1")
            and conn.raddr.ip in ("127.0.0.1", "::1")
            and conn.laddr.port == peer_port
            and conn.raddr.port == server_port
            and conn.status == "ESTABLISHED"
        ):
            return conn.pid
    return None


def is_pid_running(pid: int) -> bool:
    """Return True if a positive OS process ID appears to be alive."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD(0)
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def discover_wechat_processes() -> list[dict[str, Any]]:
    """Enumerate running desktop WeChat / WMPF process PIDs without process injection."""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == wintypes.HANDLE(-1).value:
        return []
    found: list[dict[str, Any]] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            exe_name = str(entry.szExeFile or "").strip()
            pid = int(entry.th32ProcessID or 0)
            if pid > 0 and exe_name.lower() in _WECHAT_EXE_NAMES:
                found.append({"pid": pid, "name": exe_name})
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    found.sort(key=lambda item: (item["name"].lower(), item["pid"]))
    return found


def inspect_endpoint_file(path: str | Path) -> dict[str, Any]:
    """Validate a WMPF endpoint descriptor file without returning its WebSocket secret."""
    raw_path = str(path or "").strip()
    if not raw_path:
        return {"path": "", "exists": False, "valid": False, "error": "未指定口令文件路径"}
    try:
        fpath = Path(raw_path).expanduser()
        if not fpath.exists():
            return {"path": str(fpath), "exists": False, "valid": False, "error": "口令文件不存在"}
        if not fpath.is_file():
            return {"path": str(fpath), "exists": False, "valid": False, "error": "指定路径不是文件"}
        desc = json.loads(fpath.read_text("utf-8"))
    except Exception as exc:
        return {"path": raw_path, "exists": True, "valid": False, "error": f"无法解析口令文件: {exc}"}

    if not isinstance(desc, dict):
        return {"path": str(fpath), "exists": True, "valid": False, "error": "口令文件内容格式无效"}
    bind_host = str(desc.get("bind_host") or "")
    ws_url = str(desc.get("browserWebSocketUrl") or "")
    parsed = urlsplit(ws_url)
    host = (parsed.hostname or "").lower()
    valid = (
        bind_host == "127.0.0.1"
        and desc.get("local_debug_control_secret_not_account_token") is True
        and parsed.scheme in ("ws", "wss")
        and host in ("127.0.0.1", "localhost", "::1")
        and not parsed.username
        and not parsed.password
    )
    server_pid = desc.get("server_pid")
    pid_int = int(server_pid) if isinstance(server_pid, int) and not isinstance(server_pid, bool) and server_pid > 0 else None
    return {
        "path": str(fpath),
        "exists": True,
        "valid": bool(valid),
        "bind_host": bind_host,
        "server_pid": pid_int,
        "server_running": is_pid_running(pid_int) if pid_int is not None else None,
        "error": "" if valid else "口令文件未通过本机回环与非凭据校验",
    }


class LoopbackBridge:
    """单会话本地 `127.0.0.1` WMPF 二进制 Protobuf <-> CDP WebSocket 桥接器。"""

    def __init__(
        self,
        allowed_pids: set[int],
        *,
        debug_port: int = 9421,
        cdp_port: int = 9422,
        endpoint_file: Path | None = None,
        status_file: Path | None = None,
        peer_checker: Callable[[int, int], int | None] = owner_pid,
    ) -> None:
        if not allowed_pids or any(p <= 0 for p in allowed_pids):
            raise ValueError("Explicit positive native-client PIDs required")
        self.allowed_pids = set(allowed_pids)
        self.debug_port = debug_port
        self.cdp_port = cdp_port
        self.endpoint_file = endpoint_file
        self.status_file = status_file
        self.peer_checker = peer_checker
        self.secret = secrets.token_urlsafe(24)
        self.native: Any = None
        self.cdp: Any = None
        self.seq = 0
        self.commands = 0
        self.results = 0
        self.rejected = 0
        self.errors: list[str] = []
        self.servers: list[Any] = []
        self.actual_debug_port: int | None = None
        self.actual_cdp_port: int | None = None

    @property
    def browser_ws_url(self) -> str:
        if self.actual_cdp_port is None:
            return ""
        return f"ws://127.0.0.1:{self.actual_cdp_port}/cdp/{self.secret}"

    def status(self) -> dict[str, Any]:
        return {
            "transport": "wmpf-loopback-single-session",
            "listeners_active": bool(self.servers),
            "debug_listening_port": self.actual_debug_port,
            "cdp_listening_port": self.actual_cdp_port,
            "bind_host": "127.0.0.1",
            "allowed_native_pids": sorted(self.allowed_pids),
            "native_connected": self.native is not None,
            "cdp_connected": self.cdp is not None,
            "commands_forwarded": self.commands,
            "results_forwarded": self.results,
            "rejected_connections_or_packets": self.rejected,
            "error_categories": list(self.errors[-12:]),
            "raw_payload_logging": False,
            "updated_at": datetime.datetime.now().astimezone().isoformat(),
        }

    def persist(self) -> None:
        if self.status_file:
            self.status_file.parent.mkdir(parents=True, exist_ok=True)
            self.status_file.write_text(
                json.dumps(self.status(), ensure_ascii=False, indent=2), "utf-8"
            )

    async def check_cdp(self, connection: Any, request: Any) -> Any:
        if not secrets.compare_digest(request.path, "/cdp/" + self.secret):
            return connection.respond(HTTPStatus.FORBIDDEN, "Loopback CDP authorization required\n")
        if self.cdp is not None:
            return connection.respond(HTTPStatus.CONFLICT, "Exactly one CDP client is allowed\n")
        if self.native is None:
            return connection.respond(
                HTTPStatus.SERVICE_UNAVAILABLE, "No native WMPF client is connected\n"
            )
        return None

    async def native_handler(self, ws: Any) -> None:
        from websockets.exceptions import ConnectionClosed

        if self.native is not None:
            self.rejected += 1
            await ws.close(code=1013, reason="Only one native session is allowed")
            self.persist()
            return
        try:
            pid = None
            for attempt in range(3):
                assert self.actual_debug_port is not None
                pid = await asyncio.to_thread(
                    self.peer_checker, ws.remote_address[1], self.actual_debug_port
                )
                if pid is not None:
                    break
                if attempt < 2:
                    await asyncio.sleep(0.05)
        except Exception:
            pid = None

        if pid not in self.allowed_pids:
            self.rejected += 1
            self.errors.append("native_peer_pid_unknown" if pid is None else "native_peer_pid_not_allowed")
            await ws.close(code=1008, reason="Native peer PID not allowed")
            self.persist()
            return

        self.native = ws
        self.persist()
        try:
            async for packet in ws:
                if not isinstance(packet, bytes):
                    self.rejected += 1
                    continue
                try:
                    payload = unwrap_cdp_result(packet)
                except Exception:
                    self.rejected += 1
                    self.errors.append("native_packet_decode_failed")
                    continue
                if payload is not None and self.cdp is not None:
                    try:
                        await self.cdp.send(payload)
                        self.results += 1
                    except ConnectionClosed:
                        pass
                self.persist()
        except ConnectionClosed:
            pass
        finally:
            if self.native is ws:
                self.native = None
            if self.cdp is not None:
                await self.cdp.close(code=1011, reason="Native session disconnected")
            self.persist()

    async def cdp_handler(self, ws: Any) -> None:
        from websockets.exceptions import ConnectionClosed

        if self.cdp is not None or self.native is None:
            await ws.close(code=1013, reason="Session not available")
            return
        self.cdp = ws
        self.persist()
        try:
            async for text in ws:
                if not isinstance(text, str):
                    self.rejected += 1
                    continue
                try:
                    obj = json.loads(text)
                    if not isinstance(obj, dict) or not isinstance(obj.get("method"), str) or "id" not in obj:
                        raise ValueError("CDP command expected")
                    self.seq += 1
                    packet = encode_cdp(
                        text, seq=self.seq, op_id=self.seq, category=CDP_REQUEST, compress=False
                    )
                    if self.native is None:
                        raise ValueError("Native session disconnected")
                    await self.native.send(packet)
                    self.commands += 1
                except ConnectionClosed:
                    break
                except Exception:
                    self.rejected += 1
                    self.errors.append("cdp_command_rejected")
                    await ws.close(code=1008, reason="Invalid CDP command")
                    break
                self.persist()
        except ConnectionClosed:
            pass
        finally:
            if self.cdp is ws:
                self.cdp = None
            self.persist()

    async def start(self) -> "LoopbackBridge":
        from websockets.asyncio.server import serve

        if self.endpoint_file and self.endpoint_file.exists():
            raise FileExistsError("Endpoint descriptor exists; inspect before replacing it")
        try:
            debug = await serve(
                self.native_handler,
                "127.0.0.1",
                self.debug_port,
                max_size=MAX_MESSAGE_BYTES,
                max_queue=8,
                compression=None,
            )
            self.servers.append(debug)
            self.actual_debug_port = debug.sockets[0].getsockname()[1]
            cdp = await serve(
                self.cdp_handler,
                "127.0.0.1",
                self.cdp_port,
                process_request=self.check_cdp,
                max_size=MAX_MESSAGE_BYTES,
                max_queue=8,
                compression=None,
            )
            self.servers.append(cdp)
            self.actual_cdp_port = cdp.sockets[0].getsockname()[1]
            if self.endpoint_file:
                self.endpoint_file.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.endpoint_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(
                        {
                            "browserWebSocketUrl": self.browser_ws_url,
                            "bind_host": "127.0.0.1",
                            "local_debug_control_secret_not_account_token": True,
                            "do_not_publish": True,
                            "server_pid": os.getpid(),
                        },
                        f,
                        ensure_ascii=False,
                        indent=2,
                    )
            self.persist()
            return self
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        for srv in self.servers:
            srv.close()
        for srv in self.servers:
            await srv.wait_closed()
        self.servers = []
        self.native = None
        self.cdp = None
        self.persist()
