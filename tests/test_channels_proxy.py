"""End-to-end offline tests for Channels selective local HTTPS proxy, CA, WinINet backup, and RPC bridge."""
from __future__ import annotations

import asyncio
import json
import socket
import ssl
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import h11
import httpx
import pytest
from websockets.asyncio.client import connect as ws_connect

import app.main as main
from app.channels_settings import load_persisted_channels_settings
from app.config import Config
from app.platforms.channels.client import ChannelsProtocolClient, ChannelsProtocolTransportError, _AsyncCdpSession
from app.platforms.channels.local_ca import LocalCaManager
from app.platforms.channels.proxy_bridge import (
    ChannelsProxyManager,
    _inject_bridge_script_into_html,
    _relax_csp_for_bridge,
)
from app.platforms.channels.system_proxy import (
    RegistrySnapshot,
    enable_system_proxy,
    get_system_proxy_state,
    merge_proxy_override,
    recover_stale_system_proxy,
    restore_system_proxy,
)
from app.transport_matrix import build_transport_matrix, resolve_transport
from test_project_optimizations import local_project  # noqa: F401


class _FakeRegistryBackend:
    """In-memory replacement for HKCU WinINet registry (never touches host Windows settings)."""

    def __init__(
        self,
        *,
        proxy_enable: int | None = 0,
        proxy_server: str | None = "corp.proxy:8080",
        proxy_override: str | None = "*.corp.local;127.0.0.1",
        supported: bool = True,
    ) -> None:
        self._supported = supported
        self.proxy_enable = proxy_enable
        self.proxy_server = proxy_server
        self.proxy_override = proxy_override
        self.apply_calls: list[dict[str, object]] = []

    @property
    def supported(self) -> bool:
        return self._supported

    def read_snapshot(self) -> RegistrySnapshot:
        return RegistrySnapshot(
            proxy_enable=self.proxy_enable,
            proxy_server=self.proxy_server,
            proxy_override=self.proxy_override,
        )

    def apply_values(
        self,
        *,
        proxy_enable: int | None,
        proxy_server: str | None,
        proxy_override: str | None,
    ) -> None:
        self.proxy_enable = proxy_enable
        self.proxy_server = proxy_server
        self.proxy_override = proxy_override
        self.apply_calls.append(
            {
                "proxy_enable": proxy_enable,
                "proxy_server": proxy_server,
                "proxy_override": proxy_override,
            }
        )


def _open_proxy_connect_socket(proxy_port: int, authority: str) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", proxy_port), timeout=10.0)
    req = f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode("ascii")
    sock.sendall(req)
    buf = bytearray()
    while not buf.endswith(b"\r\n\r\n"):
        ch = sock.recv(1)
        if not ch:
            raise AssertionError(f"Proxy closed during CONNECT: {bytes(buf)!r}")
        buf.extend(ch)
        if len(buf) > 8192:
            raise AssertionError("CONNECT response too long")
    first_line = bytes(buf).split(b"\r\n", 1)[0]
    assert b" 200 " in first_line, f"Unexpected CONNECT status: {first_line!r}"
    return sock


def test_local_ca_restricts_leaf_hosts_and_persists_ca(tmp_path):
    ca = LocalCaManager(tmp_path)
    try:
        cert_path, key_path, thumb = ca.ensure_ca()
        assert cert_path.is_file()
        assert key_path.is_file()
        assert len(thumb) == 40

        ctx = ca.ssl_context_for_host("channels.weixin.qq.com")
        assert isinstance(ctx, ssl.SSLContext)

        for disallowed in ("example.com", "weixin.qq.com", "res.wx.qq.com", ""):
            with pytest.raises(ValueError):
                ca.ssl_context_for_host(disallowed)
    finally:
        ca.close()


