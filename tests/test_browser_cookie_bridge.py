import json
import time
import unittest

from app.browser.manager import _bridge_cookies


def _state(*cookies):
    return json.dumps({"cookies": list(cookies), "origins": []})


class BrowserCookieBridgeTests(unittest.TestCase):
    def test_restores_session_cookie_missing_from_non_empty_profile(self):
        state = _state(
            {
                "name": "_finder_auth",
                "value": "fresh-login-token",
                "domain": ".weixin.qq.com",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "None",
            }
        )

        bridged = _bridge_cookies(
            (state,),
            existing=[
                {
                    "name": "wxuin",
                    "value": "persisted",
                    "domain": ".weixin.qq.com",
                    "path": "/",
                }
            ],
        )

        self.assertEqual([cookie["name"] for cookie in bridged], ["_finder_auth"])

    def test_existing_profile_cookie_is_never_overwritten_by_db_snapshot(self):
        state = _state(
            {
                "name": "sessionid",
                "value": "older-db-value",
                "domain": ".weixin.qq.com",
                "path": "/",
            }
        )

        bridged = _bridge_cookies(
            (state,),
            existing=[
                {
                    "name": "sessionid",
                    "value": "newer-profile-value",
                    "domain": "weixin.qq.com",
                    "path": "/",
                }
            ],
        )

        self.assertEqual(bridged, [])

    def test_expired_cookie_is_not_restored(self):
        state = _state(
            {
                "name": "_finder_auth",
                "value": "expired",
                "domain": ".weixin.qq.com",
                "path": "/",
                "expires": time.time() - 60,
            }
        )

        self.assertEqual(_bridge_cookies((state,), existing=[]), [])


if __name__ == "__main__":
    unittest.main()
