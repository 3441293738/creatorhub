"""Optional official SDK contract test; local HTTP fixture + stdio subprocess.

Run with requirements-mcp.txt available in an isolated test environment.
"""
import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="optional MCP SDK is installed separately")
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def test_official_sdk_discovers_and_calls_readonly_stdio_bridge():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            body = json.dumps([{"name": "list_accounts", "description": "fixture",
                                "inputSchema": {"type": "object", "properties": {}}}]).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            calls.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
            self.wfile.write(b'{"accounts":[{"id":1,"nickname":"fixture"}]}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()

    async def scenario():
        environment = dict(os.environ, CREATORHUB_URL=f"http://127.0.0.1:{server.server_port}")
        params = StdioServerParameters(command=sys.executable,
                                      args=[str(Path(__file__).resolve().parents[1] / "app/agent/mcp_server.py")], env=environment)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                assert [tool.name for tool in listed.tools] == ["list_accounts"]
                assert listed.tools[0].annotations.readOnlyHint
                result = await session.call_tool("list_accounts", {})
                assert not result.isError and json.loads(result.content[0].text)["accounts"][0]["id"] == 1
                rejected = await session.call_tool("delete_account", {})
                assert rejected.isError
    try:
        asyncio.run(asyncio.wait_for(scenario(), timeout=30))
        assert calls == [("/api/agent/tools/list_accounts", {})]
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
