import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

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
        main.browser = BrowserStub()
        main.engine = engine
        try:
            result = asyncio.run(main.open_account_browser(account_id))
        finally:
            main.open_browsers.pop(account_id, None)
            main.browser, main.engine = previous_browser, previous_engine

        self.assertTrue(result["ok"])
        self.assertEqual(engine.calls[0][1], OperationKind.LOGIN)

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
        self.assertEqual(context.header_calls, [])
        self.assertEqual(context.script_calls, [])

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
