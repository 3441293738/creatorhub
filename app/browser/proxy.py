"""Proxy planning shared by Patchright and the XHS Chrome CDP backend.

The account proxy URL is parsed once into an immutable in-memory plan.  Any
projection intended for a process command line is credential-free; secrets are
only returned to in-process authentication code.
"""
from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import re
from contextlib import suppress
from dataclasses import dataclass
from typing import Dict, Literal, Optional
from urllib.parse import quote, unquote, urlparse


ProxyScheme = Literal["http", "https", "socks5"]
ProxyInputFormat = Literal[
    "auto", "host_colon", "host_csv", "host_at", "auth_colon", "auth_at", "host_hash",
]
_INPUT_FORMATS = ("host_colon", "host_csv", "host_at", "auth_colon", "auth_at", "host_hash")
_INPUT_HOST = r"(?:\[[^\]]+\]|[^:\s/@?#,\[\]]+)"


class ProxyConfigError(ValueError):
    """A configured proxy is unusable without echoing its raw value."""


def _bracket_host(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _proxy_endpoint(value: str) -> tuple[str, int] | None:
    """Parse only host:port, never userinfo, URL paths, or credential fields."""
    if not re.fullmatch(rf"{_INPUT_HOST}:[0-9]{{1,5}}", value):
        return None
    try:
        parsed = urlparse("//" + value)
        if parsed.hostname and parsed.port and 1 <= parsed.port <= 65535:
            return parsed.hostname, parsed.port
    except ValueError:
        pass
    return None


def _proxy_input_parts(value: str, input_format: str) -> tuple[str, int, str, str] | None:
    endpoint = username = password = ""
    if input_format == "auth_at":
        credentials, sep, endpoint = value.rpartition("@")
        if not sep:
            return None
        username, _, password = credentials.partition(":")
        # Only URL-style userinfo is percent-encoded. CSV/hash exports are raw.
        username, password = unquote(username), unquote(password)
        endpoint = endpoint.removesuffix("/")
    elif input_format == "host_at":
        endpoint, sep, credentials = value.partition("@")
        username, colon, password = credentials.partition(":")
        if not sep or not colon:
            return None
    elif input_format == "host_hash":
        parts = value.split("##", 2)
        if len(parts) != 3:
            return None
        endpoint, username, password = parts
    elif input_format == "host_csv":
        try:
            parts = next(csv.reader([value], skipinitialspace=True, strict=True))
        except (csv.Error, StopIteration):
            return None
        if len(parts) != 4:
            return None
        host, port, username, password = parts
        endpoint = f"{host.strip()}:{port.strip()}"
    elif input_format == "host_colon":
        match = re.fullmatch(rf"({_INPUT_HOST}:[0-9]{{1,5}}):([^:]*):(.*)", value)
        if not match:
            return None
        endpoint, username, password = match.groups()
    elif input_format == "auth_colon":
        match = re.fullmatch(rf"([^:]*):(.*):({_INPUT_HOST}:[0-9]{{1,5}})", value)
        if not match:
            return None
        username, password, endpoint = match.groups()
    parsed = _proxy_endpoint(endpoint)
    if parsed is None or any(ord(ch) < 32 or ord(ch) == 127 for ch in username + password):
        return None
    return *parsed, username, password


@dataclass(frozen=True)
class ProxyPlan:
    scheme: ProxyScheme
    host: str
    port: int
    username: str = ""
    password: str = ""

    @classmethod
    def parse(cls, raw: str, *, default_scheme: str = "http",
              input_format: ProxyInputFormat = "auto") -> Optional["ProxyPlan"]:
        """Accept standard URLs and six common provider export formats.

        Ambiguous bare inputs require an explicit format rather than silently
        exchanging the proxy endpoint with numeric credentials. An explicit
        standard URL always has ordinary URL userinfo semantics.
        """
        value = str(raw or "").strip()
        if not value:
            return None
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise ProxyConfigError("代理配置无效：请每行填写一条代理")
        has_scheme = bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", value))
        scheme, value = value.split("://", 1) if has_scheme else (default_scheme, value)
        scheme = scheme.lower()
        if scheme in {"socks", "socks5h"}:
            scheme = "socks5"
        if scheme not in {"http", "https", "socks5"}:
            raise ProxyConfigError("不支持的代理协议")
        if input_format != "auto" and input_format not in _INPUT_FORMATS:
            raise ProxyConfigError("不支持的代理输入格式")

        if input_format == "auto":
            endpoint = _proxy_endpoint(value.removesuffix("/") if has_scheme else value)
            if endpoint:
                return cls(scheme, *endpoint)
            standard = _proxy_input_parts(value, "auth_at") if has_scheme else None
            if standard:
                return cls(scheme, *standard)
        formats = _INPUT_FORMATS if input_format == "auto" else (input_format,)
        candidates = {parts for fmt in formats if (parts := _proxy_input_parts(value, fmt)) is not None}
        if len(candidates) > 1:
            raise ProxyConfigError(
                "代理格式有歧义：请选择对应输入格式，或填写 协议://用户名:密码@主机:端口")
        if not candidates:
            raise ProxyConfigError("代理配置无效：请检查输入格式、主机及端口（1–65535）")
        return cls(scheme, *candidates.pop())

    @property
    def authenticated(self) -> bool:
        return bool(self.username or self.password)

    @property
    def server(self) -> str:
        return f"{self.scheme}://{_bracket_host(self.host)}:{self.port}"

    @property
    def normalized(self) -> str:
        if not self.authenticated:
            return self.server
        user = quote(self.username, safe="")
        password = quote(self.password, safe="")
        return (
            f"{self.scheme}://{user}:{password}@"
            f"{_bracket_host(self.host)}:{self.port}"
        )

    @property
    def redacted(self) -> str:
        auth = "***@" if self.authenticated else ""
        return f"{self.scheme}://{auth}{_bracket_host(self.host)}:{self.port}"

    @property
    def signature(self) -> str:
        raw = (
            f"{self.scheme}\0{self.host}\0{self.port}\0"
            f"{self.username}\0{self.password}"
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def playwright(self) -> Dict[str, str]:
        result = {"server": self.server}
        if self.username:
            result["username"] = self.username
        if self.password:
            result["password"] = self.password
        return result

    def chrome_server(self, relay_port: int | None = None) -> str:
        if self.scheme == "socks5" and self.authenticated:
            if relay_port is None or not (1 <= int(relay_port) <= 65535):
                raise ProxyConfigError("认证 SOCKS5 需要已启动的本地转发器")
            return f"socks5://127.0.0.1:{int(relay_port)}"
        return self.server


def try_proxy_plan(raw: str) -> Optional[ProxyPlan]:
    """Compatibility parser: invalid values are represented as ``None``."""
    try:
        return ProxyPlan.parse(raw)
    except ProxyConfigError:
        return None


class HttpProxyAuthRelay:
    """Loopback HTTP proxy that authenticates to an upstream HTTP proxy.

    Chromium occasionally omits credentials on the first request through an
    authenticated HTTP proxy. The browser connects to this credential-free
    loopback endpoint; the relay injects Proxy-Authorization deterministically
    for CONNECT and ordinary absolute-form HTTP requests.
    """

    listen_host = "127.0.0.1"

    def __init__(self, plan: ProxyPlan):
        if plan.scheme not in {"http", "https"} or not plan.authenticated:
            raise ProxyConfigError("本地 HTTP 转发器只接受认证 HTTP 代理")
        self.plan = plan
        self._server: asyncio.AbstractServer | None = None
        self._port = 0
        self._connections: set[asyncio.Task] = set()

    @property
    def port(self) -> int:
        return self._port

    @property
    def chrome_server(self) -> str:
        if not self._port:
            raise ProxyConfigError("认证 HTTP 本地转发器尚未启动")
        return f"http://{self.listen_host}:{self._port}"

    @property
    def active_connections(self) -> int:
        return len(self._connections)

    async def start(self) -> int:
        if self._server is not None:
            return self._port
        self._server = await asyncio.start_server(
            self._handle_client, self.listen_host, 0)
        socket = self._server.sockets[0]
        self._port = int(socket.getsockname()[1])
        return self._port

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()
        tasks = list(self._connections)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._connections.clear()
        self._port = 0

    def _authorization(self) -> bytes:
        token = base64.b64encode(
            f"{self.plan.username}:{self.plan.password}".encode("utf-8"))
        return b"Basic " + token

    async def _handle_client(self, reader, writer) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._connections.add(task)
        upstream_writer = None
        try:
            header = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=15)
            first_line, *lines = header.split(b"\r\n")
            parts = first_line.split(b" ", 2)
            if len(parts) != 3:
                return
            filtered = [
                line for line in lines
                if line and not line.lower().startswith(b"proxy-authorization:")
            ]
            outbound = b"\r\n".join([
                first_line,
                *filtered,
                b"Proxy-Authorization: " + self._authorization(),
                b"",
                b"",
            ])
            upstream_reader, upstream_writer = await asyncio.open_connection(
                self.plan.host, self.plan.port,
                ssl=(self.plan.scheme == "https"),
                server_hostname=(self.plan.host if self.plan.scheme == "https" else None),
            )
            upstream_writer.write(outbound)
            await upstream_writer.drain()
            if parts[0].upper() == b"CONNECT":
                response = await asyncio.wait_for(
                    upstream_reader.readuntil(b"\r\n\r\n"), timeout=20)
                writer.write(response)
                await writer.drain()
                status = response.split(b"\r\n", 1)[0].split(b" ", 2)
                if len(status) < 2 or status[1] != b"200":
                    return
            downstream = asyncio.create_task(self._pipe(reader, upstream_writer))
            upstream = asyncio.create_task(self._pipe(upstream_reader, writer))
            done, pending = await asyncio.wait(
                {downstream, upstream}, return_when=asyncio.FIRST_COMPLETED)
            for pending_task in pending:
                pending_task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError, asyncio.IncompleteReadError,
                asyncio.LimitOverrunError, asyncio.TimeoutError):
            pass
        finally:
            if upstream_writer is not None:
                upstream_writer.close()
                with suppress(Exception):
                    await upstream_writer.wait_closed()
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            if task is not None:
                self._connections.discard(task)

    @staticmethod
    async def _pipe(reader, writer) -> None:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()


