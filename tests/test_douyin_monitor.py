import unittest

from app.browser.fetcher import _page_reaches_boundary
from app.engine.monitor import _douyin_scan_since, _select_douyin_awemes


def aweme_item(aweme_id: str, create_time: int) -> dict:
    return {
        "aweme_id": aweme_id,
        "create_time": create_time,
        "desc": aweme_id,
        "video": {
            "play_addr": {"url_list": [f"https://media.example/{aweme_id}.mp4"]},
        },
    }


class PaginationBoundaryTests(unittest.TestCase):
    def test_single_pinned_known_item_does_not_stop_page(self):
        page = [aweme_item("pinned-old", 100), aweme_item("new", 300)]
        self.assertFalse(_page_reaches_boundary(page, {"pinned-old"}))

    def test_page_stops_only_when_every_item_is_known(self):
        page = [aweme_item("old-1", 100), aweme_item("old-2", 90)]
        self.assertTrue(_page_reaches_boundary(page, {"old-1", "old-2"}))
        self.assertFalse(_page_reaches_boundary(page, {"old-1"}))

    def test_subscription_time_boundary_requires_entire_page(self):
        self.assertTrue(_page_reaches_boundary(
            [aweme_item("a", 100), aweme_item("b", 99)], set(), stop_before=200))
        self.assertFalse(_page_reaches_boundary(
            [aweme_item("a", 100), aweme_item("b", 201)], set(), stop_before=200))


class InitialBackfillTests(unittest.TestCase):
    def setUp(self):
        # 故意打乱接口顺序，验证下载顺序按发布时间稳定化。
        self.items = [
            aweme_item("old-1", 100),
            aweme_item("new-2", 220),
            aweme_item("old-2", 150),
            aweme_item("new-1", 210),
            aweme_item("old-3", 180),
        ]

    def test_first_scan_defaults_to_posts_after_subscription(self):
        selected = _select_douyin_awemes(
            self.items, "highest", True, monitor_since=200,
            initial_backfill_count=0)
        self.assertEqual([a.aweme_id for a in selected], ["new-2", "new-1"])

    def test_first_scan_can_backfill_latest_n_historical_posts(self):
        selected = _select_douyin_awemes(
            self.items, "highest", True, monitor_since=200,
            initial_backfill_count=2)
        self.assertEqual(
            [a.aweme_id for a in selected],
            ["new-2", "new-1", "old-3", "old-2"],
        )

    def test_later_scan_never_turns_old_history_into_new_posts(self):
        selected = _select_douyin_awemes(
            self.items, "highest", False, monitor_since=200,
            initial_backfill_count=-1)
        self.assertEqual([a.aweme_id for a in selected], ["new-2", "new-1"])

    def test_full_backfill_is_sorted_newest_first(self):
        selected = _select_douyin_awemes(
            self.items, "highest", True, monitor_since=200,
            initial_backfill_count=-1)
        self.assertEqual(
            [a.aweme_id for a in selected],
            ["new-2", "new-1", "old-3", "old-2", "old-1"],
        )

    def test_legacy_partial_scan_reconciles_from_latest_known_post(self):
        self.assertEqual(
            _douyin_scan_since(monitor_since=200, known_create_times=[100, 150, 180]),
            180,
        )
        self.assertEqual(
            _douyin_scan_since(monitor_since=200, known_create_times=[210, 220]),
            200,
        )


if __name__ == "__main__":
    unittest.main()
