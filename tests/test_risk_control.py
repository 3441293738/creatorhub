import tempfile
import unittest
import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import app.db as db
from app.config import Config, RiskControlConfig, load_config
from app.models import (
    AccountRiskState,
    CommentWatch,
    DouyinAccount,
    MonitorTarget,
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
from app.engine.monitor import MonitorEngine, _round_robin_by_account


class _BrowserStub:
    def __init__(self):
        self._locks = {}

    def lock_for(self, key):
        return self._locks.setdefault(key, asyncio.Lock())


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

    def test_risk_config_yaml_override_preserves_other_defaults(self):
        config_path = Path(self.tmp.name) / "config.yaml"
        config_path.write_text(
            """
engine:
  media_dir: ./media
  profiles_dir: ./profiles
risk_control:
  publish_daily_cap: 2
  network_group_concurrency: 1
  unknown_future_key: ignored
""".strip(),
            encoding="utf-8",
        )

        cfg = load_config(str(config_path))

        self.assertEqual(cfg.risk_control.publish_daily_cap, 2)
        self.assertEqual(cfg.risk_control.comment_daily_cap, 10)
        self.assertEqual(cfg.risk_control.cooldown_steps_seconds,
                         [1800, 7200, 21600, 86400])

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

    def test_network_guard_serializes_accounts_on_same_exit(self):
        first_id = self._account(proxy="")
        second_id = self._account(proxy="")
        controller = RiskController(self.cfg)

        async def scenario():
            active = 0
            peak = 0

            async def worker(account_id):
                nonlocal active, peak
                async with controller.network_guard(account_id):
                    active += 1
                    peak = max(peak, active)
                    await asyncio.sleep(0.02)
                    active -= 1

            await asyncio.gather(worker(first_id), worker(second_id))
            return peak

        self.assertEqual(asyncio.run(scenario()), 1)

    def test_network_guard_allows_distinct_proxy_exits_to_overlap(self):
        first_id = self._account(proxy="http://proxy-a.example:8000")
        second_id = self._account(proxy="http://proxy-b.example:8000")
        controller = RiskController(self.cfg)

        async def scenario():
            active = 0
            peak = 0

            async def worker(account_id):
                nonlocal active, peak
                async with controller.network_guard(account_id):
                    active += 1
                    peak = max(peak, active)
                    await asyncio.sleep(0.02)
                    active -= 1

            await asyncio.gather(worker(first_id), worker(second_id))
            return peak

        self.assertEqual(asyncio.run(scenario()), 2)

    def test_heavy_read_budget_and_recovery_probe(self):
        account_id = self._account()
        controller = RiskController(self.cfg)
        now = datetime(2026, 8, 7, 0, 0, 0)
        controller.record_success(account_id, OperationKind.READ_HEAVY, now=now)

        too_soon = controller.preflight(
            account_id, OperationKind.READ_HEAVY, now=now + timedelta(seconds=30))
        self.assertFalse(too_soon.allowed)
        self.assertEqual(too_soon.signal, "kind_gap")

        controller.record_failure(
            account_id, OperationKind.READ_HEAVY, "HTTP 429",
            status_code=429, now=now + timedelta(minutes=2))
        after_cooldown = now + timedelta(minutes=33)
        self.assertFalse(controller.preflight(
            account_id, OperationKind.READ_HEAVY, now=after_cooldown).allowed)
        self.assertTrue(controller.preflight(
            account_id, OperationKind.READ_LIGHT, now=after_cooldown).allowed)

    def test_due_targets_are_round_robin_by_account(self):
        rows = [(1, 10), (2, 10), (3, 20), (4, 20), (5, None)]

        ordered = _round_robin_by_account(rows)

        self.assertEqual(ordered, [
            (1, 10), (3, 20), (5, None), (2, 10), (4, 20)])

    def test_scan_target_respects_light_read_budget_without_advancing_scan(self):
        account_id = self._account()
        with db.get_session() as session:
            target = MonitorTarget(
                platform="douyin", sec_uid="fixture", account_id=account_id,
                interval_seconds=60, enabled=True)
            session.add(target)
            session.commit()
            session.refresh(target)
            target_id = target.id

        engine = MonitorEngine(self.cfg, _BrowserStub())
        engine.risk.record_success(account_id, OperationKind.READ_LIGHT)

        async def unexpected(_target_id):
            raise AssertionError("platform read should have been deferred")

        engine._scan_target_locked = unexpected
        result = asyncio.run(engine.scan_target(target_id))

        self.assertTrue(result["skipped"])
        with db.get_session() as session:
            self.assertIsNone(session.get(MonitorTarget, target_id).last_scan_at)

    def test_comment_watch_respects_heavy_read_budget(self):
        account_id = self._account()
        with db.get_session() as session:
            watch = CommentWatch(
                platform="douyin", kind="video", aweme_id="fixture",
                account_id=account_id, enabled=True)
            session.add(watch)
            session.commit()
            session.refresh(watch)
            watch_id = watch.id

        engine = MonitorEngine(self.cfg, _BrowserStub())
        engine.risk.record_success(account_id, OperationKind.READ_HEAVY)

        async def unexpected(_watch_id):
            raise AssertionError("heavy platform read should have been deferred")

        engine._scan_comment_watch_locked = unexpected
        result = asyncio.run(engine.scan_comment_watch(watch_id))

        self.assertTrue(result["skipped"])
        with db.get_session() as session:
            self.assertIsNone(session.get(CommentWatch, watch_id).last_scan_at)

    def test_direct_read_pair_uses_same_budget(self):
        account_id = self._account()
        engine = MonitorEngine(self.cfg, _BrowserStub())
        engine.risk.record_success(account_id, OperationKind.READ_HEAVY)

        async def unexpected():
            raise AssertionError("direct read should have been deferred")

        rows, error = asyncio.run(engine.guarded_read_pair(
            account_id, OperationKind.READ_HEAVY, "fixture-direct", unexpected,
            empty_result=[]))

        self.assertEqual(rows, [])
        self.assertTrue(error.startswith("risk_deferred:"))


if __name__ == "__main__":
    unittest.main()
