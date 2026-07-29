import unittest

from app.browser.login import _read_nickname


class FakePage:
    def __init__(self, cached="", dom=None):
        self.cached = cached
        self.dom = dom or {}
        self.dom_calls = []

    async def evaluate(self, _script):
        if isinstance(self.cached, Exception):
            raise self.cached
        return self.cached

    async def inner_text(self, selector, timeout):
        self.dom_calls.append((selector, timeout))
        value = self.dom.get(selector)
        if isinstance(value, Exception) or value is None:
            raise value or TimeoutError(selector)
        return value


class LoginNicknameTests(unittest.IsolatedAsyncioTestCase):
    async def test_prefers_fast_web_storage_nickname(self):
        page = FakePage(cached=" 缓存昵称 ")
        self.assertEqual(await _read_nickname(page), "缓存昵称")
        self.assertEqual(page.dom_calls, [])

    async def test_falls_back_to_short_dom_probe(self):
        page = FakePage(
            cached=RuntimeError("storage unavailable"),
            dom={"span.nickname": " 页面昵称 "},
        )
        self.assertEqual(await _read_nickname(page), "页面昵称")
        self.assertEqual(page.dom_calls[0][1], 600)
        self.assertEqual(page.dom_calls[1][1], 600)


if __name__ == "__main__":
    unittest.main()
