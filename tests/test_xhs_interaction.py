import asyncio
import inspect
import random
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.browser.identity import Identity
from app.browser.manager import BrowserManager
from app.browser import account_hub, xhs_fetcher
from app.browser.xhs_interaction import (
    XhsInteractionPolicy,
    XhsVisibleActionGate,
)


class _Mouse:
    def __init__(self):
        self.wheels = []

    async def wheel(self, x, y):
        self.wheels.append((x, y))


class _Keyboard:
    def __init__(self, page):
        self.page = page
        self.insertions = []

    async def insert_text(self, text):
        self.insertions.append(text)
        self.page.focused.value = text


class _Page:
    def __init__(self):
        self.mouse = _Mouse()
        self.keyboard = _Keyboard(self)
        self.focused = None
        self.front_calls = 0
        self.goto_calls = []
        self.closed = False

    async def bring_to_front(self):
        self.front_calls += 1

    async def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))

    async def title(self):
        return "小红书"

    async def close(self):
        self.closed = True


class _Locator:
    def __init__(self, page):
        self.page = page
        self.value = "旧值"
        self.calls = []

    async def wait_for(self, **kwargs):
        self.calls.append(("wait_for", kwargs))

    async def scroll_into_view_if_needed(self, **kwargs):
        self.calls.append(("scroll", kwargs))

    async def is_enabled(self):
        return True

    async def hover(self):
        self.calls.append(("hover", {}))

    async def click(self):
        self.calls.append(("click", {}))
        self.page.focused = self

    async def focus(self):
        self.calls.append(("focus", {}))
        self.page.focused = self

    async def press(self, key):
        self.calls.append(("press", key))
        if key in {"Backspace", "Delete"}:
            self.value = ""

    async def press_sequentially(self, text, **kwargs):
        self.calls.append(("type", text, kwargs))
        self.value += text

    async def input_value(self):
        return self.value


