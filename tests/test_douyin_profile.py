import unittest

from app.browser.fetcher import (
    _extract_post_author,
    _extract_user,
    _fill_missing_user_fields,
    _self_profile_session_is_invalid,
    _user_from_web_storage,
)


class DouyinSelfProfileFallbackTests(unittest.TestCase):
    def test_extracts_nested_profile_user(self):
        user = {"sec_uid": "MS4-self", "nickname": "账号"}
        self.assertIs(_extract_user({"data": {"user": user}}), user)

    def test_storage_uid_is_normalized_to_sec_uid(self):
        user = _user_from_web_storage({
            "uid": "MS4wLjABAAAAEqzjbyRfiK6bTxcILmSxYMpGf3zjIdN8N3u",
            "nickname": "账号",
            "avatarUrl": "https://img.example/avatar.jpeg",
        })
        self.assertEqual(
            user,
            {
                "sec_uid": "MS4wLjABAAAAEqzjbyRfiK6bTxcILmSxYMpGf3zjIdN8N3u",
                "nickname": "账号",
                "avatar_thumb": {
                    "url_list": ["https://img.example/avatar.jpeg"],
                },
            },
        )

    def test_numeric_internal_uid_is_not_treated_as_sec_uid(self):
        self.assertIsNone(_user_from_web_storage({
            "uid": "2299815398743292",
            "nickname": "账号",
        }))

    def test_post_author_must_match_logged_in_user(self):
        payload = {
            "aweme_list": [
                {"author": {"sec_uid": "other", "nickname": "别人"}},
                {"author": {"sec_uid": "self", "nickname": "本人"}},
            ],
        }
        self.assertEqual(
            _extract_post_author(payload, "self")["nickname"],
            "本人",
        )
        self.assertIsNone(_extract_post_author(payload, "missing"))

    def test_storage_fallback_does_not_override_profile_response(self):
        target = {
            "sec_uid": "self",
            "nickname": "接口昵称",
            "follower_count": 12,
        }
        _fill_missing_user_fields(target, {
            "sec_uid": "self",
            "nickname": "缓存昵称",
            "avatar_thumb": {"url_list": ["https://img.example/a.jpeg"]},
        })
        self.assertEqual(target["nickname"], "接口昵称")
        self.assertEqual(target["follower_count"], 12)
        self.assertIn("avatar_thumb", target)

    def test_visible_login_button_rejects_stale_profile_and_cookies(self):
        self.assertTrue(_self_profile_session_is_invalid(
            has_login_btn=True,
            has_login_cookie=True,
            has_result=True,
            profile_user_seen=True,
        ))

    def test_hidden_login_button_accepts_authoritative_profile(self):
        self.assertFalse(_self_profile_session_is_invalid(
            has_login_btn=False,
            has_login_cookie=True,
            has_result=True,
            profile_user_seen=True,
        ))

    def test_storage_profile_requires_login_cookie(self):
        self.assertTrue(_self_profile_session_is_invalid(
            has_login_btn=False,
            has_login_cookie=False,
            has_result=True,
            profile_user_seen=False,
        ))


if __name__ == "__main__":
    unittest.main()