def test_system_proxy_backup_preserve_restore_and_crash_recovery(tmp_path):
    assert merge_proxy_override("*.corp; 127.0.0.1 ; ") == "*.corp;127.0.0.1;localhost;[::1];<local>"

    fake = _FakeRegistryBackend(
        proxy_enable=0,
        proxy_server="old.corp:3128",
        proxy_override="127.0.0.1;*.internal",
    )
    res1 = enable_system_proxy(port=2023, data_dir=tmp_path, backend=fake)
    assert res1["ok"] is True
    assert fake.proxy_enable == 1
    assert fake.proxy_server == "127.0.0.1:2023"
    assert "localhost" in (fake.proxy_override or "")
    assert "[::1]" in (fake.proxy_override or "")
    assert "<local>" in (fake.proxy_override or "")

    # Repeated enable must NOT overwrite the pre-CreatorHub baseline in backup.
    res2 = enable_system_proxy(port=2024, data_dir=tmp_path, backend=fake)
    assert res2["ok"] is True
    assert fake.proxy_server == "127.0.0.1:2024"

    restored = restore_system_proxy(data_dir=tmp_path, backend=fake)
    assert restored["ok"] is True
    assert restored["restored"] is True
    assert fake.proxy_enable == 0
    assert fake.proxy_server == "old.corp:3128"
    assert fake.proxy_override == "127.0.0.1;*.internal"
    assert not (tmp_path / "channels_proxy_backup.json").exists()

    # Simulated crash while proxy was active, then user changed proxy externally:
    # recovery must not clobber user's external proxy.
    enable_system_proxy(port=2023, data_dir=tmp_path, backend=fake)
    fake.proxy_server = "user.custom:9999"
    rec_ext = recover_stale_system_proxy(tmp_path, backend=fake)
    assert rec_ext["recovered"] is False
    assert rec_ext["reason"] == "modified_externally"
    assert fake.proxy_server == "user.custom:9999"
    assert not (tmp_path / "channels_proxy_backup.json").exists()

    # Baseline with missing registry values (`None`) restores cleanly and deletes values.
    clean_reg = _FakeRegistryBackend(proxy_enable=None, proxy_server=None, proxy_override=None)
    enable_system_proxy(port=2025, data_dir=tmp_path, backend=clean_reg)
    rec_crash = recover_stale_system_proxy(tmp_path, backend=clean_reg)
    assert rec_crash["recovered"] is True
    assert clean_reg.proxy_enable is None
    assert clean_reg.proxy_server is None
    assert clean_reg.proxy_override is None
    assert get_system_proxy_state(data_dir=tmp_path, backend=clean_reg)["backup_present"] is False


def test_html_injection_and_csp_relaxation():
    raw_html = '<html><head><script nonce="n123">console.log(1)</script></head><body>hi</body></html>'
    injected = _inject_bridge_script_into_html(raw_html, "/__creatorhub_bridge.js?v=1")
    assert '<script nonce="n123" src="/__creatorhub_bridge.js?v=1" defer></script></head>' in injected
    assert _inject_bridge_script_into_html(injected, "/__creatorhub_bridge.js?v=2") == injected

    relaxed = _relax_csp_for_bridge("default-src 'self'; script-src 'nonce-n123' https://res.wx.qq.com; connect-src https://channels.weixin.qq.com")
    assert "script-src 'self' 'nonce-n123' https://res.wx.qq.com" in relaxed
    assert "wss://channels.weixin.qq.com" in relaxed


def test_http_503_cdp_error_explains_option_a():
    err = RuntimeError("server rejected WebSocket connection: HTTP 503")
    with patch("websockets.asyncio.client.connect", side_effect=err):
        with pytest.raises(ChannelsProtocolTransportError) as exc_info:
            asyncio.run(_AsyncCdpSession.open("ws://127.0.0.1:9422/cdp/test", timeout=2.0))
    msg = str(exc_info.value)
    assert "HTTP 503" in msg
    assert "9421" in msg
    assert "内置本机微信代理桥接" in msg


