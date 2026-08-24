import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import app.db as db
import app.main as main
from app.models import (
    AccountActionTask,
    CommentTask,
    ContentRecord,
    DouyinAccount,
    KeywordCollectionContent,
    KeywordCollectionJob,
    MonitorTarget,
    PublishTask,
)


class TaskQueueTests(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "task-queue.db"))
        now = datetime.utcnow()
        with db.get_session() as session:
            account = DouyinAccount(platform="douyin", nickname="队列样本账号")
            session.add(account)
            session.commit()
            session.refresh(account)
            self.account_id = account.id

            target = MonitorTarget(
                platform="douyin", account_id=account.id,
                nickname="监控目标", sec_uid="target-user")
            session.add(target)
            session.commit()
            session.refresh(target)

            job = KeywordCollectionJob(
                platform="douyin", account_id=account.id,
                keywords='["防晒霜"]', status="pending",
                current_step="等待账号读取冷却", blocked_reason="账号处于风险冷却期",
                blocked_signal="cooldown", next_allowed_at=now + timedelta(minutes=20))
            session.add(job)
            session.commit()
            session.refresh(job)

            session.add(PublishTask(
                platform="douyin", account_id=account.id, title="夏日防晒指南",
                status="pending", scheduled_at=now + timedelta(minutes=10)))
            session.add(CommentTask(
                platform="douyin", account_id=account.id, content="感谢分享",
                aweme_id="note-1", status="doing"))
            session.add(AccountActionTask(
                platform="douyin", account_id=account.id, action="follow",
                target_nick="目标作者", status="failed", error="操作间隔不足"))
            session.add(ContentRecord(
                platform="douyin", target_id=target.id, aweme_id="work-1",
                desc="监控下载样本", download_status="pending"))
            session.add(KeywordCollectionContent(
                job_id=job.id, platform="douyin", keyword="防晒霜",
                aweme_id="work-2", desc="采集下载样本", download_status="downloading"))
            session.add(PublishTask(
                platform="douyin", account_id=account.id, title="已经发布",
                status="done", done_at=now))
            session.commit()

    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def query(self, **overrides):
        params = {
            "platform": "douyin", "queue_type": "", "state": "active",
            "q": "", "page": 1, "page_size": 20,
        }
        params.update(overrides)
        return asyncio.run(main.list_task_queue(**params))

    def test_active_queue_unifies_all_persistent_sources(self):
        result = self.query()

        self.assertEqual(result["total"], 5)
        self.assertEqual(result["summary"]["active"], 5)
        self.assertEqual(result["summary"]["blocked"], 1)
        self.assertEqual(result["summary"]["pending"], 2)
        self.assertEqual(result["summary"]["running"], 2)
        self.assertEqual(result["summary"]["failed"], 1)
        self.assertEqual({item["queue_type"] for item in result["items"]}, {
            "collections", "publishes", "comments",
            "monitor_downloads", "collection_downloads",
        })

    def test_filters_blocked_type_platform_and_search(self):
        blocked = self.query(state="blocked")
        self.assertEqual(blocked["total"], 1)
        self.assertEqual(blocked["items"][0]["blocked_signal"], "cooldown")
        self.assertIn("风险冷却", blocked["items"][0]["blocked_reason"])

        publishes = self.query(queue_type="publishes")
        self.assertEqual(publishes["total"], 1)
        self.assertEqual(publishes["items"][0]["title"], "夏日防晒指南")

        searched = self.query(q="队列样本账号")
        self.assertEqual(searched["total"], 5)
        self.assertEqual(self.query(platform="xhs")["total"], 0)

    def test_completed_history_and_pagination(self):
        completed = self.query(state="completed")
        self.assertEqual(completed["total"], 1)
        self.assertEqual(completed["items"][0]["state"], "completed")

        page = self.query(page=2, page_size=2)
        self.assertEqual(page["page"], 2)
        self.assertEqual(page["pages"], 3)
        self.assertEqual(len(page["items"]), 2)

    def test_rejects_unknown_filters(self):
        with self.assertRaises(main.HTTPException):
            self.query(queue_type="not-a-queue")
        with self.assertRaises(main.HTTPException):
            self.query(state="not-a-state")


if __name__ == "__main__":
    unittest.main()
