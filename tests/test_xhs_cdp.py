import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.browser.cdp import (
    CdpProxyAuthController,
    CdpLaunchError,
    CdpProfileConflictError,
    CdpProxyError,
    ChromeLocator,
    ProcessInfo,
    ProcessInspector,
    XhsCdpBackend,
    chrome_launch_args,
)
from app.browser.identity import Identity
from app.browser.manager import BrowserManager
from app.browser.proxy import ProxyPlan


class ChromeLocatorTests(unittest.TestCase):
    def test_macos_process_command_preserves_executable_spaces(self):
        command = (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
            "--user-data-dir=/tmp/creatorhub/profile --remote-debugging-port=43111"
        )

        executable = ProcessInspector._executable_from_command(command)

        self.assertEqual(
            executable,
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        )

    def test_chrome_arguments_use_explicit_nonzero_loopback_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = chrome_launch_args(
                profile_dir=Path(tmp) / "acc_1",
                port=42137,
                proxy_server="http://127.0.0.1:8080",
            )

        self.assertIn("--remote-debugging-address=127.0.0.1", args)
        self.assertIn("--remote-debugging-port=42137", args)
        self.assertFalse(any(arg == "--remote-debugging-port=0" for arg in args))
        self.assertFalse(any("secret" in arg for arg in args))
        self.assertNotIn("--no-sandbox", args)
        self.assertFalse(any("AutomationControlled" in arg for arg in args))
        self.assertIn("--proxy-server=http://127.0.0.1:8080", args)
        self.assertIn("--start-minimized", args)
        self.assertIn("--disable-session-crashed-bubble", args)

    def test_zero_debugging_port_is_rejected_before_launch(self):
        with self.assertRaisesRegex(ValueError, "非零"):
            chrome_launch_args(Path("profile"), 0)

    def test_locator_checks_windows_stable_chrome_locations(self):
        expected = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
        locator = ChromeLocator(
            system="Windows",
            environ={"ProgramFiles": "C:/Program Files"},
            exists=lambda path: Path(path) == expected,
        )

        self.assertEqual(locator.find(), expected)

    def test_locator_accepts_real_windows_uppercase_environment_keys(self):
        expected = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
        locator = ChromeLocator(
            system="Windows",
            environ={"PROGRAMFILES": "C:/Program Files"},
            exists=lambda path: Path(path) == expected,
        )

        self.assertEqual(locator.find(), expected)

    def test_locator_checks_macos_stable_chrome_location(self):
        expected = Path(
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        locator = ChromeLocator(
            system="Darwin", environ={}, exists=lambda path: Path(path) == expected)

        self.assertEqual(locator.find(), expected)

    def test_locator_uses_linux_google_chrome_binary(self):
        expected = Path("/usr/bin/google-chrome-stable")
        locator = ChromeLocator(
            system="Linux", environ={}, exists=lambda _path: False,
            which=lambda name: str(expected) if name == "google-chrome-stable" else None,
        )

        self.assertEqual(locator.find(), expected)


class _FakeProcess:
    def __init__(self, pid=1234):
        self.pid = pid
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            raise TimeoutError()
        return self.returncode


class _FakeBrowser:
    def __init__(self, contexts=None):
        self.contexts = [object()] if contexts is None else contexts
        self.closed = False

    async def close(self):
        self.closed = True


class XhsCdpBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.profile = Path(self.tmp.name) / "acc_7"
        self.identity = Identity(
            account_id=7,
            profile_dir=str(self.profile),
            identity_mode="native",
            platform="xhs",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _backend(self, browser=None, **overrides):
        browser = browser or _FakeBrowser()
        calls = {"process_args": None, "connect": None}
        process = _FakeProcess()

        def process_factory(args):
            calls["process_args"] = list(args)
            return process

        async def connector(endpoint, **kwargs):
            calls["connect"] = (endpoint, kwargs)
            return browser

        async def endpoint_probe(_port):
            return True

        options = dict(
            playwright=None,
            profiles_root=self.tmp.name,
            locator=SimpleNamespace(find=lambda: Path("C:/Chrome/chrome.exe")),
            process_factory=process_factory,
            connector=connector,
            endpoint_probe=endpoint_probe,
            port_selector=lambda: 42137,
        )
        options.update(overrides)
        backend = XhsCdpBackend(**options)
        return backend, browser, process, calls

    def test_open_connects_with_local_no_defaults_and_default_context(self):
        async def scenario():
            backend, browser, process, calls = self._backend()
            session = await backend.open(self.identity, None)
            self.assertIs(session.context, browser.contexts[0])
            self.assertEqual(calls["connect"], (
                "http://127.0.0.1:42137",
                {"is_local": True, "no_defaults": True},
            ))
            self.assertIn(
                f"--user-data-dir={self.profile.resolve()}",
                calls["process_args"],
            )
            marker = json.loads(
                (self.profile / ".creatorhub-cdp-owner.json").read_text("utf-8"))
            self.assertEqual(marker["pid"], process.pid)
            self.assertEqual(set(marker), {
                "pid", "executable", "profile_dir", "started_at"})
            self.assertNotIn("42137", repr(marker))
            await backend.close(session)
            self.assertTrue(browser.closed)
            self.assertTrue(process.terminated)
            self.assertFalse((self.profile / ".creatorhub-cdp-owner.json").exists())

        asyncio.run(scenario())


    def test_missing_default_context_fails_and_cleans_owned_process(self):
        async def scenario():
            backend, browser, process, _calls = self._backend(
                browser=_FakeBrowser(contexts=[]))
            with self.assertRaisesRegex(CdpLaunchError, "默认 Context"):
                await backend.open(self.identity, None)
            self.assertTrue(browser.closed)
            self.assertTrue(process.terminated)
            self.assertFalse((self.profile / ".creatorhub-cdp-owner.json").exists())

        asyncio.run(scenario())

    def test_missing_system_chrome_is_an_environment_error(self):
        async def scenario():
            backend, _browser, _process, _calls = self._backend()
            backend.locator = SimpleNamespace(find=lambda: None)
            with self.assertRaisesRegex(CdpLaunchError, "系统 Chrome"):
                await backend.open(self.identity, None)

        asyncio.run(scenario())

    def test_valid_owned_endpoint_is_recovered_without_starting_second_chrome(self):
        async def scenario():
            backend, browser, _process, calls = self._backend(
                process_inspector=SimpleNamespace(
                    inspect=lambda _pid: ProcessInfo(
                        pid=4567,
                        executable=Path("C:/Chrome/chrome.exe").resolve(),
                        command_line=(
                            f"--user-data-dir={self.profile.resolve()}",
                            "--remote-debugging-address=127.0.0.1",
                            "--remote-debugging-port=43111",
                            "--no-first-run",
                            "--no-default-browser-check",
                        ),
                    ),
                    terminate=lambda _pid: None,
                ),
            )
            self.profile.mkdir(parents=True)
            (self.profile / ".creatorhub-cdp-owner.json").write_text(
                json.dumps({
                    "pid": 4567,
                    "executable": str(Path("C:/Chrome/chrome.exe").resolve()),
                    "profile_dir": str(self.profile.resolve()),
                    "started_at": "2026-08-08T00:00:00+00:00",
                }),
                encoding="utf-8",
            )
            (self.profile / "DevToolsActivePort").write_text(
                "43111\n/devtools/browser/fixture\n", encoding="utf-8")

            session = await backend.open(self.identity, None)

            self.assertTrue(session.owned.recovered)
            self.assertEqual(session.owned.pid, 4567)
            self.assertIsNone(calls["process_args"])
            self.assertEqual(calls["connect"][0], "http://127.0.0.1:43111")
            await backend.close(session)
            self.assertTrue(browser.closed)
            self.assertFalse((self.profile / ".creatorhub-cdp-owner.json").exists())

        asyncio.run(scenario())

    def test_live_endpoint_with_invalid_owner_is_never_connected_or_terminated(self):
        async def scenario():
            terminated = []
            backend, _browser, _process, calls = self._backend(
                process_inspector=SimpleNamespace(
                    inspect=lambda _pid: ProcessInfo(
                        pid=4567,
                        executable=Path("C:/Other/chrome.exe").resolve(),
                        command_line=(f"--user-data-dir={self.profile.resolve()}",),
                    ),
                    terminate=lambda pid: terminated.append(pid),
                ),
            )
            self.profile.mkdir(parents=True)
            (self.profile / ".creatorhub-cdp-owner.json").write_text(
                json.dumps({
                    "pid": 4567,
                    "executable": str(Path("C:/Chrome/chrome.exe").resolve()),
                    "profile_dir": str(self.profile.resolve()),
                    "started_at": "2026-08-08T00:00:00+00:00",
                }), encoding="utf-8")
            (self.profile / "DevToolsActivePort").write_text(
                "43111\n/devtools/browser/fixture\n", encoding="utf-8")

            with self.assertRaisesRegex(CdpProfileConflictError, "Profile"):
                await backend.open(self.identity, None)

            self.assertIsNone(calls["connect"])
            self.assertIsNone(calls["process_args"])
            self.assertEqual(terminated, [])

        asyncio.run(scenario())

    def test_cancellation_while_waiting_for_endpoint_cleans_process_and_marker(self):
        async def scenario():
            entered = asyncio.Event()

            async def never_ready(_port):
                entered.set()
                await asyncio.Event().wait()

            backend, _browser, process, _calls = self._backend(
                endpoint_probe=never_ready)
            task = asyncio.create_task(backend.open(self.identity, None))
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(process.terminated)
            self.assertFalse((self.profile / ".creatorhub-cdp-owner.json").exists())

        asyncio.run(scenario())

    def test_startup_timeout_cleans_process_and_reports_redacted_error(self):
        async def scenario():
            now = [0.0]

            async def not_ready(_port):
                return False

            async def advance(delay):
                now[0] += delay

            backend, _browser, process, _calls = self._backend(
                endpoint_probe=not_ready,
                sleep=advance,
                monotonic=lambda: now[0],
                startup_timeout=0.2,
            )
            with self.assertRaisesRegex(CdpLaunchError, "启动超时"):
                await backend.open(self.identity, None)
            self.assertTrue(process.terminated)
            self.assertFalse((self.profile / ".creatorhub-cdp-owner.json").exists())

        asyncio.run(scenario())

    def test_port_competition_reselects_a_nonzero_port_once(self):
        async def scenario():
            first = _FakeProcess(pid=1001)
            first.returncode = 1
            second = _FakeProcess(pid=1002)
            processes = iter((first, second))
            ports = iter((41001, 41002))
            launched = []
            browser = _FakeBrowser()

            def process_factory(args):
                launched.append(list(args))
                return next(processes)

            async def connector(_endpoint, **_kwargs):
                return browser

            async def endpoint_probe(port):
                return port == 41002

            backend = XhsCdpBackend(
                playwright=None,
                profiles_root=self.tmp.name,
                locator=SimpleNamespace(
                    find=lambda: Path("C:/Chrome/chrome.exe")),
                process_factory=process_factory,
                connector=connector,
                endpoint_probe=endpoint_probe,
                port_selector=lambda: next(ports),
            )

            session = await backend.open(self.identity, None)

            self.assertEqual(session.owned.port, 41002)
            self.assertEqual(len(launched), 2)
            self.assertTrue(any(
                arg == "--remote-debugging-port=41001" for arg in launched[0]))
            self.assertTrue(any(
                arg == "--remote-debugging-port=41002" for arg in launched[1]))
            await backend.close(session)

        asyncio.run(scenario())

    def test_owned_authenticated_socks_session_is_restarted_with_fresh_relay(self):
        async def scenario():
            alive = {4567: True}
            terminated = []

            class Inspector:
                def inspect(self, pid):
                    if not alive.get(pid):
                        return None
                    return ProcessInfo(
                        pid=pid,
                        executable=Path("C:/Chrome/chrome.exe").resolve(),
                        command_line=(
                            f"--user-data-dir={self_profile}",
                            "--remote-debugging-address=127.0.0.1",
                            "--remote-debugging-port=43111",
                            "--no-first-run",
                            "--no-default-browser-check",
                            "--proxy-server=socks5://127.0.0.1:39999",
                        ),
                    )

                def terminate(self, pid):
                    terminated.append(pid)
                    alive[pid] = False

            self_profile = self.profile.resolve()

            async def endpoint_probe(port):
                return alive.get(4567, False) if port == 43111 else True

            async def proxy_probe(_plan):
                return True

            backend, _browser, _process, calls = self._backend(
                process_inspector=Inspector(),
                endpoint_probe=endpoint_probe,
                proxy_probe=proxy_probe,
            )
            self.profile.mkdir(parents=True)
            (self.profile / ".creatorhub-cdp-owner.json").write_text(
                json.dumps({
                    "pid": 4567,
                    "executable": str(Path("C:/Chrome/chrome.exe").resolve()),
                    "profile_dir": str(self.profile.resolve()),
                    "started_at": "2026-08-08T00:00:00+00:00",
                }), encoding="utf-8")
            (self.profile / "DevToolsActivePort").write_text(
                "43111\n/devtools/browser/fixture\n", encoding="utf-8")
            plan = ProxyPlan.parse(
                "socks5://alice:secret@proxy.local:1080")

            session = await backend.open(self.identity, plan)

            self.assertEqual(terminated, [4567])
            self.assertIsNotNone(calls["process_args"])
            self.assertTrue(any(
                arg.startswith("--proxy-server=socks5://127.0.0.1:")
                for arg in calls["process_args"]))
            self.assertIsNotNone(session.relay)
            await backend.close(session)

        asyncio.run(scenario())

    def test_same_profile_process_without_managed_flags_is_not_owned(self):
        async def scenario():
            terminated = []
            backend, _browser, _process, calls = self._backend(
                process_inspector=SimpleNamespace(
                    inspect=lambda _pid: ProcessInfo(
                        pid=4567,
                        executable=Path("C:/Chrome/chrome.exe").resolve(),
                        command_line=(
                            f"--user-data-dir={self.profile.resolve()}",
                            "https://www.xiaohongshu.com/",
                        ),
                    ),
                    terminate=lambda pid: terminated.append(pid),
                ),
            )
            self.profile.mkdir(parents=True)
            (self.profile / ".creatorhub-cdp-owner.json").write_text(
                json.dumps({
                    "pid": 4567,
                    "executable": str(Path("C:/Chrome/chrome.exe").resolve()),
                    "profile_dir": str(self.profile.resolve()),
                    "started_at": "2026-08-08T00:00:00+00:00",
                }), encoding="utf-8")
            (self.profile / "DevToolsActivePort").write_text(
                "43111\n/devtools/browser/fixture\n", encoding="utf-8")

            with self.assertRaisesRegex(CdpProfileConflictError, "Profile"):
                await backend.open(self.identity, None)

            self.assertIsNone(calls["connect"])
            self.assertIsNone(calls["process_args"])
            self.assertEqual(terminated, [])

        asyncio.run(scenario())

    def test_owned_endpoint_recovers_port_from_validated_process_command(self):
        async def scenario():
            backend, browser, _process, calls = self._backend(
                process_inspector=SimpleNamespace(
                    inspect=lambda _pid: ProcessInfo(
                        pid=4567,
                        executable=Path("C:/Chrome/chrome.exe").resolve(),
                        command_line=(
                            f"--user-data-dir={self.profile.resolve()}",
                            "--remote-debugging-address=127.0.0.1",
                            "--remote-debugging-port=43111",
                            "--no-first-run",
                            "--no-default-browser-check",
                        ),
                    ),
                    terminate=lambda _pid: None,
                ),
            )
            self.profile.mkdir(parents=True)
            (self.profile / ".creatorhub-cdp-owner.json").write_text(
                json.dumps({
                    "pid": 4567,
                    "executable": str(Path("C:/Chrome/chrome.exe").resolve()),
                    "profile_dir": str(self.profile.resolve()),
                    "started_at": "2026-08-08T00:00:00+00:00",
                }),
                encoding="utf-8",
            )

            session = await backend.open(self.identity, None)

            self.assertTrue(session.owned.recovered)
            self.assertEqual(session.owned.port, 43111)
            self.assertIsNone(calls["process_args"])
            self.assertEqual(calls["connect"][0], "http://127.0.0.1:43111")
            await backend.close(session)
            self.assertTrue(browser.closed)

        asyncio.run(scenario())

    def test_owned_session_with_changed_proxy_is_restarted(self):
        async def scenario():
            alive = {4567: True}
            terminated = []
            self_profile = self.profile.resolve()

            class Inspector:
                def inspect(self, pid):
                    if not alive.get(pid):
                        return None
                    return ProcessInfo(
                        pid=pid,
                        executable=Path("C:/Chrome/chrome.exe").resolve(),
                        command_line=(
                            f"--user-data-dir={self_profile}",
                            "--remote-debugging-address=127.0.0.1",
                            "--remote-debugging-port=43111",
                            "--no-first-run",
                            "--no-default-browser-check",
                            "--proxy-server=http://old-proxy.local:8080",
                        ),
                    )

                def terminate(self, pid):
                    terminated.append(pid)
                    alive[pid] = False

            async def endpoint_probe(port):
                return alive.get(4567, False) if port == 43111 else True

            backend, _browser, _process, calls = self._backend(
                process_inspector=Inspector(),
                endpoint_probe=endpoint_probe,
                proxy_probe=AsyncMock(return_value=True),
            )
            self.profile.mkdir(parents=True)
            (self.profile / ".creatorhub-cdp-owner.json").write_text(
                json.dumps({
                    "pid": 4567,
                    "executable": str(Path("C:/Chrome/chrome.exe").resolve()),
                    "profile_dir": str(self.profile.resolve()),
                    "started_at": "2026-08-08T00:00:00+00:00",
                }), encoding="utf-8")
            (self.profile / "DevToolsActivePort").write_text(
                "43111\n/devtools/browser/fixture\n", encoding="utf-8")
            plan = ProxyPlan.parse("http://new-proxy.local:8080")

            session = await backend.open(self.identity, plan)

            self.assertEqual(terminated, [4567])
            self.assertIn(
                "--proxy-server=http://new-proxy.local:8080",
                calls["process_args"],
            )
            await backend.close(session)

        asyncio.run(scenario())

    def test_unreachable_configured_proxy_fails_before_chrome_launch(self):
        async def scenario():
            async def proxy_probe(_plan):
                return False

            backend, _browser, _process, calls = self._backend(
                proxy_probe=proxy_probe)
            plan = ProxyPlan.parse(
                "http://alice:secret@unreachable.local:8080")

            with self.assertRaisesRegex(CdpProxyError, "代理连接失败") as caught:
                await backend.open(self.identity, plan)

            self.assertIsNone(calls["process_args"])
            self.assertNotIn("secret", str(caught.exception))

        asyncio.run(scenario())


class _FakeCdpSession:
    def __init__(self):
        self.handlers = {}
        self.calls = []
        self.detached = False

    def on(self, event, callback):
        self.calls.append(("on", event))
        self.handlers[event] = callback

    async def send(self, method, params=None):
        self.calls.append((method, params or {}))

    async def detach(self):
        self.detached = True

    def emit(self, event, payload):
        self.handlers[event](payload)

    def sent(self, method):
        return [params for name, params in self.calls if name == method]


class _FakeCdpContext:
    def __init__(self, session):
        self.session = session

    async def new_cdp_session(self, _page):
        return self.session


class CdpProxyAuthControllerTests(unittest.TestCase):
    def _controller(self):
        session = _FakeCdpSession()
        controller = CdpProxyAuthController(
            _FakeCdpContext(session),
            ProxyPlan.parse("http://alice:secret@proxy.local:8080"),
        )
        return controller, session

    def test_handlers_are_registered_before_fetch_enable(self):
        async def scenario():
            controller, session = self._controller()
            await controller.install(object())
            self.assertEqual(session.calls[:3], [
                ("on", "Fetch.requestPaused"),
                ("on", "Fetch.authRequired"),
                ("Fetch.enable", {"handleAuthRequests": True}),
            ])
            await controller.close()

        asyncio.run(scenario())


    def test_server_challenge_never_receives_proxy_credentials(self):
        async def scenario():
            controller, session = self._controller()
            await controller.install(object())
            session.emit("Fetch.authRequired", {
                "requestId": "r1",
                "authChallenge": {
                    "source": "Server",
                    "origin": "https://fixture.local",
                },
            })
            await controller.close()
            response = session.sent("Fetch.continueWithAuth")[0]
            self.assertEqual(response["authChallengeResponse"], {
                "response": "Default"})

        asyncio.run(scenario())

    def test_only_matching_proxy_challenge_receives_credentials(self):
        async def scenario():
            controller, session = self._controller()
            await controller.install(object())
            session.emit("Fetch.authRequired", {
                "requestId": "r1",
                "authChallenge": {
                    "source": "Proxy",
                    "origin": "http://proxy.local:8080",
                },
            })
            session.emit("Fetch.authRequired", {
                "requestId": "r2",
                "authChallenge": {
                    "source": "Proxy",
                    "origin": "http://other.local:8080",
                },
            })
            await controller.close()
            responses = session.sent("Fetch.continueWithAuth")
            self.assertEqual(responses[0]["authChallengeResponse"], {
                "response": "ProvideCredentials",
                "username": "alice",
                "password": "secret",
            })
            self.assertEqual(responses[1]["authChallengeResponse"], {
                "response": "Default"})

        asyncio.run(scenario())

    def test_third_repeated_proxy_challenge_is_cancelled(self):
        async def scenario():
            controller, session = self._controller()
            await controller.install(object())
            payload = {
                "requestId": "same-request",
                "authChallenge": {
                    "source": "Proxy",
                    "origin": "http://proxy.local:8080",
                },
            }
            for _ in range(3):
                session.emit("Fetch.authRequired", payload)
            await controller.close()
            responses = session.sent("Fetch.continueWithAuth")
            self.assertEqual(
                responses[-1]["authChallengeResponse"],
                {"response": "CancelAuth"},
            )
            self.assertTrue(session.detached)

        asyncio.run(scenario())

class _ManagerPage:
    def __init__(self):
        self.route = AsyncMock()


class _ManagerContext:
    def __init__(self):
        self.pages_created = []
        self.closed = False
        self.cookie_jar = []
        self.added_cookies = []

    async def new_page(self):
        page = _ManagerPage()
        self.pages_created.append(page)
        return page

    async def close(self):
        self.closed = True

    async def cookies(self):
        return list(self.cookie_jar)

    async def add_cookies(self, cookies):
        self.added_cookies.extend(cookies)
        self.cookie_jar.extend(cookies)


class _ManagerCdpBackend:
    def __init__(self):
        self.open_calls = []
        self.close_calls = []
        self.fail_with = None

    async def open(self, identity, proxy_plan):
        self.open_calls.append((identity, proxy_plan))
        if self.fail_with:
            raise self.fail_with
        context = _ManagerContext()
        return SimpleNamespace(
            context=context,
            proxy_signature=proxy_plan.signature if proxy_plan else "direct",
            last_used=0.0,
            browser=SimpleNamespace(),
            owned=SimpleNamespace(),
            relay=None,
            auth_controller=None,
        )

    async def close(self, session):
        self.close_calls.append(session)


class BrowserManagerCdpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.identity = Identity(
            account_id=11,
            profile_dir=str(Path(self.tmp.name) / "acc_11"),
            identity_mode="native",
            platform="xhs",
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _manager(self, mode="cdp", idle=900):
        manager = BrowserManager(
            "UA", self.tmp.name,
            xhs_browser_mode=mode,
            xhs_cdp_idle_seconds=idle,
        )
        manager._cdp_backend = _ManagerCdpBackend()
        return manager

    def test_same_xhs_account_concurrently_opens_one_cdp_session(self):
        async def scenario():
            manager = self._manager()
            contexts = await asyncio.gather(
                manager.context_for(self.identity),
                manager.context_for(self.identity),
            )
            self.assertIs(contexts[0], contexts[1])
            self.assertEqual(len(manager._cdp_backend.open_calls), 1)

        asyncio.run(scenario())

    def test_auto_fallback_keeps_the_same_proxy(self):
        async def scenario():
            manager = self._manager("auto")
            manager._cdp_backend.fail_with = CdpLaunchError("CDP 启动超时")
            fallback = _ManagerContext()
            manager._launch_persistent = AsyncMock(return_value=fallback)
            self.identity.proxy = "http://alice:secret@proxy.local:8080"

            context = await manager.context_for(self.identity)

            self.assertIs(context, fallback)
            manager._launch_persistent.assert_awaited_once_with(
                self.identity, headless=False)
            self.assertEqual(self.identity.proxy,
                             "http://alice:secret@proxy.local:8080")
            snapshot = manager.environment_snapshot(
                self.identity, headless=False)
            self.assertEqual(snapshot["backend"], "patchright")
            self.assertTrue(snapshot["fallback"])
            self.assertNotIn("secret", repr(snapshot))

        asyncio.run(scenario())

    def test_strict_cdp_mode_does_not_fallback(self):
        async def scenario():
            manager = self._manager("cdp")
            manager._cdp_backend.fail_with = CdpLaunchError("CDP 启动超时")
            manager._launch_persistent = AsyncMock()

            with self.assertRaisesRegex(CdpLaunchError, "启动超时"):
                await manager.context_for(self.identity)

            manager._launch_persistent.assert_not_awaited()

        asyncio.run(scenario())

    def test_patchright_mode_bypasses_cdp(self):
        async def scenario():
            manager = self._manager("patchright")
            fallback = _ManagerContext()
            manager._launch_persistent = AsyncMock(return_value=fallback)

            context = await manager.context_for(self.identity)

            self.assertIs(context, fallback)
            self.assertEqual(manager._cdp_backend.open_calls, [])

        asyncio.run(scenario())

    def test_legacy_playwright_mode_migrates_to_patchright(self):
        manager = self._manager("playwright")

        self.assertEqual(manager.xhs_browser_mode, "patchright")
        self.assertFalse(manager._uses_xhs_cdp(self.identity))

    def test_xhs_cdp_never_blocks_full_page_resources(self):
        async def scenario():
            manager = self._manager("cdp")

            page = await manager.new_page(self.identity, block_media=True)

            page.route.assert_not_awaited()

        asyncio.run(scenario())

    def test_new_page_relaunches_once_after_user_closed_persistent_browser(self):
        class ClosedContext(_ManagerContext):
            async def new_page(self):
                raise RuntimeError(
                    "TargetClosedError: BrowserContext.new_page: "
                    "Target page, context or browser has been closed")

        async def scenario():
            manager = self._manager("patchright")
            stale = ClosedContext()
            healthy = _ManagerContext()
            manager._launch_persistent = AsyncMock(
                side_effect=[stale, healthy])

            page = await manager.new_page(self.identity)

            self.assertIs(page, healthy.pages_created[0])
            self.assertTrue(stale.closed)
            self.assertEqual(manager._launch_persistent.await_count, 2)

        asyncio.run(scenario())

    def test_proxy_signature_change_rebuilds_session(self):
        async def scenario():
            manager = self._manager("cdp")
            self.identity.proxy = "http://proxy-a.local:8080"
            first = await manager.context_for(self.identity)
            self.identity.proxy = "http://proxy-b.local:8080"

            second = await manager.context_for(self.identity)

            self.assertIsNot(first, second)
            self.assertEqual(len(manager._cdp_backend.open_calls), 2)
            self.assertEqual(len(manager._cdp_backend.close_calls), 1)

        asyncio.run(scenario())

    def test_cdp_context_bridges_database_login_cookies(self):
        async def scenario():
            manager = self._manager("cdp")
            self.identity.bridge_states = (json.dumps({
                "cookies": [{
                    "name": "a1", "value": "fixture",
                    "domain": ".xiaohongshu.com", "path": "/",
                }],
            }),)

            context = await manager.context_for(self.identity)

            self.assertEqual(len(context.added_cookies), 1)
            self.assertEqual(context.added_cookies[0]["name"], "a1")

        asyncio.run(scenario())

    def test_xhs_patchright_fallback_is_headed_and_reused(self):
        async def scenario():
            manager = self._manager("patchright")
            fallback = _ManagerContext()
            manager._launch_persistent = AsyncMock(return_value=fallback)

            first = await manager.context_for(self.identity)
            second = await manager.open_headed(self.identity)

            self.assertIs(first, second)
            manager._launch_persistent.assert_awaited_once_with(
                self.identity, headless=False)

        asyncio.run(scenario())

    def test_proxy_change_also_rebuilds_patchright_fallback(self):
        async def scenario():
            manager = self._manager("patchright")
            contexts = [_ManagerContext(), _ManagerContext()]
            manager._launch_persistent = AsyncMock(side_effect=contexts)
            self.identity.proxy = "http://proxy-a.local:8080"
            first = await manager.context_for(self.identity)
            self.identity.proxy = "http://proxy-b.local:8080"

            second = await manager.context_for(self.identity)

            self.assertIs(first, contexts[0])
            self.assertIs(second, contexts[1])
            self.assertTrue(first.closed)
            self.assertEqual(manager._launch_persistent.await_count, 2)

        asyncio.run(scenario())

    def test_idle_collection_closes_only_unlocked_cdp_session(self):
        async def scenario():
            manager = self._manager("cdp", idle=10)
            await manager.context_for(self.identity)
            manager._last_used[self.identity.key] = 100.0

            closed = await manager.collect_idle_cdp(now=111.0)

            self.assertEqual(closed, 1)
            self.assertEqual(len(manager._cdp_backend.close_calls), 1)
            self.assertNotIn(self.identity.key, manager._contexts)

        asyncio.run(scenario())

    def test_idle_collection_keeps_session_during_visible_action(self):
        async def scenario():
            manager = self._manager("cdp", idle=10)
            await manager.context_for(self.identity)
            manager._last_used[self.identity.key] = 100.0

            async with manager.visible_action(
                    self.identity, keep_context=True):
                closed = await manager.collect_idle_cdp(now=111.0)
                self.assertEqual(closed, 0)
                self.assertIn(self.identity.key, manager._contexts)

            closed = await manager.collect_idle_cdp(now=111.0)
            self.assertEqual(closed, 1)
            self.assertEqual(len(manager._cdp_backend.close_calls), 1)

        asyncio.run(scenario())

    def test_idle_collection_also_closes_patchright_session(self):
        async def scenario():
            manager = self._manager("patchright", idle=10)
            fallback = _ManagerContext()
            manager._launch_persistent = AsyncMock(return_value=fallback)
            await manager.context_for(self.identity)
            manager._last_used[self.identity.key] = 100.0

            closed = await manager.collect_idle_sessions(now=111.0)

            self.assertEqual(closed, 1)
            self.assertTrue(fallback.closed)
            self.assertNotIn(self.identity.key, manager._contexts)

        asyncio.run(scenario())

    def test_successful_temporary_login_context_can_be_rebound(self):
        async def scenario():
            manager = self._manager("patchright")
            fallback = _ManagerContext()
            manager._launch_persistent = AsyncMock(return_value=fallback)
            temporary = Identity(
                account_id=None,
                profile_dir=str(Path(self.tmp.name) / "new_login_fixture"),
                identity_mode="native",
                platform="xhs",
            )
            old_key = temporary.key
            await manager.context_for(temporary)

            rebound = await manager.rebind_context(old_key, 72)

            self.assertTrue(rebound)
            self.assertNotIn(old_key, manager._contexts)
            self.assertIs(manager._contexts[72], fallback)
            self.assertIn(72, manager._last_used)

        asyncio.run(scenario())

    def test_environment_snapshot_labels_backend_and_redacts_endpoints(self):
        manager = self._manager("auto")
        self.identity.proxy = "http://alice:secret@proxy.local:8080"
        manager._backend_by_key[self.identity.key] = "patchright"
        manager._fallback_reason_by_key[self.identity.key] = (
            "connect ws://127.0.0.1:42137/devtools/browser/fixture "
            "via http://alice:secret@proxy.local:8080")

        snapshot = manager.environment_snapshot(
            self.identity, headless=False)
        dumped = json.dumps(snapshot, ensure_ascii=False)

        self.assertEqual(snapshot["backend_label"], "Patchright Chromium · 回退")
        self.assertNotIn("secret", dumped)
        self.assertNotIn("ws://", dumped)
        self.assertNotIn("127.0.0.1:", dumped)


if __name__ == "__main__":
    unittest.main()
