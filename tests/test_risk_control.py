import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import app.db as db
from app.config import Config, RiskControlConfig
from app.models import (
    AccountRiskState,
    DouyinAccount,
    PublishTask,
    RiskEvent,
)
from app.risk import (
    OperationKind,
    RiskCategory,
    RiskController,
    classify_platform_error,
    network_key,
)


class RiskControlTests(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "risk.db"))
        self.cfg = Config()

    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def _account(self, *, timezone_id="Asia/Shanghai", proxy=""):
        with db.get_session() as session:
            account = DouyinAccount(
                nickname="fixture",
                timezone_id=timezone_id,
                proxy=proxy,
                status="active",
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            return account.id

    def test_risk_config_uses_conservative_defaults(self):
        cfg = RiskControlConfig()

        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.mode, "conservative")
        self.assertEqual(cfg.network_group_concurrency, 1)
        self.assertEqual(cfg.publish_daily_cap, 3)
        self.assertEqual(cfg.cooldown_steps_seconds, [1800, 7200, 21600, 86400])

    def test_new_risk_models_persist(self):
        account_id = self._account()

        with db.get_session() as session:
            state = AccountRiskState(account_id=account_id, risk_level=2)
            event = RiskEvent(
                account_id=account_id,
                network_key="direct",
                operation_kind="comment",
                outcome="risk",
                signal="http_429",
            )
            task = PublishTask(account_id=account_id, status="done", done_at=datetime.utcnow())
            session.add(state)
            session.add(event)
            session.add(task)
            session.commit()

            self.assertEqual(session.get(AccountRiskState, account_id).risk_level, 2)
            self.assertEqual(event.signal, "http_429")
            self.assertIsNotNone(task.done_at)

    def test_existing_account_identity_mode_defaults_to_legacy(self):
        account_id = self._account()

        with db.get_session() as session:
            self.assertEqual(session.get(DouyinAccount, account_id).identity_mode, "legacy")

    def test_network_key_groups_direct_and_hashes_proxy_credentials(self):
        self.assertEqual(network_key(""), "direct")
        first = network_key("http://user:secret@proxy.example:8080")
        second = network_key("http://user:secret@proxy.example:8080")

        self.assertEqual(first, second)
        self.assertNotIn("secret", first)
        self.assertTrue(first.startswith("proxy:"))

    def test_platform_error_classifier_distinguishes_risk_auth_and_network(self):
        for status in (403, 429, 461, 471):
            category, signal = classify_platform_error("", status_code=status)
            self.assertEqual(category, RiskCategory.RISK)
            self.assertEqual(signal, f"http_{status}")

        self.assertEqual(
            classify_platform_error("请完成验证码")[0], RiskCategory.RISK)
        self.assertEqual(
            classify_platform_error("登录态已失效")[0], RiskCategory.AUTH)
        self.assertEqual(
            classify_platform_error("ProxyError: connection timeout")[0],
            RiskCategory.NETWORK,
        )

    def test_platform_error_classifier_accepts_structured_enum_category(self):
        class StructuredError(Exception):
            category = RiskCategory.RISK
            signal = "captcha_required"

        category, signal = classify_platform_error(StructuredError("fixture"))

        self.assertEqual(category, RiskCategory.RISK)
        self.assertEqual(signal, "captcha_required")

    def test_risk_failures_escalate_cooldown_steps(self):
        account_id = self._account()
        controller = RiskController(self.cfg)
        now = datetime(2026, 8, 7, 0, 0, 0)

        expected = (1800, 7200, 21600, 86400, 86400)
        for seconds in expected:
            failure = controller.record_failure(
                account_id,
                OperationKind.COMMENT,
                "访问频繁",
                now=now,
            )
            self.assertEqual(failure.category, RiskCategory.RISK)
            self.assertEqual(failure.next_allowed_at, now + timedelta(seconds=seconds))
            now = failure.next_allowed_at

    def test_write_is_denied_during_cooldown(self):
        account_id = self._account()
        controller = RiskController(self.cfg)
        now = datetime(2026, 8, 7, 0, 0, 0)
        controller.record_failure(
            account_id, OperationKind.COMMENT, "HTTP 429", status_code=429, now=now)

        decision = controller.preflight(
            account_id, OperationKind.PUBLISH, now=now + timedelta(minutes=1))

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.signal, "cooldown")
        self.assertEqual(decision.next_allowed_at, now + timedelta(minutes=30))

    def test_daily_cap_uses_account_local_midnight(self):
        account_id = self._account(timezone_id="Asia/Shanghai")
        self.cfg.risk_control.publish_min_gap_seconds = 0
        self.cfg.risk_control.publish_hourly_cap = 0
        self.cfg.risk_control.shared_write_gap_seconds = 0
        self.cfg.engine.quiet_hours_enabled = False
        controller = RiskController(self.cfg)
        before_midnight_utc = datetime(2026, 8, 6, 15, 59, 0)
        after_midnight_utc = datetime(2026, 8, 6, 16, 1, 0)

        for minute in (0, 10, 20):
            controller.record_success(
                account_id,
                OperationKind.PUBLISH,
                now=before_midnight_utc - timedelta(minutes=minute),
            )

        decision = controller.preflight(
            account_id, OperationKind.PUBLISH, now=after_midnight_utc)

        self.assertTrue(decision.allowed)

    def test_three_spaced_light_reads_reduce_one_risk_level(self):
        account_id = self._account()
        controller = RiskController(self.cfg)
        start = datetime(2026, 8, 7, 0, 0, 0)
        controller.record_failure(
            account_id,
            OperationKind.READ_HEAVY,
            "HTTP 429",
            status_code=429,
            now=start,
        )

        for offset in (31, 42, 53):
            controller.record_success(
                account_id,
                OperationKind.READ_LIGHT,
                now=start + timedelta(minutes=offset),
            )

        with db.get_session() as session:
            state = session.get(AccountRiskState, account_id)
            self.assertEqual(state.risk_level, 0)
            self.assertEqual(state.recovery_successes, 0)


if __name__ == "__main__":
    unittest.main()
