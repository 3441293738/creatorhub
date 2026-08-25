import asyncio
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from patchright.async_api import async_playwright

from app.browser.login import (
    XhsSecurityVerificationRequired,
    _is_xhs_security_verification_url,
    _reuse_or_create_login_page,
    _xhs_page_identity,
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

    @staticmethod
    def _qr_status_response(code_status: int):
        response = AsyncMock()
        response.url = (
            "https://www.xiaohongshu.com"
            "/api/sns/web/v1/login/qrcode/status"
        )
        response.status = 200
        response.json.return_value = {
            "code": 0,
            "success": True,
            "data": {"code_status": code_status},
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

    async def test_page_identity_requires_current_user_id(self):
        page = AsyncMock()
        page.evaluate.side_effect = [
            {"user_id": "", "red_id": "", "nickname": "guest"},
            {"user_id": "member-id", "nickname": "fixture-member"},
        ]

        self.assertEqual(await _xhs_page_identity(page), {})
        self.assertEqual(
            await _xhs_page_identity(page),
            {"user_id": "member-id", "nickname": "fixture-member"},
        )

    async def test_unfinished_security_verification_returns_clear_error(self):
        manager = AsyncMock()
        context = AsyncMock()
        page = AsyncMock()
        manager.context_for.return_value = context
        context.pages = [page]
        context.on = MagicMock()
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
        status_callback = AsyncMock()

        with self.assertRaisesRegex(
            XhsSecurityVerificationRequired,
            "小红书要求完成设备安全验证",
        ):
            await interactive_xhs_login(
                manager, object(), status_callback=status_callback)

        status_callback.assert_awaited_once()
        context.close.assert_not_awaited()
        page.close.assert_awaited_once()

    async def test_login_accepts_authenticated_second_xhs_tab(self):
        manager = AsyncMock()
        context = AsyncMock()
        captcha_page = AsyncMock()
        healthy_page = AsyncMock()
        manager.context_for.return_value = context
        context.pages = [captcha_page, healthy_page]
        context.on = MagicMock()
        context.storage_state.return_value = {
            "cookies": [{"name": "web_session", "value": "w" * 24}]
        }
        captcha_page.url = "about:blank"
        captcha_page.is_closed = MagicMock(return_value=False)
        captcha_page.on = MagicMock()
        healthy_page.url = "https://www.xiaohongshu.com/explore"
        response = self._user_me_response(
            guest=False, user_id="manual-tab-user")

        def healthy_on(event, listener):
            if event == "response":
                asyncio.get_running_loop().create_task(listener(response))

        healthy_page.on = MagicMock(side_effect=healthy_on)

        async def cookies():
            # Let the response listener scheduled for the manually opened tab
            # finish before the login state is evaluated.
            await asyncio.sleep(0)
            return [{"name": "web_session", "value": "w" * 24}]

        context.cookies.side_effect = cookies

        async def goto(_url, **_kwargs):
            captcha_page.url = (
                "https://www.xiaohongshu.com/website-login/captcha"
                "?verifyType=124&verifyBiz=461"
            )

        captcha_page.goto.side_effect = goto

        @asynccontextmanager
        async def visible_page(_identity):
            try:
                yield captcha_page
            finally:
                await captcha_page.close()

        manager.visible_page = visible_page
        manager.xhs_interaction.pause = AsyncMock()

        with patch(
            "app.browser.login._read_xhs_nickname",
            new_callable=AsyncMock,
            return_value="",
        ):
            logged, state, nickname = await interactive_xhs_login(
                manager, object(), timeout_seconds=2)

        self.assertTrue(logged)
        self.assertIn("web_session", state)
        self.assertEqual(nickname, "")
        healthy_page.on.assert_called()
        captcha_page.close.assert_awaited_once()

    async def test_security_page_gets_one_visible_recovery_navigation(self):
        manager = AsyncMock()
        context = AsyncMock()
        captcha_page = AsyncMock()
        manager.context_for.return_value = context
        context.pages = [captcha_page]
        context.on = MagicMock()
        context.storage_state.return_value = {
            "cookies": [{"name": "web_session", "value": "r" * 24}]
        }
        context.cookies.return_value = [
            {"name": "web_session", "value": "r" * 24}
        ]
        captcha_page.url = "about:blank"
        captcha_page.is_closed = MagicMock(return_value=False)
        response_listener = {}

        def page_on(event, listener):
            if event == "response":
                response_listener["handler"] = listener

        captcha_page.on = MagicMock(side_effect=page_on)
        navigation_count = 0

        async def initial_goto(_url, **_kwargs):
            nonlocal navigation_count
            navigation_count += 1
            if navigation_count == 1:
                captcha_page.url = (
                    "https://www.xiaohongshu.com/website-login/captcha"
                    "?verifyType=124&verifyBiz=461"
                )
                return
            captcha_page.url = "https://www.xiaohongshu.com/explore"
            handler = response_listener.get("handler")
            if handler is not None:
                await handler(self._user_me_response(
                    guest=False, user_id="recovered-user"))

        captcha_page.goto.side_effect = initial_goto

        @asynccontextmanager
        async def visible_page(_identity):
            try:
                yield captcha_page
            finally:
                await captcha_page.close()

        manager.visible_page = visible_page
        manager.xhs_interaction.pause = AsyncMock()
        status_callback = AsyncMock()

        with (
            patch(
                "app.browser.login._XHS_SECURITY_RETRY_DELAY_SECONDS", 0,
            ),
            patch(
                "app.browser.login._read_xhs_nickname",
                new_callable=AsyncMock,
                return_value="",
            ),
        ):
            logged, state, nickname = await interactive_xhs_login(
                manager, object(), timeout_seconds=2,
                status_callback=status_callback)

        self.assertTrue(logged)
        self.assertIn("web_session", state)
        self.assertEqual(nickname, "")
        self.assertEqual(captcha_page.goto.await_count, 2)
        self.assertEqual(
            captcha_page.goto.await_args_list[1].args[0],
            "https://www.xiaohongshu.com/",
        )
        self.assertGreaterEqual(captcha_page.bring_to_front.await_count, 2)
        self.assertEqual(status_callback.await_count, 2)
        self.assertIn("website-login/captcha",
                      status_callback.await_args_list[0].args[0])
        self.assertEqual(
            status_callback.await_args_list[1].args[0],
            "https://www.xiaohongshu.com/explore",
        )

    async def test_web_login_starts_from_homepage_and_skips_creator_platform(self):
        manager = AsyncMock()
        context = AsyncMock()
        page = AsyncMock()
        manager.context_for.return_value = context
        context.pages = [page]
        context.on = MagicMock()
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

    async def test_changed_web_session_reloads_and_confirms_identity(self):
        manager = AsyncMock()
        context = AsyncMock()
        page = AsyncMock()
        manager.context_for.return_value = context
        context.pages = [page]
        context.on = MagicMock()
        context.cookies.side_effect = [
            [{"name": "web_session", "value": "guest-session-000000000001"}],
            [{"name": "web_session", "value": "member-session-00000000002"}],
        ]
        context.storage_state.return_value = {
            "cookies": [{
                "name": "web_session",
                "value": "member-session-00000000002",
            }],
        }
        page.url = "about:blank"
        page.is_closed = MagicMock(return_value=False)
        response_listener = {}

        def on(event, listener):
            if event == "response":
                response_listener["handler"] = listener

        page.on = MagicMock(side_effect=on)

        async def goto(_url, **_kwargs):
            page.url = "https://www.xiaohongshu.com/explore"
            handler = response_listener.get("handler")
            if handler is not None:
                await handler(self._user_me_response(guest=True))

        reload_count = 0

        async def reload(**_kwargs):
            nonlocal reload_count
            reload_count += 1
            handler = response_listener.get("handler")
            if handler is not None:
                await handler(self._user_me_response(
                    guest=reload_count == 1,
                    user_id="" if reload_count == 1 else "member-user",
                ))

        page.goto.side_effect = goto
        page.reload.side_effect = reload

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
                "app.browser.login._XHS_IDENTITY_RECHECK_DELAY_SECONDS", 0,
            ),
            patch(
                "app.browser.login._read_xhs_nickname",
                new_callable=AsyncMock,
                return_value="fixture-member",
            ),
        ):
            logged, state, nickname = await interactive_xhs_login(
                manager, object(), timeout_seconds=2)

        self.assertTrue(logged)
        self.assertIn("member-session", state)
        self.assertEqual(nickname, "fixture-member")
        self.assertEqual(page.reload.await_count, 2)

    async def test_page_state_confirms_login_without_new_user_me_response(self):
        manager = AsyncMock()
        context = AsyncMock()
        page = AsyncMock()
        manager.context_for.return_value = context
        context.pages = [page]
        context.on = MagicMock()
        context.cookies.return_value = [
            {"name": "web_session", "value": "stable-session-00000000001"}
        ]
        context.storage_state.return_value = {
            "cookies": [{
                "name": "web_session",
                "value": "stable-session-00000000001",
            }],
        }
        page.url = "about:blank"
        page.is_closed = MagicMock(return_value=False)
        page.on = MagicMock()
        page.evaluate.return_value = {
            "user_id": "page-state-member",
            "nickname": "page-state-name",
            "guest": False,
        }

        async def goto(_url, **_kwargs):
            page.url = "https://www.xiaohongshu.com/explore"

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
            manager, object(), timeout_seconds=2)

        self.assertTrue(logged)
        self.assertIn("stable-session", state)
        self.assertEqual(nickname, "page-state-name")

    async def test_qrcode_status_two_confirms_login_without_user_me(self):
        manager = AsyncMock()
        context = AsyncMock()
        page = AsyncMock()
        manager.context_for.return_value = context
        context.pages = [page]
        context.on = MagicMock()
        context.cookies.return_value = [
            {"name": "web_session", "value": "qr-member-session-00000001"}
        ]
        context.storage_state.return_value = {
            "cookies": [{
                "name": "web_session",
                "value": "qr-member-session-00000001",
            }],
        }
        page.url = "about:blank"
        page.is_closed = MagicMock(return_value=False)
        page.evaluate.return_value = None
        response_listener = {}

        def on(event, listener):
            if event == "response":
                response_listener["handler"] = listener

        page.on = MagicMock(side_effect=on)

        async def goto(_url, **_kwargs):
            page.url = "https://www.xiaohongshu.com/explore"
            handler = response_listener.get("handler")
            if handler is not None:
                await handler(self._qr_status_response(2))

        page.goto.side_effect = goto

        @asynccontextmanager
        async def visible_page(_identity):
            try:
                yield page
            finally:
                await page.close()

        manager.visible_page = visible_page
        manager.xhs_interaction.pause = AsyncMock()

        with patch(
            "app.browser.login._read_xhs_nickname",
            new_callable=AsyncMock,
            return_value="",
        ):
            logged, state, nickname = await interactive_xhs_login(
                manager, object(), timeout_seconds=2)

        self.assertTrue(logged)
        self.assertIn("qr-member-session", state)
        self.assertEqual(nickname, "")

    async def test_rotated_guest_web_session_is_not_treated_as_logged_in(self):
        manager = AsyncMock()
        context = AsyncMock()
        page = AsyncMock()
        manager.context_for.return_value = context
        context.pages = [page]
        context.on = MagicMock()
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
