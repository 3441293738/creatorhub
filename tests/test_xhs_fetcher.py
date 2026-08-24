import asyncio
import unittest
from contextlib import asynccontextmanager

from app.browser.identity import Identity
from app.browser.xhs_fetcher import (
    FEED_API,
    SEARCH_API,
    SEARCH_API_LEGACY,
    USER_ME_API,
    _xhs_page_failure,
    fetch_xhs_search,
    fetch_xhs_note_detail,
    fetch_xhs_self_profile,
)


class _Response:
    url = f"https://edith.xiaohongshu.com{FEED_API}"
    status = 200

    async def json(self):
        return {
            "data": {
                "items": [{
                    "id": "note-1",
                    "note_card": {"note_id": "note-1", "title": "fixture"},
                }],
            },
        }


class _Page:
    def __init__(self):
        self.response = _Response()
        self.listener = None

    def on(self, event, listener):
        if event == "response":
            self.listener = listener

    async def goto(self, *_args, **_kwargs):
        return None

    async def wait_for_response(self, predicate, **_kwargs):
        assert predicate(self.response)
        # The event listener is intentionally not run before this method
        # returns, reproducing the scheduling race seen with async callbacks.
        return self.response


class _Manager:
    def __init__(self):
        self.page = _Page()

    @asynccontextmanager
    async def visible_page(self, _identity):
        yield self.page


