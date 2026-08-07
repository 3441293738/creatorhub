import asyncio
import tempfile
import unittest
from pathlib import Path

import app.db as db
import app.main as main
from app.browser.identity import Identity
from app.browser.manager import BrowserManager
from app.config import Config
from app.models import DouyinAccount
from app.profiles import ensure_identity


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
