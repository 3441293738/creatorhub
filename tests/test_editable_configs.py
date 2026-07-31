import asyncio
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException
import app.db as db
from app.main import (
    PublishUpdate,
    TargetUpdate,
    WatchUpdate,
    update_monitor,
    update_publish,
    update_watch,
)
from app.models import CommentWatch, DouyinAccount, MonitorTarget, PublishTask


class EditableConfigTests(unittest.TestCase):
    def setUp(self):
        self._previous_engine = db._engine
        self._tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self._tmp.name) / "editable.db"))

    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self._previous_engine
        self._tmp.cleanup()

    def test_pending_publish_task_can_be_edited(self):
        with db.get_session() as session:
            account = DouyinAccount(platform="douyin", nickname="主号", status="active")
            session.add(account)
            session.commit()
            session.refresh(account)
            task = PublishTask(
                platform="douyin",
                account_id=account.id,
                title="旧标题",
                status="failed",
                error="old error",
            )
            session.add(task)
            session.commit()
            session.refresh(task)
            task_id = task.id
            account_id = account.id

        result = asyncio.run(update_publish(task_id, PublishUpdate(
            account_id=account_id,
            title="新标题",
            desc="新正文",
            visibility="friends",
            allow_save=False,
            scheduled_at=None,
        )))
        self.assertEqual(result["title"], "新标题")
        self.assertEqual(result["desc"], "新正文")
        self.assertEqual(result["visibility"], "friends")
        self.assertFalse(result["allow_save"])
        self.assertEqual(result["status"], "pending")
        self.assertIsNone(result["scheduled_at"])

    def test_completed_publish_task_is_read_only(self):
        with db.get_session() as session:
            task = PublishTask(platform="douyin", title="已发", status="done")
            session.add(task)
            session.commit()
            session.refresh(task)
            task_id = task.id

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(update_publish(task_id, PublishUpdate(title="不应保存")))
        self.assertEqual(caught.exception.status_code, 400)

    def test_comment_watch_edit_updates_schedule_source_and_account(self):
        with db.get_session() as session:
            account = DouyinAccount(
                platform="douyin",
                nickname="创作号",
                status="active",
                creator_storage_state='{"cookies":[]}',
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            watch = CommentWatch(
                platform="douyin",
                kind="user",
                sec_uid="target",
                interval_seconds=600,
                mode="public",
            )
            session.add(watch)
            session.commit()
            session.refresh(watch)
            watch_id = watch.id
            account_id = account.id

        result = asyncio.run(update_watch(watch_id, WatchUpdate(
            interval_seconds=1800,
            mode="creator",
            account_id=account_id,
            recent_works=10,
            recent_days=30,
            max_scrolls=12,
            alias="重点评论",
            tags=["重点", "待回复"],
        )))
        self.assertEqual(result["interval_seconds"], 1800)
        self.assertEqual(result["mode"], "creator")
        self.assertEqual(result["account_id"], account_id)
        self.assertEqual(result["recent_works"], 10)
        self.assertEqual(result["recent_days"], 30)
        self.assertEqual(result["max_scrolls"], 12)
        self.assertEqual(result["alias"], "重点评论")
        self.assertEqual(result["tags"], ["重点", "待回复"])

    def test_monitor_edit_controls_download_and_scan_strategy(self):
        with db.get_session() as session:
            account = DouyinAccount(
                platform="douyin", nickname="抓取号", sec_uid="self",
                status="active",
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            target = MonitorTarget(
                platform="douyin", sec_uid="target", account_id=account.id,
                interval_seconds=300,
            )
            session.add(target)
            session.commit()
            session.refresh(target)
            target_id = target.id

        result = asyncio.run(update_monitor(target_id, TargetUpdate(
            interval_seconds=1800,
            initial_backfill_count=20,
            download_enabled=False,
            media_filter="video",
        )))
        self.assertEqual(result["interval_seconds"], 1800)
        self.assertEqual(result["initial_backfill_count"], 20)
        self.assertFalse(result["download_enabled"])
        self.assertEqual(result["media_filter"], "video")

    def test_backfill_cannot_change_after_first_scan(self):
        with db.get_session() as session:
            target = MonitorTarget(
                platform="kuaishou", sec_uid="target",
                last_scan_at=datetime.utcnow(),
            )
            session.add(target)
            session.commit()
            session.refresh(target)
            target_id = target.id

        with self.assertRaises(HTTPException) as caught:
            asyncio.run(update_monitor(
                target_id, TargetUpdate(initial_backfill_count=5)))
        self.assertEqual(caught.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
