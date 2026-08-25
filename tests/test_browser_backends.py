import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlmodel import select

import app.db as db
import app.main as main
from app.browser.backends import (
    BrowserBackendUnavailableError,
    FingerprintChromiumBackend,
    fingerprint_seed_u32,
    parse_extra_launch_args,
)
from app.browser.identity import Identity
from app.browser.manager import BrowserManager
from app.config import Config, load_config
from app.models import BrowserRuntime, DouyinAccount


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
        self.assertEqual(fingerprint_seed_u32("2023"), 2023)

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

    def test_launch_plan_applies_all_supported_account_overrides(self):
        backend = FingerprintChromiumBackend(
            str(self.executable), platform="windows",
            version="142.0.1", runtime_id="fp-142")
        identity = Identity(
            account_id=8,
            profile_dir=str(Path(self.tmp.name) / "profile-8"),
            browser_backend="fingerprint_chromium",
            fp_seed="manual-seed",
            fp_platform="macos",
            fp_platform_version="15.2.0",
            fp_brand="Edge",
            fp_brand_version="142.0.0.0",
            fp_hardware_concurrency=12,
            fp_gpu_vendor="Apple Inc.",
            fp_gpu_renderer="Apple M3",
            fp_accept_languages="en-US,en",
            fp_disable_spoofing="canvas,gpu",
            locale="en-US",
            timezone_id="America/Los_Angeles",
        )

        args = backend.launch_plan(
            identity, requested_headless=False).args

        self.assertIn("--fingerprint-platform=macos", args)
        self.assertIn("--fingerprint-platform-version=15.2.0", args)
        self.assertIn("--fingerprint-brand=Edge", args)
        self.assertIn("--fingerprint-brand-version=142.0.0.0", args)
        self.assertIn("--fingerprint-hardware-concurrency=12", args)
        self.assertIn("--fingerprint-gpu-vendor=Apple Inc.", args)
        self.assertIn("--fingerprint-gpu-renderer=Apple M3", args)
        self.assertIn("--accept-lang=en-US,en", args)
        self.assertIn("--disable-spoofing=canvas,gpu", args)

    def test_privacy_mode_and_safe_additional_launch_args_are_applied(self):
        backend = FingerprintChromiumBackend(
            str(self.executable), platform="windows")
        identity = Identity(
            account_id=11,
            profile_dir=str(Path(self.tmp.name) / "profile-11"),
            browser_backend="fingerprint_chromium",
            fp_webrtc_mode="allow",
            fp_extra_args="--mute-audio\n--disable-notifications",
        )

        args = backend.launch_plan(identity, requested_headless=False).args

        self.assertNotIn("--disable-non-proxied-udp", args)
        self.assertIn("--mute-audio", args)
        self.assertIn("--disable-notifications", args)

    def test_additional_launch_args_reject_owned_runtime_settings(self):
        self.assertEqual(
            parse_extra_launch_args("--mute-audio, --disable-notifications"),
            ("--mute-audio", "--disable-notifications"),
        )
        with self.assertRaisesRegex(ValueError, "账号环境冲突"):
            parse_extra_launch_args("--proxy-server=http://fixture:8080")
        with self.assertRaisesRegex(ValueError, "必须使用"):
            parse_extra_launch_args("mute-audio")

    def test_config_loads_fingerprint_runtime_options(self):
        config_path = Path(self.tmp.name) / "config.yaml"
        config_path.write_text(
            "engine:\n"
            "  browser_backend: fingerprint_chromium\n"
            f"  fingerprint_chromium_path: '{self.executable.as_posix()}'\n"
            f"  fingerprint_chromium_root: '{Path(self.tmp.name).as_posix()}'\n"
            "  fingerprint_chromium_allow_headless: true\n"
            "  fingerprint_chromium_platform: windows\n",
            encoding="utf-8",
        )

        cfg = load_config(str(config_path))

        self.assertEqual(cfg.engine.browser_backend, "fingerprint_chromium")
        self.assertEqual(cfg.engine.fingerprint_chromium_path,
                         self.executable.as_posix())
        self.assertEqual(cfg.engine.fingerprint_chromium_root,
                         Path(self.tmp.name).as_posix())
        self.assertTrue(cfg.engine.fingerprint_chromium_allow_headless)
        self.assertEqual(cfg.engine.fingerprint_chromium_platform, "windows")

    def test_config_loads_xhs_browser_read_throttling(self):
        config_path = Path(self.tmp.name) / "config.yaml"
        config_path.write_text(
            "engine:\n"
            "  xhs_read_mode: browser\n"
            "  xhs_keyword_gap_seconds: 12\n"
            "  xhs_item_gap_seconds: 3.5\n"
            "  xhs_request_jitter: 0.4\n",
            encoding="utf-8",
        )

        cfg = load_config(str(config_path))

        self.assertEqual(cfg.engine.xhs_read_mode, "browser")
        self.assertEqual(cfg.engine.xhs_keyword_gap_seconds, 12)
        self.assertEqual(cfg.engine.xhs_item_gap_seconds, 3.5)
        self.assertEqual(cfg.engine.xhs_request_jitter, 0.4)

    def test_direct_request_ua_matches_selected_fingerprint_runtime(self):
        manager = BrowserManager(
            "Mozilla/5.0 Chrome/130.0.0.0 Safari/537.36",
            str(Path(self.tmp.name) / "profiles"),
            fingerprint_chromium_runtimes=[{
                "runtime_id": "fp-148",
                "executable_path": str(self.executable),
                "version": "148.0.1",
                "name": "Chromium 148",
                "enabled": True,
                "is_default": True,
            }],
            fingerprint_default_runtime_id="fp-148",
        )
        identity = Identity(
            account_id=9,
            profile_dir=str(Path(self.tmp.name) / "profile-9"),
            browser_backend="fingerprint_chromium",
            browser_runtime_id="fp-148",
            ua="Mozilla/5.0 Chrome/130.0.0.0 Safari/537.36",
        )

        ua = manager.direct_request_user_agent(identity)

        self.assertIn("Chrome/148.0.0.0", ua)
        self.assertNotIn("Chrome/130.", ua)

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
        self.assertEqual(
            kwargs["ignore_default_args"],
            ["--disable-blink-features=AutomationControlled"],
        )
        self.assertIn("--window-size=1280,800", kwargs["args"])
        self.assertEqual(kwargs["permissions"], ["geolocation"])
        self.assertIn("latitude", kwargs["geolocation"])
        self.assertEqual(context.script_calls, [])

    def test_manager_honors_denied_geolocation_permission(self):
        class ContextStub:
            pages = []

            async def new_page(self):
                class Page:
                    async def evaluate(self, _expression):
                        return "Mozilla/5.0 Chrome/148.0.0.0 Safari/537.36"

                    async def close(self):
                        return None
                return Page()

            async def cookies(self):
                return []

        class ChromiumStub:
            def __init__(self):
                self.kwargs = None

            async def launch_persistent_context(self, **kwargs):
                self.kwargs = kwargs
                return ContextStub()

        manager = BrowserManager(
            "UA", str(Path(self.tmp.name) / "profiles-denied"),
            fingerprint_chromium_path=str(self.executable),
            fingerprint_chromium_platform="windows",
        )
        manager._pw = type("PW", (), {"chromium": ChromiumStub()})()
        identity = Identity(
            account_id=12,
            profile_dir=str(Path(self.tmp.name) / "profile-12"),
            browser_backend="fingerprint_chromium",
            fp_geolocation_permission="deny",
        )

        asyncio.run(manager._launch_persistent(identity))
        kwargs = manager._pw.chromium.kwargs

        self.assertIn("--deny-permission-prompts", kwargs["args"])
        self.assertNotIn("geolocation", kwargs)
        self.assertNotIn("permissions", kwargs)

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

    def test_account_runtime_selects_executable_and_isolated_profile(self):
        runtime_a = Path(self.tmp.name) / "chrome-a.exe"
        runtime_b = Path(self.tmp.name) / "chrome-b.exe"
        runtime_a.write_bytes(b"a")
        runtime_b.write_bytes(b"b")

        class PageStub:
            async def evaluate(self, _expression):
                return "Mozilla/5.0 Chrome/149.0.0.0 Safari/537.36"

            async def close(self):
                return None

        class ContextStub:
            pages = []

            async def new_page(self):
                return PageStub()

            async def cookies(self):
                return []

            def on(self, *_args):
                return None

        class ChromiumStub:
            def __init__(self):
                self.kwargs = None

            async def launch_persistent_context(self, **kwargs):
                self.kwargs = kwargs
                return ContextStub()

        manager = BrowserManager(
            "UA", str(Path(self.tmp.name) / "profiles"),
            fingerprint_chromium_runtimes=[
                {"runtime_id": "fp-148-a", "executable_path": str(runtime_a),
                 "version": "148.0.1", "name": "内核 148", "enabled": True,
                 "is_default": True},
                {"runtime_id": "fp-149-b", "executable_path": str(runtime_b),
                 "version": "149.0.1", "name": "内核 149", "enabled": True},
            ],
            fingerprint_default_runtime_id="fp-148-a",
        )
        manager._pw = type("PW", (), {"chromium": ChromiumStub()})()
        identity = Identity(
            account_id=19,
            profile_dir=str(Path(self.tmp.name) / "account-19"),
            identity_mode="native",
            browser_backend="fingerprint_chromium",
            browser_runtime_id="fp-149-b",
        )

        asyncio.run(manager._launch_persistent(identity, headless=False))
        kwargs = manager._pw.chromium.kwargs

        self.assertEqual(Path(kwargs["executable_path"]), runtime_b.resolve())
        self.assertEqual(
            Path(kwargs["user_data_dir"]),
            Path(identity.profile_dir) / "runtimes" / "fp-149-b")
        preferences = json.loads((
            Path(kwargs["user_data_dir"]) / "Default" / "Preferences"
        ).read_text(encoding="utf-8"))
        self.assertFalse(preferences["profile"]["block_third_party_cookies"])
        self.assertEqual(preferences["profile"]["cookie_controls_mode"], 2)
        snapshot = manager.environment_snapshot(identity, headless=False)
        self.assertEqual(snapshot["runtime_id"], "fp-149-b")
        self.assertEqual(snapshot["runtime_version"], "149.0.1")
        manager._release_profile_lock(identity.key)

    def test_existing_fingerprint_cookie_choice_is_not_overwritten(self):
        profile = Path(self.tmp.name) / "existing-profile"
        preferences_path = profile / "Default" / "Preferences"
        preferences_path.parent.mkdir(parents=True)
        preferences_path.write_text(
            json.dumps({"profile": {"cookie_controls_mode": 1}}),
            encoding="utf-8",
        )

        changed = BrowserManager._seed_fingerprint_profile_preferences(profile)

        self.assertFalse(changed)
        saved = json.loads(preferences_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["profile"]["cookie_controls_mode"], 1)


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

            def backend_status(self, requested, runtime_id=""):
                return {
                    "name": "fingerprint_chromium",
                    "available": (requested == "fingerprint_chromium"
                                  and runtime_id == "fp-148-fixture"),
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
                browser_backend="fingerprint_chromium",
                browser_runtime_id="fp-148-fixture"),
        ))

        with db.get_session() as session:
            saved = session.get(DouyinAccount, account_id)
            self.assertEqual(saved.browser_backend, "fingerprint_chromium")
            self.assertEqual(saved.browser_runtime_id, "fp-148-fixture")
        self.assertEqual(stub.closed, [account_id])
        self.assertEqual(result["browser_backend"], "fingerprint_chromium")

    def test_runtime_scan_accepts_machine_specific_directory(self):
        install_root = Path(self.tmp.name) / "custom-browser-location"
        executable = install_root / "Application" / "chrome.exe"
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"portable-browser-fixture")
        main.browser = BrowserManager(
            "UA", str(Path(self.tmp.name) / "profiles"))

        result = asyncio.run(main.scan_browser_runtimes(
            main.BrowserRuntimeScanIn(root=str(install_root))))

        self.assertEqual(result["found"], 1)
        self.assertEqual(result["created"], 1)
        with db.get_session() as session:
            runtime = session.exec(select(BrowserRuntime)).first()
            self.assertIsNotNone(runtime)
            self.assertEqual(
                Path(runtime.executable_path), executable.resolve())

    def test_environment_can_supply_runtime_paths_without_config_drive(self):
        first = Path(self.tmp.name) / "runtime-one"
        second = Path(self.tmp.name) / "runtime-two"
        first.mkdir(); second.mkdir()
        main.cfg.engine.fingerprint_chromium_root = ""
        with patch.dict("os.environ", {
                "CREATORHUB_FINGERPRINT_CHROMIUM_ROOTS":
                    str(first) + main.os.pathsep + str(second)}, clear=False):
            roots = main._browser_runtime_scan_roots()

        self.assertIn(str(first.resolve()), roots)
        self.assertIn(str(second.resolve()), roots)


if __name__ == "__main__":
    unittest.main()
