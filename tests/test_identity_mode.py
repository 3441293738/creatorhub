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
from app.config import Config, load_config
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


class _PatchrightStub:
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
        self.assertTrue(engine.resident_browser_sessions)
        self.assertEqual(engine.browser_session_idle_seconds, 1800)
        self.assertIsNone(engine.xhs_cdp_idle_seconds)
        self.assertEqual(engine.xhs_publish_mode, "browser")
        self.assertEqual(engine.xhs_comment_write_mode, "browser")

    def test_legacy_playwright_config_value_migrates_to_patchright(self):
        config_path = Path(self.tmp.name) / "legacy-browser-mode.yaml"
        config_path.write_text(
            "engine:\n  xhs_browser_mode: playwright\n",
            encoding="utf-8",
        )

        cfg = load_config(str(config_path))

        self.assertEqual(cfg.engine.xhs_browser_mode, "patchright")

    def test_legacy_cdp_idle_option_remains_available(self):
        config_path = Path(self.tmp.name) / "legacy-idle.yaml"
        config_path.write_text(
            "engine:\n  xhs_cdp_idle_seconds: 900\n",
            encoding="utf-8",
        )

        cfg = load_config(str(config_path))
        manager = BrowserManager(
            "UA", self.cfg.engine.profiles_dir,
            xhs_cdp_idle_seconds=cfg.engine.xhs_cdp_idle_seconds,
            session_idle_seconds=cfg.engine.browser_session_idle_seconds,
        )

        self.assertEqual(manager.session_idle_seconds, 900)

    def test_open_browser_url_requires_the_real_platform_host(self):
        self.assertTrue(main._platform_url_allowed(
            "xhs", "https://www.xiaohongshu.com/explore/fixture"))
        self.assertTrue(main._platform_url_allowed(
            "xhs", "https://creator.xiaohongshu.com/publish"))
        self.assertFalse(main._platform_url_allowed(
            "xhs", "https://evil.example/?next=xiaohongshu.com"))
        self.assertFalse(main._platform_url_allowed(
            "xhs", "https://xiaohongshu.com.evil.example/"))

    def test_xhs_open_page_ignores_transient_login_button_without_auth_evidence(self):
        class Locator:
            first = None

            def __init__(self):
                self.first = self

            async def is_visible(self, **_kwargs):
                return True

        class Page:
            url = "https://www.xiaohongshu.com/user/profile/me"

            def get_by_text(self, *_args, **_kwargs):
                return Locator()

        async def scenario():
            self.assertEqual(
                await main._opened_page_login_state(Page(), "xhs", {}),
                "unconfirmed",
            )
            self.assertEqual(
                await main._opened_page_login_state(
                    Page(), "xhs", {"authenticated": True}),
                "authenticated",
            )

        asyncio.run(scenario())

    def test_xhs_open_page_user_me_handler_records_strong_identity(self):
        class Response:
            url = "https://edith.xiaohongshu.com/api/sns/web/v2/user/me"
            status = 200

            async def json(self):
                return {"data": {"guest": False, "user_id": "fixture-user"}}

        evidence = {}
        asyncio.run(main._xhs_open_auth_response_handler(evidence)(Response()))

        self.assertTrue(evidence["seen"])
        self.assertTrue(evidence["authenticated"])

    def test_xhs_read_login_requires_main_site_web_session(self):
        creator_only = (
            '{"cookies":[{"name":"customer-sso-sid","value":"creator"}]}'
        )
        read_login = (
            '{"cookies":[{"name":"web_session","value":"main-session"}]}'
        )

        self.assertFalse(main._xhs_has_read_login_state(creator_only))
        self.assertTrue(main._xhs_has_read_login_state(read_login))

    def test_xhs_creator_page_has_its_own_login_classification(self):
        async def scenario():
            active = SimpleNamespace(
                url="https://creator.xiaohongshu.com/home")
            logged_out = SimpleNamespace(
                url="https://creator.xiaohongshu.com/login")
            self.assertEqual(
                await main._opened_page_login_state(active, "xhs_creator"),
                "authenticated",
            )
            self.assertEqual(
                await main._opened_page_login_state(logged_out, "xhs_creator"),
                "logged_out",
            )

        asyncio.run(scenario())

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

    def test_duplicate_xhs_login_reuses_and_refocuses_active_task(self):
        task_id = "active-xhs-login"
        main.login_tasks[task_id] = main._login_task_state(
            status="waiting", platform="xhs", creator=False,
            account_id=None)
        try:
            with patch("app.main.bring_window_to_front",
                       return_value=True) as focus:
                result = asyncio.run(main._reuse_or_reject_interactive_login(
                    "xhs", False))
        finally:
            main.login_tasks.pop(task_id, None)

        self.assertEqual(result["task_id"], task_id)
        self.assertTrue(result["reused"])
        focus.assert_called_once()

    def test_different_login_scope_is_rejected_instead_of_queued(self):
        task_id = "active-xhs-creator-login"
        main.login_tasks[task_id] = main._login_task_state(
            status="waiting", platform="xhs", creator=True,
            account_id=None)
        try:
            with self.assertRaises(main.HTTPException) as caught:
                asyncio.run(main._reuse_or_reject_interactive_login(
                    "xhs", False))
        finally:
            main.login_tasks.pop(task_id, None)

        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("已有小红书创作者扫码登录窗口", caught.exception.detail)

    def test_open_account_browser_rejects_new_scan_login_immediately(self):
        class Lease:
            active = True

        main.open_browsers[73] = Lease()
        try:
            with self.assertRaises(main.HTTPException) as caught:
                asyncio.run(main._reuse_or_reject_interactive_login(
                    "xhs", False))
        finally:
            main.open_browsers.pop(73, None)

        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("先关闭已打开的指纹浏览器窗口", caught.exception.detail)

    def test_native_identity_initialization_does_not_generate_spoofed_ua(self):
        account = DouyinAccount(id=1, nickname="fixture", identity_mode="native")

        changed = ensure_identity(account, self.cfg, assign_proxy=False)

        self.assertTrue(changed)
        self.assertEqual(account.ua, "")
        self.assertTrue(account.fp_seed)

    def test_profile_allocation_does_not_reuse_database_account_id(self):
        first = DouyinAccount(id=1, nickname="first", identity_mode="native")
        replacement = DouyinAccount(
            id=1, nickname="replacement", identity_mode="native")

        ensure_identity(first, self.cfg, assign_proxy=False)
        ensure_identity(replacement, self.cfg, assign_proxy=False)

        self.assertNotEqual(first.profile_dir, replacement.profile_dir)
        self.assertTrue(Path(first.profile_dir).name.startswith("account_"))
        self.assertTrue(Path(replacement.profile_dir).name.startswith("account_"))

    def test_context_signature_includes_profile_and_fingerprint(self):
        manager = BrowserManager("DEFAULT_UA", self.cfg.engine.profiles_dir)
        first = Identity(
            account_id=1,
            profile_dir=str(Path(self.tmp.name) / "profile-first"),
            identity_mode="native",
            fp_seed="first-seed",
        )
        replacement = Identity(
            account_id=1,
            profile_dir=str(Path(self.tmp.name) / "profile-replacement"),
            identity_mode="native",
            fp_seed="replacement-seed",
        )

        first_signature = manager._context_signature(
            first, "patchright", "", "direct")
        replacement_signature = manager._context_signature(
            replacement, "patchright", "", "direct")

        self.assertNotEqual(first_signature, replacement_signature)

    def test_delete_account_closes_leases_and_removes_only_its_profile(self):
        profile = Path(self.cfg.engine.profiles_dir) / "account_delete_fixture"
        profile.mkdir(parents=True)
        (profile / "Cookies").write_text("fixture", encoding="utf-8")
        with db.get_session() as session:
            account = DouyinAccount(
                nickname="delete", profile_dir=str(profile),
                identity_mode="native")
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = account.id

        lease = SimpleNamespace(close=AsyncMock())
        manager = SimpleNamespace(close_context=AsyncMock())
        previous_cfg, previous_browser = main.cfg, main.browser
        main.cfg, main.browser = self.cfg, manager
        main.open_browsers[account_id] = lease
        try:
            result = asyncio.run(main.del_account(account_id))
        finally:
            main.cfg, main.browser = previous_cfg, previous_browser
            main.open_browsers.pop(account_id, None)

        self.assertTrue(result["ok"])
        self.assertTrue(result["profile_removed"])
        self.assertFalse(profile.exists())
        lease.close.assert_awaited_once()
        manager.close_context.assert_awaited_once_with(account_id)
        with db.get_session() as session:
            self.assertIsNone(session.get(DouyinAccount, account_id))

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

        async def expired_login(_browser, _identity, **_kwargs):
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

    def test_successful_new_xhs_login_rebinds_temporary_session(self):
        previous_cfg, previous_browser, previous_engine = (
            main.cfg, main.browser, main.engine)

        class BrowserStub:
            def __init__(self):
                self.closed_keys = []
                self.rebound_keys = []

            def environment_snapshot(self, identity, *, headless):
                return {
                    "browser": "chrome", "headless": headless,
                    "profile_dir": identity.profile_dir,
                    "backend": "cdp", "backend_label": "系统 Chrome · CDP",
                }

            async def close_context(self, key):
                self.closed_keys.append(key)

            async def rebind_context(self, old_key, new_key):
                self.rebound_keys.append((old_key, new_key))
                return True

        browser = BrowserStub()
        captured = {}

        async def logged_in(_browser, identity, **_kwargs):
            captured["key"] = identity.key
            captured["browser_backend"] = identity.browser_backend
            identity.observed_login_profile = {
                "user_id": "xhs-member-73",
                "red_id": "red-73",
                "nickname": "fixture",
            }
            return True, '{"cookies":[{"name":"a1","value":"fixture"}]}', "fixture"

        task_id = "successful-xhs-login"
        main.cfg = self.cfg
        main.browser = browser
        main.engine = None
        enrich = AsyncMock(return_value="ok")
        try:
            with patch("app.main.interactive_xhs_login", logged_in), \
                    patch("app.main._enrich_account_profile", enrich):
                asyncio.run(main._run_login(
                    task_id, platform="xhs", proxy_choice="none",
                    browser_backend="fingerprint_chromium"))
            result = main.login_tasks[task_id]
            with db.get_session() as session:
                saved = session.get(DouyinAccount, result["account_id"])
                saved_browser_backend = saved.browser_backend
                saved_sec_uid = saved.sec_uid
        finally:
            main.login_tasks.pop(task_id, None)
            main.cfg, main.browser, main.engine = (
                previous_cfg, previous_browser, previous_engine)

        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(saved_browser_backend, "fingerprint_chromium")
        self.assertEqual(captured.get("browser_backend"),
                         "fingerprint_chromium")
        self.assertEqual(saved_sec_uid, "xhs-member-73")
        enrich.assert_not_awaited()
        self.assertEqual(browser.closed_keys, [])
        self.assertEqual(
            browser.rebound_keys,
            [(captured["key"], result["account_id"])],
        )

    def test_first_login_custom_fingerprint_is_used_and_persisted(self):
        previous_cfg, previous_browser, previous_engine = (
            main.cfg, main.browser, main.engine)

        class BrowserStub:
            def __init__(self):
                self.closed_keys = []

            def backend_status(self, requested, _runtime_id=""):
                return {
                    "name": requested,
                    "available": True,
                    "detail": "",
                }

            def environment_snapshot(self, identity, *, headless):
                return {
                    "browser": "fingerprint-chromium",
                    "headless": headless,
                    "profile_dir": identity.profile_dir,
                }

            async def close_context(self, key):
                self.closed_keys.append(key)

        captured = {}

        async def logged_in(_browser, identity, **_kwargs):
            captured["identity"] = identity
            identity.observed_login_profile = {
                "user_id": "prelogin-fingerprint-user",
                "nickname": "prelogin-fixture",
            }
            return True, '{"cookies":[{"name":"a1","value":"ok"}]}', "fixture"

        custom = main._validate_fingerprint_update(
            main.AccountFingerprintUpdateIn(
                seed="first-login-custom-seed",
                source_ip="203.0.113.8",
                country="US",
                region="California",
                city="Los Angeles",
                timezone="America/Los_Angeles",
                locale="en-US",
                accept_languages="en-US,en;q=0.9",
                viewport_w=1440,
                viewport_h=900,
                geo_lat=34.0522,
                geo_lon=-118.2437,
                platform="windows",
                platform_version="10.0.19045",
                brand="Chrome",
                brand_version="148.0.0.0",
                hardware_concurrency=12,
                disable_spoofing=["font"],
                language_mode="custom",
                timezone_mode="custom",
                viewport_mode="custom",
                location_mode="custom",
                geolocation_permission="ask",
                webrtc_mode="conceal",
                extra_args="--mute-audio",
            ))
        browser = BrowserStub()
        task_id = "first-login-custom-fingerprint"
        main.cfg = self.cfg
        main.browser = browser
        main.engine = None
        try:
            with patch("app.main._proxy_geo", AsyncMock(return_value={
                    "ip": "198.51.100.9", "country": "CN",
                    "region": "Shanghai", "city": "Shanghai",
                    "timezone": "Asia/Shanghai", "lat": 31.23,
                    "lon": 121.47,
                    })), patch("app.main.interactive_xhs_login", logged_in):
                asyncio.run(main._run_login(
                    task_id,
                    platform="xhs",
                    proxy_choice="none",
                    browser_backend="fingerprint_chromium",
                    fingerprint_overrides=custom,
                ))
            result = main.login_tasks[task_id]
            with db.get_session() as session:
                saved = session.get(DouyinAccount, result["account_id"])
                saved_values = {
                    "seed": saved.fp_seed,
                    "ip": saved.fp_source_ip,
                    "timezone": saved.timezone_id,
                    "locale": saved.locale,
                    "viewport": (saved.viewport_w, saved.viewport_h),
                    "platform": saved.fp_platform,
                    "cpu": saved.fp_hardware_concurrency,
                    "disabled": saved.fp_disable_spoofing,
                    "extra": saved.fp_extra_args,
                }
        finally:
            main.login_tasks.pop(task_id, None)
            main.cfg, main.browser, main.engine = (
                previous_cfg, previous_browser, previous_engine)

        identity = captured["identity"]
        self.assertEqual(identity.fp_seed, "first-login-custom-seed")
        self.assertEqual(identity.timezone_id, "America/Los_Angeles")
        self.assertEqual(identity.locale, "en-US")
        self.assertEqual((identity.viewport_w, identity.viewport_h), (1440, 900))
        self.assertEqual(identity.fp_accept_languages, "en-US,en;q=0.9")
        self.assertEqual(identity.fp_platform, "windows")
        self.assertEqual(identity.fp_geolocation_permission, "ask")
        self.assertEqual(saved_values, {
            "seed": "first-login-custom-seed",
            "ip": "203.0.113.8",
            "timezone": "America/Los_Angeles",
            "locale": "en-US",
            "viewport": (1440, 900),
            "platform": "windows",
            "cpu": 12,
            "disabled": "font",
            "extra": "--mute-audio",
        })

    def test_fresh_xhs_creator_login_does_not_claim_main_read_session(self):
        previous_cfg, previous_browser, previous_engine = (
            main.cfg, main.browser, main.engine)

        class BrowserStub:
            def environment_snapshot(self, identity, *, headless):
                return {"browser": "chrome", "headless": headless,
                        "profile_dir": identity.profile_dir}

            async def close_context(self, _key):
                return None

        state = (
            '{"cookies":[{"name":"customer-sso-sid",'
            '"value":"creator"}]}'
        )

        async def logged_in(_browser, _identity):
            return True, state, "creator-fixture"

        task_id = "successful-xhs-creator-login"
        main.cfg = self.cfg
        main.browser = BrowserStub()
        main.engine = None
        try:
            with patch("app.main.interactive_xhs_creator_login", logged_in), \
                    patch("app.main._enrich_account_profile",
                          AsyncMock(return_value="ok")):
                asyncio.run(main._run_login(
                    task_id, creator=True, platform="xhs",
                    proxy_choice="none"))
            result = main.login_tasks[task_id]
            with db.get_session() as session:
                saved = session.get(DouyinAccount, result["account_id"])
                self.assertEqual(saved.creator_storage_state, state)
                self.assertEqual(saved.storage_state, "")
        finally:
            main.login_tasks.pop(task_id, None)
            main.cfg, main.browser, main.engine = (
                previous_cfg, previous_browser, previous_engine)

        self.assertEqual(result["status"], "confirmed")

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

    def test_repeated_open_browser_click_reuses_existing_window(self):
        with db.get_session() as session:
            account = DouyinAccount(
                nickname="xhs-resident", platform="xhs",
                identity_mode="native",
                profile_dir=str(Path(self.tmp.name) / "resident-profile"),
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            account_id = account.id

        class PageStub:
            def __init__(self):
                self.goto_calls = []
                self.front_calls = 0

            def is_closed(self):
                return False

            async def goto(self, url, **_kwargs):
                self.goto_calls.append(url)

            async def bring_to_front(self):
                self.front_calls += 1

        page = PageStub()
        lease = SimpleNamespace(
            active=True, page=page, context=object(),
            close=AsyncMock())

        class BrowserStub:
            def identity_for(self, account):
                return Identity.from_account(
                    account, self_outer.cfg.engine.profiles_dir, "DEFAULT_UA")

        self_outer = self
        previous_browser = main.browser
        main.browser = BrowserStub()
        main.open_browsers[account_id] = lease
        try:
            result = asyncio.run(main.open_account_browser(
                account_id,
                url="https://www.xiaohongshu.com/explore/resident"))
        finally:
            main.open_browsers.pop(account_id, None)
            main.browser = previous_browser

        self.assertTrue(result["reused"])
        self.assertEqual(page.goto_calls, [
            "https://www.xiaohongshu.com/explore/resident"])
        self.assertEqual(page.front_calls, 1)
        lease.close.assert_not_awaited()

    def test_manual_windows_for_different_accounts_do_not_share_lifetime_lock(self):
        account_ids = []
        with db.get_session() as session:
            for platform in ("xhs", "douyin"):
                account = DouyinAccount(
                    nickname=f"{platform}-window", platform=platform,
                    identity_mode="native",
                    profile_dir=str(Path(self.tmp.name) / f"{platform}-window"),
                )
                session.add(account)
                session.commit()
                session.refresh(account)
                account_ids.append(account.id)

        class Locator:
            first = None

            def __init__(self):
                self.first = self

            async def is_visible(self, **_kwargs):
                return False

        class PageStub:
            def __init__(self):
                self.url = "about:blank"

            async def goto(self, url, **_kwargs):
                self.url = url

            async def wait_for_timeout(self, _milliseconds):
                return None

            def get_by_text(self, *_args, **_kwargs):
                return Locator()

            async def bring_to_front(self):
                return None

            def on(self, *_args):
                return None

            def is_closed(self):
                return False

        class ContextStub:
            def __init__(self):
                self.page = PageStub()
                self.closed = False

            async def add_cookies(self, _cookies):
                return None

            async def new_page(self):
                return self.page

            async def close(self):
                self.closed = True

            def on(self, *_args):
                return None

        class BrowserStub:
            def __init__(self):
                self.locks = {}
                self.contexts = {}
                self.visible_lock = asyncio.Lock()

            def identity_for(self, account):
                return Identity.from_account(
                    account, self_outer.cfg.engine.profiles_dir, "DEFAULT_UA")

            def lock_for(self, key):
                return self.locks.setdefault(key, asyncio.Lock())

            @asynccontextmanager
            async def visible_action(self, _identity, **_kwargs):
                async with self.visible_lock:
                    yield

            async def open_headed(self, identity):
                return self.contexts.setdefault(identity.key, ContextStub())

            async def new_page(self, identity, **_kwargs):
                context = self.contexts.setdefault(
                    identity.key, ContextStub())
                return context.page

            async def close_context(self, key):
                context = self.contexts.get(key)
                if context is not None:
                    await context.close()

        class EngineStub:
            @asynccontextmanager
            async def operation_guard(self, *_args, **_kwargs):
                raise AssertionError(
                    "manual resident windows must not hold network guard")
                yield

        self_outer = self
        previous_browser, previous_engine = main.browser, main.engine
        browser = BrowserStub()
        main.browser, main.engine = browser, EngineStub()

        async def scenario():
            first = await main.open_account_browser(account_ids[0])
            second = await asyncio.wait_for(
                main.open_account_browser(account_ids[1]), timeout=0.5)
            self.assertTrue(first["ok"])
            self.assertTrue(second["ok"])
            self.assertTrue(main.open_browsers[account_ids[0]].active)
            self.assertTrue(main.open_browsers[account_ids[1]].active)
            await main.open_browsers[account_ids[0]].close()
            await main.open_browsers[account_ids[1]].close()

        try:
            asyncio.run(scenario())
        finally:
            for account_id in account_ids:
                main.open_browsers.pop(account_id, None)
            main.browser, main.engine = previous_browser, previous_engine

    def test_xhs_open_browser_closes_through_manager_and_visible_gate(self):
        with db.get_session() as session:
            account = DouyinAccount(
                nickname="xhs-fixture", platform="xhs", identity_mode="native",
                profile_dir=str(Path(self.tmp.name) / "xhs-open-profile"),
                creator_storage_state=(
                    '{"cookies":[{"name":"customer-sso-sid",'
                    '"value":"creator"}]}'),
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
            url = "https://creator.xiaohongshu.com/"

            async def goto(self, *_args, **_kwargs):
                self_outer.assertTrue(state["engine"])
                self_outer.assertTrue(state["visible"])

            async def wait_for_timeout(self, _milliseconds):
                return None

            def get_by_text(self, *_args, **_kwargs):
                class Locator:
                    first = None

                    def __init__(self):
                        self.first = self

                    async def is_visible(self, **_kwargs):
                        return True
                return Locator()

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
            async def visible_action(self, _identity, **_kwargs):
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
            self.assertFalse(result["logged_out"])
            self.assertEqual(result["login_state"], "authenticated")
            self.assertEqual(result["login_scope"], "creator")
            self.assertTrue(state["engine"])
            # The machine-wide XHS visible gate protects only startup and is
            # released while the user keeps this account window open.
            self.assertFalse(state["visible"])
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
        with db.get_session() as session:
            self.assertEqual(
                session.get(DouyinAccount, account_id).status, "active")

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
        manager._pw = _PatchrightStub()
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
        if __import__("os").name == "nt":
            self.assertTrue(kwargs["chromium_sandbox"])
        self.assertEqual(context.header_calls, [])
        self.assertEqual(context.script_calls, [])

    def test_native_proxy_launch_only_adds_webrtc_proxy_routing_flags(self):
        manager = BrowserManager("DEFAULT_UA", self.cfg.engine.profiles_dir)
        manager._pw = _PatchrightStub()
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

    def test_windows_legacy_launch_keeps_chromium_sandbox_enabled(self):
        manager = BrowserManager("DEFAULT_UA", self.cfg.engine.profiles_dir)
        manager._pw = _PatchrightStub()
        identity = Identity(
            account_id=2,
            profile_dir=str(Path(self.tmp.name) / "legacy-sandbox"),
            identity_mode="legacy",
            ua="LEGACY_UA",
        )

        asyncio.run(manager._launch_persistent(identity, headless=False))
        kwargs = manager._pw.chromium.kwargs

        self.assertNotIn("--no-sandbox", kwargs.get("args", []))
        if __import__("os").name == "nt":
            self.assertTrue(kwargs["chromium_sandbox"])

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
        manager._pw = _PatchrightStub()
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
            "backend": "patchright",
            "backend_label": "Patchright Chromium",
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
        manager._backend_by_key[identity.key] = "patchright"
        manager._fallback_reason_by_key[identity.key] = (
            "connect ws://127.0.0.1:43111/devtools/browser/fixture "
            "via http://alice:secret@proxy.local:8080")
        main.browser = manager
        try:
            body = asyncio.run(main.account_browser_environment(account_id))
        finally:
            main.browser = previous_browser

        dumped = repr(body)
        self.assertEqual(body["backend_label"], "Patchright Chromium · 回退")
        self.assertNotIn("secret", dumped)
        self.assertNotIn("ws://", dumped)
        self.assertNotIn("127.0.0.1:", dumped)

    def test_native_launch_captures_actual_context_user_agent(self):
        manager = BrowserManager("DEFAULT_UA", self.cfg.engine.profiles_dir)
        manager._pw = _PatchrightStub()
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
        manager._pw = _PatchrightStub()
        manager._pw.chromium.context.pages = [_PageStub()]

        asyncio.run(manager._launch_persistent(identity))

        with db.get_session() as session:
            self.assertEqual(
                session.get(DouyinAccount, account_id).ua,
                "ACTUAL_NATIVE_UA",
            )

    def test_legacy_launch_keeps_existing_identity_behavior(self):
        manager = BrowserManager("DEFAULT_UA", self.cfg.engine.profiles_dir)
        manager._pw = _PatchrightStub()
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
