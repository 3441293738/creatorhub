import json
import time
import unittest

from app.browser.manager import _bridge_cookies
from app.browser.identity import Identity
from app.browser.manager import BrowserManager


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


class ForcedBrowserCookieBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_visible_collection_overwrites_stale_profile_cookie(self):
        state = _state({
            "name": "sessionid",
            "value": "fresh-db-value",
            "domain": ".douyin.com",
            "path": "/",
        })

        class FakeContext:
            def __init__(self):
                self.added = []

            async def cookies(self):
                return [{
                    "name": "sessionid", "value": "stale-profile-value",
                    "domain": ".douyin.com", "path": "/",
                }]

            async def add_cookies(self, cookies):
                self.added.extend(cookies)

        context = FakeContext()
        identity = Identity(
            account_id=1, profile_dir="fixture", platform="douyin",
            bridge_states=(state,),
        )

        await BrowserManager._bridge_identity_cookies(
            context, identity, overwrite=True)

        self.assertEqual(len(context.added), 1)
        self.assertEqual(context.added[0]["value"], "fresh-db-value")


if __name__ == "__main__":
    unittest.main()
