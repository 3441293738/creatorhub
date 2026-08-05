import asyncio
import tempfile
import unittest
from pathlib import Path

import app.db as db
from app.config import Config, EngineConfig
from app.engine.monitor import MonitorEngine
from app.models import AccountActionTask, CommentRule, CommentTask, DouyinAccount
from sqlmodel import select


class _BrowserStub:
    def __init__(self):
        self._locks = {}

    def lock_for(self, key):
        return self._locks.setdefault(key, asyncio.Lock())

    def identity_for(self, _account):
        return None

    def anon_identity(self):
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

    def test_manual_comment_execution_keeps_task_queued_outside_active_window(self):
        account_id = self._account()
        task_id = self._comment_task(account_id)
        engine = MonitorEngine(self.cfg, _BrowserStub())
        engine._in_active_window = lambda: False

        result = asyncio.run(engine.execute_comment_task(task_id))

        self.assertFalse(result["ok"])
        with db.get_session() as session:
            task = session.get(CommentTask, task_id)
            self.assertEqual(task.status, "pending")

    def test_manual_action_execution_uses_same_write_gate(self):
        account_id = self._account()
        task_id = self._action_task(account_id)
        engine = MonitorEngine(self.cfg, _BrowserStub())
        engine._in_active_window = lambda: False

        result = asyncio.run(engine.execute_action_task(task_id))

        self.assertFalse(result["ok"])
        with db.get_session() as session:
            task = session.get(AccountActionTask, task_id)
            self.assertEqual(task.status, "pending")

    def test_risk_pause_blocks_and_persists_until_cleared(self):
        account_id = self._account()
        engine = MonitorEngine(self.cfg, _BrowserStub())
        engine._pause_account_writes(account_id, "status_code=8 风控")

        message = engine._comment_gate_error(account_id)
        self.assertIn("暂停至", message)
        with db.get_session() as session:
            account = session.get(DouyinAccount, account_id)
            self.assertIsNotNone(account.write_paused_until)
            account.write_paused_until = None
            account.write_pause_reason = ""
            session.add(account)
            session.commit()
        self.assertEqual(engine._comment_gate_error(account_id), "")

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
