import json
import tempfile
import unittest
from pathlib import Path

import app.db as db
from app.engine.monitor import MonitorEngine
from app.main import app
from app.models import ContentRecord, PublishTask


class ChannelsRepostTests(unittest.TestCase):
    def setUp(self):
        self._previous_engine = db._engine
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "channels-repost.db"
        db.init_db(str(self.db_path))

    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self._previous_engine
        self._tmp.cleanup()

    def test_channels_repost_route_is_registered(self):
        routes = {
            (route.path, method)
            for route in app.routes
            for method in getattr(route, "methods", set())
        }
        self.assertIn(
            ("/api/contents/{cid}/repost-shipinhao", "POST"),
            routes,
        )

    def test_relay_task_targets_channels_and_uses_channels_title_limit(self):
        media = Path(self._tmp.name) / "source.mp4"
        media.write_bytes(b"video")
        with db.get_session() as session:
            record = ContentRecord(
                platform="douyin",
                target_id=1,
                aweme_id="source-id",
                desc="原始描述",
                media_type="video",
                download_status="done",
                local_path=str(media),
            )
            session.add(record)
            session.commit()
            session.refresh(record)
            content_id = record.id

        engine = MonitorEngine.__new__(MonitorEngine)
        task_id = engine.create_relay_publish(
            content_id,
            account_id=123,
            target_platform="shipinhao",
            title="这是一个超过视频号短标题限制的标题文本",
            desc="转发正文",
            topics="测试",
        )

        self.assertIsNotNone(task_id)
        with db.get_session() as session:
            task = session.get(PublishTask, task_id)
            self.assertEqual(task.platform, "shipinhao")
            self.assertEqual(task.account_id, 123)
            self.assertEqual(task.source_platform, "douyin")
            self.assertEqual(task.source_content_id, content_id)
            self.assertEqual(task.title, "这是一个超过视频号短标题限制的标")
            self.assertEqual(json.loads(task.media_json), [str(media)])


if __name__ == "__main__":
    unittest.main()