class XhsInteractionTests(unittest.TestCase):
    def test_xhs_account_hub_uses_locator_actions_not_javascript_clicks(self):
        self.assertFalse(hasattr(account_hub, "_XHS_OPEN_STAT_JS"))
        self.assertEqual(
            account_hub._FOLLOW_NAV["xhs"]["open"]["following"],
            ["xhs:关注"],
        )
        dm_source = inspect.getsource(account_hub.fetch_dm_conversations)
        self.assertNotIn("xhs im-entry jump", dm_source)

    def test_xhs_account_hub_visible_tasks_share_the_global_gate(self):
        for function in (
                account_hub.fetch_follows,
                account_hub.fetch_dm_conversations,
                account_hub.do_follow,
                account_hub.send_dm):
            with self.subTest(function=function.__name__):
                self.assertIn("visible_action", inspect.getsource(function))

    def test_xhs_account_hub_pages_use_manager_proxy_hooks(self):
        self.assertIn(
            "mgr.new_page", inspect.getsource(account_hub._open_target_profile))
        self.assertIn("mgr.new_page", inspect.getsource(account_hub.send_dm))

    def test_xhs_profile_stat_click_uses_visible_scored_locator(self):
        class Candidate:
            def __init__(self, text, parent=None):
                self.text = text
                self.parent = parent or self

            async def is_visible(self):
                return True

            def locator(self, selector):
                self.assert_parent_selector = selector
                return self.parent

            async def inner_text(self):
                return self.text

        class Collection:
            def __init__(self, items):
                self.items = items

            async def count(self):
                return len(self.items)

            def nth(self, index):
                return self.items[index]

        weak_parent = Candidate("关注")
        strong_parent = Candidate("12 关注")
        candidates = Collection([
            Candidate("关注", weak_parent),
            Candidate("关注", strong_parent),
        ])

        class Root:
            async def count(self):
                return 1

            async def is_visible(self):
                return True

            def get_by_text(self, text, exact):
                self.lookup = (text, exact)
                return candidates

        root = Root()

        class First:
            @property
            def first(self):
                return root

        class Page:
            def locator(self, selector):
                self.selector = selector
                return First()

        async def scenario():
            manager = type("Manager", (), {})()
            manager.xhs_interaction = type("Interaction", (), {})()
            manager.xhs_interaction.click_visible = AsyncMock()
            clicked = await account_hub._click_xhs_profile_stat(
                manager, Page(), "关注")
            self.assertTrue(clicked)
            manager.xhs_interaction.click_visible.assert_awaited_once_with(
                strong_parent)

        asyncio.run(scenario())

    def test_xhs_fetcher_has_no_instant_fill_fixed_wait_or_large_scroll(self):
        source = inspect.getsource(xhs_fetcher)

        self.assertNotIn(".fill(", source)
        self.assertNotIn("wait_for_timeout", source)
        self.assertNotIn("scrollTop", source)
        wheel_amounts = [
            int(value) for value in re.findall(
                r"mouse\.wheel\(\s*0\s*,\s*(\d+)", source)
        ]
        self.assertTrue(all(amount <= 900 for amount in wheel_amounts))

    def test_visible_gate_serializes_accounts_and_recovers_after_cancel(self):
        async def scenario():
            gate = XhsVisibleActionGate()
            events = []
            first_entered = asyncio.Event()

            async def first():
                async with gate.acquire(1):
                    events.append("first-in")
                    first_entered.set()
                    await asyncio.Event().wait()

            async def second():
                async with gate.acquire(2):
                    events.append("second-in")

            task = asyncio.create_task(first())
            await first_entered.wait()
            second_task = asyncio.create_task(second())
            await asyncio.sleep(0)
            self.assertEqual(events, ["first-in"])
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await second_task
            self.assertEqual(events, ["first-in", "second-in"])
            self.assertIsNone(gate.active_account)

        asyncio.run(scenario())

    def test_visible_gate_is_reentrant_for_one_logical_task(self):
        async def scenario():
            gate = XhsVisibleActionGate()
            events = []
            async with gate.acquire(1):
                events.append("outer")
                async with gate.acquire(1):
                    events.append("inner")
            self.assertEqual(events, ["outer", "inner"])
            self.assertIsNone(gate.active_account)

        asyncio.run(asyncio.wait_for(scenario(), timeout=0.5))

    def test_visible_action_closes_context_only_after_outermost_action(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                manager = BrowserManager("UA", tmp, xhs_browser_mode="cdp")
                manager.close_context = AsyncMock()
                identity = Identity(
                    account_id=19,
                    profile_dir=str(Path(tmp) / "acc_19"),
                    identity_mode="native",
                    platform="xhs",
                )
                async with manager.visible_action(identity):
                    async with manager.visible_action(identity):
                        manager.close_context.assert_not_awaited()
                manager.close_context.assert_awaited_once_with(identity.key)

        asyncio.run(scenario())

    def test_manual_visible_action_can_keep_context_open(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                manager = BrowserManager("UA", tmp, xhs_browser_mode="cdp")
                manager.close_context = AsyncMock()
                identity = Identity(
                    account_id=20,
                    profile_dir=str(Path(tmp) / "acc_20"),
                    identity_mode="native",
                    platform="xhs",
                )
                async with manager.visible_action(
                        identity, keep_context=True):
                    pass
                manager.close_context.assert_not_awaited()

        asyncio.run(scenario())

    def test_scroll_steps_are_bounded_and_predicate_stops_early(self):
        async def scenario():
            page = _Page()
            sleeps = []

            async def record_sleep(delay):
                sleeps.append(delay)

            policy = XhsInteractionPolicy(
                rng=random.Random(7), sleep=record_sleep)

            async def ready():
                return len(page.mouse.wheels) >= 3

            found = await policy.scroll_until(page, ready, max_steps=12)
            self.assertTrue(found)
            self.assertEqual(len(page.mouse.wheels), 3)
            self.assertTrue(all(
                350 <= abs(y) <= 900 for _x, y in page.mouse.wheels))
            self.assertTrue(all(0.18 <= delay <= 0.52 for delay in sleeps))

        asyncio.run(scenario())

    def test_short_text_is_typed_one_character_at_a_time(self):
        async def scenario():
            page = _Page()
            locator = _Locator(page)
            sleeps = []

            async def record_sleep(delay):
                sleeps.append(delay)

            policy = XhsInteractionPolicy(
                rng=random.Random(3), sleep=record_sleep)
            await policy.type_short(locator, "露营")

            typed = [call[1] for call in locator.calls if call[0] == "type"]
            self.assertEqual(typed, ["露", "营"])
            self.assertEqual(await locator.input_value(), "露营")
            character_sleeps = [delay for delay in sleeps if delay >= 0.035]
            self.assertTrue(all(delay <= 0.22 for delay in character_sleeps))

        asyncio.run(scenario())

    def test_long_text_uses_one_controlled_insert(self):
        async def scenario():
            page = _Page()
            locator = _Locator(page)
            policy = XhsInteractionPolicy(
                rng=random.Random(1), sleep=AsyncMock())

            await policy.insert_long(locator, "这是一段较长正文", page=page)

            self.assertEqual(page.keyboard.insertions, ["这是一段较长正文"])
            self.assertEqual(await locator.input_value(), "这是一段较长正文")
            self.assertFalse(any(
                call[0] == "type" for call in locator.calls))

        asyncio.run(scenario())

    def test_visible_page_closes_temporary_page_and_one_shot_context(self):
        async def scenario():
            with tempfile.TemporaryDirectory() as tmp:
                manager = BrowserManager("UA", tmp, xhs_browser_mode="cdp")
                page = _Page()

                class Context:
                    close = AsyncMock()

                    async def new_page(self):
                        return page

                context = Context()
                manager.context_for = AsyncMock(return_value=context)
                manager.close_context = AsyncMock()
                identity = Identity(
                    account_id=9,
                    profile_dir=str(Path(tmp) / "acc_9"),
                    identity_mode="native",
                    platform="xhs",
                )
                with patch(
                        "app.browser.manager.bring_window_to_front",
                        return_value=True):
                    async with manager.visible_page(
                            identity, url="https://www.xiaohongshu.com/") as leased:
                        self.assertIs(leased, page)

                self.assertTrue(page.closed)
                context.close.assert_not_awaited()
                manager.close_context.assert_awaited_once_with(identity.key)
                self.assertEqual(page.front_calls, 1)
                self.assertEqual(page.goto_calls[0][0],
                                 "https://www.xiaohongshu.com/")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
