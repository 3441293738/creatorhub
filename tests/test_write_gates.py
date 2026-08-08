import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import app.db as db
from app.config import Config, EngineConfig
from app.engine.monitor import MonitorEngine
from app.risk import OperationKind
from app.models import (
    AccountActionTask,
    CommentRule,
    CommentTask,
    DmConversation,
    DouyinAccount,
    PublishTask,
)
from sqlmodel import select


class _BrowserStub:
    def __init__(self):
        self._locks = {}
        self.identity_calls = 0
        self.anon_calls = 0

    def lock_for(self, key):
        return self._locks.setdefault(key, asyncio.Lock())

    def identity_for(self, _account):
        self.identity_calls += 1
        return None

    def anon_identity(self):
        self.anon_calls += 1
        return None


class WriteGateTests(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "gates.db"))
        self.cfg = Config(engine=EngineConfig(
            media_dir=str(Path(self.tmp.name) / "media"),
            profiles_dir=str(Path(self.tmp.name) / "profiles"),
            quiet_hours_enabled=False,
            comment_daily_cap_per_account=10,
            comment_hourly_cap_per_account=1,
            comment_min_gap_seconds=60,
            action_daily_cap_per_account=10,
            action_hourly_cap_per_account=1,
            action_min_gap_seconds=60,
            comment_risk_cooldown_seconds=3600,
        ))

    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def _account(self, platform="douyin"):
        with db.get_session() as session:
            account = DouyinAccount(
                platform=platform, nickname="fixture", status="active",
                storage_state='{"cookies": []}',
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            return account.id

    def _comment_task(self, account_id):
        with db.get_session() as session:
            task = CommentTask(
                platform="douyin", account_id=account_id,
                aweme_id="fixture-work", target_comment_id="fixture-comment",
                target_nick="visitor", target_text="hello",
                content="thanks", status="pending",
            )
            session.add(task)
            session.commit()
            session.refresh(task)
            return task.id

    def _xhs_comment_task(self, account_id):
        with db.get_session() as session:
            task = CommentTask(
                platform="xhs", account_id=account_id,
                aweme_id="fixture-note", xsec_token="fixture-token",
                target_comment_id="fixture-comment", target_nick="visitor",
                target_text="hello", content="thanks", status="pending",
            )
            session.add(task)
            session.commit()
            session.refresh(task)
            return task.id

    def _action_task(self, account_id):
        with db.get_session() as session:
            task = AccountActionTask(
                platform="douyin", account_id=account_id, action="follow",
                target_uid="fixture-user", status="pending",
            )
            session.add(task)
            session.commit()
            session.refresh(task)
            return task.id

    def _publish_task(self, account_id, *, platform="douyin"):
        with db.get_session() as session:
            task = PublishTask(
                platform=platform,
                account_id=account_id,
                media_type="images",
                media_json="[]",
                status="pending",
            )
            session.add(task)
            session.commit()
            session.refresh(task)
            return task.id

    def test_manual_comment_execution_keeps_task_queued_outside_active_window(self):
        account_id = self._account()
        task_id = self._comment_task(account_id)
        engine = MonitorEngine(self.cfg, _BrowserStub())
        engine._in_active_window = lambda *_args: False

        result = asyncio.run(engine.execute_comment_task(task_id))

        self.assertFalse(result["ok"])
        with db.get_session() as session:
            task = session.get(CommentTask, task_id)
            self.assertEqual(task.status, "pending")

    def test_manual_action_execution_uses_same_write_gate(self):
        account_id = self._account()
        task_id = self._action_task(account_id)
        engine = MonitorEngine(self.cfg, _BrowserStub())
        engine._in_active_window = lambda *_args: False

        result = asyncio.run(engine.execute_action_task(task_id))

        self.assertFalse(result["ok"])
        with db.get_session() as session:
            task = session.get(AccountActionTask, task_id)
            self.assertEqual(task.status, "pending")

    def test_risk_pause_blocks_and_persists_until_cleared(self):
        account_id = self._account()
        engine = MonitorEngine(self.cfg, _BrowserStub())
        engine.risk.record_failure(
            account_id, OperationKind.COMMENT, "HTTP 429")

        message = engine._comment_gate_error(account_id)
        self.assertIn("暂停至", message)
        with db.get_session() as session:
            account = session.get(DouyinAccount, account_id)
            self.assertIsNotNone(account.write_paused_until)
        engine.risk.clear_account(account_id)
        self.assertEqual(engine._comment_gate_error(account_id), "")

    def test_publish_outside_active_window_stays_pending(self):
        account_id = self._account()
        task_id = self._publish_task(account_id)
        engine = MonitorEngine(self.cfg, _BrowserStub())
        engine._in_active_window = lambda *_args: False

        result = asyncio.run(engine.publish_task(task_id))

        self.assertFalse(result["ok"])
        with db.get_session() as session:
            task = session.get(PublishTask, task_id)
            self.assertEqual(task.status, "pending")

    def test_publish_during_risk_pause_stays_pending(self):
        account_id = self._account()
        task_id = self._publish_task(account_id)
        engine = MonitorEngine(self.cfg, _BrowserStub())
        engine.risk.record_failure(
            account_id, OperationKind.COMMENT, "HTTP 429")

        result = asyncio.run(engine.publish_task(task_id))

        self.assertFalse(result["ok"])
        with db.get_session() as session:
            task = session.get(PublishTask, task_id)
            self.assertEqual(task.status, "pending")
            self.assertIsNotNone(task.scheduled_at)

    def test_publish_with_missing_account_fails_before_browser_use(self):
        browser = _BrowserStub()
        task_id = self._publish_task(999999)
        engine = MonitorEngine(self.cfg, browser)

        result = asyncio.run(engine.publish_task(task_id))

        self.assertFalse(result["ok"])
        self.assertEqual(browser.identity_calls, 0)
        self.assertEqual(browser.anon_calls, 0)
        with db.get_session() as session:
            self.assertEqual(session.get(PublishTask, task_id).status, "failed")

    def test_publish_with_bad_proxy_is_deferred_for_recovery(self):
        account_id = self._account()
        with db.get_session() as session:
            account = session.get(DouyinAccount, account_id)
            account.proxy = "http://bad.example:8000"
            account.proxy_status = "bad"
            session.add(account)
            session.commit()
        task_id = self._publish_task(account_id)
        engine = MonitorEngine(self.cfg, _BrowserStub())

        result = asyncio.run(engine.publish_task(task_id))

        self.assertFalse(result["ok"])
        with db.get_session() as session:
            task = session.get(PublishTask, task_id)
            self.assertEqual(task.status, "pending")
            self.assertGreater(task.scheduled_at, datetime.utcnow())

    def test_successful_comment_blocks_immediate_cross_feature_write(self):
        account_id = self._account()
        comment_id = self._comment_task(account_id)
        action_id = self._action_task(account_id)
        engine = MonitorEngine(self.cfg, _BrowserStub())

        async def sent(*_args, **_kwargs):
            return True, ""

        with patch("app.engine.monitor.post_comment_browser", sent):
            first = asyncio.run(engine.execute_comment_task(comment_id))
        second = asyncio.run(engine.execute_action_task(action_id))

        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        with db.get_session() as session:
            task = session.get(AccountActionTask, action_id)
            self.assertEqual(task.status, "pending")
            self.assertIsNotNone(task.scheduled_at)

    def test_dm_risk_response_does_not_retry_through_browser(self):
        account_id = self._account()
        with db.get_session() as session:
            session.add(DmConversation(
                account_id=account_id,
                conv_id="conv-fixture",
                conv_short_id="short-fixture",
                ticket="ticket-fixture",
            ))
            task = AccountActionTask(
                platform="douyin", account_id=account_id, action="send_dm",
                conv_id="conv-fixture", target_uid="target", content="hello",
                status="pending",
            )
            session.add(task)
            session.commit()
            session.refresh(task)
            task_id = task.id
        engine = MonitorEngine(self.cfg, _BrowserStub())
        browser_calls = 0

        async def api_risk(*_args, **_kwargs):
            return False, "HTTP 429"

        async def browser_retry(*_args, **_kwargs):
            nonlocal browser_calls
            browser_calls += 1
            return True, ""

        with patch("app.engine.monitor.send_dm_api", api_risk), \
                patch("app.engine.monitor.send_dm", browser_retry):
            result = asyncio.run(engine.execute_action_task(task_id))

        self.assertFalse(result["ok"])
        self.assertEqual(browser_calls, 0)
        with db.get_session() as session:
            self.assertEqual(session.get(AccountActionTask, task_id).status, "pending")

    def test_publish_risk_response_returns_task_to_pending(self):
        account_id = self._account()
        task_id = self._publish_task(account_id)
        engine = MonitorEngine(self.cfg, _BrowserStub())

        async def rejected(*_args, **_kwargs):
            return False, "", "HTTP 429"

        with patch("app.engine.monitor.publish_douyin", rejected):
            result = asyncio.run(engine.publish_task(task_id))

        self.assertFalse(result["ok"])
        with db.get_session() as session:
            task = session.get(PublishTask, task_id)
            self.assertEqual(task.status, "pending")
            self.assertIsNotNone(task.scheduled_at)
            self.assertGreater(task.scheduled_at, datetime.utcnow())

    def test_publish_auth_expiry_keeps_task_pending_and_invalidates_account(self):
        account_id = self._account()
        task_id = self._publish_task(account_id)
        engine = MonitorEngine(self.cfg, _BrowserStub())

        async def logged_out(*_args, **_kwargs):
            return False, "", "logged_out"

        with patch("app.engine.monitor.publish_douyin", logged_out):
            result = asyncio.run(engine.publish_task(task_id))

        self.assertFalse(result["ok"])
        with db.get_session() as session:
            task = session.get(PublishTask, task_id)
            account = session.get(DouyinAccount, account_id)
            self.assertEqual(task.status, "pending")
            self.assertIsNotNone(task.scheduled_at)
            self.assertEqual(account.status, "invalid")

    def test_disabled_risk_controller_does_not_defer_rejected_publish(self):
        self.cfg.risk_control.enabled = False
        account_id = self._account()
        task_id = self._publish_task(account_id)
        engine = MonitorEngine(self.cfg, _BrowserStub())

        async def rejected(*_args, **_kwargs):
            return False, "", "HTTP 429"

        with patch("app.engine.monitor.publish_douyin", rejected):
            result = asyncio.run(engine.publish_task(task_id))

        self.assertFalse(result["ok"])
        with db.get_session() as session:
            task = session.get(PublishTask, task_id)
            account = session.get(DouyinAccount, account_id)
            self.assertEqual(task.status, "failed")
            self.assertIsNone(task.scheduled_at)
            self.assertIsNone(account.write_paused_until)

    def test_disabled_risk_controller_ignores_existing_compatibility_pause(self):
        account_id = self._account()
        with db.get_session() as session:
            account = session.get(DouyinAccount, account_id)
            account.write_paused_until = datetime.utcnow() + timedelta(hours=1)
            account.write_pause_reason = "legacy risk pause"
            session.add(account)
            session.commit()
        self.cfg.risk_control.enabled = False
        engine = MonitorEngine(self.cfg, _BrowserStub())

        self.assertEqual(engine._write_pause_error(account_id), "")

    def test_comment_at_daily_cap_remains_queued(self):
        self.cfg.engine.comment_daily_cap_per_account = 1
        account_id = self._account()
        with db.get_session() as session:
            session.add(CommentTask(
                platform="douyin",
                account_id=account_id,
                aweme_id="already-sent",
                content="done",
                status="done",
                done_at=datetime.utcnow(),
            ))
            session.commit()
        task_id = self._comment_task(account_id)
        engine = MonitorEngine(self.cfg, _BrowserStub())

        result = asyncio.run(engine.execute_comment_task(task_id))

        self.assertFalse(result["ok"])
        with db.get_session() as session:
            task = session.get(CommentTask, task_id)
            self.assertEqual(task.status, "pending")
            self.assertIsNotNone(task.scheduled_at)

    def test_startup_recovers_interrupted_write_tasks(self):
        account_id = self._account()
        now = datetime(2026, 8, 7, 1, 0, 0)
        with db.get_session() as session:
            comment = CommentTask(
                account_id=account_id, aweme_id="fixture", content="fixture",
                status="doing")
            action = AccountActionTask(
                account_id=account_id, action="follow", status="doing")
            publish = PublishTask(
                account_id=account_id, media_json="[]", status="publishing")
            session.add(comment)
            session.add(action)
            session.add(publish)
            session.commit()
            comment_id, action_id, publish_id = comment.id, action.id, publish.id

        engine = MonitorEngine(self.cfg, _BrowserStub())
        recovered = engine.recover_interrupted_tasks(now=now)

        self.assertEqual(recovered, 3)
        with db.get_session() as session:
            rows = [
                session.get(CommentTask, comment_id),
                session.get(AccountActionTask, action_id),
                session.get(PublishTask, publish_id),
            ]
            for row in rows:
                self.assertEqual(row.status, "pending")
                self.assertEqual(row.scheduled_at, now + timedelta(minutes=5))

    def test_legacy_active_window_check_uses_account_timezone(self):
        self.cfg.engine.quiet_hours_enabled = True
        account_id = self._account()
        with db.get_session() as session:
            account = session.get(DouyinAccount, account_id)
            account.timezone_id = "America/New_York"
            session.add(account)
            session.commit()
        engine = MonitorEngine(self.cfg, _BrowserStub())
        engine.risk._in_active_window = (
            lambda account, _now: account.timezone_id == "America/New_York")

        self.assertTrue(engine._in_active_window(account_id))

    def test_legacy_daily_counter_uses_account_local_midnight(self):
        account_id = self._account()
        with db.get_session() as session:
            account = session.get(DouyinAccount, account_id)
            account.timezone_id = "America/New_York"
            session.add(account)
            session.commit()
        engine = MonitorEngine(self.cfg, _BrowserStub())
        expected = engine.risk._local_day_start_utc(
            engine._load_account(account_id), datetime.utcnow())

        self.assertEqual(engine._today_start(account_id), expected)

    def test_self_comment_filter_prefers_account_ids(self):
        raw = {"user": {"uid": "self-uid", "nickname": "changed"}}
        self.assertTrue(MonitorEngine._is_self_comment(
            raw, "old-name", acc_uid="self-uid"))
        self.assertFalse(MonitorEngine._is_self_comment(
            {"user": {"uid": "other", "nickname": "visitor"}},
            "owner", acc_uid="self-uid"))

    def test_rule_persists_source_text_for_strict_reply_targeting(self):
        account_id = self._account()
        with db.get_session() as session:
            rule = CommentRule(
                platform="douyin", mode="auto_reply", target_kind="self",
                account_id=account_id, templates='["收到啦"]',
                max_per_run=1, daily_cap=1, min_gap_seconds=60,
            )
            session.add(rule)
            session.commit()
            session.refresh(rule)
            rule_id = rule.id

        engine = MonitorEngine(self.cfg, _BrowserStub())

        async def discover(*_args):
            return ([{
                "aweme_id": "fixture-work",
                "target_comment_id": "fixture-comment",
                "target_nick": "visitor",
                "source_text": "hello from visitor",
                "ctx": {"nick": "visitor"},
            }], "")

        engine._discover_targets = discover
        result = asyncio.run(engine.run_comment_rule(rule_id))

        self.assertTrue(result["ok"])
        with db.get_session() as session:
            task = session.exec(select(CommentTask).where(
                CommentTask.rule_id == rule_id)).one()
            self.assertEqual(task.target_text, "hello from visitor")

    def test_concurrent_comment_rule_discovery_rechecks_read_budget_in_lock(self):
        account_id = self._account()
        rule_ids = []
        with db.get_session() as session:
            for _ in range(2):
                rule = CommentRule(
                    platform="douyin", mode="auto_reply", target_kind="self",
                    account_id=account_id, templates='["ok"]',
                    max_per_run=1, daily_cap=10, min_gap_seconds=60,
                )
                session.add(rule)
                session.commit()
                session.refresh(rule)
                rule_ids.append(rule.id)
        engine = MonitorEngine(self.cfg, _BrowserStub())
        calls = 0

        async def discover(*_args):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.02)
            return [], ""

        engine._discover_targets = discover

        async def scenario():
            return await asyncio.gather(*(
                engine.run_comment_rule(rule_id) for rule_id in rule_ids))

        results = asyncio.run(scenario())

        self.assertEqual(calls, 1)
        self.assertEqual(sum(
            1 for result in results if result.get("next_allowed_at")), 1)

    def test_xhs_manual_mode_keeps_draft_without_api_call(self):
        account_id = self._account(platform="xhs")
        task_id = self._xhs_comment_task(account_id)
        engine = MonitorEngine(self.cfg, _BrowserStub())
        engine.cfg.engine.xhs_comment_write_mode = "manual"

        def unexpected_api(*_args, **_kwargs):
            raise AssertionError("XHS direct comment API must stay disabled by default")

        engine._xhs_client = unexpected_api
        result = asyncio.run(engine.execute_comment_task(task_id))

        self.assertFalse(result["ok"])
        self.assertIn("未调用评论发布接口", result["error"])
        with db.get_session() as session:
            task = session.get(CommentTask, task_id)
            self.assertEqual(task.status, "draft")
            self.assertEqual(task.method, "manual")

    def test_xhs_rule_creates_review_draft_then_auto_publishes_after_approval(self):
        account_id = self._account(platform="xhs")
        with db.get_session() as session:
            rule = CommentRule(
                platform="xhs", mode="auto_reply", target_kind="self",
                account_id=account_id, templates='["收到"]',
                max_per_run=1, daily_cap=1, min_gap_seconds=60,
            )
            session.add(rule)
            session.commit()
            session.refresh(rule)
            rule_id = rule.id

        engine = MonitorEngine(self.cfg, _BrowserStub())

        async def discover(*_args):
            return ([{
                "aweme_id": "fixture-note",
                "target_comment_id": "fixture-comment",
                "target_nick": "visitor",
                "source_text": "hello from visitor",
                "ctx": {"nick": "visitor"},
            }], "")

        engine._discover_targets = discover
        result = asyncio.run(engine.run_comment_rule(rule_id))

        self.assertTrue(result["ok"])
        self.assertTrue(result["review"])
        self.assertFalse(result["manual_only"])
        with db.get_session() as session:
            task = session.exec(select(CommentTask).where(
                CommentTask.rule_id == rule_id)).one()
            self.assertEqual(task.status, "draft")
            task_id = task.id
            task.status = "pending"  # fixture:等价于人工点击“通过”
            session.add(task)
            session.commit()

        class _XhsClientStub:
            async def post_comment(self, *_args, **_kwargs):
                return {"comment": {"id": "fixture-sent"}}

        engine._xhs_client = lambda *_args, **_kwargs: _XhsClientStub()
        sent = asyncio.run(engine.execute_comment_task(task_id))
        self.assertTrue(sent["ok"])
        with db.get_session() as session:
            task = session.get(CommentTask, task_id)
            self.assertEqual(task.status, "done")
            self.assertEqual(task.method, "api")

    def test_xhs_review_switch_false_enqueues_automatic_publish(self):
        self.cfg.engine.xhs_comment_review_before_publish = False
        account_id = self._account(platform="xhs")
        with db.get_session() as session:
            rule = CommentRule(
                platform="xhs", mode="auto_reply", target_kind="self",
                account_id=account_id, templates='["收到"]',
                max_per_run=1, daily_cap=1, min_gap_seconds=60,
            )
            session.add(rule)
            session.commit()
            session.refresh(rule)
            rule_id = rule.id

        engine = MonitorEngine(self.cfg, _BrowserStub())

        async def discover(*_args):
            return ([{
                "aweme_id": "fixture-note",
                "target_comment_id": "fixture-comment",
                "target_nick": "visitor",
                "source_text": "hello from visitor",
                "ctx": {"nick": "visitor"},
            }], "")

        engine._discover_targets = discover
        result = asyncio.run(engine.run_comment_rule(rule_id))
        self.assertTrue(result["ok"])
        self.assertFalse(result["review"])
        with db.get_session() as session:
            task = session.exec(select(CommentTask).where(
                CommentTask.rule_id == rule_id)).one()
            self.assertEqual(task.status, "pending")


if __name__ == "__main__":
    unittest.main()