class XhsFetcherResponseTests(unittest.TestCase):
    def test_waited_detail_response_is_parsed_before_page_is_released(self):
        async def scenario():
            identity = Identity(
                account_id=1, profile_dir="fixture", platform="xhs",
                identity_mode="native")
            detail, error = await fetch_xhs_note_detail(
                _Manager(), identity, "note-1")
            self.assertEqual(error, "")
            self.assertEqual(detail["note_id"], "note-1")

        asyncio.run(scenario())

    def test_self_profile_arms_user_me_wait_before_opening_homepage(self):
        class Response:
            url = f"https://edith.xiaohongshu.com{USER_ME_API}"
            status = 200

            async def json(self):
                return {"data": {
                    "guest": False,
                    "user_id": "fixture-user",
                    "red_id": "fixture-red",
                    "nickname": "fixture-name",
                }}

        class Locator:
            first = None

            def __init__(self):
                self.first = self

            async def is_visible(self, **_kwargs):
                return False

        class Page:
            def __init__(self):
                self.url = "about:blank"
                self.response = Response()
                self.waiting = False
                self.navigation_started = asyncio.Event()
                self.gotos = []

            def on(self, *_args):
                return None

            async def wait_for_response(self, predicate, **_kwargs):
                self.waiting = True
                await self.navigation_started.wait()
                assert predicate(self.response)
                return self.response

            async def goto(self, url, **_kwargs):
                self.gotos.append(url)
                self.assert_waiter_was_armed = self.waiting
                self.url = "https://www.xiaohongshu.com/explore"
                self.navigation_started.set()

            def get_by_text(self, *_args, **_kwargs):
                return Locator()

        class Manager:
            def __init__(self):
                self.page = Page()

            @asynccontextmanager
            async def visible_page(self, _identity):
                yield self.page

        async def scenario():
            manager = Manager()
            identity = Identity(
                account_id=1, profile_dir="fixture", platform="xhs",
                identity_mode="native")
            profile, error = await fetch_xhs_self_profile(
                manager, identity, timeout_ms=100)

            self.assertEqual(error, "")
            self.assertEqual(profile["user_id"], "fixture-user")
            self.assertTrue(manager.page.assert_waiter_was_armed)
            self.assertEqual(
                manager.page.gotos, ["https://www.xiaohongshu.com/"])

        asyncio.run(scenario())

    def test_unrelated_api_responses_do_not_masquerade_as_user_me(self):
        class Response:
            url = "https://edith.xiaohongshu.com/api/sns/web/v1/config"
            status = 200

            async def json(self):
                return {"data": {}}

        class Locator:
            first = None

            def __init__(self):
                self.first = self

            async def is_visible(self, **_kwargs):
                return False

        class Page:
            url = "about:blank"

            def on(self, event, listener):
                if event == "response":
                    self.listener = listener

            async def wait_for_response(self, *_args, **_kwargs):
                raise TimeoutError()

            async def goto(self, *_args, **_kwargs):
                self.url = "https://www.xiaohongshu.com/explore"
                await self.listener(Response())

            def get_by_text(self, *_args, **_kwargs):
                return Locator()

            async def evaluate(self, *_args, **_kwargs):
                return None

        class Manager:
            @asynccontextmanager
            async def visible_page(self, _identity):
                yield Page()

        async def scenario():
            identity = Identity(
                account_id=1, profile_dir="fixture", platform="xhs",
                identity_mode="native")
            profile, error = await fetch_xhs_self_profile(
                Manager(), identity, timeout_ms=1)
            self.assertEqual(profile, {})
            self.assertEqual(error, "no_user_me_xhr")

        asyncio.run(scenario())

    def test_search_accepts_v2_and_legacy_v1_responses(self):
        class Locator:
            first = None

            def __init__(self, page):
                self.first = self
                self.page = page

            async def wait_for(self, **_kwargs):
                return None

            async def press(self, key):
                if key == "Enter":
                    self.page.enter_pressed = True
                    self.page.search_listener_armed = (
                        len(self.page.listeners) >= 2)
                    await self.page.emit_response()

        class LoginLocator:
            first = None

            def __init__(self):
                self.first = self

            async def is_visible(self, **_kwargs):
                return False

        class Interaction:
            async def type_short(self, locator, text):
                locator.page.typed_text = text

            async def scroll_step(self, *_args, **_kwargs):
                return None

        class Response:
            status = 200

            def __init__(self, path):
                self.url = f"https://edith.xiaohongshu.com{path}"

            async def json(self):
                return {"data": {"items": [{
                    "id": "note-v2",
                    "model_type": "note",
                    "note_card": {"display_title": "fixture"},
                    "xsec_token": "fixture-token",
                }]}}

        class Page:
            def __init__(self, path):
                self.url = "about:blank"
                self.response = Response(path)
                self.listeners = []
                self.enter_pressed = False
                self.search_listener_armed = False
                self.typed_text = ""

            def on(self, event, listener):
                if event == "response":
                    self.listeners.append(listener)

            async def emit_response(self):
                for listener in list(self.listeners):
                    result = listener(self.response)
                    if asyncio.iscoroutine(result):
                        await result

            async def goto(self, url, **_kwargs):
                self.url = url

            def locator(self, _selector):
                return Locator(self)

            def get_by_text(self, *_args, **_kwargs):
                return LoginLocator()

        class Manager:
            def __init__(self, path):
                self.page = Page(path)
                self.xhs_interaction = Interaction()

            @asynccontextmanager
            async def visible_page(self, _identity):
                yield self.page

        async def scenario(path):
            manager = Manager(path)
            identity = Identity(
                account_id=1, profile_dir="fixture", platform="xhs",
                identity_mode="native")
            items, error = await fetch_xhs_search(
                manager, identity, "防晒霜", set(), max_scrolls=0)
            self.assertEqual(error, "")
            self.assertEqual([item["id"] for item in items], ["note-v2"])
            self.assertEqual(manager.page.typed_text, "防晒霜")
            self.assertTrue(manager.page.search_listener_armed)
            self.assertTrue(manager.page.enter_pressed)

        for path in (SEARCH_API, SEARCH_API_LEGACY):
            with self.subTest(path=path):
                asyncio.run(scenario(path))

    def test_page_failure_requires_an_explicit_login_or_verification_signal(self):
        class Locator:
            first = None

            def __init__(self, visible=False):
                self.first = self
                self.visible = visible

            async def is_visible(self, **_kwargs):
                return self.visible

        class Page:
            def __init__(self, url, login_visible=False):
                self.url = url
                self.login_visible = login_visible

            def get_by_text(self, *_args, **_kwargs):
                return Locator(self.login_visible)

        async def scenario():
            self.assertEqual(
                await _xhs_page_failure(Page(
                    "https://www.xiaohongshu.com/search_result?keyword=fixture")),
                "",
            )
            self.assertTrue((await _xhs_page_failure(Page(
                "https://www.xiaohongshu.com/login"))).startswith("logged_out:"))
            self.assertTrue((await _xhs_page_failure(Page(
                "https://www.xiaohongshu.com/website-login/captcha"))).startswith("captcha:"))
            self.assertTrue((await _xhs_page_failure(Page(
                "https://www.xiaohongshu.com/explore", True))).startswith("logged_out:"))

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
