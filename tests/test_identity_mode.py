import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app.db as db
import app.main as main
from app.browser.identity import Identity
from app.browser.manager import BrowserManager
from app.config import Config
from app.models import DouyinAccount
from app.profiles import ensure_identity
from app.risk import OperationKind


class _ContextStub:
    def __init__(self):
        self.header_calls = []
        self.script_calls = []

    async def set_extra_http_headers(self, headers):
        self.header_calls.append(headers)

    async def add_init_script(self, script):
        self.script_calls.append(script)

    async def cookies(self):
        return []

    async def add_cookies(self, _cookies):
        return None


class _PageStub:
    async def evaluate(self, expression):
        if expression == "navigator.userAgent":
            return "ACTUAL_NATIVE_UA"
        return ""


class _ChromiumStub:
    def __init__(self):
        self.kwargs = None
        self.context = _ContextStub()

    async def launch_persistent_context(self, **kwargs):
        self.kwargs = kwargs
        return self.context


class _PlaywrightStub:
    def __init__(self):
        self.chromium = _ChromiumStub()


class IdentityModeTests(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "identity.db"))
        self.cfg = Config()
        self.cfg.engine.profiles_dir = str(Path(self.tmp.name) / "profiles")

    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def test_identity_from_account_preserves_mode(self):
        for mode in ("legacy", "native"):
            account = DouyinAccount(id=1, nickname="fixture", identity_mode=mode)

            identity = Identity.from_account(
                account, self.cfg.engine.profiles_dir, "DEFAULT_UA")

            self.assertEqual(identity.identity_mode, mode)

    def test_xhs_browser_defaults_are_page_native(self):
        engine = Config().engine

        self.assertEqual(engine.xhs_browser_mode, "auto")
        self.assertEqual(engine.xhs_cdp_idle_seconds, 900)
        self.assertEqual(engine.xhs_publish_mode, "browser")
        self.assertEqual(engine.xhs_comment_write_mode, "browser")

    def test_open_browser_url_requires_the_real_platform_host(self):
        self.assertTrue(main._platform_url_allowed(
            "xhs", "https://www.xiaohongshu.com/explore/fixture"))
        self.assertTrue(main._platform_url_allowed(
            "xhs", "https://creator.xiaohongshu.com/publish"))
        self.assertFalse(main._platform_url_allowed(
            "xhs", "https://evil.example/?next=xiaohongshu.com"))
        self.assertFalse(main._platform_url_allowed(
            "xhs", "https://xiaohongshu.com.evil.example/"))

    def test_identity_from_account_remembers_platform(self):
        account = DouyinAccount(
            id=7,
            platform="xhs",
            nickname="fixture",
            identity_mode="native",
        )

        identity = Identity.from_account(
            account, self.cfg.engine.profiles_dir, "DEFAULT_UA")

        self.assertEqual(identity.platform, "xhs")

    def test_temporary_identities_use_profile_scoped_keys(self):
        first = Identity(
            account_id=None,
            profile_dir=str(Path(self.tmp.name) / "login-a"),
            platform="xhs",
        )
        second = Identity(
            account_id=None,
            profile_dir=str(Path(self.tmp.name) / "login-b"),
            platform="xhs",
        )

        self.assertNotEqual(first.key, second.key)
        self.assertEqual(first.key, Identity(
            account_id=None,
            profile_dir=first.profile_dir,
            platform="xhs",
        ).key)

    def test_native_identity_initialization_does_not_generate_spoofed_ua(self):
        account = DouyinAccount(id=1, nickname="fixture", identity_mode="native")

        changed = ensure_identity(account, self.cfg, assign_proxy=False)

        self.assertTrue(changed)
        self.assertEqual(account.ua, "")
        self.assertTrue(account.fp_seed)

    def test_cookie_login_creates_native_account(self):
        previous_cfg = main.cfg
        main.cfg = self.cfg
        try:
            result = asyncio.run(main.login_cookie(main.CookieIn(
                platform="douyin", nickname="fixture", cookie="sid=fixture")))
        finally:
            main.cfg = previous_cfg

        with db.get_session() as session:
            account = session.get(DouyinAccount, result["account_id"])
            self.assertEqual(account.identity_mode, "native")
            self.assertEqual(account.ua, "")

    def test_scan_login_runs_through_unified_login_operation_guard(self):
        previous_cfg, previous_browser, previous_engine = (
            main.cfg, main.browser, main.engine)

        class BrowserStub:
            def environment_snapshot(self, identity, *, headless):
                return {
                    "browser": "chrome",
                    "chrome_major": 150,
                    "headless": headless,
                    "identity_mode": identity.identity_mode,
                    "profile_dir": identity.profile_dir,
                    "has_proxy": bool(identity.proxy),
                }

            async def close_context(self, _key):
                return None

        class EngineStub:
            def __init__(self):
                self.calls = []
                self.inside = False

            @asynccontextmanager
            async def operation_guard(self, account_id, kind, **kwargs):
                self.calls.append((account_id, kind, kwargs))
                self.inside = True
                try:
                    yield None
                finally:
                    self.inside = False

        engine = EngineStub()

        async def expired_login(_browser, _identity):
            self.assertTrue(engine.inside)
            return False, "", ""

        main.cfg = self.cfg
        main.browser = BrowserStub()
        main.engine = engine
        try:
            with patch("app.main.interactive_login", expired_login):
                asyncio.run(main._run_login(
                    "fixture-login", platform="douyin", proxy_choice="none"))
        finally:
            main.cfg, main.browser, main.engine = (
                previous_cfg, previous_browser, previous_engine)

        self.assertEqual(len(engine.calls), 1)
        self.assertEqual(engine.calls[0][1], OperationKind.LOGIN)

    def test_scan_login_task_reports_non_sensitive_browser_environment(self):
        previous_cfg, previous_browser, previous_engine = (
            main.cfg, main.browser, main.engine)

        class BrowserStub:
            def environment_snapshot(self, identity, *, headless):
                return {
                    "browser": "chrome",
                    "chrome_major": 150,
                    "headless": headless,
                    "identity_mode": identity.identity_mode,
                    "profile_dir": identity.profile_dir,
                    "has_proxy": bool(identity.proxy),
                }

            async def close_context(self, _key):
                return None

        async def expired_login(_browser, _identity):
            return False, "", ""

        task_id = "environment-diagnostic"
        main.cfg = self.cfg
        main.browser = BrowserStub()
        main.engine = None
        try:
            with patch("app.main.interactive_xhs_login", expired_login):
                asyncio.run(main._run_login(
                    task_id,
                    platform="xhs",
                    proxy_choice="http://user:secret@127.0.0.1:8080",
                ))

            result = main.login_tasks[task_id]
        finally:
            main.login_tasks.pop(task_id, None)
            main.cfg, main.browser, main.engine = (
                previous_cfg, previous_browser, previous_engine)

        self.assertEqual(result["status"], "expired")
        self.assertEqual(result["environment"]["browser"], "chrome")
        self.assertTrue(result["environment"]["has_proxy"])
        self.assertNotIn("secret", repr(result["environment"]))

    def test_successful_new_xhs_login_releases_temporary_session(self):
        previous_cfg, previous_browser, previous_engine = (
            main.cfg, main.browser, main.engine)

        class BrowserStub:
            def __init__(self):
                self.closed_keys = []

            def environment_snapshot(self, identity, *, headless):
                return {
                    "browser": "chrome", "headless": headless,
                    "profile_dir": identity.profile_dir,
                    "backend": "cdp", "backend_label": "系统 Chrome · CDP",
                }

            async def close_context(self, key):
                self.closed_keys.append(key)

        browser = BrowserStub()
        captured = {}

        async def logged_in(_browser, identity):
            captured["key"] = identity.key
            return True, '{"cookies":[{"name":"a1","value":"fixture"}]}', "fixture"

        task_id = "successful-xhs-login"
        main.cfg = self.cfg
        main.browser = browser
        main.engine = None
        try:
            with patch("app.main.interactive_xhs_login", logged_in), \
                    patch("app.main._enrich_account_profile",
                          AsyncMock(return_value="ok")):
                asyncio.run(main._run_login(
                    task_id, platform="xhs", proxy_choice="none"))
            result = main.login_tasks[task_id]
        finally:
            main.login_tasks.pop(task_id, None)
            main.cfg, main.browser, main.engine = (
                previous_cfg, previous_browser, previous_engine)

        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(browser.closed_keys, [captured["key"]])

    def test_open_account_browser_uses_unified_login_operation_guard(self):
        with db.get_session() as session:
            account = DouyinAccount(
                nickname="fixture", platform="douyin", identity_mode="native",
                profile_dir=str(Path(self.tmp.name) / "open-profile"),
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = account.id
        previous_browser, previous_engine = main.browser, main.engine

        class EngineStub:
            def __init__(self):
                self.inside = False
                self.calls = []

            @asynccontextmanager
            async def operation_guard(self, account_id, kind, **kwargs):
                self.calls.append((account_id, kind, kwargs))
                self.inside = True
                try:
                    yield None
                finally:
                    self.inside = False

        engine = EngineStub()

        class PageStub:
            async def goto(self, *_args, **_kwargs):
                self_outer.assertTrue(engine.inside)

        class ContextStub:
            async def add_cookies(self, _cookies):
                return None

            async def new_page(self):
                return PageStub()

            async def close(self):
                return None

            def on(self, *_args):
                return None

        class BrowserStub:
            def identity_for(self, account):
                return Identity.from_account(
                    account, self_outer.cfg.engine.profiles_dir, "DEFAULT_UA")

            async def open_headed(self, _identity):
                self_outer.assertTrue(engine.inside)
                return ContextStub()

        self_outer = self
        async def scenario():
            result = await main.open_account_browser(account_id)
            self.assertTrue(engine.inside)
            await main.open_browsers[account_id].close()
            self.assertFalse(engine.inside)
            return result

        main.browser = BrowserStub()
        main.engine = engine
        try:
            result = asyncio.run(scenario())
        finally:
            main.open_browsers.pop(account_id, None)
            main.browser, main.engine = previous_browser, previous_engine

        self.assertTrue(result["ok"])
        self.assertEqual(engine.calls[0][1], OperationKind.LOGIN)

    def test_xhs_open_browser_closes_through_manager_and_visible_gate(self):
        with db.get_session() as session:
            account = DouyinAccount(
                nickname="xhs-fixture", platform="xhs", identity_mode="native",
                profile_dir=str(Path(self.tmp.name) / "xhs-open-profile"),
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = account.id
        previous_browser, previous_engine = main.browser, main.engine

        state = {"engine": False, "visible": False}

        class EngineStub:
            @asynccontextmanager
            async def operation_guard(self, *_args, **_kwargs):
                state["engine"] = True
                try:
                    yield None
                finally:
                    state["engine"] = False

        class PageStub:
            async def goto(self, *_args, **_kwargs):
                self_outer.assertTrue(state["engine"])
                self_outer.assertTrue(state["visible"])

            async def bring_to_front(self):
                return None

            def on(self, *_args):
                return None

        class ContextStub:
            def __init__(self):
                self.closed_directly = False

            async def add_cookies(self, _cookies):
                return None

            async def close(self):
                self.closed_directly = True

            def on(self, *_args):
                return None

        context = ContextStub()

        class BrowserStub:
            def __init__(self):
                self.closed_keys = []

            def identity_for(self, account):
                return Identity.from_account(
                    account, self_outer.cfg.engine.profiles_dir, "DEFAULT_UA")

            @asynccontextmanager
            async def visible_action(self, _identity):
                state["visible"] = True
                try:
                    yield
                finally:
                    state["visible"] = False

            async def open_headed(self, _identity):
                return context

            async def new_page(self, _identity, **_kwargs):
                return PageStub()

            async def close_context(self, key):
                self.closed_keys.append(key)

        self_outer = self
        browser = BrowserStub()

        async def scenario():
            result = await main.open_account_browser(account_id)
            self.assertTrue(result["ok"])
            self.assertTrue(state["engine"])
            self.assertTrue(state["visible"])
            await main.open_browsers[account_id].close()
            self.assertFalse(state["engine"])
            self.assertFalse(state["visible"])

        main.browser = browser
        main.engine = EngineStub()
        try:
            asyncio.run(scenario())
        finally:
            main.open_browsers.pop(account_id, None)
            main.browser, main.engine = previous_browser, previous_engine

        self.assertEqual(browser.closed_keys, [account_id])
        self.assertFalse(context.closed_directly)

    def test_open_account_browser_releases_guard_when_cancelled_before_lease(self):
        with db.get_session() as session:
            account = DouyinAccount(
                nickname="fixture", platform="douyin", identity_mode="native",
                profile_dir=str(Path(self.tmp.name) / "cancel-profile"),
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = account.id
        previous_browser, previous_engine = main.browser, main.engine

        class EngineStub:
            def __init__(self):
                self.inside = False

            @asynccontextmanager
            async def operation_guard(self, *_args, **_kwargs):
                self.inside = True
                try:
                    yield None
                finally:
                    self.inside = False

        engine = EngineStub()

        class BrowserStub:
            def identity_for(self, account):
                return Identity.from_account(
                    account, self_outer.cfg.engine.profiles_dir, "DEFAULT_UA")

            async def open_headed(self, _identity):
                self_outer.assertTrue(engine.inside)
                raise asyncio.CancelledError()

        self_outer = self

        async def scenario():
            with self.assertRaises(asyncio.CancelledError):
                await main.open_account_browser(account_id)
            self.assertFalse(engine.inside)

        main.browser = BrowserStub()
        main.engine = engine
        try:
            asyncio.run(scenario())
        finally:
            main.open_browsers.pop(account_id, None)
            main.browser, main.engine = previous_browser, previous_engine

    def test_native_launch_omits_spoofing_options_and_hooks(self):
        manager = BrowserManager("DEFAULT_UA", self.cfg.engine.profiles_dir)
        manager._pw = _PlaywrightStub()
        identity = Identity(
            account_id=1,
            profile_dir=str(Path(self.tmp.name) / "native"),
            identity_mode="native",
            ua="SPOOFED_UA",
            fp_seed="fixture-seed",
        )

        context = asyncio.run(manager._launch_persistent(identity))
        kwargs = manager._pw.chromium.kwargs

        self.assertNotIn("user_agent", kwargs)
        self.assertNotIn("geolocation", kwargs)
        self.assertNotIn("permissions", kwargs)
        self.assertNotIn("args", kwargs)
        self.assertNotIn("viewport", kwargs)
        self.assertNotIn("locale", kwargs)
        self.assertNotIn("timezone_id", kwargs)
        self.assertTrue(kwargs["no_viewport"])
        self.assertEqual(context.header_calls, [])
        self.assertEqual(context.script_calls, [])

    def test_native_proxy_launch_only_adds_webrtc_proxy_routing_flags(self):
        manager = BrowserManager("DEFAULT_UA", self.cfg.engine.profiles_dir)
        manager._pw = _PlaywrightStub()
        identity = Identity(
            account_id=1,
            profile_dir=str(Path(self.tmp.name) / "native-proxy"),
            identity_mode="native",
            proxy="http://127.0.0.1:8080",
        )

        asyncio.run(manager._launch_persistent(identity, headless=False))
        kwargs = manager._pw.chromium.kwargs

        self.assertEqual(kwargs["args"], [
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
            "--webrtc-ip-handling-policy=disable_non_proxied_udp",
        ])
        self.assertEqual(kwargs["proxy"], {"server": "http://127.0.0.1:8080"})
        self.assertTrue(kwargs["no_viewport"])

    def test_browser_probe_prefers_installed_stable_chrome(self):
        manager = BrowserManager("DEFAULT_UA", self.cfg.engine.profiles_dir)
        page = AsyncMock()
        page.evaluate.return_value = (
            "Mozilla/5.0 Chrome/150.0.0.0 Safari/537.36")
        browser = AsyncMock()
        browser.new_page.return_value = page
        chromium = AsyncMock()
        chromium.launch.return_value = browser
        manager._pw = SimpleNamespace(chromium=chromium)

        major = asyncio.run(manager._detect_chrome_major())

        self.assertEqual(major, 150)
        self.assertEqual(manager._browser_channel, "chrome")
        self.assertEqual(chromium.launch.await_args.kwargs["channel"], "chrome")
        self.assertNotIn("args", chromium.launch.await_args.kwargs)

    def test_browser_probe_falls_back_when_stable_chrome_is_unavailable(self):
        manager = BrowserManager("DEFAULT_UA", self.cfg.engine.profiles_dir)
        page = AsyncMock()
        page.evaluate.return_value = (
            "Mozilla/5.0 Chrome/149.0.0.0 Safari/537.36")
        browser = AsyncMock()
        browser.new_page.return_value = page
        chromium = AsyncMock()
        chromium.launch.side_effect = [RuntimeError("no stable chrome"), browser]
        manager._pw = SimpleNamespace(chromium=chromium)

        major = asyncio.run(manager._detect_chrome_major())

        self.assertEqual(major, 149)
        self.assertIsNone(manager._browser_channel)
        self.assertEqual(chromium.launch.await_count, 2)
        self.assertNotIn("channel", chromium.launch.await_args.kwargs)

    def test_persistent_context_uses_selected_browser_channel(self):
        manager = BrowserManager("DEFAULT_UA", self.cfg.engine.profiles_dir)
        manager._pw = _PlaywrightStub()
        manager._browser_channel = "chrome"
        identity = Identity(
            account_id=1,
            profile_dir=str(Path(self.tmp.name) / "stable-channel"),
            identity_mode="native",
        )

        asyncio.run(manager._launch_persistent(identity))

        self.assertEqual(manager._pw.chromium.kwargs["channel"], "chrome")

    def test_environment_snapshot_is_diagnostic_and_does_not_expose_proxy(self):
        manager = BrowserManager("DEFAULT_UA", self.cfg.engine.profiles_dir)
        manager._browser_channel = "chrome"
        manager._chrome_major = 150
        identity = Identity(
            account_id=7,
            profile_dir=str(Path(self.tmp.name) / "diagnostic"),
            identity_mode="native",
            proxy="http://user:secret@127.0.0.1:8080",
        )

        snapshot = manager.environment_snapshot(identity, headless=False)

        self.assertEqual(snapshot, {
            "browser": "chrome",
            "chrome_major": 150,
            "headless": False,
            "identity_mode": "native",
            "profile_dir": identity.profile_dir,
            "has_proxy": True,
            "backend": "playwright",
            "backend_label": "Playwright Chromium",
            "fallback": False,
            "fallback_reason": "",
        })
        self.assertNotIn("secret", repr(snapshot))

    def test_account_environment_endpoint_is_redacted(self):
        with db.get_session() as session:
            account = DouyinAccount(
                nickname="fixture", platform="xhs", identity_mode="native",
                profile_dir=str(Path(self.tmp.name) / "env-profile"),
                proxy="http://alice:secret@proxy.local:8080",
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = account.id

        previous_browser = main.browser
        manager = BrowserManager(
            "UA", self.cfg.engine.profiles_dir, xhs_browser_mode="auto")
        with db.get_session() as session:
            identity = manager.identity_for(
                session.get(DouyinAccount, account_id))
        manager._backend_by_key[identity.key] = "playwright"
        manager._fallback_reason_by_key[identity.key] = (
            "connect ws://127.0.0.1:43111/devtools/browser/fixture "
            "via http://alice:secret@proxy.local:8080")
        main.browser = manager
        try:
            body = asyncio.run(main.account_browser_environment(account_id))
        finally:
            main.browser = previous_browser

        dumped = repr(body)
        self.assertEqual(body["backend_label"], "Playwright Chromium · 回退")
        self.assertNotIn("secret", dumped)
        self.assertNotIn("ws://", dumped)
        self.assertNotIn("127.0.0.1:", dumped)

    def test_native_launch_captures_actual_context_user_agent(self):
        manager = BrowserManager("DEFAULT_UA", self.cfg.engine.profiles_dir)
        manager._pw = _PlaywrightStub()
        manager._pw.chromium.context.pages = [_PageStub()]
        identity = Identity(
            account_id=None,
            profile_dir=str(Path(self.tmp.name) / "native-ua"),
            identity_mode="native",
            ua="",
        )

        asyncio.run(manager._launch_persistent(identity))

        self.assertEqual(identity.ua, "ACTUAL_NATIVE_UA")

    def test_native_launch_persists_actual_ua_for_cookie_account(self):
        with db.get_session() as session:
            account = DouyinAccount(
                nickname="cookie", identity_mode="native", ua="",
                profile_dir=str(Path(self.tmp.name) / "cookie-profile"),
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = account.id
            identity = Identity.from_account(
                account, self.cfg.engine.profiles_dir, "DEFAULT_UA")
        manager = BrowserManager(
            "DEFAULT_UA", self.cfg.engine.profiles_dir,
            native_ua_callback=main._persist_native_ua,
        )
        manager._pw = _PlaywrightStub()
        manager._pw.chromium.context.pages = [_PageStub()]

        asyncio.run(manager._launch_persistent(identity))

        with db.get_session() as session:
            self.assertEqual(
                session.get(DouyinAccount, account_id).ua,
                "ACTUAL_NATIVE_UA",
            )

    def test_legacy_launch_keeps_existing_identity_behavior(self):
        manager = BrowserManager("DEFAULT_UA", self.cfg.engine.profiles_dir)
        manager._pw = _PlaywrightStub()
        manager._chrome_major = 131
        identity = Identity(
            account_id=1,
            profile_dir=str(Path(self.tmp.name) / "legacy"),
            identity_mode="legacy",
            ua=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36"),
            fp_seed="fixture-seed",
        )

        context = asyncio.run(manager._launch_persistent(identity))
        kwargs = manager._pw.chromium.kwargs

        self.assertIn("user_agent", kwargs)
        self.assertIn("geolocation", kwargs)
        self.assertEqual(kwargs["permissions"], ["geolocation"])
        self.assertEqual(len(context.header_calls), 1)
        self.assertEqual(len(context.script_calls), 1)


if __name__ == "__main__":
    unittest.main()
