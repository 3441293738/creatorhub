import asyncio
import unittest
from types import SimpleNamespace

from app.browser.proxy import (HttpProxyAuthRelay, ProxyConfigError, ProxyPlan,
                               Socks5AuthRelay)
from app.engine.monitor import MonitorEngine


class ProxyPlanTests(unittest.TestCase):
    def test_rotating_proxy_exit_is_usable_but_hard_failures_are_blocked(self):
        self.assertFalse(MonitorEngine._proxy_bad(SimpleNamespace(
            proxy="http://proxy.local:8080", proxy_status="rotating")))
        self.assertFalse(MonitorEngine._proxy_bad(SimpleNamespace(
            proxy="http://proxy.local:8080", proxy_status="drifted")))
        for status in ("bad", "auth_error", "blocked"):
            with self.subTest(status=status):
                self.assertTrue(MonitorEngine._proxy_bad(SimpleNamespace(
                    proxy="http://proxy.local:8080", proxy_status=status)))
    def test_supports_http_and_socks_with_or_without_authentication(self):
        cases = (
            ("http://127.0.0.1:8080", "http", False),
            ("http://user:secret@proxy.local:8080", "http", True),
            ("socks5://127.0.0.1:1080", "socks5", False),
            ("socks5://user:secret@proxy.local:1080", "socks5", True),
        )

        for raw, scheme, authenticated in cases:
            with self.subTest(raw=raw):
                plan = ProxyPlan.parse(raw)
                self.assertIsNotNone(plan)
                self.assertEqual(plan.scheme, scheme)
                self.assertEqual(plan.authenticated, authenticated)
                self.assertNotIn("secret", plan.redacted)
                self.assertNotIn("secret", plan.signature)

    def test_empty_proxy_is_the_only_direct_configuration(self):
        self.assertIsNone(ProxyPlan.parse(""))
        self.assertIsNone(ProxyPlan.parse("   "))

    def test_invalid_config_is_not_treated_as_direct(self):
        with self.assertRaisesRegex(ProxyConfigError, "代理配置无效") as caught:
            ProxyPlan.parse("http://user:secret@")

        self.assertNotIn("secret", str(caught.exception))

    def test_playwright_projection_keeps_credentials_out_of_server(self):
        plan = ProxyPlan.parse("http://alice:s%40cret@proxy.local:8080")

        self.assertEqual(plan.playwright(), {
            "server": "http://proxy.local:8080",
            "username": "alice",
            "password": "s@cret",
        })
        self.assertEqual(plan.chrome_server(), "http://proxy.local:8080")

    def test_authenticated_socks_requires_a_loopback_relay_for_chrome(self):
        plan = ProxyPlan.parse("socks5://alice:secret@proxy.local:1080")

        with self.assertRaisesRegex(ProxyConfigError, "本地转发器"):
            plan.chrome_server()
        self.assertEqual(
            plan.chrome_server(relay_port=32109),
            "socks5://127.0.0.1:32109",
        )

    def test_ipv6_hosts_are_bracketed_in_projected_urls(self):
        plan = ProxyPlan.parse("http://[::1]:8080")

        self.assertEqual(plan.chrome_server(), "http://[::1]:8080")
        self.assertEqual(plan.normalized, "http://[::1]:8080")


class _AuthenticatedSocksFixture:
    def __init__(self, username="alice", password="secret"):
        self.username = username.encode()
        self.password = password.encode()
        self.server = None
        self.target = None
        self.request_seen = asyncio.Event()

    @property
    def url(self):
        port = self.server.sockets[0].getsockname()[1]
        return f"socks5://alice:secret@127.0.0.1:{port}"

    async def start(self):
        self.server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0)

    async def close(self):
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer):
        try:
            version, count = await reader.readexactly(2)
            methods = await reader.readexactly(count)
            assert version == 5 and 2 in methods
            writer.write(b"\x05\x02")
            await writer.drain()
            assert await reader.readexactly(1) == b"\x01"
            user_len = (await reader.readexactly(1))[0]
            user = await reader.readexactly(user_len)
            password_len = (await reader.readexactly(1))[0]
            password = await reader.readexactly(password_len)
            if user != self.username or password != self.password:
                writer.write(b"\x01\x01")
                await writer.drain()
                return
            writer.write(b"\x01\x00")
            await writer.drain()

            header = await reader.readexactly(4)
            assert header[:3] == b"\x05\x01\x00"
            atyp = header[3]
            if atyp == 3:
                size = (await reader.readexactly(1))[0]
                host = (await reader.readexactly(size)).decode()
            else:
                raise AssertionError(f"unexpected ATYP {atyp}")
            port = int.from_bytes(await reader.readexactly(2), "big")
            self.target = (host, port)
            self.request_seen.set()
            writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
            await writer.drain()
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


