import asyncio
import base64
import os
import tempfile
import unittest
from pathlib import Path

from playwright.async_api import async_playwright

from app.browser.cdp import (
    CdpProxyAuthController,
    ChromeLocator,
    XhsCdpBackend,
)
from app.browser.identity import Identity
from app.browser.proxy import ProxyPlan
from tests.fixtures.cdp_site import running_fixture_site


RUN_LOCAL = os.environ.get("CREATORHUB_RUN_LOCAL_CDP") == "1"


class _AuthenticatedHttpProxy:
    def __init__(self):
        self.server = None
        self.requests = []

    async def start(self):
        self.server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0)
        return int(self.server.sockets[0].getsockname()[1])

    async def close(self):
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer):
        try:
            try:
                raw = await asyncio.wait_for(
                    reader.readuntil(b"\r\n\r\n"), 2)
            except Exception:
                return
            request = raw.decode("latin1")
            self.requests.append(request)
            token = base64.b64encode(b"alice:secret").decode("ascii")
            expected = f"proxy-authorization: basic {token}".lower()
            if expected not in request.lower():
                writer.write(
                    b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                    b'Proxy-Authenticate: Basic realm="fixture"\r\n'
                    b"Content-Length: 0\r\nConnection: close\r\n\r\n")
            else:
                body = b'<html><title>proxy-ok</title><body id="ok">ok</body></html>'
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                    + f"Content-Length: {len(body)}\r\n".encode("ascii")
                    + b"Connection: close\r\n\r\n" + body)
            await writer.drain()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


class _AuthenticatedSocksHttpProxy:
    def __init__(self):
        self.server = None
        self.targets = []

    async def start(self):
        self.server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0)
        return int(self.server.sockets[0].getsockname()[1])

    async def close(self):
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader, writer):
        try:
            version, count = await reader.readexactly(2)
            methods = await reader.readexactly(count)
            if version != 5 or 2 not in methods:
                return
            writer.write(b"\x05\x02")
            await writer.drain()
            if await reader.readexactly(1) != b"\x01":
                return
            user_size = (await reader.readexactly(1))[0]
            username = await reader.readexactly(user_size)
            password_size = (await reader.readexactly(1))[0]
            password = await reader.readexactly(password_size)
            if (username, password) != (b"alice", b"secret"):
                writer.write(b"\x01\x01")
                await writer.drain()
                return
            writer.write(b"\x01\x00")
            await writer.drain()
            header = await reader.readexactly(4)
            if header[:3] != b"\x05\x01\x00":
                return
            if header[3] == 3:
                size = (await reader.readexactly(1))[0]
                host = (await reader.readexactly(size)).decode("ascii")
            elif header[3] == 1:
                host = ".".join(str(part) for part in await reader.readexactly(4))
            else:
                return
            port = int.from_bytes(await reader.readexactly(2), "big")
            self.targets.append((host, port))
            writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
            await writer.drain()
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            body = b"<html><title>socks-ok</title><body>ok</body></html>"
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n"
                + f"Content-Length: {len(body)}\r\n".encode("ascii")
                + b"Connection: close\r\n\r\n" + body)
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