def test_selective_proxy_passthrough_injection_and_rpc_end_to_end(tmp_path):
    async def _run():
        fake_reg = _FakeRegistryBackend(proxy_enable=0, proxy_server="", proxy_override="")

        def _upstream_handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/web/pages/feed":
                headers = [
                    ("Content-Type", "text/html; charset=utf-8"),
                    ("Content-Security-Policy", "script-src 'nonce-xyz' https://res.wx.qq.com; connect-src https://channels.weixin.qq.com"),
                    ("ETag", '"etag-1"'),
                    ("Set-Cookie", "wxuin=123; Path=/"),
                    ("Set-Cookie", "session=abc; Path=/"),
                ]
                return httpx.Response(
                    200,
                    headers=headers,
                    text='<!doctype html><html><head><script nonce="xyz">1;</script></head><body>Finder</body></html>',
                )
            return httpx.Response(200, json={"ok": True, "path": request.url.path})

        echo_server = None
        proxy = ChannelsProxyManager(
            tmp_path,
            system_proxy_backend=fake_reg,
            upstream_transport=httpx.MockTransport(_upstream_handler),
        )

        # 1. Start a dummy non-channels TCP echo server to test encrypted/raw passthrough.
        async def _echo_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            try:
                while data := await reader.read(4096):
                    writer.write(b"ECHO:" + data)
                    await writer.drain()
            finally:
                writer.close()

        echo_server = await asyncio.start_server(_echo_client, "127.0.0.1", 0)
        echo_port = echo_server.sockets[0].getsockname()[1]

        try:
            started = await proxy.start(proxy_port=0, enable_system_proxy_flag=True)
            assert started["running"] is True
            proxy_port = started["proxy_port"]
            assert proxy_port > 0
            assert fake_reg.proxy_enable == 1
            assert fake_reg.proxy_server == f"127.0.0.1:{proxy_port}"

            # 2. Verify non-channels CONNECT traffic goes through raw TCP passthrough untouched.
            def _roundtrip_passthrough() -> bytes:
                raw_sock = _open_proxy_connect_socket(proxy_port, f"127.0.0.1:{echo_port}")
                try:
                    raw_sock.sendall(b"opaque-tls-bytes")
                    return raw_sock.recv(64)
                finally:
                    raw_sock.close()

            reply = await asyncio.to_thread(_roundtrip_passthrough)
            assert reply == b"ECHO:opaque-tls-bytes"
            assert proxy.status()["connect_passthrough"] == 1
            assert proxy.status()["connect_intercepted"] == 0

            # 3. Verify intercepted https://channels.weixin.qq.com injects bridge JS, fixes CSP & preserves cookies.
            ca_ctx = ssl.create_default_context(cafile=str(proxy.ca.ca_cert_path))

            def _fetch_over_intercept_tls(path: str) -> tuple[int, list[tuple[bytes, bytes]], bytes]:
                sock = _open_proxy_connect_socket(proxy_port, "channels.weixin.qq.com:443")
                tls_sock = ca_ctx.wrap_socket(sock, server_hostname="channels.weixin.qq.com")
                try:
                    c = h11.Connection(h11.CLIENT)
                    tls_sock.sendall(
                        c.send(
                            h11.Request(
                                method="GET",
                                target=path,
                                headers=[("Host", "channels.weixin.qq.com"), ("Accept", "text/html"), ("Connection", "close")],
                            )
                        )
                        + c.send(h11.EndOfMessage())
                    )
                    resp_ev = None
                    body_chunks = []
                    while True:
                        ev = c.next_event()
                        if ev is h11.NEED_DATA:
                            chunk = tls_sock.recv(65536)
                            c.receive_data(chunk)
                            continue
                        if isinstance(ev, h11.Response):
                            resp_ev = ev
                        elif isinstance(ev, h11.Data):
                            body_chunks.append(ev.data)
                        elif isinstance(ev, h11.EndOfMessage):
                            break
                    assert resp_ev is not None
                    return resp_ev.status_code, list(resp_ev.headers.raw_items()), b"".join(body_chunks)
                finally:
                    tls_sock.close()

            status_code, hdrs, html_bytes = await asyncio.to_thread(
                _fetch_over_intercept_tls, "/web/pages/feed"
            )
            assert status_code == 200
            html_str = html_bytes.decode("utf-8")
            assert '<script nonce="xyz" src="/__creatorhub_bridge.js?' in html_str
            set_cookies = [v.decode("latin-1") for k, v in hdrs if k.lower() == b"set-cookie"]
            assert set_cookies == ["wxuin=123; Path=/", "session=abc; Path=/"]
            csp_vals = [v.decode("latin-1") for k, v in hdrs if k.lower() == b"content-security-policy"]
            assert csp_vals and "'self'" in csp_vals[0] and "wss://channels.weixin.qq.com" in csp_vals[0]

            # 4. Fetch /__creatorhub_bridge.js through proxy.
            js_status, _, js_bytes = await asyncio.to_thread(
                _fetch_over_intercept_tls, "/__creatorhub_bridge.js"
            )
            assert js_status == 200
            js_text = js_bytes.decode("utf-8")
            assert "__finderProtocolLoad" in js_text
            assert "__creatorhubProxyBridgeActive" in js_text

            # 5. Reject bad WS token, then open real authenticated wss://channels.weixin.qq.com/__creatorhub_ws.
            bad_sock = await asyncio.to_thread(
                _open_proxy_connect_socket, proxy_port, "channels.weixin.qq.com:443"
            )
            with pytest.raises(Exception):
                async with ws_connect(
                    "wss://channels.weixin.qq.com/__creatorhub_ws?token=wrong",
                    sock=bad_sock,
                    ssl=ca_ctx,
                    server_hostname="channels.weixin.qq.com",
                ):
                    pass

            good_sock = await asyncio.to_thread(
                _open_proxy_connect_socket, proxy_port, "channels.weixin.qq.com:443"
            )

            async def _simulated_wechat_page():
                async with ws_connect(
                    f"wss://channels.weixin.qq.com/__creatorhub_ws?token={proxy._ws_token}",
                    sock=good_sock,
                    ssl=ca_ctx,
                    server_hostname="channels.weixin.qq.com",
                ) as ws:
                    await ws.send(
                        json.dumps(
                            {
                                "type": "hello",
                                "pageId": "wechat-page-1",
                                "url": "https://channels.weixin.qq.com/web/pages/feed",
                                "title": "微信视频号",
                                "ready": True,
                                "origin": "existing WXU.API",
                                "capabilities": {
                                    "search": True,
                                    "fetchUserPage": True,
                                    "fetchFeedDetail": True,
                                    "fetchCommentsPage": True,
                                },
                                "hasWeixinJSBridge": True,
                            }
                        )
                    )
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("type") == "probe":
                            await ws.send(
                                json.dumps(
                                    {
                                        "type": "result",
                                        "id": msg["id"],
                                        "ok": True,
                                        "result": {
                                            "ready": True,
                                            "url": "https://channels.weixin.qq.com/web/pages/feed",
                                            "title": "微信视频号",
                                            "origin": "existing WXU.API",
                                            "capabilities": {"search": True},
                                        },
                                    }
                                )
                            )
                        elif msg.get("type") == "call":
                            method = msg["method"]
                            if method == "search":
                                result = {
                                    "errCode": 0,
                                    "data": {
                                        "objectList": [
                                            {
                                                "id": "10001",
                                                "objectNonceId": "nonce_1",
                                                "createtime": 1730000000,
                                                "contact": {"username": "v2_author@finder", "nickname": "航拍作者"},
                                                "objectDesc": {"description": "无人机航拍", "media": []},
                                            }
                                        ],
                                        "lastBuff": "",
                                        "continueFlag": 0,
                                    },
                                }
                            elif method == "fetchUserPage":
                                result = {
                                    "errCode": 0,
                                    "data": {
                                        "contact": {"username": "v2_author@finder", "nickname": "航拍作者"},
                                        "object": [
                                            {
                                                "id": "10002",
                                                "objectNonceId": "nonce_2",
                                                "createtime": 1730000100,
                                                "objectDesc": {"description": "作者作品2", "media": []},
                                            }
                                        ],
                                        "lastBuffer": "",
                                        "continueFlag": 0,
                                    },
                                }
                            elif method == "fetchFeedDetail":
                                result = {
                                    "errCode": 0,
                                    "data": {
                                        "object": {
                                            "id": "10001",
                                            "objectNonceId": "nonce_1",
                                            "objectDesc": {
                                                "description": "解密测试视频",
                                                "media": [{"url": "https://finder.video.qq.com/test.mp4", "decodeKey": "12345"}],
                                            },
                                        }
                                    },
                                }
                            elif method == "fetchCommentsPage":
                                result = {
                                    "errCode": 0,
                                    "data": {
                                        "BaseResponse": {"Ret": 0},
                                        "commentInfo": [
                                            {
                                                "commentId": "9001",
                                                "content": "画面很稳",
                                                "nickname": "评论观众",
                                                "createtime": 1730000200,
                                                "likeCount": 12,
                                                "levelTwoComment": [],
                                            }
                                        ],
                                        "continueFlag": 0,
                                        "lastBuffer": "",
                                    },
                                }
                            else:
                                result = {"errCode": 0, "data": {}}
                            await ws.send(
                                json.dumps({"type": "result", "id": msg["id"], "ok": True, "result": result})
                            )

            page_task = asyncio.create_task(_simulated_wechat_page())
            try:
                for _ in range(40):
                    if proxy.status()["ready_pages"] == 1:
                        break
                    await asyncio.sleep(0.05)
                assert proxy.status()["ready_pages"] == 1

                client = ChannelsProtocolClient(use_proxy=True, proxy_manager=proxy, timeout=8.0)
                probe_res = await client.probe()
                assert probe_res["ok"] is True
                assert probe_res["ready"] is True
                assert probe_res["transport"] == "channels-local-proxy"

                feeds, search_err = await client.search_feeds("无人机", max_results=5, max_pages=1)
                assert search_err == ""
                assert len(feeds) == 1 and str(feeds[0]["id"]) == "10001"

                user_feeds, contact, user_err = await client.fetch_user_feeds("v2_author@finder", max_results=5, max_pages=1)
                assert user_err == ""
                assert contact["nickname"] == "航拍作者"
                assert len(user_feeds) == 1 and str(user_feeds[0]["id"]) == "10002"

                detail, detail_err = await client.fetch_feed_detail(object_id="10001", object_nonce_id="nonce_1")
                assert detail_err == ""
                assert detail["objectDesc"]["media"][0]["decodeKey"] == "12345"

                comments, audit, c_err = await client.collect_comments(
                    object_id="10001", object_nonce_id="nonce_1", include_replies=False, max_pages=1
                )
                assert c_err == ""
                assert len(comments) == 1 and comments[0]["text"] == "画面很稳"
                main_pages = audit.get("pages") or ((audit.get("main_pagination") or {}).get("pages") or [])
                assert any(p.get("evidence_ref", "").startswith("proxy://exchange/") for p in main_pages)
            finally:
                page_task.cancel()
                await asyncio.gather(page_task, return_exceptions=True)

            await proxy.stop()
            assert fake_reg.proxy_enable == 0
            assert fake_reg.proxy_server == ""
            assert not (tmp_path / "channels_proxy_backup.json").exists()
        finally:
            await proxy.stop()
            proxy.ca.close()
            if echo_server is not None:
                echo_server.close()
                await echo_server.wait_closed()

    asyncio.run(_run())


