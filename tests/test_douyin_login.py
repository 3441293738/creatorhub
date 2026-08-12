import json
import unittest
from unittest.mock import AsyncMock, patch

from app.browser.identity import Identity
from app.browser.login import interactive_login


class _Locator:
    def __init__(self, visible):
        self.visible = visible
        self.first = self

    async def is_visible(self, **_kwargs):
        return self.visible


class _Page:
    def __init__(self, *, login_visible=False):
        self.url = "about:blank"
        self.login_visible = login_visible

    async def bring_to_front(self):
        return None

    async def goto(self, url, **_kwargs):
        self.url = url

    async def click(self, *_args, **_kwargs):
        return None

    def is_closed(self):
        return False

    def get_by_text(self, *_args, **_kwargs):
        return _Locator(self.login_visible)

    async def wait_for_timeout(self, _milliseconds):
        return None

    async def evaluate(self, _script):
        return "新登录账号"


class _Context:
    def __init__(self, *, login_visible=False, rotate_after_clear=True):
        self.page = _Page(login_visible=login_visible)
        self.cleared = False
        self.cookie_reads = 0
        self.rotate_after_clear = rotate_after_clear
        self.closed = False

    async def clear_cookies(self):
        self.cleared = True
        self.cookie_reads = 0

    async def new_page(self):
        return self.page

    async def cookies(self):
        self.cookie_reads += 1
        if not self.cleared or (self.rotate_after_clear and self.cookie_reads >= 2):
            return [{"name": "sessionid", "value": "fixture-new-session"}]
        return []

    async def storage_state(self):
        return {"cookies": await self.cookies(), "origins": []}

    async def close(self):
        self.closed = True


class _Manager:
    def __init__(self, context):
        self.context = context

    async def open_headed(self, _identity):
        return self.context


class DouyinReloginTests(unittest.IsolatedAsyncioTestCase):
    def _identity(self):
        return Identity(account_id=1, profile_dir="fixture", platform="douyin")

    async def test_force_reauth_clears_stale_cookies_before_polling(self):
        context = _Context()
        with patch("app.browser.login.asyncio.sleep", new=AsyncMock()):
            ok, state, nickname = await interactive_login(
                _Manager(context), self._identity(),
                timeout_seconds=1, force_reauth=True,
            )

        self.assertTrue(context.cleared)
        self.assertTrue(ok)
        self.assertEqual(nickname, "新登录账号")
        self.assertEqual(
            json.loads(state)["cookies"][0]["value"],
            "fixture-new-session",
        )

    async def test_visible_login_dialog_blocks_cookie_only_success(self):
        context = _Context(login_visible=True, rotate_after_clear=False)
        with patch("app.browser.login.asyncio.sleep", new=AsyncMock()):
            ok, state, _nickname = await interactive_login(
                _Manager(context), self._identity(), timeout_seconds=0.1,
            )

        self.assertFalse(ok)
        self.assertEqual(state, "")


if __name__ == "__main__":
    unittest.main()