class Socks5AuthRelay:
    """Loopback-only no-auth SOCKS5 endpoint backed by an authenticated proxy.

    Chrome connects to the local endpoint without credentials.  The relay
    performs RFC 1929 authentication against the account's upstream SOCKS5
    server and forwards only TCP CONNECT.  Destination domain names remain in
    domain form, so they are resolved by the upstream rather than locally.
    """

    listen_host = "127.0.0.1"

    def __init__(self, plan: ProxyPlan):
        if plan.scheme != "socks5" or not plan.authenticated:
            raise ProxyConfigError("本地转发器只接受认证 SOCKS5 代理")
        if len(plan.username.encode("utf-8")) > 255:
            raise ProxyConfigError("SOCKS5 用户名过长")
        if len(plan.password.encode("utf-8")) > 255:
            raise ProxyConfigError("SOCKS5 密码过长")
        self.plan = plan
        self._server: asyncio.AbstractServer | None = None
        self._port = 0
        self._connections: set[asyncio.Task] = set()

    @property
    def port(self) -> int:
        return self._port

    @property
    def chrome_server(self) -> str:
        if not self._port:
            raise ProxyConfigError("认证 SOCKS5 本地转发器尚未启动")
        return f"socks5://{self.listen_host}:{self._port}"

    @property
    def active_connections(self) -> int:
        return len(self._connections)

    async def start(self) -> int:
        if self._server is not None:
            return self._port
        self._server = await asyncio.start_server(
            self._handle_client, self.listen_host, 0)
        socket = self._server.sockets[0]
        self._port = int(socket.getsockname()[1])
        return self._port

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()
        tasks = list(self._connections)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._connections.clear()
        self._port = 0

    async def _handle_client(
            self, reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._connections.add(task)
        upstream_writer: asyncio.StreamWriter | None = None
        try:
            if not await self._accept_local_greeting(reader, writer):
                return
            request_head = await reader.readexactly(4)
            if request_head[0] != 5:
                return
            if request_head[1] != 1:  # TCP CONNECT only
                await self._reply(writer, 7)
                return
            address = await self._read_address(reader, request_head[3])

            upstream_reader, upstream_writer = await asyncio.open_connection(
                self.plan.host, self.plan.port)
            await self._authenticate_upstream(upstream_reader, upstream_writer)
            upstream_writer.write(b"\x05\x01\x00" + address)
            await upstream_writer.drain()
            response_head = await upstream_reader.readexactly(4)
            response_address = await self._read_address(
                upstream_reader, response_head[3])
            writer.write(response_head[:3] + response_address)
            await writer.drain()
            if response_head[0] != 5 or response_head[1] != 0:
                return

            downstream = asyncio.create_task(
                self._pipe(reader, upstream_writer))
            upstream = asyncio.create_task(
                self._pipe(upstream_reader, writer))
            done, pending = await asyncio.wait(
                {downstream, upstream},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for pending_task in pending:
                pending_task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError, asyncio.IncompleteReadError,
                ProxyConfigError):
            with suppress(Exception):
                await self._reply(writer, 1)
        finally:
            if upstream_writer is not None:
                upstream_writer.close()
                with suppress(Exception):
                    await upstream_writer.wait_closed()
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()
            if task is not None:
                self._connections.discard(task)

    @staticmethod
    async def _accept_local_greeting(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter) -> bool:
        version, count = await reader.readexactly(2)
        methods = await reader.readexactly(count)
        if version != 5 or 0 not in methods:
            writer.write(b"\x05\xff")
            await writer.drain()
            return False
        writer.write(b"\x05\x00")
        await writer.drain()
        return True

    async def _authenticate_upstream(
            self, reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter) -> None:
        writer.write(b"\x05\x01\x02")
        await writer.drain()
        if await reader.readexactly(2) != b"\x05\x02":
            raise ProxyConfigError("SOCKS5 上游不接受账号认证")
        username = self.plan.username.encode("utf-8")
        password = self.plan.password.encode("utf-8")
        writer.write(
            b"\x01" + bytes([len(username)]) + username
            + bytes([len(password)]) + password
        )
        await writer.drain()
        response = await reader.readexactly(2)
        if response != b"\x01\x00":
            raise ProxyConfigError("SOCKS5 上游认证失败")

    @staticmethod
    async def _read_address(
            reader: asyncio.StreamReader, atyp: int) -> bytes:
        if atyp == 1:
            body = await reader.readexactly(4)
        elif atyp == 3:
            length = await reader.readexactly(1)
            body = length + await reader.readexactly(length[0])
        elif atyp == 4:
            body = await reader.readexactly(16)
        else:
            raise ProxyConfigError("SOCKS5 地址类型不受支持")
        port = await reader.readexactly(2)
        return bytes([atyp]) + body + port

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, code: int) -> None:
        writer.write(
            bytes([5, code, 0, 1]) + b"\x00\x00\x00\x00\x00\x00")
        await writer.drain()

    @staticmethod
    async def _pipe(
            reader: asyncio.StreamReader,
            writer: asyncio.StreamWriter) -> None:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
