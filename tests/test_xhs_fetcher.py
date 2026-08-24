import asyncio
import unittest
from contextlib import asynccontextmanager

from app.browser.identity import Identity
from app.browser.xhs_fetcher import (
    FEED_API,
    _xhs_page_failure,
    fetch_xhs_note_detail,
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
