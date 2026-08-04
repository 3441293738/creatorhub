import unittest

from app.browser.fetcher import _danmaku_position_ms, _dig_danmaku_list, _is_danmaku_url
from app.config import EngineConfig
from app.engine.monitor import _danmaku_matches
from app.models import DanmakuRecord, DanmakuWatch, SQLModel
from app.platforms.douyin.extract import danmaku_key, parse_danmaku


class DanmakuParserTests(unittest.TestCase):
    def test_parse_player_shape_preserves_video_position(self):
        row = parse_danmaku({
            "danmaku_id": "dm-1",
            "content": "这句好有意思",
            "user": {"uid": "u-1", "nickname": "用户甲"},
            "time_point": 12.5,
            "create_time": 1_750_000_000_000,
            "is_blocked": "false",
        }, "aweme-1")
        self.assertEqual(row["danmaku_id"], "dm-1")
        self.assertEqual(row["aweme_id"], "aweme-1")
        self.assertEqual(row["video_time_ms"], 12500)
        self.assertEqual(row["create_time"], 1_750_000_000)
        self.assertEqual(row["user_id"], "u-1")
        self.assertFalse(row["is_blocked"])

    def test_parse_analysis_shape_and_fallback_key(self):
        raw = {
            "text": "弹幕内容",
            "uid": "u-2",
            "video_time_ms": 9000,
            "like_count": 2,
        }
        row = parse_danmaku(raw)
        self.assertTrue(row["danmaku_id"].startswith("hash:"))
        self.assertEqual(row["danmaku_id"], danmaku_key(raw))
        self.assertEqual(row["video_time_ms"], 9000)
        self.assertEqual(row["like_count"], 2)

    def test_parse_current_player_offset_shape(self):
        row = parse_danmaku({
            "danmaku_id": "dm-offset",
            "item_id": "aweme-2",
            "user_id": "uid-2",
            "offset_time": 30666,
            "text": "mj",
            "digg_count": 0,
        })
        self.assertEqual(row["aweme_id"], "aweme-2")
        self.assertEqual(row["user_id"], "uid-2")
        self.assertEqual(row["video_time_ms"], 30666)


class DanmakuResponseTests(unittest.TestCase):
    def test_recursive_extraction_finds_danmaku_list(self):
        data = {"data": {"items": [
            {"id": "1", "text": "a", "time_point": 1},
            {"id": "2", "text": "b", "time_point": 2},
        ]}}
        self.assertEqual(len(_dig_danmaku_list(data)), 2)

    def test_danmaku_endpoint_is_separate_from_comments(self):
        self.assertTrue(_is_danmaku_url(
            "https://www.douyin.com/aweme/v1/web/danmaku/get_v2/"))
        self.assertFalse(_is_danmaku_url(
            "https://www.douyin.com/aweme/v1/web/comment/list/"))
        self.assertTrue(_is_danmaku_url(
            "https://creator.douyin.com/api/danmaku/list", creator=True))
        self.assertFalse(_is_danmaku_url(
            "https://www.douyin.com/api/danmaku/list", creator=True))

    def test_offset_time_is_sorted_as_milliseconds(self):
        self.assertEqual(_danmaku_position_ms({"offset_time": 1066}), 1066)
        self.assertEqual(_danmaku_position_ms({"time_point": 1.5}), 1500)


class DanmakuModelTests(unittest.TestCase):
    def test_watch_zero_values_follow_global_defaults(self):
        watch = DanmakuWatch()
        defaults = EngineConfig()
        self.assertEqual(watch.interval_seconds, 0)
        self.assertEqual(watch.recent_works, 0)
        self.assertEqual(watch.recent_days, 0)
        self.assertEqual(watch.max_scrolls, 0)
        self.assertEqual(defaults.danmaku_recent_works, 5)
        self.assertEqual(defaults.danmaku_recent_days, 7)
        self.assertEqual(defaults.danmaku_max_scrolls, 6)
        self.assertEqual(defaults.danmaku_probe_step_seconds, 1.0)
        self.assertEqual(defaults.danmaku_max_records_per_scan, 1000)

    def test_danmaku_filters_use_video_time_and_keywords(self):
        settings = {
            "time_start_ms": 1000, "time_end_ms": 20000,
            "include_keywords": ["mj"], "exclude_keywords": ["广告"],
            "min_text_length": 2, "max_text_length": 6,
            "min_like_count": 1,
        }
        self.assertTrue(_danmaku_matches({
            "text": "mj好看", "video_time_ms": 5000, "like_count": 2,
        }, settings))
        self.assertFalse(_danmaku_matches({
            "text": "mj", "video_time_ms": 500, "like_count": 2,
        }, settings))
        self.assertFalse(_danmaku_matches({
            "text": "广告mj", "video_time_ms": 5000, "like_count": 2,
        }, settings))

    def test_tables_are_registered(self):
        self.assertIn("danmakuwatch", SQLModel.metadata.tables)
        self.assertIn("danmakurecord", SQLModel.metadata.tables)
        self.assertIn("video_time_ms", DanmakuRecord.__table__.columns)
        self.assertIn("mode", DanmakuWatch.__table__.columns)


if __name__ == "__main__":
    unittest.main()
