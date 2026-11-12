"""Optional stdio MCP bridge. It only calls local allowlisted read endpoints.

Start CreatorHub and enable MCP queries first. Run in a separate Python
environment with requirements-mcp.txt; this module never imports app.main,
opens the database, starts a browser, or invokes a paid model.
"""
from __future__ import annotations

import asyncio
import json
import os
from ipaddress import ip_address
from urllib.parse import urlsplit

import httpx

ALLOWED_TOOLS = frozenset({"list_accounts", "list_tasks", "get_task", "get_metrics", "get_risk_events"})


def validate_base(value: str) -> str:
    value = value.rstrip("/")
    parsed = urlsplit(value)
    host = parsed.hostname or ""
    try:
        local = ip_address(host).is_loopback
    except ValueError:
        local = host == "localhost"
    if (not local or parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}):
        raise ValueError("CREATORHUB_URL 须为本机回环地址，不含路径或凭据")
    parsed.port  # Validate malformed ports before creating a client.
    return value


async def local_request(base: str, method: str, path: str, arguments=None):
    try:
        async with httpx.AsyncClient(base_url=validate_base(base), timeout=20,
                                     follow_redirects=False, trust_env=False) as client:
            response = await client.request(method, path, json=arguments)
        if response.status_code != 200:
            raise ValueError(f"CreatorHub 返回 HTTP {response.status_code}；检查本地服务、MCP 开关和工具参数")
        return response.json()
    except httpx.HTTPError as exc:
        raise ValueError("连接本地 CreatorHub 失败，请先启动工作台服务") from exc


def build_server(base: str):
    import mcp.types as types
    from mcp.server.lowlevel import Server

    base = validate_base(base)
    server = Server("creatorhub-readonly")

    @server.list_tools()
    async def list_tools():
        definitions = await local_request(base, "GET", "/api/agent/tools")
        return [types.Tool(name=tool["name"], description=tool["description"],
                           inputSchema=tool["inputSchema"],
                           annotations=types.ToolAnnotations(readOnlyHint=True, destructiveHint=False,
                                                             idempotentHint=True, openWorldHint=False))
                for tool in definitions if tool["name"] in ALLOWED_TOOLS]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        if name not in ALLOWED_TOOLS:
            raise ValueError("MCP 仅开放业务查询工具")
        result = await local_request(base, "POST", "/api/agent/tools/" + name, arguments)
        return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

    return server


async def serve():
    from mcp.server.stdio import stdio_server

    server = build_server(os.environ.get("CREATORHUB_URL", "http://127.0.0.1:8000"))
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(serve())
