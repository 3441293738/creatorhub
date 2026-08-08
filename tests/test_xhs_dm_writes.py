import asyncio
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

from app.browser.xhs_dm import send_xhs_dm_page


class _Response:
    def __init__(
            self, payload, *,
            url="https://www.xiaohongshu.com/api/im/web/message/send",
            status=200, method="POST"):
        self.payload = payload
        self.url = url
        self.status = status
        self.request = type("Request", (), {"method": method})()

    async def json(self):
        return self.payload


class _MissingLocator:
    @property
    def first(self):
        return self

    async def count(self):
        return 0

    async def is_visible(self):
        return False

    async def is_enabled(self):
        return False


class _MessageLocator:
    def __init__(self, page, text):
        self.page = page
        self.text = text

    @property
    def first(self):
        return self

    async def count(self):
        count = self.page.message_counts.get(self.text, 0)
        if self.page.editor_contributes_match \
                and self.page.current_text == self.text:
            count += 1
        return count

    async def is_visible(self):
        return bool(await self.count())


class _Locator:
    def __init__(self, page, name, *, enabled=True):
        self.page = page
        self.name = name
        self.enabled = enabled
        self.click_count = 0
        self.enter_count = 0
        self.value = ""

    @property
    def first(self):
        return self

    async def count(self):
        return 1

    async def is_visible(self):
        return True

    async def is_enabled(self):
        return self.enabled

    async def click(self, *_args, **_kwargs):
        self.click_count += 1
        if self.name == "submit":
            self.page.after_submit()
            if self.page.click_error is not None:
                raise self.page.click_error

    async def press(self, key):
        if key == "Enter":
            self.enter_count += 1
            self.page.after_submit()


class _Page:
    def __init__(
            self, *, has_entry=True, has_editor=True, has_button=True,
            click_error=None, response=None, existing_count=0,
            add_message_after_submit=False,
            editor_contributes_match=False,
            editor_ready_after_pauses=0,
            button_ready_after_pauses=0):
        self.url = (
            "https://www.xiaohongshu.com/user/profile/fixture"
            "?xsec_token=secret-token#private-fragment")
        self.has_entry = has_entry
        self.has_editor = has_editor
        self.has_button = has_button
        self.click_error = click_error
        self.response = response
        self.add_message_after_submit = add_message_after_submit
        self.editor_contributes_match = editor_contributes_match
        self.editor_ready_after_pauses = editor_ready_after_pauses
        self.button_ready_after_pauses = button_ready_after_pauses
        self.pause_count = 0
        self.entry = _Locator(self, "entry")
        self.editor = _Locator(self, "editor")
        self.submit = _Locator(self, "submit")
        self.response_listener = None
        self.message_counts = {"唯一消息": existing_count}
        self.current_text = ""
        self.closed = False

    def locator(self, selector):
        if selector in {
                'textarea[placeholder*="发送"]',
                'div[contenteditable="true"][placeholder*="发送"]',
                'textarea[placeholder*="私信"]',
                'div[contenteditable="true"]',
                "textarea",
                'input[type="text"]'}:
            ready = self.pause_count >= self.editor_ready_after_pauses
            return self.editor if self.has_editor and ready else _MissingLocator()
        if selector in {
                'button:has-text("发送")',
                'span:has-text("发送")',
                ".send-btn"}:
            ready = self.pause_count >= self.button_ready_after_pauses
            return self.submit if self.has_button and ready else _MissingLocator()
        return _MissingLocator()

    def get_by_text(self, text, *, exact=False):
        if text in {"私信", "发消息", "发私信"}:
            return self.entry if self.has_entry else _MissingLocator()
        if exact:
            return _MessageLocator(self, text)
        return _MissingLocator()

    def on(self, event, listener):
        if event == "response":
            self.response_listener = listener

    def remove_listener(self, event, listener):
        if event == "response" and self.response_listener is listener:
            self.response_listener = None

    def is_closed(self):
        return self.closed

    def after_submit(self):
        if self.add_message_after_submit:
            self.message_counts[self.current_text] = (
                self.message_counts.get(self.current_text, 0) + 1)
        if self.response is not None and self.response_listener is not None:
            self.response_listener(self.response)


class _Interaction:
    def __init__(self, page):
        self.page = page
        self.pause = AsyncMock(side_effect=self._pause)

    async def _pause(self, *_args):
        self.page.pause_count += 1

    async def click_visible(self, locator, **_kwargs):
        await locator.click()

    async def type_short(self, locator, text):
        locator.value = text
        self.page.current_text = text

    async def insert_long(self, locator, text, *, page):
        locator.value = text
        page.current_text = text


