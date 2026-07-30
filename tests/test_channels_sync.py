import unittest

from app.browser.account_hub import _norm_channels_work
from app.browser.channels_fetcher import (
    POST_IMAGE_LIST_URL,
    POST_LIST_URLS,
    POST_VIDEO_LIST_URL,
    _dig_posts,
    _obj_id,
)
from app.platforms.channels.extract import parse_channels_feed


def _photo_item():
    return {
        "objectId": "export/PHOTO_ID",
        "exportId": "export/PHOTO_ID",
        "createTime": 1_785_000_000,
        "likeCount": 2,
        "commentCount": 3,
        "readCount": 4,
        "favCount": 5,
        "forwardCount": 6,
        "desc": {
            "description": "图文作品",
            "mediaType": 2,
            "media": [{
                "mediaType": 2,
                "url": "https://example.test/photo.jpg",
                "thumbUrl": "https://example.test/thumb.jpg",
            }],
        },
    }


class ChannelsSyncTests(unittest.TestCase):
    def test_fetcher_opens_both_video_and_photo_routes(self):
        self.assertEqual(
            POST_LIST_URLS,
            (POST_VIDEO_LIST_URL, POST_IMAGE_LIST_URL),
        )
        self.assertIn("/post/list", POST_VIDEO_LIST_URL)
        self.assertIn("/post/finderNewLifePostList", POST_IMAGE_LIST_URL)

    def test_current_post_list_shape_is_extracted(self):
        item = _photo_item()
        payload = {
            "errCode": 0,
            "data": {"list": [item], "totalCount": 1},
        }
        self.assertEqual(_dig_posts(payload), [item])
        self.assertEqual(_obj_id(item), "export/PHOTO_ID")

    def test_account_hub_normalizes_current_desc_shape(self):
        work = _norm_channels_work(_photo_item())
        self.assertIsNotNone(work)
        self.assertEqual(work["item_id"], "export/PHOTO_ID")
        self.assertEqual(work["desc"], "图文作品")
        self.assertEqual(work["media_type"], "images")
        self.assertEqual(work["cover_url"], "https://example.test/thumb.jpg")

    def test_platform_parser_normalizes_current_desc_shape(self):
        aweme = parse_channels_feed(_photo_item())
        self.assertIsNotNone(aweme)
        self.assertEqual(aweme.aweme_id, "export/PHOTO_ID")
        self.assertEqual(aweme.desc, "图文作品")
        self.assertEqual(aweme.media_type, "images")


if __name__ == "__main__":
    unittest.main()