async def _authenticated_socks_relay_forwards_domain_without_local_dns():
    upstream = _AuthenticatedSocksFixture()
    await upstream.start()
    relay = Socks5AuthRelay(ProxyPlan.parse(upstream.url))
    port = await relay.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        assert await reader.readexactly(2) == b"\x05\x00"

        host = b"fixture.local"
        writer.write(
            b"\x05\x01\x00\x03" + bytes([len(host)]) + host
            + (8080).to_bytes(2, "big")
        )
        await writer.drain()
        assert await reader.readexactly(10) == (
            b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
        await asyncio.wait_for(upstream.request_seen.wait(), 1)
        assert upstream.target == ("fixture.local", 8080)

        writer.write(b"echo-through-relay")
        await writer.drain()
        assert await reader.readexactly(18) == b"echo-through-relay"
    finally:
        writer.close()
        await writer.wait_closed()
        await relay.close()
        await upstream.close()


def test_authenticated_socks_relay_forwards_domain_without_local_dns():
    asyncio.run(_authenticated_socks_relay_forwards_domain_without_local_dns())


async def _authenticated_socks_relay_rejects_udp_and_closes_cleanly():
    upstream = _AuthenticatedSocksFixture()
    await upstream.start()
    relay = Socks5AuthRelay(ProxyPlan.parse(upstream.url))
    port = await relay.start()
    assert relay.chrome_server == f"socks5://127.0.0.1:{port}"
    assert relay.listen_host == "127.0.0.1"
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        assert await reader.readexactly(2) == b"\x05\x00"
        writer.write(b"\x05\x03\x00\x01\x7f\x00\x00\x01\x00\x35")
        await writer.drain()
        assert (await reader.readexactly(2)) == b"\x05\x07"
    finally:
        writer.close()
        await writer.wait_closed()
        await relay.close()
        await upstream.close()
    assert relay.active_connections == 0


def test_authenticated_socks_relay_rejects_udp_and_closes_cleanly():
    asyncio.run(_authenticated_socks_relay_rejects_udp_and_closes_cleanly())


class _AuthenticatedHttpProxyFixture:
    def __init__(self, username="alice", password="secret"):
        self.expected = "Basic " + __import__("base64").b64encode(
            f"{username}:{password}".encode()).decode()
        self.server = None
        self.authorization = ""
        self.target = ""
        self.request_seen = asyncio.Event()

    @property
    def url(self):
        port = self.server.sockets[0].getsockname()[1]
        return f"http://alice:secret@127.0.0.1:{port}"

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)

    async def close(self):
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer):
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            lines = header.decode("latin-1").split("\r\n")
            self.target = lines[0]
            for line in lines[1:]:
                if line.lower().startswith("proxy-authorization:"):
                    self.authorization = line.split(":", 1)[1].strip()
            self.request_seen.set()
            if self.authorization != self.expected:
                writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\n\r\n")
                await writer.drain()
                return
            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


async def _authenticated_http_relay_injects_proxy_authorization():
    upstream = _AuthenticatedHttpProxyFixture()
    await upstream.start()
    relay = HttpProxyAuthRelay(ProxyPlan.parse(upstream.url))
    port = await relay.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(
            b"CONNECT fixture.local:443 HTTP/1.1\r\n"
            b"Host: fixture.local:443\r\n\r\n")
        await writer.drain()
        response = await reader.readuntil(b"\r\n\r\n")
        assert response.startswith(b"HTTP/1.1 200")
        await asyncio.wait_for(upstream.request_seen.wait(), 1)
        assert upstream.authorization == upstream.expected
        assert upstream.target == "CONNECT fixture.local:443 HTTP/1.1"
        writer.write(b"echo-through-http-relay")
        await writer.drain()
        assert await reader.readexactly(23) == b"echo-through-http-relay"
    finally:
        writer.close()
        await writer.wait_closed()
        await relay.close()
        await upstream.close()
    assert relay.active_connections == 0


def test_authenticated_http_relay_injects_proxy_authorization():
    asyncio.run(_authenticated_http_relay_injects_proxy_authorization())


async def _browser_manager_uses_socks5_relay_for_persistent_context(tmp_path):
    from types import SimpleNamespace
    from app.browser.identity import Identity
    from app.browser.manager import BrowserManager

    captured = {}

    class FakeContext:
        pages = []

        def on(self, *_args, **_kw):
            return None

        async def new_page(self):
            return SimpleNamespace(
                evaluate=lambda _script: asyncio.sleep(0, result="Mozilla/5.0 Chrome/138.0.0.0"),
                close=lambda: asyncio.sleep(0),
            )

        async def cookies(self):
            return []

        async def close(self):
            return None

    async def fake_launch_persistent_context(**kwargs):
        captured.update(kwargs)
        return FakeContext()

    mgr = BrowserManager("Mozilla/5.0 Chrome/138.0.0.0", profiles_root=tmp_path / "profiles")
    mgr._pw = SimpleNamespace(
        chromium=SimpleNamespace(launch_persistent_context=fake_launch_persistent_context)
    )
    ident = Identity(
        account_id=42,
        platform="rednote",
        profile_dir=tmp_path / "profiles" / "rn_42",
        proxy="socks5://user1:pass1@203.0.113.9:1080",
    )
    ctx = await mgr.context_for(ident)
    assert ctx is not None
    assert "proxy" in captured
    assert captured["proxy"]["server"].startswith("socks5://127.0.0.1:")
    assert "username" not in captured["proxy"]
    assert "password" not in captured["proxy"]
    assert ident.key in mgr._proxy_relays
    assert isinstance(mgr._proxy_relays[ident.key], Socks5AuthRelay)
    await mgr._close_key_unlocked(ident.key)
    assert ident.key not in mgr._proxy_relays


def test_browser_manager_uses_socks5_relay_for_persistent_context(tmp_path):
    asyncio.run(_browser_manager_uses_socks5_relay_for_persistent_context(tmp_path))


if __name__ == "__main__":
    unittest.main()
