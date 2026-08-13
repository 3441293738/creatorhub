import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app.db as db
from app.browser.identity import Identity
from app.browser.manager import (
    BrowserManager,
    BrowserProfileConflictError,
    _ProfileProcessLock,
)
from app.config import Config
from app.models import AccountRiskState, DouyinAccount
from app.risk import OperationKind, RiskController


class NativeWriteSafetyTests(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "native-write.db"))
        self.cfg = Config()

    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def test_profile_lock_blocks_a_second_process_owner(self):
        profile = Path(self.tmp.name) / "profile"
        first = _ProfileProcessLock(profile)
        second = _ProfileProcessLock(profile)
        first.acquire()
        try:
            with self.assertRaises(BrowserProfileConflictError):
                second.acquire()
        finally:
            first.release()
        second.acquire()
        second.release()

    def test_native_write_gate_requires_chrome_and_fresh_proxy_baseline(self):
        manager = BrowserManager("UA")
        manager._pw = object()
        account = SimpleNamespace(
            identity_mode="native",
            proxy="http://proxy.example:8080",
            proxy_status="unknown",
            exit_ip="",
            exit_proxy_signature="",
            exit_checked_at=None,
        )

        self.assertIn("系统稳定版 Chrome", manager.native_write_gate_error(account))
        manager._browser_channel = "chrome"
        self.assertIn("尚未通过", manager.native_write_gate_error(account))

        account.proxy_status = "ok"
        account.exit_ip = "203.0.113.10"
        account.exit_proxy_signature = manager.proxy_signature(account.proxy)
        account.exit_checked_at = datetime.utcnow()
        self.assertEqual(manager.native_write_gate_error(account), "")

        account.exit_checked_at = datetime.utcnow() - timedelta(days=2)
        self.assertIn("已过期", manager.native_write_gate_error(account))

    def test_legacy_account_is_not_changed_by_native_gate(self):
        manager = BrowserManager("UA")
        account = SimpleNamespace(identity_mode="legacy")
        self.assertEqual(manager.native_write_gate_error(account), "")

    def test_browser_exit_probe_uses_the_account_context(self):
        class Page:
            url = "https://ipinfo.io/json"

            async def goto(self, *_args, **_kwargs):
                return SimpleNamespace(status=200)

            def locator(self, _selector):
                return self

            async def inner_text(self, **_kwargs):
                return ('{"ip":"203.0.113.8","country":"cn",'
                        '"org":"AS64500 Fixture","timezone":"Asia/Shanghai",'
                        '"city":"Shanghai"}')

            async def close(self):
                return None

        page = Page()
        context = SimpleNamespace(new_page=AsyncMock(return_value=page))
        manager = BrowserManager("UA")
        identity = Identity(
            account_id=1,
            profile_dir=str(Path(self.tmp.name) / "probe-profile"),
            identity_mode="native",
        )

        with patch.object(manager, "context_for", AsyncMock(return_value=context)):
            result = asyncio.run(manager.probe_browser_exit(identity))

        self.assertEqual(result["ip"], "203.0.113.8")
        self.assertEqual(result["country"], "CN")
        self.assertEqual(result["asn"], "AS64500")
        context.new_page.assert_awaited_once()

    def test_distinct_native_accounts_trigger_shared_exit_circuit_breaker(self):
        proxy = "http://proxy.example:8080"
        with db.get_session() as session:
            accounts = [
                DouyinAccount(
                    nickname=f"native-{index}", identity_mode="native",
                    proxy=proxy, status="active")
                for index in range(3)
            ]
            other = DouyinAccount(
                nickname="other", identity_mode="native",
                proxy="http://other.example:8080", status="active")
            session.add_all([*accounts, other])
            session.commit()
            ids = [row.id for row in accounts]
            other_id = other.id

        controller = RiskController(self.cfg)
        now = datetime(2026, 8, 13, 3, 0, 0)
        controller.record_failure(
            ids[0], OperationKind.COMMENT, "HTTP 429", now=now)
        decision = controller.record_failure(
            ids[1], OperationKind.PUBLISH, "安全验证", now=now)

        self.assertEqual(
            decision.next_allowed_at,
            now + timedelta(seconds=self.cfg.risk_control.network_group_cooldown_seconds),
        )
        with db.get_session() as session:
            untouched = session.get(DouyinAccount, other_id)
            self.assertIsNone(untouched.write_paused_until)
            for account_id in ids:
                account = session.get(DouyinAccount, account_id)
                state = session.get(AccountRiskState, account_id)
                self.assertEqual(account.write_paused_until,
                                 decision.next_allowed_at)
                self.assertGreaterEqual(state.risk_level, 1)
                self.assertIn("出口组熔断", account.write_pause_reason)


if __name__ == "__main__":
    unittest.main()
