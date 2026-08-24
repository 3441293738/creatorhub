import asyncio
import tempfile
import unittest
from pathlib import Path

import app.db as db
import app.main as main
from app.browser.backends import (
    BrowserBackendUnavailableError,
    FingerprintChromiumBackend,
    fingerprint_seed_u32,
)
from app.browser.identity import Identity
from app.browser.manager import BrowserManager
from app.config import Config, load_config
from app.models import DouyinAccount


class FingerprintChromiumBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.executable = Path(self.tmp.name) / "fingerprint-chrome.exe"
        self.executable.write_bytes(b"fixture")

    def tearDown(self):
        self.tmp.cleanup()

    def test_seed_is_deterministic_unsigned_32_bit(self):
        first = fingerprint_seed_u32("account-fixture")
        second = fingerprint_seed_u32("account-fixture")

        self.assertEqual(first, second)
        self.assertGreaterEqual(first, 0)
        self.assertLessEqual(first, 0xFFFFFFFF)

    def test_launch_plan_uses_engine_level_identity_and_forces_headed_by_default(self):
        backend = FingerprintChromiumBackend(
            str(self.executable), platform="windows")
        identity = Identity(
            account_id=7,
            profile_dir=str(Path(self.tmp.name) / "profile"),
            browser_backend="fingerprint_chromium",
            fp_seed="account-fixture",
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )

        plan = backend.launch_plan(identity, requested_headless=True)

        self.assertEqual(plan.name, "fingerprint_chromium")
        self.assertEqual(Path(plan.executable_path), self.executable.resolve())
        self.assertFalse(plan.headless)
        self.assertIn(f"--fingerprint={fingerprint_seed_u32('account-fixture')}", plan.args)
        self.assertIn("--fingerprint-platform=windows", plan.args)
        self.assertIn("--fingerprint-brand=Chrome", plan.args)
        self.assertIn("--timezone=Asia/Shanghai", plan.args)
        self.assertIn("--lang=zh-CN", plan.args)
        self.assertTrue(plan.engine_controlled_identity)

    def test_missing_executable_fails_closed(self):
        backend = FingerprintChromiumBackend(
            str(Path(self.tmp.name) / "missing.exe"))
        identity = Identity(
            account_id=1,
            profile_dir=str(Path(self.tmp.name) / "profile"),
            browser_backend="fingerprint_chromium",
        )

        with self.assertRaises(BrowserBackendUnavailableError):
            backend.launch_plan(identity, requested_headless=False)

    def test_config_loads_fingerprint_runtime_options(self):
        config_path = Path(self.tmp.name) / "config.yaml"
        config_path.write_text(
            "engine:\n"
            "  browser_backend: fingerprint_chromium\n"
            f"  fingerprint_chromium_path: '{self.executable.as_posix()}'\n"
            "  fingerprint_chromium_allow_headless: true\n"
            "  fingerprint_chromium_platform: windows\n",
            encoding="utf-8",
        )

        cfg = load_config(str(config_path))

        self.assertEqual(cfg.engine.browser_backend, "fingerprint_chromium")
        self.assertEqual(cfg.engine.fingerprint_chromium_path,
                         self.executable.as_posix())
        self.assertTrue(cfg.engine.fingerprint_chromium_allow_headless)
        self.assertEqual(cfg.engine.fingerprint_chromium_platform, "windows")

    def test_manager_launches_custom_runtime_without_legacy_js_injection(self):
        class ContextStub:
            def __init__(self):
                self.pages = []
                self.script_calls = []

            async def new_page(self):
                class Page:
                    async def evaluate(self, _expression):
                        return "Mozilla/5.0 Chrome/148.0.0.0 Safari/537.36"

                    async def close(self):
                        return None

                return Page()

            async def add_init_script(self, value):
                self.script_calls.append(value)

            async def cookies(self):
                return []

        class ChromiumStub:
            def __init__(self):
                self.kwargs = None
                self.context = ContextStub()

            async def launch_persistent_context(self, **kwargs):
                self.kwargs = kwargs
                return self.context

        manager = BrowserManager(
            "UA",
            str(Path(self.tmp.name) / "profiles"),
            fingerprint_chromium_path=str(self.executable),
            fingerprint_chromium_platform="windows",
        )
        manager._pw = type("PW", (), {"chromium": ChromiumStub()})()
        manager._browser_channel = "chrome"
        identity = Identity(
            account_id=9,
            profile_dir=str(Path(self.tmp.name) / "profile-9"),
            identity_mode="legacy",
            browser_backend="fingerprint_chromium",
            fp_seed="fixture-9",
        )

        context = asyncio.run(manager._launch_persistent(identity))
        kwargs = manager._pw.chromium.kwargs

        self.assertEqual(Path(kwargs["executable_path"]), self.executable.resolve())
        self.assertNotIn("channel", kwargs)
        self.assertNotIn("user_agent", kwargs)
        self.assertNotIn("timezone_id", kwargs)
        self.assertTrue(kwargs["no_viewport"])
        self.assertFalse(kwargs["headless"])
        self.assertEqual(context.script_calls, [])

    def test_xhs_fingerprint_account_bypasses_system_chrome_cdp_backend(self):
        manager = BrowserManager(
            "UA",
            str(Path(self.tmp.name) / "profiles"),
            xhs_browser_mode="auto",
            fingerprint_chromium_path=str(self.executable),
        )
        identity = Identity(
            account_id=10,
            profile_dir=str(Path(self.tmp.name) / "profile-10"),
            platform="xhs",
            browser_backend="fingerprint_chromium",
        )

        self.assertFalse(manager._uses_xhs_cdp(identity))
        snapshot = manager.environment_snapshot(identity, headless=True)
        self.assertEqual(snapshot["backend"], "fingerprint_chromium")
        self.assertEqual(snapshot["browser"], "fingerprint-chromium")
        self.assertEqual(snapshot["backend_label"],
                         "Fingerprint Chromium · 开源内核")
        self.assertFalse(snapshot["headless"])


class AccountBrowserBackendApiTests(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.previous_browser = main.browser
        self.previous_cfg = main.cfg
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "backend.db"))
        main.cfg = Config()

    def tearDown(self):
        main.browser = self.previous_browser
        main.cfg = self.previous_cfg
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def test_account_backend_can_be_selected_and_closes_live_context(self):
        with db.get_session() as session:
            account = DouyinAccount(
                platform="douyin",
                nickname="fixture",
                browser_backend="default",
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = account.id

        class BrowserStub:
            def __init__(self):
                self.closed = []

            def backend_status(self, requested):
                return {
                    "name": "fingerprint_chromium",
                    "available": requested == "fingerprint_chromium",
                    "detail": "",
                }

            async def close_context(self, key):
                self.closed.append(key)

            def identity_for(self, account):
                return Identity.from_account(
                    account, main.cfg.engine.profiles_dir, "UA")

            def environment_snapshot(self, identity, *, headless):
                return {
                    "backend": identity.browser_backend,
                    "backend_label": "Fingerprint Chromium · 开源内核",
                    "headless": headless,
                }

        stub = BrowserStub()
        main.browser = stub

        result = asyncio.run(main.set_account_browser_backend(
            account_id,
            main.AccountBrowserBackendIn(
                browser_backend="fingerprint_chromium"),
        ))

        with db.get_session() as session:
            saved = session.get(DouyinAccount, account_id)
            self.assertEqual(saved.browser_backend, "fingerprint_chromium")
        self.assertEqual(stub.closed, [account_id])
        self.assertEqual(result["browser_backend"], "fingerprint_chromium")


if __name__ == "__main__":
    unittest.main()
