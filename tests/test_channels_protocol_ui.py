"""Tests for visual local WeChat Channels CDP config, bridge lifecycle, and matrix linkage."""
from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import app.db as db
import app.main as main
from app.channels_settings import (
    CHANNELS_SETTINGS_KEY,
    export_channels_settings,
    load_persisted_channels_settings,
    reset_channels_settings,
    save_channels_settings,
)
from app.config import Config
from app.models import AppSetting
from app.platforms.channels import (
    ChannelsBridgeManager,
    ChannelsProtocolClient,
    discover_local_cdp_endpoints,
    inspect_endpoint_file,
    owner_pid,
)
from app.transport_matrix import build_transport_matrix, resolve_transport
from test_project_optimizations import local_project  # noqa: F401


def _api(method: str, path: str, payload: dict | None = None, *, headers=None, peer="127.0.0.1"):
    async def _run():
        transport = httpx.ASGITransport(app=main.app, client=(peer, 2345), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            kwargs = {"headers": headers or {}}
            if payload is not None:
                kwargs["json"] = payload
            return await client.request(method, path, **kwargs)

    return asyncio.run(_run())


def test_channels_protocol_config_persists_validates_and_resets(local_project):
    cfg = local_project.cfg
    cfg.engine.channels_cdp_endpoint = "http://127.0.0.1:9200"

    res = _api(
        "PUT",
        "/api/channels/protocol/config",
        {
            "channels_read_mode": "protocol",
            "connection_mode": "endpoint",
            "channels_cdp_endpoint": "http://127.0.0.1:9222",
        },
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["read_mode"] == "protocol"
    assert body["configured"] is True
    assert body["endpoint_configured"] is True
    assert body["config"]["cdp_endpoint"] == "http://127.0.0.1:9222"
    assert "yuanbao_cookie_set" not in body["config"]
    assert cfg.engine.channels_cdp_endpoint == "http://127.0.0.1:9222"
    assert cfg.engine.channels_read_mode == "protocol"

    # Restart simulation restores saved UI overrides without mutating config.yaml.
    restarted = Config()
    restarted.engine.channels_cdp_endpoint = "http://127.0.0.1:9200"
    assert load_persisted_channels_settings(restarted) is True
    assert restarted.engine.channels_cdp_endpoint == "http://127.0.0.1:9222"

    # Reject non-loopback endpoint atomically without mutating channels_read_mode.
    bad = _api(
        "PUT",
        "/api/channels/protocol/config",
        {
            "channels_read_mode": "browser",
            "connection_mode": "endpoint",
            "channels_cdp_endpoint": "http://192.168.1.25:9222",
        },
    )
    assert bad.status_code == 422
    assert cfg.engine.channels_read_mode == "protocol"
    assert cfg.engine.channels_cdp_endpoint == "http://127.0.0.1:9222"

    # Reject removed yuanbao cookie fields cleanly.
    rejected_yuanbao = _api(
        "PUT",
        "/api/channels/protocol/config",
        {"channels_yuanbao_cookie": "abc"},
    )
    assert rejected_yuanbao.status_code == 422

    # Reset restores the startup base config captured on cfg.
    reset_res = _api("DELETE", "/api/channels/protocol/config")
    assert reset_res.status_code == 200
    assert reset_res.json()["config"]["cdp_endpoint"] == "http://127.0.0.1:9200"
    with db.get_session() as session:
        assert session.get(AppSetting, CHANNELS_SETTINGS_KEY) is None


def test_channels_protocol_config_enforces_local_and_same_origin(local_project):
    assert _api("GET", "/api/channels/protocol/config", peer="192.0.2.1").status_code == 403
    assert _api(
        "PUT",
        "/api/channels/protocol/config",
        {"connection_mode": "none"},
        headers={"Origin": "https://evil.invalid"},
    ).status_code == 403


def test_channels_protocol_probe_tests_draft_before_saving(local_project):
    with patch.object(
        ChannelsProtocolClient,
        "probe",
        new=AsyncMock(return_value={
            "ok": True,
            "ready": True,
            "transport": "wechat-main-world-cdp",
            "environment": {"host": "channels.weixin.qq.com", "path": "/web/pages/feed", "nativeBridge": True, "wechatUA": True},
            "service": {"capabilities": ["finderGetCommentList", "finderSearch"]},
        }),
    ):
        res = _api(
            "POST",
            "/api/channels/protocol/probe",
            {"connection_mode": "endpoint", "cdp_endpoint": "http://127.0.0.1:9222"},
        )
    assert res.status_code == 200, res.text
    assert res.json()["ok"] is True
    assert res.json()["ready"] is True
    # Probing a draft must not persist it.
    assert local_project.cfg.engine.channels_cdp_endpoint == ""

    bad = _api(
        "POST",
        "/api/channels/protocol/probe",
        {"connection_mode": "endpoint", "cdp_endpoint": "http://user:pass@127.0.0.1:9222"},
    )
    assert bad.status_code == 422


def test_discover_local_cdp_endpoints_filters_finder_pages_and_strips_query():
    targets_payload = [
        {
            "id": "page-1",
            "type": "page",
            "title": "微信视频号",
            "url": "https://channels.weixin.qq.com/web/pages/feed?token=secret_query_param",
        },
        {
            "id": "page-2",
            "type": "page",
            "title": "普通页面",
            "url": "https://example.com/other",
        },
    ]

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/json/list":
                data = json.dumps(targets_payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        found = asyncio.run(
            discover_local_cdp_endpoints(
                [f"http://127.0.0.1:{server.server_port}", "http://192.168.1.99:9222"],
                ports=(),
                timeout=1.0,
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)

    assert len(found) == 1
    assert found[0]["endpoint"] == f"http://127.0.0.1:{server.server_port}"
    assert found[0]["has_finder_page"] is True
    assert found[0]["total_pages"] == 2
    assert found[0]["finder_targets"] == [
        {"id": "page-1", "title": "微信视频号", "path": "/web/pages/feed"}
    ]
    assert "secret_query_param" not in json.dumps(found)


def test_owner_pid_matches_loopback_socket():
    srv = socket.create_server(("127.0.0.1", 0))
    cli = socket.create_connection(("127.0.0.1", srv.getsockname()[1]))
    conn, _ = srv.accept()
    try:
        pid = owner_pid(cli.getsockname()[1], srv.getsockname()[1])
        if os.name == "nt":
            assert pid == os.getpid()
    finally:
        conn.close()
        cli.close()
        srv.close()


def test_managed_bridge_start_stop_and_transport_matrix_linkage(local_project, monkeypatch):
    cfg = local_project.cfg
    cfg.db_path = str(local_project.path)
    bridge_mgr = ChannelsBridgeManager(local_project.root)
    monkeypatch.setattr(main, "channels_bridge", bridge_mgr)

    # Before configuring CDP, matrix must honestly report fallback / unavailability.
    cfg.engine.channels_read_mode = "auto"
    assert resolve_transport(cfg, "shipinhao", "finder_protocol")["effective_mode"] == "browser"
    assert resolve_transport(cfg, "shipinhao", "share_download")["effective_mode"] == "api"
    cfg.engine.channels_read_mode = "protocol"
    assert resolve_transport(cfg, "shipinhao", "finder_protocol")["effective_mode"] == "unavailable"
    cfg.engine.channels_read_mode = "auto"

    async def _run_lifecycle():
        transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 2345), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            start_res = await client.post(
                "/api/channels/protocol/bridge/start",
                json={"debug_port": 0, "cdp_port": 0, "allowed_pids": [os.getpid()], "apply_to_config": True},
            )
            assert start_res.status_code == 200, start_res.text
            started = start_res.json()
            assert started["ok"] is True
            assert started["bridge"]["running"] is True
            assert started["bridge"]["debug_listening_port"] > 0
            assert started["bridge"]["cdp_listening_port"] > 0
            assert "browserWebSocketUrl" not in start_res.text
            assert "/cdp/" not in start_res.text
            assert started["config"]["connection_mode"] == "endpoint_file"
            assert started["config"]["endpoint_file_info"]["valid"] is True
            assert started["capabilities"]["keyword_search"] is True

            # Transport matrix now upgrades to hybrid/api because endpoint_file is valid on disk.
            matrix = build_transport_matrix(cfg, [])
            assert matrix["settings"]["channels_cdp_configured"] is True
            assert matrix["settings"]["channels_cdp_ready"] is True
            finder_row = next(
                r for r in matrix["rows"] if r["platform"] == "shipinhao" and r["operation"] == "finder_protocol"
            )
            assert finder_row["effective_mode"] == "hybrid"

            # Stop bridge cleans up the managed file and clears the dangling managed path.
            stop_res = await client.post("/api/channels/protocol/bridge/stop")
            assert stop_res.status_code == 200, stop_res.text
            stopped = stop_res.json()
            assert stopped["bridge"]["running"] is False
            assert not bridge_mgr.endpoint_file.exists()
            assert stopped["config"]["cdp_endpoint_file"] == ""
            assert resolve_transport(cfg, "shipinhao", "finder_protocol")["effective_mode"] == "browser"

    asyncio.run(_run_lifecycle())


def test_visual_channels_card_has_accessible_controls_and_workbench_tab():
    html = (Path(__file__).parents[1] / "app" / "web" / "index.html").read_text(encoding="utf-8")
    workbench_src = (Path(__file__).parents[1] / "frontend" / "workbench.jsx").read_text(encoding="utf-8")
    workbench_bundle = (Path(__file__).parents[1] / "app" / "web" / "workbench.js").read_text(encoding="utf-8")

    assert '["channels", "视频号微信通道"]' in workbench_src
    assert (
        "视频号微信通道" in workbench_bundle
        or r"\u89c6\u9891\u53f7\u5fae\u4fe1\u901a\u9053" in workbench_bundle.lower()
    )
    assert 'id="channels-protocol-card" data-section="channels"' in html
    assert 'id="channels-advanced-details"' in html
    assert '<details class="collection-advanced" id="channels-advanced-details" open>' not in html
    assert 'id="ch-proto-advanced"' not in html
    assert 'id="ch-proto-yuanbao-cookie"' not in html

    card_html = html.split('id="channels-protocol-card"', 1)[1].split("</section>", 1)[0]
    control_ids = set(re.findall(r'<(?:input|select)\b[^>]*\bid="([^"]+)"', card_html))
    label_fors = set(re.findall(r'<label\b[^>]*\bfor="([^"]+)"', card_html))
    assert control_ids and control_ids <= label_fors, control_ids - label_fors
