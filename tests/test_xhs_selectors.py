import asyncio
import unittest

from app.browser.xhs_selectors import (
    find_present,
    find_visible,
    selector_candidates,
    selector_diagnostic,
)


class _Locator:
    def __init__(self, *, count=1, visible=True, enabled=True):
        self.count_value = count
        self.visible = visible
        self.enabled = enabled

    @property
    def first(self):
        return self

    async def count(self):
        return self.count_value

    async def is_visible(self):
        return self.visible

    async def is_enabled(self):
        return self.enabled


class _BrokenLocator(_Locator):
    async def count(self):
        raise RuntimeError("private-message secret-token proxy-user:proxy-pass 9222")


class _Page:
    def __init__(self, *, css=None, text=None, url="https://www.xiaohongshu.com/"):
        self.css = css or {}
        self.text = text or {}
        self.url = url
        self.closed = False
        self.html = "<main>typed private message</main>"
        self.input_value = "typed private message"

    def locator(self, selector):
        return self.css.get(selector, _Locator(count=0, visible=False))

    def get_by_text(self, value, *, exact=False):
        return self.text.get(
            (value, exact),
            self.text.get(value, _Locator(count=0, visible=False)),
        )

    def is_closed(self):
        return self.closed


class XhsSelectorCatalogTests(unittest.TestCase):
    def test_find_visible_skips_hidden_candidate_and_returns_stable_name(self):
        hidden = _Locator(visible=False)
        visible = _Locator(visible=True)
        page = _Page(css={
            'input[placeholder*="标题"]': hidden,
            ".d-text input": visible,
        })

        locator, name = asyncio.run(find_visible(page, "publish.title"))

        self.assertIs(locator, visible)
        self.assertEqual(name, "title_d_text_input")

    def test_find_visible_requires_enabled_submit_candidate(self):
        disabled = _Locator(visible=True, enabled=False)
        enabled = _Locator(visible=True, enabled=True)
        page = _Page(css={
            'button:has-text("发送")': disabled,
            '[class*="comment-send"]': enabled,
        })

        locator, name = asyncio.run(find_visible(
            page, "comment.submit", require_enabled=True))

        self.assertIs(locator, enabled)
        self.assertEqual(name, "comment_send_class")

    def test_find_present_can_return_hidden_file_input(self):
        hidden_file = _Locator(visible=False)
        page = _Page(css={'input[type="file"]': hidden_file})

        locator, name = asyncio.run(find_present(page, "publish.file"))

        self.assertIs(locator, hidden_file)
        self.assertEqual(name, "publish_file_input")

    def test_catalog_exposes_every_supported_semantic_group(self):
        groups = (
            "publish.kind.image",
            "publish.kind.video",
            "publish.file",
            "publish.title",
            "publish.body",
            "publish.submit",
            "publish.progress",
            "comment.editor",
            "comment.submit",
            "dm.entry",
            "dm.editor",
            "dm.submit",
        )

        for group in groups:
            with self.subTest(group=group):
                candidates = selector_candidates(group)
                self.assertIsInstance(candidates, tuple)
                self.assertTrue(candidates)
                self.assertEqual(
                    len({candidate.name for candidate in candidates}),
                    len(candidates),
                )

    def test_diagnostic_is_bounded_and_does_not_expose_page_secrets(self):
        url = (
            "https://www.xiaohongshu.com/explore/note"
            "?xsec_token=secret-token"
            "&proxy=http://proxy-user:proxy-pass@127.0.0.1:8080"
            "&cdp=127.0.0.1:9222#fragment"
        )
        page = _Page(
            url=url,
            css={
                'textarea[placeholder*="发送"]': _Locator(
                    count=150, visible=False),
                'div[contenteditable="true"][placeholder*="发送"]': (
                    _BrokenLocator()),
            },
        )

        diagnostic = asyncio.run(selector_diagnostic(page, "dm.editor"))

        self.assertLessEqual(len(diagnostic), 1200)
        self.assertIn("selector_diag group=dm.editor", diagnostic)
        self.assertIn("page=www.xiaohongshu.com/explore/note", diagnostic)
        self.assertIn("dm_editor_send_textarea(count=99+,visible=false)", diagnostic)
        self.assertIn("dm_editor_send_contenteditable(count=?,visible=?)", diagnostic)
        self.assertIn("closed=false", diagnostic)
        for secret in (
                "secret-token", "xsec_token", "fragment", "proxy-user",
                "proxy-pass", "127.0.0.1", "8080", "9222",
                "typed private message", "<main>", "[contenteditable"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, diagnostic)


if __name__ == "__main__":
    unittest.main()
