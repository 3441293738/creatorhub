"""小红书 feed 解析的离线回归:EF 编码档位与作者信息保留。"""
import unittest

from app.platforms.xhs.extract import parse_note_brief, parse_note_detail


def _video_card(stream, *, note_id="note-1", user=None):
    return {
        "note_id": note_id,
        "type": "video",
        "title": "测试视频",
        "user": user if user is not None else {"nickname": "作者甲", "user_id": "u-1"},
        "interact_info": {"liked_count": 10, "comment_count": 2},
        "video": {"media": {"stream": stream}},
    }


class XhsVideoStreamTest(unittest.TestCase):
    def test_prefers_ef4_even_when_other_tiers_are_larger(self):
        aw = parse_note_detail(_video_card({
            "EF4": [{
                "master_url": "https://cdn.test/ef4-720.mp4",
                "width": 720, "height": 1280,
                "video_bitrate": 3_800_000, "size": 12_466_031,
            }],
            "EF5": [{
                "master_url": "https://cdn.test/ef5-2160.mp4",
                "width": 3840, "height": 2160,
                "video_bitrate": 12_000_000, "size": 60_000_000,
            }],
            "EF6": [{"master_url": "https://cdn.test/ef6-1080.mp4",
                     "width": 1080, "height": 1920}],
            "EF7": [{"master_url": "https://cdn.test/ef7-540.mp4",
                     "width": 540, "height": 960}],
        }), {})

        self.assertIsNotNone(aw)
        self.assertEqual([m.url for m in aw.medias], ["https://cdn.test/ef4-720.mp4"])
        self.assertEqual(aw.author_name, "作者甲")
        self.assertEqual(aw.platform, "xhs")

    def test_picks_highest_variant_inside_ef4(self):
        aw = parse_note_detail(_video_card({
            "EF4": [
                {"master_url": "https://cdn.test/ef4-720.mp4",
                 "width": 720, "height": 1280, "video_bitrate": 3_000_000},
                {"master_url": "https://cdn.test/ef4-1080.mp4",
                 "width": 1080, "height": 1920, "video_bitrate": 5_000_000},
            ],
        }), {})

        self.assertEqual(aw.medias[0].url, "https://cdn.test/ef4-1080.mp4")

    def test_legacy_h264_and_backup_url_are_supported(self):
        aw = parse_note_detail(_video_card({
            "h264": [{"backup_urls": ["https://cdn.test/legacy-h264.mp4"]}],
        }), {})

        self.assertEqual(aw.medias[0].url, "https://cdn.test/legacy-h264.mp4")

    def test_missing_media_keeps_author_and_metadata(self):
        card = _video_card({})
        card["image_list"] = [{
            "info_list": [{"image_scene": "WB_DFT", "url": "https://cdn.test/cover.jpg"}],
        }]
        aw = parse_note_detail(card, {})

        self.assertIsNotNone(aw)
        self.assertEqual(aw.medias, [])
        self.assertEqual(aw.author_name, "作者甲")
        self.assertEqual(aw.desc, "测试视频")
        self.assertEqual(aw.cover, "https://cdn.test/cover.jpg")

    def test_author_falls_back_to_search_brief(self):
        aw = parse_note_detail(
            {"note_id": "note-2", "type": "video", "video": {}},
            {"note_id": "note-2", "author_name": "搜索页作者"},
        )

        self.assertEqual(aw.author_name, "搜索页作者")

    def test_brief_carries_author_from_inner_or_outer_card(self):
        inner = parse_note_brief({
            "note_card": {
                "note_id": "note-3", "type": "video",
                "user": {"nickname": "卡片作者", "user_id": "u-3"},
            },
        })
        outer = parse_note_brief({
            "note_card": {"note_id": "note-4", "type": "video"},
            "user": {"nickname": "外层作者", "user_id": "u-4"},
        })

        self.assertEqual((inner["author_name"], inner["author_id"]), ("卡片作者", "u-3"))
        self.assertEqual((outer["author_name"], outer["author_id"]), ("外层作者", "u-4"))


if __name__ == "__main__":
    unittest.main()