@unittest.skipUnless(RUN_LOCAL, "set CREATORHUB_RUN_LOCAL_CDP=1 for visible Chrome integration")
class XhsCdpLocalIntegrationTests(unittest.TestCase):
    def test_authenticated_http_proxy_handles_real_chrome_407(self):
        if ChromeLocator().find() is None:
            self.skipTest("stable Google Chrome is not installed")

        async def scenario(root):
            proxy = _AuthenticatedHttpProxy()
            port = await proxy.start()
            playwright = await async_playwright().start()
            backend = XhsCdpBackend(playwright, root, startup_timeout=15)
            identity = Identity(
                account_id=7301,
                profile_dir=str(Path(root) / "account-7301"),
                platform="xhs", identity_mode="native")
            plan = ProxyPlan.parse(
                f"http://alice:secret@127.0.0.1:{port}")
            session = None
            try:
                session = await backend.open(identity, plan)
                controller = CdpProxyAuthController(session.context, plan)
                session.auth_controller = controller
                page = await session.context.new_page()
                await controller.install(page)

                await page.goto(
                    "http://fixture.invalid/",
                    wait_until="domcontentloaded", timeout=15_000)

                self.assertEqual(await page.title(), "proxy-ok")
                self.assertGreaterEqual(len(proxy.requests), 2)
                self.assertTrue(any(
                    "proxy-authorization: basic" in request.lower()
                    for request in proxy.requests))
                self.assertNotIn("secret", repr(controller.last_error))
                await page.close()
            finally:
                if session is not None:
                    await backend.close(session)
                await playwright.stop()
                await proxy.close()

        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(scenario(tmp))

    def test_authenticated_socks_proxy_uses_real_chrome_relay(self):
        if ChromeLocator().find() is None:
            self.skipTest("stable Google Chrome is not installed")

        async def scenario(root):
            upstream = _AuthenticatedSocksHttpProxy()
            port = await upstream.start()
            playwright = await async_playwright().start()
            backend = XhsCdpBackend(playwright, root, startup_timeout=15)
            identity = Identity(
                account_id=7401,
                profile_dir=str(Path(root) / "account-7401"),
                platform="xhs", identity_mode="native")
            plan = ProxyPlan.parse(
                f"socks5://alice:secret@127.0.0.1:{port}")
            session = None
            try:
                session = await backend.open(identity, plan)
                self.assertIsNotNone(session.relay)
                page = await session.context.new_page()

                await page.goto(
                    "http://fixture.invalid/",
                    wait_until="domcontentloaded", timeout=15_000)

                self.assertEqual(await page.title(), "socks-ok")
                self.assertIn(("fixture.invalid", 80), upstream.targets)
                await page.close()
            finally:
                if session is not None:
                    await backend.close(session)
                await playwright.stop()
                await upstream.close()

        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(scenario(tmp))

    def test_native_cdp_persists_state_loads_resources_and_cleans_up(self):
        if ChromeLocator().find() is None:
            self.skipTest("stable Google Chrome is not installed")

        async def open_once(root, url, *, write_value=None):
            playwright = await async_playwright().start()
            backend = XhsCdpBackend(playwright, root, startup_timeout=15)
            identity = Identity(
                account_id=7001,
                profile_dir=str(Path(root) / "account-7001"),
                platform="xhs",
                identity_mode="native",
            )
            session = None
            try:
                session = await backend.open(identity, None)
                active_file = Path(identity.profile_dir) / "DevToolsActivePort"
                if active_file.exists():
                    active_port = active_file.read_text(
                        encoding="utf-8").splitlines()[0]
                    self.assertEqual(int(active_port), session.owned.port)
                page = await session.context.new_page()
                await page.goto(url, wait_until="load", timeout=15_000)
                await page.locator("#asset").wait_for(state="visible")
                webdriver = await page.evaluate("navigator.webdriver")
                previous = await page.evaluate(
                    "localStorage.getItem('fixture-account')")
                if write_value is not None:
                    await page.evaluate(
                        "value => localStorage.setItem('fixture-account', value)",
                        write_value,
                    )
                await page.close()
                return webdriver, previous
            finally:
                if session is not None:
                    await backend.close(session)
                await playwright.stop()

        with tempfile.TemporaryDirectory() as tmp, running_fixture_site() as site:
            first = asyncio.run(open_once(
                tmp, site.base_url + "/", write_value="account-7001"))
            second = asyncio.run(open_once(tmp, site.base_url + "/"))

            self.assertFalse(first[0])
            self.assertFalse(second[0])
            self.assertIsNone(first[1])
            self.assertEqual(second[1], "account-7001")
            self.assertGreaterEqual(site.counts["/pixel.png"], 2)
            self.assertGreaterEqual(site.counts["/font.woff2"], 2)
            self.assertFalse(
                (Path(tmp) / "account-7001" /
                 ".creatorhub-cdp-owner.json").exists())

    def test_two_accounts_use_distinct_processes_profiles_and_storage(self):
        if ChromeLocator().find() is None:
            self.skipTest("stable Google Chrome is not installed")

        async def scenario(root, url):
            playwright = await async_playwright().start()
            backend = XhsCdpBackend(playwright, root, startup_timeout=15)
            identities = [
                Identity(
                    account_id=account_id,
                    profile_dir=str(Path(root) / f"account-{account_id}"),
                    platform="xhs", identity_mode="native")
                for account_id in (7101, 7102)
            ]
            sessions = []
            try:
                for identity in identities:
                    sessions.append(await backend.open(identity, None))
                pages = [await session.context.new_page() for session in sessions]
                for page in pages:
                    await page.goto(url, wait_until="load", timeout=15_000)
                await pages[0].evaluate(
                    "localStorage.setItem('owner', 'account-7101')")
                await pages[1].evaluate(
                    "localStorage.setItem('owner', 'account-7102')")
                values = [await page.evaluate(
                    "localStorage.getItem('owner')") for page in pages]
                pids = [session.owned.pid for session in sessions]
                ports = [session.owned.port for session in sessions]
                for page in pages:
                    await page.close()
                return values, pids, ports
            finally:
                for session in reversed(sessions):
                    await backend.close(session)
                await playwright.stop()

        with tempfile.TemporaryDirectory() as tmp, running_fixture_site() as site:
            values, pids, ports = asyncio.run(scenario(tmp, site.base_url + "/"))
            self.assertEqual(values, ["account-7101", "account-7102"])
            self.assertEqual(len(set(pids)), 2)
            self.assertEqual(len(set(ports)), 2)
            for account_id in (7101, 7102):
                self.assertFalse(
                    (Path(tmp) / f"account-{account_id}" /
                     ".creatorhub-cdp-owner.json").exists())

    def test_owned_chrome_recovers_after_playwright_driver_restart(self):
        if ChromeLocator().find() is None:
            self.skipTest("stable Google Chrome is not installed")

        async def scenario(root):
            identity = Identity(
                account_id=7201,
                profile_dir=str(Path(root) / "account-7201"),
                platform="xhs", identity_mode="native")
            playwright1 = await async_playwright().start()
            backend1 = XhsCdpBackend(playwright1, root, startup_timeout=15)
            session1 = await backend1.open(identity, None)
            first_pid = session1.owned.pid
            first_port = session1.owned.port
            playwright2 = None
            session2 = None
            try:
                # Stopping the Playwright driver simulates losing the app-side
                # CDP connection while the externally owned Chrome stays alive.
                await playwright1.stop()
                playwright1 = None
                playwright2 = await async_playwright().start()
                backend2 = XhsCdpBackend(
                    playwright2, root, startup_timeout=15)

                session2 = await backend2.open(identity, None)

                self.assertTrue(session2.owned.recovered)
                self.assertEqual(session2.owned.pid, first_pid)
                self.assertEqual(session2.owned.port, first_port)
                await backend2.close(session2)
                session2 = None
            finally:
                if session2 is not None:
                    await backend2.close(session2)
                # Safe even after the recovered backend already stopped it.
                await backend1._stop_process(session1.owned.process)
                backend1._remove_owned_marker(
                    session1.owned.marker_path, session1.owned.pid)
                if playwright2 is not None:
                    await playwright2.stop()
                if playwright1 is not None:
                    await playwright1.stop()

        with tempfile.TemporaryDirectory() as tmp:
            asyncio.run(scenario(tmp))


if __name__ == "__main__":
    unittest.main()
