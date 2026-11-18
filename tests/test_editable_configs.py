import asyncio
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
import app.db as db
from app.config import load_config
from app.engine.monitor import MonitorEngine
from app.main import (
    PublishUpdate,
    TargetUpdate,
    WatchUpdate,
    update_monitor,
    update_publish,
    update_watch,
)
from app.models import CommentWatch, ContentRecord, DouyinAccount, MonitorTarget, PublishTask


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
            max_scrolls=12,
            max_items_per_scan=20,
            record_media_filter="images",
            min_like_count=100,
            min_comment_count=10,
            recent_days=30,
            include_keywords=["防晒", "测评"],
            exclude_keywords=["广告"],
        )))
        self.assertEqual(result["interval_seconds"], 1800)
        self.assertEqual(result["initial_backfill_count"], 20)
        self.assertFalse(result["download_enabled"])
        self.assertEqual(result["media_filter"], "video")
        self.assertEqual(result["max_scrolls"], 12)
        self.assertEqual(result["max_items_per_scan"], 20)
        self.assertEqual(result["record_media_filter"], "images")
        self.assertEqual(result["min_like_count"], 100)
        self.assertEqual(result["min_comment_count"], 10)
        self.assertEqual(result["recent_days"], 30)
        self.assertEqual(result["include_keywords"], ["防晒", "测评"])
        self.assertEqual(result["exclude_keywords"], ["广告"])

    def test_backfill_can_recover_after_empty_scan_but_not_after_contents_captured(self):
        with db.get_session() as session:
            target = MonitorTarget(
                platform="kuaishou", sec_uid="target",
                last_scan_at=datetime.utcnow(),
                initial_backfill_count=0,
            )
            session.add(target)
            session.commit()
            session.refresh(target)
            target_id = target.id

        # 0-item scan: allowed to update backfill count so user can retry historical backfill
        updated = asyncio.run(update_monitor(
            target_id, TargetUpdate(initial_backfill_count=5)))
        self.assertEqual(updated["initial_backfill_count"], 5)

        # Once at least one work is captured, changing backfill count is rejected
        with db.get_session() as session:
            session.add(ContentRecord(
                platform="kuaishou", target_id=target_id, aweme_id="ks_1",
                desc="captured", media_type="video",
            ))
            session.commit()

        # Unchanged value is a no-op and allowed
        same = asyncio.run(update_monitor(
            target_id, TargetUpdate(initial_backfill_count=5, interval_seconds=600)))
        self.assertEqual(same["interval_seconds"], 600)

        # Changing backfill count after content capture raises 400
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(update_monitor(
                target_id, TargetUpdate(initial_backfill_count=20)))
        self.assertEqual(caught.exception.status_code, 400)

    def test_kuaishou_empty_first_scan_can_recover_historical_backfill(self):
        with db.get_session() as session:
            target = MonitorTarget(
                platform="kuaishou",
                sec_uid="3xfs5g9xb4tez3m",
                nickname="光影孤赏",
                last_scan_at=datetime.utcnow(),
                initial_backfill_count=5,
                download_enabled=False,
            )
            session.add(target)
            session.commit()
            session.refresh(target)
            target_id = target.id

        engine = MonitorEngine(
            load_config(),
            SimpleNamespace(
                anon_identity=lambda: SimpleNamespace(ua="Mozilla/5.0", proxy=""),
                identity_for=lambda _acc: SimpleNamespace(ua="Mozilla/5.0", proxy=""),
                lock_for=lambda _key: asyncio.Lock(),
            ),
        )
        old_ts_ms = (int(time.time()) - 7200) * 1000
        hist_feed = {
            "photo": {
                "id": "3xhistphoto001",
                "caption": "快手历史作品回填",
                "timestamp": old_ts_ms,
                "photoUrl": "https://media.example/3xhistphoto001.mp4",
            },
            "author": {"name": "光影孤赏"},
        }
        fetch_mock = AsyncMock(return_value=([hist_feed], None, ""))
        with patch("app.engine.monitor.fetch_ks_videos", new=fetch_mock), \
             patch.object(engine, "_notify_new", new=AsyncMock()) as notify_mock:
            res = asyncio.run(engine.scan_target(target_id))

        self.assertTrue(res["ok"])
        self.assertEqual(res["new"], 1)
        self.assertEqual(res["backfill_count"], 5)
        self.assertEqual(fetch_mock.await_args.kwargs["stop_before"], 0)
        notify_mock.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
