import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import app.db as db
from app.main import (
    _load_meta_tags,
    _meta_tags,
    all_contents,
    list_comments,
)
from app.models import CommentRecord, CommentWatch, ContentRecord, MonitorTarget


class MonitorMetadataTests(unittest.TestCase):
    def setUp(self):
        self._previous_engine = db._engine
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "metadata.db"
        db.init_db(str(self.db_path))

    def tearDown(self):
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self._previous_engine
        self._tmp.cleanup()

    def test_tags_are_cleaned_and_legacy_values_are_supported(self):
        self.assertEqual(_meta_tags([" 热点 ", "热点", "待分发"]), ["热点", "待分发"])
        self.assertEqual(_load_meta_tags('["热点","待分发"]'), ["热点", "待分发"])
        self.assertEqual(_load_meta_tags("热点，待分发"), ["热点", "待分发"])

    def test_content_and_comment_filters_use_group_and_tag(self):
        with db.get_session() as session:
            target_a = MonitorTarget(
                platform="douyin", sec_uid="u1", group_name="brand",
                tags=json.dumps(["hot", "pending"]),
            )
            target_b = MonitorTarget(
                platform="douyin", sec_uid="u2", group_name="competitor",
                tags=json.dumps(["observe"]),
            )
            target_other_platform = MonitorTarget(
                platform="xhs", sec_uid="x1", group_name="brand",
                tags=json.dumps(["hot"]),
            )
            session.add(target_a)
            session.add(target_b)
            session.add(target_other_platform)
            session.commit()
            session.refresh(target_a)
            session.refresh(target_b)
            session.refresh(target_other_platform)
            session.add(ContentRecord(
                platform="douyin", target_id=target_a.id, aweme_id="a1",
                create_time=2,
            ))
            session.add(ContentRecord(
                platform="douyin", target_id=target_b.id, aweme_id="a2",
                create_time=1,
            ))
            session.add(ContentRecord(
                platform="xhs", target_id=target_other_platform.id,
                aweme_id="x1", create_time=3,
            ))

            watch_a = CommentWatch(
                platform="douyin", aweme_id="v1", group_name="priority",
                tags=json.dumps(["reply", "lead"]),
            )
            watch_b = CommentWatch(
                platform="douyin", aweme_id="v2", group_name="routine",
                tags=json.dumps(["done"]),
            )
            watch_other_platform = CommentWatch(
                platform="xhs", aweme_id="xv1", group_name="priority",
                tags=json.dumps(["reply"]),
            )
            session.add(watch_a)
            session.add(watch_b)
            session.add(watch_other_platform)
            session.commit()
            session.refresh(watch_a)
            session.refresh(watch_b)
            session.refresh(watch_other_platform)
            session.add(CommentRecord(
                platform="douyin", watch_id=watch_a.id, aweme_id="v1",
                comment_id="c1", user_sec_uid="commenter-sec-1",
            ))
            session.add(CommentRecord(
                platform="douyin", watch_id=watch_b.id, aweme_id="v2",
                comment_id="c2",
            ))
            session.add(CommentRecord(
                platform="xhs", watch_id=watch_other_platform.id,
                aweme_id="xv1", comment_id="xc1",
            ))
            session.commit()

        contents = asyncio.run(all_contents(
            platform="douyin", group_name="brand", tag="hot",
        ))
        comments = asyncio.run(list_comments(
            platform="douyin", group_name="priority", tag="reply",
        ))
        self.assertEqual([row["aweme_id"] for row in contents], ["a1"])
        self.assertEqual([row["comment_id"] for row in comments], ["c1"])
        self.assertEqual(comments[0]["user_sec_uid"], "commenter-sec-1")
        by_sec_uid = asyncio.run(list_comments(
            platform="douyin", q="commenter-sec-1",
        ))
        self.assertEqual([row["comment_id"] for row in by_sec_uid], ["c1"])

    def test_content_and_comment_pagination_and_record_filters(self):
        with db.get_session() as session:
            target = MonitorTarget(platform="douyin", sec_uid="paged-target")
            session.add(target)
            session.commit()
            session.refresh(target)
            for index in range(3):
                session.add(ContentRecord(
                    platform="douyin", target_id=target.id,
                    aweme_id=f"needle-{index}", desc=f"needle {index}",
                    create_time=index + 1, like_count=index,
                    media_type="images" if index == 0 else "video",
                    download_status="done" if index == 2 else "failed",
                ))
            watch = CommentWatch(platform="douyin", aweme_id="paged-video")
            session.add(watch)
            session.commit()
            session.refresh(watch)
            for index in range(3):
                session.add(CommentRecord(
                    platform="douyin", watch_id=watch.id,
                    aweme_id="paged-video", comment_id=f"paged-{index}",
                    text=f"needle comment {index}", create_time=index + 1,
                    like_count=index, reply_to="" if index != 1 else "parent",
                ))
            session.commit()

        contents = asyncio.run(all_contents(
            platform="douyin", q="needle", media_type="video",
            sort="likes_desc", page=2, page_size=1, paginate=True,
        ))
        comments = asyncio.run(list_comments(
            platform="douyin", q="needle", reply_type="top",
            min_like_count=1, sort="oldest", page=1, page_size=1,
            paginate=True,
        ))
        self.assertEqual((contents["total"], contents["pages"]), (2, 2))
        self.assertEqual([row["aweme_id"] for row in contents["items"]], ["needle-1"])
        self.assertEqual((comments["total"], comments["pages"]), (1, 1))
        self.assertEqual([row["comment_id"] for row in comments["items"]], ["paged-2"])


class MonitorMetadataMigrationTests(unittest.TestCase):
    def test_existing_tables_receive_metadata_columns(self):
        previous_engine = db._engine
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "legacy.db"
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE monitortarget ("
                "id INTEGER PRIMARY KEY, "
                "platform VARCHAR NOT NULL DEFAULT 'douyin', "
                "sec_uid VARCHAR NOT NULL DEFAULT '')"
            )
            connection.execute(
                "CREATE TABLE commentwatch ("
                "id INTEGER PRIMARY KEY, "
                "platform VARCHAR NOT NULL DEFAULT 'douyin', "
                "aweme_id VARCHAR NOT NULL DEFAULT '')"
            )
            connection.execute(
                "CREATE TABLE commentrecord ("
                "id INTEGER PRIMARY KEY, "
                "aweme_id VARCHAR NOT NULL DEFAULT '', "
                "comment_id VARCHAR NOT NULL DEFAULT '')"
            )
            connection.commit()
            connection.close()

            engine = db.init_db(str(path))
            with engine.connect() as connection:
                monitor_columns = {
                    row[1] for row in connection.exec_driver_sql(
                        "PRAGMA table_info(monitortarget)"
                    )
                }
                watch_columns = {
                    row[1] for row in connection.exec_driver_sql(
                        "PRAGMA table_info(commentwatch)"
                    )
                }
                comment_columns = {
                    row[1] for row in connection.exec_driver_sql(
                        "PRAGMA table_info(commentrecord)"
                    )
                }
            engine.dispose()
            db._engine = previous_engine

        expected = {
            "alias", "group_name", "tags", "max_scrolls",
            "max_items_per_scan", "record_media_filter", "min_like_count",
            "min_comment_count", "recent_days", "include_keywords",
            "exclude_keywords",
        }
        self.assertTrue(expected <= monitor_columns)
        self.assertTrue({"alias", "group_name", "tags"} <= watch_columns)
        self.assertIn("user_sec_uid", comment_columns)


if __name__ == "__main__":
    unittest.main()
