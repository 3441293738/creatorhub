import asyncio
import unittest
from contextlib import asynccontextmanager

from app.browser.identity import Identity
from app.browser.xhs_fetcher import FEED_API, fetch_xhs_note_detail


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


if __name__ == "__main__":
    unittest.main()
