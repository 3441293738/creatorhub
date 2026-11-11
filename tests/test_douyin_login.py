import json
import unittest
from unittest.mock import AsyncMock, patch
from patchright.async_api import TimeoutError as BrowserTimeoutError

from app.browser.identity import Identity
from app.browser.login import interactive_login


class _Locator:
    def __init__(self, visible):
        self.visible = visible
        self.first = self

    async def is_visible(self, **_kwargs):
        return self.visible

    async def all(self):
        return [self]


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

    async def test_navigation_timeout_keeps_loaded_login_window_until_scan(self):
        context = _Context(login_visible=True)
        async def slow_navigation(url, **kwargs):
            context.page.url = url
            raise BrowserTimeoutError("DOMContentLoaded timed out")
        context.page.goto = AsyncMock(side_effect=slow_navigation)
        async def scan(_seconds):
            self.assertFalse(context.closed)
            context.page.login_visible = False
        with patch("app.browser.login.asyncio.sleep", new=scan):
            ok, state, _ = await interactive_login(
                _Manager(context), self._identity(), timeout_seconds=2)
        self.assertTrue(ok)
        self.assertTrue(state)
        context.page.goto.assert_awaited_once()
        self.assertTrue(context.closed)

    async def test_navigation_timeout_without_scan_never_reports_success(self):
        context = _Context(login_visible=True)
        context.page.url = "https://www.douyin.com/"
        context.page.goto = AsyncMock(side_effect=BrowserTimeoutError("timeout"))
        with patch("app.browser.login.asyncio.sleep", new=AsyncMock()):
            ok, state, _ = await interactive_login(
                _Manager(context), self._identity(), timeout_seconds=1)
        self.assertFalse(ok)
        self.assertEqual(state, "")
        self.assertTrue(context.closed)

    async def test_navigation_timeout_on_blank_or_error_page_has_clear_error(self):
        for url in ("about:blank", "chrome-error://chromewebdata/", "https://other.example/"):
            context = _Context()
            context.page.url = url
            context.page.goto = AsyncMock(side_effect=BrowserTimeoutError("timeout"))
            with self.assertRaisesRegex(RuntimeError, "检查网络或所选代理"):
                await interactive_login(_Manager(context), self._identity())
            self.assertEqual(context.cookie_reads, 0)
            self.assertTrue(context.closed)

    async def test_non_timeout_navigation_failure_is_not_swallowed(self):
        context = _Context()
        context.page.goto = AsyncMock(side_effect=RuntimeError("net::ERR_PROXY_CONNECTION_FAILED"))
        with self.assertRaisesRegex(RuntimeError, "ERR_PROXY_CONNECTION_FAILED"):
            await interactive_login(_Manager(context), self._identity())
        self.assertTrue(context.closed)

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
