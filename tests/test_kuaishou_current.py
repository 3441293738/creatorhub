import asyncio
import unittest

import app.main  # noqa: F401 - initializes the package import graph used by browser modules
from app.browser.ks_fetcher import _dig_comments, _extract_rest_profile
from app.browser.login import (
    _KS_HOME_URL,
    _click_ks_login_button,
    _ks_web_login_ready,
)
from app.platforms.kuaishou.extract import (
    flatten_ks_comments,
    parse_ks_comment,
    parse_self_user,
)
from app.models import PublishTask
from app.platforms.kuaishou.publish import (
    IMAGE_URL,
    MANAGE_URL,
    VIDEO_URL,
    _published_work_url,
    _upload_files,
)


class _LoginCandidate:
    def __init__(self, visible):
        self.visible = visible
        self.clicked = False

    async def is_visible(self):
        return self.visible

    async def click(self, **_kwargs):
        self.clicked = True


class _LoginLocator:
    def __init__(self, candidates):
        self.candidates = candidates

    async def count(self):
        return len(self.candidates)

    def nth(self, index):
        return self.candidates[index]


class _LoginPage:
    def __init__(self, candidates):
        self.candidates = candidates

    def locator(self, _selector):
        return _LoginLocator(self.candidates)


class _UploadButton(_LoginCandidate):
    pass


class _UploadButtonLocator(_LoginLocator):
    pass


class _Chooser:
    def __init__(self):
        self.files = None

    async def set_files(self, files):
        self.files = files


class _ChooserInfo:
    def __init__(self, chooser):
        self._chooser = chooser

    @property
    def value(self):
        async def get():
            return self._chooser
        return get()


class _ChooserContext:
    def __init__(self, chooser):
        self.info = _ChooserInfo(chooser)

    async def __aenter__(self):
        return self.info

    async def __aexit__(self, *_args):
        return False


class _UploadPage:
    def __init__(self):
        self.button = _UploadButton(True)
        self.chooser = _Chooser()
        self.requested_name = ""

    def expect_file_chooser(self, **_kwargs):
        return _ChooserContext(self.chooser)

    def get_by_role(self, _role, *, name, exact):
        self.requested_name = name
        assert exact is True
        return _UploadButtonLocator([self.button])


class KuaishouLoginTests(unittest.TestCase):
    def test_login_opens_rendered_recommendation_page(self):
        self.assertEqual(_KS_HOME_URL, "https://www.kuaishou.com/new-reco")

    def test_current_webday7_cookie_is_accepted_with_user_id(self):
        self.assertTrue(_ks_web_login_ready({
            "userId", "kuaishou.server.webday7_st",
        }))
        self.assertTrue(_ks_web_login_ready({"passToken"}))
        self.assertFalse(_ks_web_login_ready({"userId"}))

    def test_only_visible_login_button_is_clicked(self):
        hidden = _LoginCandidate(False)
        visible = _LoginCandidate(True)
        clicked = asyncio.run(_click_ks_login_button(
            _LoginPage([hidden, visible]), ("selector",)))
        self.assertTrue(clicked)
        self.assertFalse(hidden.clicked)
        self.assertTrue(visible.clicked)


class KuaishouProfileTests(unittest.TestCase):
    def test_current_flat_profile_response_is_normalized(self):
        raw = {
            "result": 1,
            "eid": "3xPUBLIC",
            "userName": "账号名称",
            "userId": 123456,
            "userDefineId": "KS-NAME",
            "userHead": "https://img.example/avatar.jpg",
            "fans": 23,
            "follows": 7,
            "like": 999,
            "sex": "M",
            "photoCount": 4,
        }
        profile, keys = _extract_rest_profile(raw)
        self.assertIn("eid", keys)
        parsed = parse_self_user(profile)
        self.assertEqual(parsed["nickname"], "账号名称")
        self.assertEqual(parsed["sec_uid"], "3xPUBLIC")
        self.assertEqual(parsed["douyin_id"], "KS-NAME")
        self.assertEqual(parsed["avatar"], "https://img.example/avatar.jpg")
        self.assertEqual(parsed["follower_count"], 23)
        self.assertEqual(parsed["following_count"], 7)
        self.assertEqual(parsed["aweme_count"], 4)
        self.assertEqual(parsed["total_favorited"], 999)
        self.assertEqual(parsed["gender"], "M")


class KuaishouCommentTests(unittest.TestCase):
    def test_graphql_root_comments_v2_and_replies_are_parsed(self):
        reply = {
            "commentId": "reply-1", "content": "回复", "authorName": "乙",
            "timestamp": 1_700_000_000_000,
        }
        root = {
            "commentId": "root-1", "content": "评论", "authorName": "甲",
            "timestamp": 1_700_000_000_000, "subCommentsV2": [reply],
        }
        payload = {
            "data": {"visionCommentList": {"rootCommentsV2": [root]}}
        }
        roots = _dig_comments(payload)
        self.assertEqual(roots, [root])
        flattened = flatten_ks_comments(roots)
        self.assertEqual([item["commentId"] for item in flattened],
                         ["root-1", "reply-1"])
        self.assertEqual(parse_ks_comment(flattened[0])["text"], "评论")


class KuaishouPublishRouteTests(unittest.TestCase):
    def test_images_and_video_use_the_current_unified_page(self):
        self.assertEqual(IMAGE_URL, VIDEO_URL)
        self.assertTrue(VIDEO_URL.endswith("/article/publish/video"))

    def test_publish_response_photo_id_builds_direct_work_url(self):
        self.assertEqual(_published_work_url({
            "result": 1, "data": {"photoId": "3xNEWPHOTO123"},
        }), "https://www.kuaishou.com/short-video/3xNEWPHOTO123")

    def test_old_publish_form_result_is_normalized_to_manage_page(self):
        task = PublishTask(
            platform="kuaishou", status="done", result_url=VIDEO_URL,
            media_json="[]",
        )
        self.assertEqual(app.main._publish_dict(task)["result_url"], MANAGE_URL)

    def test_image_upload_uses_the_real_upload_button_and_file_chooser(self):
        page = _UploadPage()
        uploaded = asyncio.run(_upload_files(page, "images", ["one.png", "two.jpg"]))
        self.assertTrue(uploaded)
        self.assertEqual(page.requested_name, "上传图片")
        self.assertTrue(page.button.clicked)
        self.assertEqual(page.chooser.files, ["one.png", "two.jpg"])


if __name__ == "__main__":
    unittest.main()