class _Manager:
    def __init__(self, page):
        self.xhs_interaction = _Interaction(page)


class XhsDmWriteTests(unittest.TestCase):
    def test_account_hub_forwards_xhs_submit_callback_to_state_machine(self):
        from app.browser import account_hub

        class HubPage:
            def __init__(self):
                self.url = "about:blank"
                self.goto_calls = []
                self.closed = False

            async def goto(self, url, **kwargs):
                self.url = url
                self.goto_calls.append((url, kwargs))

            async def bring_to_front(self):
                return None

            async def close(self):
                self.closed = True

        class HubManager:
            def __init__(self):
                self.page = HubPage()
                self.xhs_interaction = type(
                    "Interaction", (), {"pause": AsyncMock()})()

            @asynccontextmanager
            async def visible_action(self, _identity):
                yield

            async def context_for(self, _identity):
                return object()

            async def new_page(self, _identity, block_media=False):
                self.block_media = block_media
                return self.page

        async def scenario():
            manager = HubManager()
            callback = AsyncMock()
            state_machine = AsyncMock(return_value=(True, ""))
            with unittest.mock.patch.object(
                    account_hub, "send_xhs_dm_page", state_machine,
                    create=True):
                result = await account_hub.send_dm(
                    manager, object(), "xhs", target_uid="fixture-user",
                    text="唯一消息", on_submit=callback)

            self.assertEqual(result, (True, ""))
            state_machine.assert_awaited_once_with(
                manager, manager.page, "唯一消息", on_submit=callback)
            self.assertEqual(len(manager.page.goto_calls), 1)
            self.assertTrue(manager.page.closed)

        asyncio.run(scenario())

    def test_button_disconnect_marks_once_and_never_falls_back_to_enter(self):
        async def scenario():
            page = _Page(click_error=RuntimeError("disconnected"))
            manager = _Manager(page)
            on_submit = AsyncMock()

            ok, error = await send_xhs_dm_page(
                manager, page, "唯一消息",
                on_submit=on_submit, timeout_seconds=1)

            self.assertFalse(ok)
            self.assertTrue(error.startswith("write_uncertain:"))
            self.assertEqual(page.submit.click_count, 1)
            self.assertEqual(page.editor.enter_count, 0)
            on_submit.assert_awaited_once_with()

        asyncio.run(scenario())

    def test_submit_marker_failure_dispatches_nothing(self):
        async def scenario():
            page = _Page()
            manager = _Manager(page)
            on_submit = AsyncMock(side_effect=RuntimeError("db unavailable"))

            ok, error = await send_xhs_dm_page(
                manager, page, "唯一消息",
                on_submit=on_submit, timeout_seconds=1)

            self.assertFalse(ok)
            self.assertFalse(error.startswith("write_uncertain:"))
            self.assertEqual(page.submit.click_count, 0)
            self.assertEqual(page.editor.enter_count, 0)

        asyncio.run(scenario())

    def test_missing_button_uses_one_enter_and_explicit_response_succeeds(self):
        async def scenario():
            page = _Page(
                has_button=False,
                response=_Response({"success": True}),
            )
            manager = _Manager(page)
            on_submit = AsyncMock()

            ok, error = await send_xhs_dm_page(
                manager, page, "唯一消息",
                on_submit=on_submit, timeout_seconds=1)

            self.assertTrue(ok, error)
            self.assertEqual(page.submit.click_count, 0)
            self.assertEqual(page.editor.enter_count, 1)
            on_submit.assert_awaited_once_with()

        asyncio.run(scenario())

    def test_delayed_editor_is_polled_before_reporting_selector_failure(self):
        async def scenario():
            page = _Page(
                response=_Response({"success": True}),
                editor_ready_after_pauses=2,
            )
            manager = _Manager(page)

            ok, error = await send_xhs_dm_page(
                manager, page, "唯一消息", timeout_seconds=1)

            self.assertTrue(ok, error)
            self.assertGreaterEqual(page.pause_count, 2)
            self.assertEqual(page.submit.click_count, 1)

        asyncio.run(scenario())

    def test_delayed_submit_button_is_used_instead_of_early_enter_fallback(self):
        async def scenario():
            page = _Page(
                response=_Response({"success": True}),
                button_ready_after_pauses=2,
            )
            manager = _Manager(page)

            ok, error = await send_xhs_dm_page(
                manager, page, "唯一消息", timeout_seconds=1)

            self.assertTrue(ok, error)
            self.assertEqual(page.submit.click_count, 1)
            self.assertEqual(page.editor.enter_count, 0)

        asyncio.run(scenario())

    def test_preexisting_identical_message_is_not_new_success_evidence(self):
        async def scenario():
            page = _Page(existing_count=1)
            manager = _Manager(page)

            ok, error = await send_xhs_dm_page(
                manager, page, "唯一消息", timeout_seconds=1)

            self.assertFalse(ok)
            self.assertTrue(error.startswith("write_uncertain:"))
            self.assertEqual(page.submit.click_count, 1)

        asyncio.run(scenario())

    def test_editor_text_itself_is_not_new_message_success_evidence(self):
        async def scenario():
            page = _Page(editor_contributes_match=True)
            manager = _Manager(page)

            ok, error = await send_xhs_dm_page(
                manager, page, "唯一消息", timeout_seconds=1)

            self.assertFalse(ok)
            self.assertTrue(error.startswith("write_uncertain:"))
            self.assertEqual(page.submit.click_count, 1)

        asyncio.run(scenario())

    def test_new_exact_message_node_is_success_evidence(self):
        async def scenario():
            page = _Page(existing_count=1, add_message_after_submit=True)
            manager = _Manager(page)

            ok, error = await send_xhs_dm_page(
                manager, page, "唯一消息", timeout_seconds=1)

            self.assertTrue(ok, error)
            self.assertEqual(page.submit.click_count, 1)

        asyncio.run(scenario())

    def test_http_200_business_rejection_is_uncertain(self):
        async def scenario():
            page = _Page(response=_Response({
                "success": False,
                "code": -1,
                "msg": "验证失败",
            }))
            manager = _Manager(page)

            ok, error = await send_xhs_dm_page(
                manager, page, "唯一消息", timeout_seconds=1)

            self.assertFalse(ok)
            self.assertTrue(error.startswith("write_uncertain:"))
            self.assertIn("业务拒绝", error)

        asyncio.run(scenario())

    def test_conflicting_response_fields_prefer_explicit_business_failure(self):
        async def scenario(payload):
            page = _Page(response=_Response(payload))
            manager = _Manager(page)

            ok, error = await send_xhs_dm_page(
                manager, page, "唯一消息", timeout_seconds=1)

            self.assertFalse(ok)
            self.assertTrue(error.startswith("write_uncertain:"))
            self.assertIn("业务拒绝", error)

        for payload in (
                {"code": 0, "data": {"success": False}},
                {"success": True, "code": -1}):
            with self.subTest(payload=payload):
                asyncio.run(scenario(payload))

    def test_zero_result_code_is_explicit_success_evidence(self):
        async def scenario():
            page = _Page(response=_Response({"result_code": "0"}))
            manager = _Manager(page)

            ok, error = await send_xhs_dm_page(
                manager, page, "唯一消息", timeout_seconds=1)

            self.assertTrue(ok, error)

        asyncio.run(scenario())

    def test_read_only_entry_response_with_send_query_is_ignored(self):
        async def scenario():
            page = _Page(response=_Response(
                {"success": True},
                url=(
                    "https://www.xiaohongshu.com/api/im/web/message/entry"
                    "?next=/api/im/web/message/send"),
            ))
            manager = _Manager(page)

            ok, error = await send_xhs_dm_page(
                manager, page, "唯一消息", timeout_seconds=1)

            self.assertFalse(ok)
            self.assertTrue(error.startswith("write_uncertain:"))

        asyncio.run(scenario())

    def test_missing_editor_reports_safe_diagnostic_without_message_or_token(self):
        async def scenario():
            page = _Page(has_editor=False)
            manager = _Manager(page)
            private_content = "private-message-content"

            ok, error = await send_xhs_dm_page(
                manager, page, private_content, timeout_seconds=1)

            self.assertFalse(ok)
            self.assertFalse(error.startswith("write_uncertain:"))
            self.assertIn("selector_diag group=dm.editor", error)
            self.assertIn(
                "page=www.xiaohongshu.com/user/profile/fixture", error)
            self.assertNotIn("secret-token", error)
            self.assertNotIn("private-fragment", error)
            self.assertNotIn(private_content, error)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