def test_proxy_api_routes_config_persistence_and_matrix_linkage(local_project, monkeypatch):
    cfg = local_project.cfg
    cfg.db_path = str(local_project.path)
    fake_reg = _FakeRegistryBackend(proxy_enable=0, proxy_server="", proxy_override="")
    proxy_mgr = ChannelsProxyManager(local_project.root, system_proxy_backend=fake_reg)
    monkeypatch.setattr(main, "channels_proxy", proxy_mgr)
    from app.platforms.channels import proxy_bridge

    monkeypatch.setattr(proxy_bridge, "_ACTIVE_PROXY_MANAGER", proxy_mgr)

    # Save proxy mode via API & verify persistence + matrix readiness
    async def _run():
        transport = httpx.ASGITransport(app=main.app, client=("127.0.0.1", 2345), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
            cfg.engine.channels_read_mode = "auto"

            # Before starting proxy, matrix reports not ready.
            save_res = await client.put(
                "/api/channels/protocol/config",
                json={"connection_mode": "proxy", "channels_proxy_port": 2099},
            )
            assert save_res.status_code == 200, save_res.text
            assert save_res.json()["config"]["connection_mode"] == "proxy"
            assert save_res.json()["config"]["proxy_port"] == 2099
            assert resolve_transport(cfg, "shipinhao", "finder_protocol")["effective_mode"] == "browser"

            # Simulate restart config restore
            restarted = Config()
            assert load_persisted_channels_settings(restarted) is True
            assert restarted.engine.channels_proxy_enabled is True
            assert restarted.engine.channels_proxy_port == 2099

            # CA cert download exposes public certificate ONLY (never private key)
            ca_res = await client.get("/api/channels/protocol/proxy/ca.cer")
            assert ca_res.status_code == 200
            assert "BEGIN CERTIFICATE" in ca_res.text
            assert "PRIVATE KEY" not in ca_res.text

            # Start proxy cleanly over ephemeral port (mocked trust_status check inside cert)
            with patch.object(
                proxy_mgr.ca,
                "trust_status",
                return_value={"exists": True, "trusted": True, "thumbprint": "ABCD", "store": "CurrentUser\\Root"},
            ):
                start_res = await client.post(
                    "/api/channels/protocol/proxy/start",
                    json={"proxy_port": 0, "enable_system_proxy": True, "apply_to_config": True},
                )
                assert start_res.status_code == 200, start_res.text
                started = start_res.json()
                assert started["proxy"]["running"] is True
                assert started["capabilities"]["keyword_search"] is True
                assert fake_reg.proxy_enable == 1

                # Simulate a ready Finder page registered on proxy_mgr
                proxy_mgr._pages["p1"] = SimpleNamespace(
                    page_id="p1",
                    websocket=object(),
                    url="https://channels.weixin.qq.com/web/pages/feed",
                    title="微信视频号",
                    ready=True,
                    origin="existing WXU.API",
                    reason="",
                    capabilities={"search": True},
                    has_weixin_js_bridge=True,
                    connected_at=1.0,
                    last_seen=2.0,
                    to_public_dict=lambda: {
                        "page_id": "p1",
                        "url": "https://channels.weixin.qq.com/web/pages/feed",
                        "title": "微信视频号",
                        "ready": True,
                    },
                )
                matrix = build_transport_matrix(cfg, [])
                assert matrix["settings"]["channels_cdp_configured"] is True
                assert matrix["settings"]["channels_cdp_ready"] is True
                finder_row = next(
                    r for r in matrix["rows"] if r["platform"] == "shipinhao" and r["operation"] == "finder_protocol"
                )
                assert finder_row["effective_mode"] == "hybrid"

                stop_res = await client.post("/api/channels/protocol/proxy/stop")
                assert stop_res.status_code == 200
                assert stop_res.json()["proxy"]["running"] is False
                assert fake_reg.proxy_enable == 0

            # Switching mode to none clears channels_proxy_enabled
            del_res = await client.delete("/api/channels/protocol/config")
            assert del_res.status_code == 200
            assert cfg.engine.channels_proxy_enabled is False

    asyncio.run(_run())
    proxy_mgr.ca.close()


def test_local_ca_decodes_cp936_and_falls_back_to_uac_on_impersonation_error(tmp_path, monkeypatch):
    from app.platforms.channels import local_ca as lca

    # 解码按系统 OEM 代码页走;CI runner 是英文 Windows(cp437),这里固定模拟中文系统(cp936)。
    monkeypatch.setattr(lca, "_windows_code_pages", lambda: ["cp936"])
    sample_cp936 = "CertUtil: -addstore 失败: 0x80070542 (WIN32: 1346 ERROR_BAD_IMPERSONATION_LEVEL)".encode("cp936")
    decoded = lca._decode_windows_output(sample_cp936)
    assert "失败" in decoded
    assert "0x80070542" in decoded

    ca = LocalCaManager(tmp_path)
    cert_path, _, thumb = ca.ensure_ca()
    installed_in_machine = False

    def _fake_certutil(args, **_kwargs):
        nonlocal installed_in_machine
        if args == ["-user", "-store", "Root", thumb]:
            return (1, "", "")
        if args == ["-store", "Root", thumb]:
            return (0, f"Cert Hash(sha1): {thumb}", "") if installed_in_machine else (1, "", "")
        if args[:3] == ["-user", "-addstore", "Root"]:
            return (
                1,
                "Root \"受信任的根证书颁发机构\"\r\nCertUtil: -addstore 失败: 0x80070542 (WIN32: 1346 ERROR_BAD_IMPERSONATION_LEVEL)",
                "",
            )
        if args == ["-delstore", "Root", thumb]:
            return (1, "CertUtil: -delstore 失败: 0x80070005 (WIN32: 5 ERROR_ACCESS_DENIED)", "")
        return (1, "", "")

    elevated_calls: list[list[str]] = []

    def _fake_elevated_cancel(args, **_kwargs):
        return (False, 1223, "已取消系统管理员授权（UAC）弹窗，未能完成证书安装")

    def _fake_elevated_ok(args, **_kwargs):
        nonlocal installed_in_machine
        elevated_calls.append(args)
        if "-addstore" in args:
            installed_in_machine = True
        elif "-delstore" in args:
            installed_in_machine = False
        return (True, 0, "")

    monkeypatch.setattr(lca.sys, "platform", "win32")
    monkeypatch.setattr(lca, "_run_certutil", _fake_certutil)

    # First test UAC cancel returns clear ungarbled Chinese message
    monkeypatch.setattr(lca, "_run_elevated_certutil", _fake_elevated_cancel)
    res_cancel = ca.install_trust()
    assert res_cancel["ok"] is False
    assert "UAC" in res_cancel["error"]
    assert "下载证书" in res_cancel["error"]

    # Next test UAC approval installs cleanly into LocalMachine\Root and uninstall cleans it up
    monkeypatch.setattr(lca, "_run_elevated_certutil", _fake_elevated_ok)
    res_ok = ca.install_trust()
    assert res_ok["ok"] is True
    assert res_ok["trusted"] is True
    assert res_ok["store"] == "LocalMachine\\Root"
    assert elevated_calls[0] == ["-f", "-addstore", "Root", str(cert_path)]

    res_un = ca.uninstall_trust()
    assert res_un["ok"] is True
    assert res_un["trusted"] is False
    assert elevated_calls[1] == ["-delstore", "Root", thumb]
    ca.close()


def test_system_proxy_backup_handles_replace_permission_error_and_fallback_dir(tmp_path, monkeypatch):
    from app.platforms.channels import system_proxy as sp

    fake = _FakeRegistryBackend(proxy_enable=1, proxy_server="127.0.0.1:7897", proxy_override="localhost;<local>")
    data_dir = tmp_path / "restricted_data"
    fallback_dir = tmp_path / "localappdata_fallback"
    monkeypatch.setenv("LOCALAPPDATA", str(fallback_dir))

    # 1. If Path.replace raises PermissionError (e.g., restricted folder DELETE_CHILD), direct overwrite succeeds.
    orig_replace = Path.replace

    def _deny_replace(self, target):
        if self.name.endswith(".json.tmp"):
            raise PermissionError(5, "拒绝访问。")
        return orig_replace(self, target)

    monkeypatch.setattr(Path, "replace", _deny_replace)
    res1 = enable_system_proxy(port=2023, data_dir=data_dir, backend=fake)
    assert res1["ok"] is True
    assert (data_dir / "channels_proxy_backup.json").is_file()
    assert not (data_dir / "channels_proxy_backup.json.tmp").exists()
    res_restore1 = restore_system_proxy(data_dir=data_dir, backend=fake)
    assert res_restore1["restored"] is True
    assert fake.proxy_server == "127.0.0.1:7897"

    # 2. If writing to data_dir itself raises PermissionError, backup falls back cleanly to LOCALAPPDATA.
    def _deny_primary(target_path, text):
        if restricted_root in target_path.parents:
            raise PermissionError(5, "拒绝访问。")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(text, encoding="utf-8")

    restricted_root = data_dir
    monkeypatch.setattr(sp, "_write_backup_at", _deny_primary)
    res2 = enable_system_proxy(port=2023, data_dir=data_dir, backend=fake)
    assert res2["ok"] is True
    fallback_file = (fallback_dir / "CreatorHub" / "channels_proxy_backup.json").resolve()
    assert fallback_file.is_file()
    assert res2["backup_path"] == str(fallback_file)

    res_restore2 = restore_system_proxy(data_dir=data_dir, backend=fake)
    assert res_restore2["restored"] is True
    assert fake.proxy_server == "127.0.0.1:7897"
    assert not fallback_file.exists()


def test_low_integrity_detection_explains_access_denied(monkeypatch):
    from app.platforms.channels import system_proxy as sp
    import winreg

    backend = sp.WindowsWinInetBackend()
    monkeypatch.setattr(sp.WindowsWinInetBackend, "_open_settings_key", staticmethod(lambda *_a, **_kw: (_ for _ in ()).throw(PermissionError(5, "拒绝访问。"))))
    monkeypatch.setattr(sp, "current_process_integrity_rid", lambda: 0x1000)

    with pytest.raises(PermissionError) as exc_info:
        backend.apply_values(proxy_enable=1, proxy_server="127.0.0.1:2023", proxy_override="localhost")
    msg = str(exc_info.value)
    assert "Low IL" in msg or "低完整性" in msg
    assert "127.0.0.1:2023" in msg
    assert hasattr(winreg, "HKEY_CURRENT_USER")


def test_ws_bridge_handles_bootstrap_list_capabilities(tmp_path):
    """Ensure list-shaped capabilities emitted by bootstrap JS register and probe cleanly."""
    from app.platforms.channels.client import normalize_capabilities

    assert normalize_capabilities(["finderGetCommentList", "finderSearch", ""]) == {
        "finderGetCommentList": True,
        "finderSearch": True,
    }
    assert normalize_capabilities({"search": True, "user": 0}) == {"search": True, "user": False}

    async def _run():
        fake_reg = _FakeRegistryBackend(proxy_enable=0, proxy_server="", proxy_override="")
        proxy = ChannelsProxyManager(tmp_path, system_proxy_backend=fake_reg)
        started = await proxy.start(proxy_port=0, enable_system_proxy_flag=False)
        proxy_port = started["proxy_port"]
        ca_ctx = ssl.create_default_context(cafile=str(proxy.ca.ca_cert_path))

        # Also verify the generated JS bundle includes normalizeCapabilities for arrays.
        bundle = proxy._build_bridge_bundle().decode("utf-8")
        assert "normalizeCapabilities" in bundle

        sock = await asyncio.to_thread(_open_proxy_connect_socket, proxy_port, "channels.weixin.qq.com:443")

        async def _wechat_list_cap_page():
            async with ws_connect(
                f"wss://channels.weixin.qq.com/__creatorhub_ws?token={proxy._ws_token}",
                sock=sock,
                ssl=ca_ctx,
                server_hostname="channels.weixin.qq.com",
            ) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "type": "hello",
                            "pageId": "wechat-list-cap",
                            "url": "https://channels.weixin.qq.com/web/pages/home",
                            "title": "视频号",
                            "ready": True,
                            "origin": "module export a4",
                            "capabilities": [
                                "finderGetCommentList",
                                "finderGetCommentDetail",
                                "finderUserPage",
                                "finderSearch",
                            ],
                            "hasWeixinJSBridge": True,
                        }
                    )
                )
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") == "probe":
                        await ws.send(
                            json.dumps(
                                {
                                    "type": "result",
                                    "id": msg["id"],
                                    "ok": True,
                                    "result": {
                                        "ready": True,
                                        "url": "https://channels.weixin.qq.com/web/pages/home",
                                        "title": "视频号",
                                        "origin": "module export a4",
                                        "capabilities": [
                                            "finderGetCommentList",
                                            "finderSearch",
                                        ],
                                    },
                                }
                            )
                        )

        task = asyncio.create_task(_wechat_list_cap_page())
        try:
            for _ in range(40):
                if proxy.status()["ready_pages"] == 1:
                    break
                await asyncio.sleep(0.05)
            st = proxy.status()
            assert st["ready_pages"] == 1
            assert st["pages"][0]["capabilities"] == {
                "finderGetCommentList": True,
                "finderGetCommentDetail": True,
                "finderUserPage": True,
                "finderSearch": True,
            }

            client = ChannelsProtocolClient(use_proxy=True, proxy_manager=proxy, timeout=6.0)
            info = await client.connect()
            assert info["ready"] is True
            assert info["capabilities"] == {
                "finderGetCommentList": True,
                "finderSearch": True,
            }
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await proxy.stop()
            proxy.ca.close()

    asyncio.run(_run())
