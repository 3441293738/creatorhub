import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from playwright.async_api import async_playwright

from app.browser.login import (
    XhsSecurityVerificationRequired,
    _is_xhs_security_verification_url,
    _reuse_or_create_login_page,
    interactive_xhs_login,
)


class XhsLoginPageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.context = await self.browser.new_context()
        self.page = await self.context.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def test_login_reuses_persistent_context_initial_blank_page(self):
        login_page = await _reuse_or_create_login_page(self.context)

        self.assertIs(login_page, self.page)
        self.assertEqual(len(self.context.pages), 1)

    async def test_login_creates_page_when_context_has_no_blank_page(self):
        await self.page.goto("data:text/html,<main>existing</main>")

        login_page = await _reuse_or_create_login_page(self.context)

        self.assertIsNot(login_page, self.page)
        self.assertEqual(login_page.url, "about:blank")
        self.assertEqual(len(self.context.pages), 2)


class XhsWebLoginIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _user_me_response(*, guest: bool, user_id: str = ""):
        response = AsyncMock()
        response.url = "https://www.xiaohongshu.com/api/sns/web/v2/user/me"
        response.status = 200
        response.json.return_value = {
            "data": {"guest": guest, "user_id": user_id},
        }
        return response

    async def test_security_verification_url_is_recognized(self):
        self.assertTrue(_is_xhs_security_verification_url(
            "https://www.xiaohongshu.com/website-login/captcha?verifyType=124"
        ))
        self.assertTrue(_is_xhs_security_verification_url(
            "https://www.xiaohongshu.com/explore?error_code=300012"
        ))
        self.assertFalse(_is_xhs_security_verification_url(
            "https://www.xiaohongshu.com/explore"
        ))

    async def test_unfinished_security_verification_returns_clear_error(self):
        manager = AsyncMock()
        context = AsyncMock()
        page = AsyncMock()
        manager.context_for.return_value = context
        context.cookies.side_effect = [[], []]
        page.url = "about:blank"
        page.is_closed = MagicMock(side_effect=[False, True])
        page.on = MagicMock()

        async def goto(_url, **_kwargs):
            page.url = (
                "https://www.xiaohongshu.com/website-login/captcha"
                "?verifyType=124&verifyBiz=461"
            )

        page.goto.side_effect = goto

        @asynccontextmanager
        async def visible_page(_identity):
            try:
                yield page
            finally:
                await page.close()

        manager.visible_page = visible_page
        manager.xhs_interaction.pause = AsyncMock()

        with self.assertRaisesRegex(
            XhsSecurityVerificationRequired,
            "小红书要求完成设备安全验证",
        ):
            await interactive_xhs_login(manager, object())

        context.close.assert_not_awaited()
        page.close.assert_awaited_once()

    async def test_web_login_starts_from_homepage_and_skips_creator_platform(self):
        manager = AsyncMock()
        context = AsyncMock()
        page = AsyncMock()
        manager.context_for.return_value = context
        context.cookies.side_effect = [
            [],
            [{"name": "web_session", "value": "w" * 24}],
        ]
        context.storage_state.return_value = {
            "cookies": [{"name": "web_session", "value": "w" * 24}]
        }
        page.url = "about:blank"
        page.is_closed = MagicMock(side_effect=[False, False, True])
        visited = []
        response_listener = {}

        def on(event, listener):
            if event == "response":
                response_listener["handler"] = listener

        page.on = MagicMock(side_effect=on)
        page.remove_listener = MagicMock()

        async def goto(url, **_kwargs):
            visited.append(url)
            page.url = url
            handler = response_listener.get("handler")
            if handler is not None:
                await handler(self._user_me_response(
                    guest=False, user_id="fixture-user"))

        page.goto.side_effect = goto

        @asynccontextmanager
        async def visible_page(_identity):
            try:
                yield page
            finally:
                await page.close()

        manager.visible_page = visible_page
        manager.xhs_interaction.pause = AsyncMock()
        with (
            patch(
                "app.browser.login._read_xhs_nickname",
                new_callable=AsyncMock,
                return_value="普通账号",
            ),
        ):
            logged, state, nickname = await interactive_xhs_login(manager, object())

        self.assertTrue(logged)
        self.assertEqual(nickname, "普通账号")
        self.assertIn("web_session", state)
        self.assertEqual(visited, ["https://www.xiaohongshu.com/"])
        context.close.assert_not_awaited()
        page.close.assert_awaited_once()

    async def test_rotated_guest_web_session_is_not_treated_as_logged_in(self):
        manager = AsyncMock()
        context = AsyncMock()
        page = AsyncMock()
        manager.context_for.return_value = context
        context.cookies.side_effect = [
            [{"name": "web_session", "value": "guest-session-before-0001"}],
            [{"name": "web_session", "value": "guest-session-after-00002"}],
        ]
        context.storage_state.return_value = {
            "cookies": [{
                "name": "web_session",
                "value": "guest-session-after-00002",
            }],
        }
        page.url = "about:blank"
        page.is_closed = MagicMock(side_effect=[False, False, True])
        response_listener = {}

        def on(event, listener):
            if event == "response":
                response_listener["handler"] = listener

        page.on = MagicMock(side_effect=on)
        page.remove_listener = MagicMock()

        async def goto(url, **_kwargs):
            page.url = url
            handler = response_listener.get("handler")
            if handler is not None:
                await handler(self._user_me_response(guest=True))

        page.goto.side_effect = goto

        @asynccontextmanager
        async def visible_page(_identity):
            try:
                yield page
            finally:
                await page.close()

        manager.visible_page = visible_page
        manager.xhs_interaction.pause = AsyncMock()

        logged, state, nickname = await interactive_xhs_login(
            manager, object(), timeout_seconds=10)

        self.assertFalse(logged)
        self.assertEqual(state, "")
        self.assertEqual(nickname, "")
        context.storage_state.assert_not_awaited()
        page.close.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
