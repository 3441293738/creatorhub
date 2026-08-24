import tempfile
import unittest
import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from sqlmodel import select

import app.db as db
import app.main as main
from app.browser.identity import Identity
from app.config import Config, RiskControlConfig, load_config
from app.models import (
    AccountRiskState,
    CommentTask,
    CommentWatch,
    ContentRecord,
    DouyinAccount,
    MonitorTarget,
    ProxyPool,
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

    def test_unknown_risk_mode_falls_back_to_conservative(self):
        config_path = Path(self.tmp.name) / "config.yaml"
        config_path.write_text(
            "risk_control:\n  mode: Conservativ\n"
            "  publish_min_gap_seconds: 0\n"
            "  network_group_concurrency: 99\n",
            encoding="utf-8",
        )
        cfg = load_config(str(config_path))
        controller = RiskController(cfg)

        self.assertEqual(cfg.risk_control.mode, "conservative")
        self.assertEqual(controller._limits(OperationKind.PUBLISH), (7200, 1, 3))
        self.assertEqual(controller._network_concurrency(), 1)

    def test_explicit_custom_risk_mode_is_normalized(self):
        config_path = Path(self.tmp.name) / "config.yaml"
        config_path.write_text(
            "risk_control:\n  mode: ' CUSTOM '\n  publish_min_gap_seconds: 0\n",
            encoding="utf-8",
        )

        cfg = load_config(str(config_path))

        self.assertEqual(cfg.risk_control.mode, "custom")
        self.assertEqual(RiskController(cfg)._limits(OperationKind.PUBLISH)[0], 0)

    def test_invalid_timezone_falls_back_to_shanghai_day_boundary(self):
        account_id = self._account(timezone_id="invalid/timezone")
        with db.get_session() as session:
            account = session.get(DouyinAccount, account_id)

        start = RiskController(self.cfg)._local_day_start_utc(
            account, datetime(2026, 8, 7, 1, 0, 0))

        self.assertEqual(start, datetime(2026, 8, 6, 16, 0, 0))

    def test_conservative_mode_enforces_hard_write_ceilings(self):
        policy = self.cfg.risk_control
        policy.comment_min_gap_seconds = 0
        policy.comment_hourly_cap = 99
        policy.comment_daily_cap = 0
        policy.shared_write_gap_seconds = 1
        policy.combined_action_hourly_cap = 99
        policy.combined_action_daily_cap = 0
        controller = RiskController(self.cfg)

        self.assertEqual(controller._limits(OperationKind.COMMENT), (600, 3, 10))
        self.assertEqual(controller._shared_write_gap(), 300)
        self.assertEqual(controller._combined_action_caps(), (3, 10))

        policy.mode = "custom"
        self.assertEqual(controller._limits(OperationKind.COMMENT), (0, 99, 0))
        self.assertEqual(controller._shared_write_gap(), 1)
        self.assertEqual(controller._combined_action_caps(), (99, 0))

    def test_programmatic_mode_uses_custom_as_the_only_floor_opt_out(self):
        policy = self.cfg.risk_control
        policy.mode = " Typo "
        policy.publish_min_gap_seconds = 0
        policy.publish_hourly_cap = 99
        policy.publish_daily_cap = 0
        policy.shared_write_gap_seconds = 1
        policy.combined_action_hourly_cap = 99
        policy.combined_action_daily_cap = 0
        policy.network_group_concurrency = 8
        controller = RiskController(self.cfg)

        self.assertEqual(controller._limits(OperationKind.PUBLISH), (7200, 1, 3))
        self.assertEqual(controller._shared_write_gap(), 300)
        self.assertEqual(controller._combined_action_caps(), (3, 10))
        self.assertEqual(controller._network_concurrency(), 1)

        policy.mode = " CUSTOM "
        self.assertEqual(controller._limits(OperationKind.PUBLISH), (0, 99, 0))
        self.assertEqual(controller._shared_write_gap(), 1)
        self.assertEqual(controller._combined_action_caps(), (99, 0))
        self.assertEqual(controller._network_concurrency(), 8)

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

    def test_network_key_normalizes_equivalent_proxy_urls(self):
        self.assertEqual(
            network_key("proxy.example:8080"),
            network_key("http://proxy.example:8080"),
        )

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
        for transport_error in (
                TimeoutError("opaque timeout"),
                ConnectionError("opaque connection failure")):
            self.assertEqual(
                classify_platform_error(transport_error)[0],
                RiskCategory.NETWORK,
            )
        self.assertEqual(
            classify_platform_error(RuntimeError("parser failed"))[0],
            RiskCategory.BUSINESS,
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

    def test_two_consecutive_network_failures_disable_bound_proxy(self):
        proxy = "http://proxy.example:8080"
        account_id = self._account(proxy=proxy)
        with db.get_session() as session:
            session.add(ProxyPool(url=proxy, status="ok"))
            session.commit()
        controller = RiskController(self.cfg)

        controller.record_failure(
            account_id, OperationKind.READ_LIGHT, "connection timeout")
        with db.get_session() as session:
            self.assertNotEqual(session.get(DouyinAccount, account_id).proxy_status, "bad")

        controller.record_failure(
            account_id, OperationKind.READ_LIGHT, "connection timeout")

        with db.get_session() as session:
            self.assertEqual(session.get(DouyinAccount, account_id).proxy_status, "bad")
            proxy_row = session.exec(select(ProxyPool).where(ProxyPool.url == proxy)).one()
            self.assertEqual(proxy_row.status, "bad")

    def test_platform_http_500_does_not_disable_proxy(self):
        proxy = "http://proxy.example:8080"
        account_id = self._account(proxy=proxy)
        controller = RiskController(self.cfg)

        controller.record_failure(
            account_id, OperationKind.READ_LIGHT, "server error", status_code=500)
        controller.record_failure(
            account_id, OperationKind.READ_LIGHT, "server error", status_code=500)

        with db.get_session() as session:
            account = session.get(DouyinAccount, account_id)
            state = session.get(AccountRiskState, account_id)
            self.assertNotEqual(account.proxy_status, "bad")
            self.assertEqual(state.consecutive_network_failures, 0)

    def test_repeated_proxy_auth_failures_use_auth_error_status(self):
        proxy = "http://proxy.example:8080"
        account_id = self._account(proxy=proxy)
        with db.get_session() as session:
            session.add(ProxyPool(url=proxy, status="ok"))
            session.commit()
        controller = RiskController(self.cfg)

        controller.record_failure(
            account_id, OperationKind.READ_LIGHT, "proxy auth", status_code=407)
        controller.record_failure(
            account_id, OperationKind.READ_LIGHT, "proxy auth", status_code=407)

        with db.get_session() as session:
            account = session.get(DouyinAccount, account_id)
            proxy_row = session.exec(
                select(ProxyPool).where(ProxyPool.url == proxy)).one()
            self.assertEqual(account.proxy_status, "auth_error")
            self.assertEqual(proxy_row.status, "auth_error")

    def test_network_failure_counter_is_scoped_to_normalized_proxy(self):
        first = "http://proxy-a.example:8080"
        second = "http://proxy-b.example:8080"
        account_id = self._account(proxy=first)
        controller = RiskController(self.cfg)
        controller.record_failure(
            account_id, OperationKind.READ_LIGHT, "connection timeout")
        with db.get_session() as session:
            account = session.get(DouyinAccount, account_id)
            account.proxy = second
            session.add(account)
            session.commit()

        controller.record_failure(
            account_id, OperationKind.READ_LIGHT, "connection timeout")

        with db.get_session() as session:
            account = session.get(DouyinAccount, account_id)
            state = session.get(AccountRiskState, account_id)
            self.assertNotEqual(account.proxy_status, "bad")
            self.assertEqual(state.consecutive_network_failures, 1)
            self.assertEqual(state.network_failure_key, network_key(second))

    def test_success_resets_consecutive_network_failures(self):
        account_id = self._account(proxy="http://proxy.example:8080")
        controller = RiskController(self.cfg)
        controller.record_failure(
            account_id, OperationKind.READ_LIGHT, "connection timeout")
        controller.record_success(account_id, OperationKind.READ_LIGHT)
        controller.record_failure(
            account_id, OperationKind.READ_LIGHT, "connection timeout")

        with db.get_session() as session:
            state = session.get(AccountRiskState, account_id)
            self.assertEqual(state.consecutive_network_failures, 1)
            self.assertNotEqual(session.get(DouyinAccount, account_id).proxy_status, "bad")

    def test_next_write_at_combines_cooldown_and_shared_gap(self):
        account_id = self._account()
        controller = RiskController(self.cfg)
        now = datetime(2026, 8, 7, 0, 0, 0)
        controller.record_success(account_id, OperationKind.COMMENT, now=now)

        self.assertEqual(
            controller.next_write_at(account_id),
            now + timedelta(seconds=300),
        )

    def test_invalid_account_blocks_platform_operations_until_relogin(self):
        account_id = self._account()
        controller = RiskController(self.cfg)
        controller.record_failure(
            account_id, OperationKind.READ_LIGHT, "logged_out")

        decision = controller.preflight(account_id, OperationKind.READ_LIGHT)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.signal, "auth_required")

    def test_manual_probe_can_recheck_inconclusive_invalid_account(self):
        account_id = self._account()
        with db.get_session() as session:
            account = session.get(DouyinAccount, account_id)
            account.status = "invalid"
            session.add(account)
            session.commit()

        decision = RiskController(self.cfg).preflight(
            account_id, OperationKind.READ_LIGHT,
            allow_invalid_probe=True)

        self.assertTrue(decision.allowed)

    def test_bad_bound_proxy_blocks_future_platform_operations(self):
        account_id = self._account(proxy="http://proxy.example:8080")
        with db.get_session() as session:
            account = session.get(DouyinAccount, account_id)
            account.proxy_status = "bad"
            session.add(account)
            session.commit()

        decision = RiskController(self.cfg).preflight(
            account_id, OperationKind.READ_LIGHT)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.signal, "proxy_unavailable")

    def test_disabled_controller_does_not_persist_failure_side_effects(self):
        proxy = "http://proxy.example:8080"
        account_id = self._account(proxy=proxy)
        self.cfg.risk_control.enabled = False
        controller = RiskController(self.cfg)

        first = controller.record_failure(
            account_id, OperationKind.READ_LIGHT, "connection timeout")
        controller.record_failure(
            account_id, OperationKind.READ_LIGHT, "logged_out")
        controller.record_success(account_id, OperationKind.READ_LIGHT)

        self.assertFalse(first.controlled)
        with db.get_session() as session:
            account = session.get(DouyinAccount, account_id)
            self.assertEqual(account.status, "active")
            self.assertNotEqual(account.proxy_status, "bad")
            self.assertIsNone(session.get(AccountRiskState, account_id))
            self.assertEqual(session.exec(select(RiskEvent)).all(), [])

    def test_disabled_controller_does_not_serialize_network_exit(self):
        first_id = self._account()
        second_id = self._account()
        self.cfg.risk_control.enabled = False
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
        self.cfg.risk_control.mode = "custom"
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

    def test_upgrade_day_counts_legacy_completed_write_tasks(self):
        account_id = self._account()
        self.cfg.risk_control.mode = "custom"
        self.cfg.risk_control.comment_min_gap_seconds = 0
        self.cfg.risk_control.comment_hourly_cap = 0
        self.cfg.risk_control.comment_daily_cap = 1
        self.cfg.risk_control.shared_write_gap_seconds = 0
        self.cfg.engine.quiet_hours_enabled = False
        now = datetime(2026, 8, 7, 3, 0, 0)
        with db.get_session() as session:
            session.add(CommentTask(
                account_id=account_id, status="done", content="legacy",
                done_at=now - timedelta(hours=1)))
            session.commit()

        decision = RiskController(self.cfg).preflight(
            account_id, OperationKind.COMMENT, now=now)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.signal, "daily_cap")

    def test_upgrade_day_counts_legacy_publish_created_at_when_done_at_missing(self):
        account_id = self._account()
        self.cfg.risk_control.mode = "custom"
        self.cfg.risk_control.publish_min_gap_seconds = 0
        self.cfg.risk_control.publish_hourly_cap = 0
        self.cfg.risk_control.publish_daily_cap = 1
        self.cfg.risk_control.shared_write_gap_seconds = 0
        self.cfg.engine.quiet_hours_enabled = False
        now = datetime(2026, 8, 7, 3, 0, 0)
        with db.get_session() as session:
            session.add(PublishTask(
                account_id=account_id,
                status="done",
                created_at=now - timedelta(hours=1),
                done_at=None,
            ))
            session.commit()

        decision = RiskController(self.cfg).preflight(
            account_id, OperationKind.PUBLISH, now=now)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.signal, "daily_cap")

    def test_legacy_publish_without_done_at_enforces_min_gap(self):
        account_id = self._account()
        self.cfg.risk_control.mode = "custom"
        self.cfg.risk_control.publish_min_gap_seconds = 3600
        self.cfg.risk_control.publish_hourly_cap = 0
        self.cfg.risk_control.publish_daily_cap = 0
        self.cfg.risk_control.shared_write_gap_seconds = 0
        self.cfg.engine.quiet_hours_enabled = False
        now = datetime(2026, 8, 7, 3, 0, 0)
        with db.get_session() as session:
            session.add(PublishTask(
                account_id=account_id,
                status="done",
                created_at=now - timedelta(hours=2),
                done_at=None,
            ))
            session.flush()
            session.add(PublishTask(
                account_id=account_id,
                status="done",
                created_at=now - timedelta(minutes=30),
                done_at=None,
            ))
            session.commit()

        decision = RiskController(self.cfg).preflight(
            account_id, OperationKind.PUBLISH, now=now)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.signal, "kind_gap")

    def test_risk_event_pruning_uses_a_monotonic_day_watermark(self):
        engine = MonitorEngine(self.cfg, _BrowserStub())
        calls = []
        engine.risk.prune_events = lambda **kwargs: calls.append(kwargs["now"]) or 2
        day_one = datetime(2026, 8, 7, 1, 0, 0)
        day_two = datetime(2026, 8, 8, 1, 0, 0)
        engine._last_risk_prune_day = None

        self.assertEqual(engine._prune_risk_events_if_due(day_one), 2)
        self.assertEqual(engine._prune_risk_events_if_due(day_two), 2)
        self.assertEqual(
            engine._prune_risk_events_if_due(day_one + timedelta(hours=2)), 0)
        self.assertEqual(calls, [day_one, day_two])

    def test_risk_event_pruning_failure_does_not_escape_scheduler(self):
        engine = MonitorEngine(self.cfg, _BrowserStub())
        engine._last_risk_prune_day = None
        calls = []

        def fail(**kwargs):
            calls.append(kwargs["now"])
            raise RuntimeError("db")

        engine.risk.prune_events = fail
        day_one = datetime(2026, 8, 7, 1, 0, 0)
        day_two = datetime(2026, 8, 8, 1, 0, 0)

        self.assertEqual(engine._prune_risk_events_if_due(day_one), 0)
        self.assertEqual(
            engine._prune_risk_events_if_due(day_one + timedelta(hours=2)), 0)
        self.assertEqual(engine._prune_risk_events_if_due(day_two), 0)
        self.assertEqual(calls, [day_one, day_two])

    def test_monitor_idle_collection_uses_browser_manager_collector(self):
        class Browser(_BrowserStub):
            def __init__(self):
                super().__init__()
                self.samples = []

            async def collect_idle_cdp(self, *, now=None):
                self.samples.append(now)
                return 2

        browser = Browser()
        engine = MonitorEngine(self.cfg, browser)

        closed = asyncio.run(engine._collect_idle_browser_sessions(now=123.5))

        self.assertEqual(closed, 2)
        self.assertEqual(browser.samples, [123.5])

    def test_lifespan_prunes_once_and_updates_same_day_watermark(self):
        previous_browser = main.browser
        previous_engine = main.engine
        previous_receiver = main.im_receiver

        class BrowserStub:
            def __init__(self):
                self._locks = {}

            async def start(self):
                return None

            async def stop(self):
                return None

            def lock_for(self, key):
                return self._locks.setdefault(key, asyncio.Lock())

        class ReceiverStub:
            async def stop_all(self):
                return None

        try:
            with patch("app.main.init_db"), \
                    patch("app.main._backfill_danmaku_records", return_value=0), \
                    patch("app.main._backfill_share_download_history", return_value=0), \
                    patch("app.main.seed_proxy_pool", return_value=0), \
                    patch("app.main.migrate_identities", return_value=0), \
                    patch("app.main.BrowserManager", return_value=BrowserStub()), \
                    patch.object(MonitorEngine, "start", autospec=True), \
                    patch.object(RiskController, "prune_events", autospec=True,
                                 return_value=0) as prune_events, \
                    patch("app.engine.im_receiver.ImReceiverManager",
                          return_value=ReceiverStub()):
                async def scenario():
                    async with main.lifespan(main.app):
                        startup_now = prune_events.call_args.kwargs["now"]
                        self.assertEqual(
                            main.engine._prune_risk_events_if_due(
                                startup_now), 0)

                asyncio.run(scenario())
            self.assertEqual(prune_events.call_count, 1)
        finally:
            main.browser = previous_browser
            main.engine = previous_engine
            main.im_receiver = previous_receiver

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
        self.cfg.risk_control.network_group_concurrency = 4
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

    def test_fresh_login_guard_uses_selected_proxy_exit(self):
        engine = MonitorEngine(self.cfg, _BrowserStub())
        identities = [
            Identity(None, str(Path(self.tmp.name) / "login-a"),
                     identity_mode="native", proxy="http://proxy-a.example:8080"),
            Identity(None, str(Path(self.tmp.name) / "login-b"),
                     identity_mode="native", proxy="http://proxy-b.example:8080"),
        ]

        async def scenario():
            active = 0
            peak = 0

            async def worker(index, identity):
                nonlocal active, peak
                async with engine.operation_guard(
                        None, OperationKind.LOGIN,
                        fallback_key=f"login:{index}",
                        operation_target=identity):
                    active += 1
                    peak = max(peak, active)
                    await asyncio.sleep(0.02)
                    active -= 1

            await asyncio.gather(*(
                worker(index, identity)
                for index, identity in enumerate(identities)))
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

    def test_xhs_download_retry_refetch_respects_heavy_read_budget(self):
        account_id = self._account()
        with db.get_session() as session:
            target = MonitorTarget(
                platform="xhs", sec_uid="fixture", account_id=account_id,
                enabled=True)
            session.add(target)
            session.commit()
            session.refresh(target)
            record = ContentRecord(
                platform="xhs", target_id=target.id, aweme_id="fixture-note",
                media_json="", download_status="failed")
            session.add(record)
            session.commit()
            session.refresh(record)
            record_id = record.id
        engine = MonitorEngine(self.cfg, _BrowserStub())
        engine.risk.record_success(account_id, OperationKind.READ_HEAVY)
        calls = 0

        class ClientStub:
            async def note_detail(self, *_args, **_kwargs):
                nonlocal calls
                calls += 1
                return {}

        engine._xhs_client = lambda *_args: ClientStub()

        result = asyncio.run(engine.retry_download(record_id))

        self.assertFalse(result["ok"])
        self.assertEqual(calls, 0)
        self.assertIn("risk_deferred:", result["error"])
        with db.get_session() as session:
            record = session.get(ContentRecord, record_id)
            self.assertEqual(record.retry_count, 0)
            self.assertEqual(record.download_status, "failed")

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

    def test_read_budget_is_rechecked_after_serialization_lock(self):
        account_id = self._account()
        engine = MonitorEngine(self.cfg, _BrowserStub())
        calls = 0

        async def operation():
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)
            return ["ok"], ""

        async def scenario():
            return await asyncio.gather(*(
                engine.guarded_read_pair(
                    account_id, OperationKind.READ_HEAVY, "fixture", operation,
                    empty_result=[])
                for _ in range(2)
            ))

        results = asyncio.run(scenario())

        self.assertEqual(calls, 1)
        self.assertEqual(sum(1 for payload, _error in results if payload == ["ok"]), 1)
        self.assertEqual(sum(
            1 for _payload, error in results if error.startswith("risk_deferred:")), 1)


if __name__ == "__main__":
    unittest.main()
