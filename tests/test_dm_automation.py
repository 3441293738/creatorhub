import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import app.db as db
from app.engine.dm_automation import XhsDmAutomation, render_reply, rule_matches
from app.models import (
    AccountActionTask,
    DmAutoReplyRule,
    DmConversation,
    DmMessage,
    DmMonitorState,
    DouyinAccount,
)
from sqlmodel import select


class DmAutomationTests(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "dm.db"))

    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def test_rule_matching_and_placeholders(self):
        rule = DmAutoReplyRule(
            account_id=1, keywords=json.dumps(["合作", "价格"]),
            exclude_keywords=json.dumps(["不要回复"]),
            reply_templates=json.dumps(["你好 {nickname}，已收到"]),
        )
        self.assertTrue(rule_matches(rule, "想咨询合作方式"))
        self.assertFalse(rule_matches(rule, "合作，但不要回复"))
        self.assertEqual(render_reply(rule, nickname="小王", message="合作"),
                         "你好 小王，已收到")

    def test_fresh_message_creates_review_draft_once(self):
        with db.get_session() as session:
            account = DouyinAccount(platform="xhs", nickname="me")
            session.add(account); session.commit(); session.refresh(account)
            conv = DmConversation(
                platform="xhs", account_id=account.id, conv_id="peer",
                peer_uid="peer", peer_nickname="访客")
            rule = DmAutoReplyRule(
                platform="xhs", account_id=account.id, enabled=True,
                keywords=json.dumps(["价格"]),
                reply_templates=json.dumps(["你好 {nickname}，稍后回复"]),
                review_before_send=True)
            msg = DmMessage(
                platform="xhs", account_id=account.id, conv_id="peer",
                msg_id="m1", direction="in", text="请问价格",
                create_time=2_000_000_000)
            session.add(conv); session.add(rule); session.add(msg); session.commit()
            account_id = account.id

        cfg = SimpleNamespace(
            engine=SimpleNamespace(
                xhs_dm_auto_reply_enabled=True,
                xhs_dm_poll_interval_seconds=75,
                xhs_dm_poll_jitter=0.3,
                xhs_dm_max_conversations_per_poll=2),
            risk_control=SimpleNamespace(dm_min_gap_seconds=900),
        )
        automation = XhsDmAutomation(cfg, browser=object())
        self.assertEqual(automation._evaluate_message(account_id, "peer", "m1"), "draft")
        self.assertEqual(automation._evaluate_message(account_id, "peer", "m1"), "duplicate")
        with db.get_session() as session:
            tasks = session.exec(select(AccountActionTask)).all()
            self.assertEqual(len(tasks), 1)
            self.assertEqual(tasks[0].status, "draft")
            self.assertEqual(tasks[0].source_msg_id, "m1")

    def test_push_frame_classification_ignores_heartbeats_and_acks(self):
        self.assertFalse(XhsDmAutomation._business_push('{"v":1,"t":0}'))
        self.assertFalse(XhsDmAutomation._business_push(
            '{"v":1,"t":2,"b":{"a":{"c":0,"m":"success"}}}'))
        self.assertTrue(XhsDmAutomation._business_push(
            '{"v":1,"t":2,"b":{"d":{"biz":"im"}}}'))

    def test_new_conversation_after_account_baseline_is_processed(self):
        class Page:
            url = "https://www.xiaohongshu.com/chat"

        class Browser:
            xhs_interaction = SimpleNamespace(pause=AsyncMock())

            def identity_for(self, _account):
                return object()

            @asynccontextmanager
            async def visible_page(self, _identity, **_kwargs):
                yield Page()

        cfg = SimpleNamespace(
            engine=SimpleNamespace(
                xhs_dm_auto_reply_enabled=False,
                xhs_dm_monitor_enabled=True,
                xhs_dm_realtime_enabled=False,
                xhs_dm_realtime_debounce_seconds=1.5,
                xhs_dm_fallback_interval_seconds=600,
                xhs_dm_poll_interval_seconds=120,
                xhs_dm_poll_jitter=0.3,
                xhs_dm_max_conversations_per_poll=2),
            risk_control=SimpleNamespace(dm_min_gap_seconds=900),
        )
        raw_old = json.dumps({"max_store_id": 5, "self_uid": "me"})
        with db.get_session() as session:
            account = DouyinAccount(platform="xhs", nickname="me", uid="me")
            session.add(account); session.commit(); session.refresh(account)
            session.add(DmConversation(
                platform="xhs", account_id=account.id, conv_id="known",
                peer_uid="known", raw_json=raw_old))
            session.commit()
            account_id = account.id

        new_conv = {
            "conv_id": "new-peer", "peer_uid": "new-peer",
            "peer_sec_uid": "", "peer_nickname": "new visitor",
            "peer_avatar": "", "last_text": "hello", "last_time": 2_000_000_000,
            "unread_count": 1, "conv_short_id": "", "ticket": "",
            "raw_json": json.dumps({"max_store_id": 1, "self_uid": "me"}),
        }
        history = {"messages": [{
            "server_msg_id": "new-message", "direction": "in",
            "msg_type": "text", "text": "hello", "create_time": 2_000_000_000,
            "store_id": 1, "sender_uid": "new-peer", "receiver_uid": "me",
            "group_chat": False, "content": {"content": "hello"},
        }]}

        async def scenario():
            automation = XhsDmAutomation(cfg, Browser())
            with db.get_session() as session:
                account = session.get(DouyinAccount, account_id)
            with patch("app.engine.dm_automation.fetch_conversations_in_page",
                       AsyncMock(return_value=([new_conv], ""))), patch(
                       "app.engine.dm_automation.fetch_history_in_page",
                       AsyncMock(return_value=(history, ""))):
                result = await automation.poll(account, trigger="push")
            self.assertTrue(result["ok"])
            self.assertEqual(result["messages"], 1)
            self.assertTrue(any(event.get("new_conversation")
                                for event in result["events"]))

        import asyncio
        asyncio.run(scenario())
        with db.get_session() as session:
            state = session.exec(select(DmMonitorState).where(
                DmMonitorState.account_id == account_id)).first()
            message = session.exec(select(DmMessage).where(
                DmMessage.account_id == account_id,
                DmMessage.msg_id == "new-message")).first()
            self.assertTrue(state.baseline_initialized)
            self.assertIsNotNone(message)


if __name__ == "__main__":
    unittest.main()
