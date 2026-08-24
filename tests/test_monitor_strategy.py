import time
import unittest

from app.engine.monitor import (
    _monitor_content_matches,
    _monitor_strategy,
)
from app.models import MonitorTarget
from app.platforms.douyin.extract import Aweme


class MonitorStrategyTests(unittest.TestCase):
    def test_platform_defaults_and_configured_limits_are_clamped(self):
        target = MonitorTarget()
        default = _monitor_strategy(target, default_scrolls=6, default_items=12)
        self.assertEqual(default["max_scrolls"], 6)
        self.assertEqual(default["max_items"], 12)

        target.max_scrolls = 99
        target.max_items_per_scan = 999
        configured = _monitor_strategy(target, default_scrolls=6, default_items=12)
        self.assertEqual(configured["max_scrolls"], 30)
        self.assertEqual(configured["max_items"], 100)

    def test_content_filters_apply_type_metrics_age_and_terms(self):
        now = int(time.time())
        target = MonitorTarget(
            record_media_filter="images",
            min_like_count=100,
            min_comment_count=5,
            recent_days=30,
            include_keywords='["防晒","测评"]',
            exclude_keywords='["广告"]',
        )
        strategy = _monitor_strategy(target, default_scrolls=6, default_items=12)
        accepted = Aweme(
            aweme_id="1", desc="学生防晒霜真实体验", create_time=now - 86400,
            author_name="", media_type="images", like_count=120,
            comment_count=8)
        excluded = Aweme(
            aweme_id="2", desc="防晒广告", create_time=now - 86400,
            author_name="", media_type="images", like_count=120,
            comment_count=8)
        old = Aweme(
            aweme_id="3", desc="防晒测评", create_time=now - 40 * 86400,
            author_name="", media_type="images", like_count=120,
            comment_count=8)

        self.assertTrue(_monitor_content_matches(accepted, strategy, now_ts=now))
        self.assertFalse(_monitor_content_matches(excluded, strategy, now_ts=now))
        self.assertFalse(_monitor_content_matches(old, strategy, now_ts=now))


if __name__ == "__main__":
    unittest.main()
