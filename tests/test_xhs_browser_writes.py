import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.browser.identity import Identity


class _Locator:
    def __init__(self, page, name, *, visible=True, enabled=True,
                 text="", attrs=None):
        self.page = page
        self.name = name
        self.visible = visible
        self.enabled = enabled
        self.value = ""
        self.click_count = 0
        self.files = []
        self.text = text
        self.attrs = attrs or {}

    @property
    def first(self):
        return self

    async def count(self):
        return 1 if self.visible else 0

    async def is_visible(self, **_kwargs):
        return self.visible

    async def is_enabled(self):
        return self.enabled

    async def wait_for(self, **_kwargs):
        if not self.visible:
            raise TimeoutError(self.name)

    async def scroll_into_view_if_needed(self, **_kwargs):
        return None

    async def hover(self):
        return None

    async def click(self, **_kwargs):
        self.click_count += 1
        if self.name == "publish":
            if self.page.disconnect_after_submit:
                self.page.closed = True
                raise RuntimeError("Target page, context or browser has been closed")
            if self.page.response_listener and self.page.publish_response:
                self.page.response_listener(self.page.publish_response)
            if self.page.publish_success_url:
                self.page.url = "https://creator.xiaohongshu.com/publish/success"
        if self.name == "comment-send":
            if self.page.disconnect_after_submit:
                self.page.closed = True
                raise RuntimeError("Target page, context or browser has been closed")
            self.page.posted_text = self.page.composer.value
        if self.name == "private-option":
            self.page.visibility_current.text = "仅自己可见"

    async def set_input_files(self, files, **_kwargs):
        self.files = list(files)

    async def focus(self):
        return None

    async def press(self, key):
        if key in {"Backspace", "Delete"}:
            self.value = ""

    async def press_sequentially(self, text, **_kwargs):
        self.value += text

    async def input_value(self):
        return self.value

    async def get_attribute(self, name):
        return self.attrs.get(name)

    async def inner_text(self):
        return self.text

    def nth(self, index):
        return self if index == 0 else _MissingLocator(self.page)


class _MissingLocator(_Locator):
    def __init__(self, page, name="missing"):
        super().__init__(page, name, visible=False, enabled=False)


class _Response:
    def __init__(self, payload, *, status=200):
        self.url = "https://creator.xiaohongshu.com/api/publish"
        self.status = status
        self.request = type("Request", (), {"method": "POST"})()
        self.payload = payload

    async def json(self):
        return self.payload


class _Page:
    def __init__(self, disconnect_after_submit=False, *,
                 publish_success_url=True, publish_response=None):
        self.url = "https://creator.xiaohongshu.com/publish/publish"
        self.closed = False
        self.disconnect_after_submit = disconnect_after_submit
        self.publish_success_url = publish_success_url
        self.publish_response = publish_response
        self.response_listener = None
        self.image_tab = _Locator(self, "image-tab")
        self.file_input = _Locator(self, "file")
        self.title = _Locator(self, "title")
        self.body = _Locator(self, "body")
        self.publish = _Locator(self, "publish")
        self.visibility_current = _Locator(
            self, "visibility-current", text="公开可见",
            attrs={"class": "d-select-description"})

    def locator(self, selector):
        if selector == "xhs-publish-btn":
            return _MissingLocator(self)
        if selector == ".d-select-description":
            return self.visibility_current
        if selector == 'input[type="file"]':
            return self.file_input
        if "标题" in selector or selector in {".d-text input", "input.c-input_inner"}:
            return self.title
        if "contenteditable" in selector or selector in {
                ".ql-editor", "#post-textarea", "textarea"}:
            return self.body
        if "发布" in selector or "submit" in selector:
            return self.publish
        if "upload" in selector or "progress" in selector:
            return _MissingLocator(self)
        return _MissingLocator(self)

    def get_by_text(self, text, **_kwargs):
        if text in {"上传图文", "图文"}:
            return self.image_tab
        if text in {"发布成功", "发布完成"}:
            return _Locator(self, "success", visible="success" in self.url)
        if "/new/note-manager" in self.url \
                and text == self.title.value and self.title.value:
            return _Locator(self, "confirmed-note", text=text)
        if text == "仅自己可见":
            return _Locator(
                self, "private-option", text=text, attrs={"class": "name"})
        return _MissingLocator(self)

    def on(self, event, listener):
        if event == "response":
            self.response_listener = listener

    def remove_listener(self, event, listener):
        if event == "response" and self.response_listener is listener:
            self.response_listener = None

    def is_closed(self):
        return self.closed

    async def goto(self, url, **_kwargs):
        self.url = url

    async def reload(self, **_kwargs):
        return None


class _CdpSession:
    def __init__(self, page):
        self.page = page
        self.calls = []
        self.detached = False

    async def send(self, method, params=None):
        self.calls.append((method, params or {}))
        if method == "DOM.getDocument":
            return {"root": {
                "nodeName": "#document",
                "children": [{
                    "nodeName": "XHS-PUBLISH-BTN",
                    "attributes": ["submit-disabled", "false"],
                    "shadowRoots": [{
                        "nodeName": "#document-fragment",
                        "shadowRootType": "closed",
                        "children": [{
                            "nodeName": "BUTTON",
                            "backendNodeId": 99,
                            "children": [{
                                "nodeName": "#text",
                                "nodeValue": "发布",
                            }],
                        }],
                    }],
                }],
            }}
        if method == "DOM.getBoxModel":
            return {"model": {"border": [10, 20, 110, 20, 110, 60, 10, 60]}}
        if method == "Input.dispatchMouseEvent" \
                and (params or {}).get("type") == "mouseReleased":
            self.page.url = "https://creator.xiaohongshu.com/publish/success"
        return {}

    async def detach(self):
        self.detached = True


class _CdpContext:
    def __init__(self, page):
        self.session = _CdpSession(page)

    async def new_cdp_session(self, _page):
        return self.session


class _WebComponentPage(_Page):
    def __init__(self):
        super().__init__(publish_success_url=False)
        self.web_component = _Locator(self, "web-component")
        self.context = _CdpContext(self)

    def locator(self, selector):
        if selector == "xhs-publish-btn":
            return self.web_component
        return super().locator(selector)


class _DelayedFileInputPage(_Page):
    def __init__(self):
        super().__init__()
        self.file_queries = 0

    def locator(self, selector):
        if selector in {
                "input.upload-input",
                'input[type="file"][accept*="image"]',
                'input[type="file"][accept*="video"]',
                'input[type="file"]'}:
            self.file_queries += 1
            if self.file_queries <= 4:
                return _MissingLocator(self)
            return self.file_input
        return super().locator(selector)


class _LocatorList:
    def __init__(self, locators):
        self.locators = list(locators)

    async def count(self):
        return len(self.locators)

    def nth(self, index):
        return self.locators[index]

    @property
    def first(self):
        return self.locators[0]


class _HiddenVisibilityScaffoldPage(_Page):
    def __init__(self):
        super().__init__()
        self.hidden_visibility = _Locator(
            self, "hidden-visibility", visible=False, text="")

    def locator(self, selector):
        if selector == ".d-select-description":
            return _LocatorList([
                self.hidden_visibility, self.visibility_current])
        return super().locator(selector)


class _CommentPage:
    def __init__(self, *, target_present=True, disconnect_after_submit=False):
        self.url = "https://www.xiaohongshu.com/explore/note1"
        self.closed = False
        self.target_present = target_present
        self.disconnect_after_submit = disconnect_after_submit
        self.posted_text = ""
        self.composer = _Locator(self, "comment-composer")
        self.send = _Locator(self, "comment-send")
        self.target = _Locator(self, "target", visible=target_present)
        self.reply = _Locator(self, "reply", visible=target_present)

    def locator(self, selector):
        if "data-comment-id" in selector:
            return self.target
        if "contenteditable" in selector or "textarea" in selector \
                or "comment-input" in selector:
            return self.composer
        if "发送" in selector or "submit" in selector or "comment-send" in selector:
            return self.send
        return _MissingLocator(self)

    def get_by_text(self, text, **kwargs):
        if text == "目标原文":
            return self.target
        if text == "回复":
            return self.reply
        if text == self.posted_text and self.posted_text:
            return _Locator(self, "posted")
        if text == "发送":
            return self.send
        return _MissingLocator(self)

    def on(self, *_args):
        return None

    def remove_listener(self, *_args):
        return None

    def is_closed(self):
        return self.closed


class _PreexistingCommentPage(_CommentPage):
    def __init__(self, existing_text):
        super().__init__()
        self.existing_text = existing_text

    def get_by_text(self, text, **kwargs):
        if text == self.existing_text:
            return _Locator(self, "preexisting-comment")
        return super().get_by_text(text, **kwargs)


class _Interaction:
    def __init__(self):
        self.type_short = AsyncMock(side_effect=self._type_short)
        self.insert_long = AsyncMock(side_effect=self._insert_long)
        self.click_visible = AsyncMock(side_effect=self._click_visible)
        self.pause = AsyncMock()
        self.scroll_step = AsyncMock(return_value=500)

    async def _type_short(self, locator, text):
        locator.value = text

    async def _insert_long(self, locator, text, *, page):
        locator.value = text

    async def _click_visible(self, locator, **_kwargs):
        await locator.click()


class _Manager:
    def __init__(self, page):
        self.page = page
        self.xhs_interaction = _Interaction()
        self.visible_leases = 0

    @asynccontextmanager
    async def visible_page(self, _identity, *, url=""):
        self.visible_leases += 1
        if url:
            self.page.url = url
        yield self.page


class XhsBrowserWriteTests(unittest.TestCase):
    def _identity(self, root):
        return Identity(
            account_id=1, profile_dir=str(Path(root) / "acc_1"),
            platform="xhs", identity_mode="native")

    def test_publish_uses_visible_page_and_clicks_submit_once(self):
        from app.platforms.xhs.browser_writes import publish_xhs_browser

        async def scenario(media_path):
            page = _Page()
            manager = _Manager(page)
            on_submit = AsyncMock()
            outcome = await publish_xhs_browser(
                manager, self._identity(Path(media_path).parent), "images",
                "露营标题", "这是一段正文", [], [media_path],
                on_submit=on_submit)
            self.assertEqual(outcome.status, "success")
            self.assertEqual(page.publish.click_count, 1)
            on_submit.assert_awaited_once_with()
            self.assertEqual(page.file_input.files, [media_path])
            manager.xhs_interaction.type_short.assert_awaited_once_with(
                page.title, "露营标题")
            manager.xhs_interaction.insert_long.assert_awaited_once()
            self.assertEqual(manager.visible_leases, 1)

        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "one.jpg"
            media.write_bytes(b"fixture")
            asyncio.run(scenario(str(media)))

    def test_disconnect_after_submit_is_uncertain_and_never_resubmits(self):
        from app.platforms.xhs.browser_writes import publish_xhs_browser

        async def scenario(media_path):
            page = _Page(disconnect_after_submit=True)
            manager = _Manager(page)
            outcome = await publish_xhs_browser(
                manager, self._identity(Path(media_path).parent), "images",
                "标题", "正文", [], [media_path])
            self.assertEqual(outcome.status, "uncertain")
            self.assertEqual(page.publish.click_count, 1)

        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "one.jpg"
            media.write_bytes(b"fixture")
            asyncio.run(scenario(str(media)))

    def test_new_web_component_publish_button_uses_one_closed_shadow_click(self):
        from app.platforms.xhs.browser_writes import publish_xhs_browser

        async def scenario(media_path):
            page = _WebComponentPage()
            manager = _Manager(page)
            on_submit = AsyncMock()
            outcome = await publish_xhs_browser(
                manager, self._identity(Path(media_path).parent), "images",
                "新版标题", "新版正文", [], [media_path],
                on_submit=on_submit)
            self.assertEqual(outcome.status, "success")
            on_submit.assert_awaited_once_with()
            events = [
                params["type"] for method, params in page.context.session.calls
                if method == "Input.dispatchMouseEvent"
            ]
            self.assertEqual(events, [
                "mouseMoved", "mousePressed", "mouseReleased"])
            self.assertTrue(page.context.session.detached)

        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "one.jpg"
            media.write_bytes(b"fixture")
            asyncio.run(scenario(str(media)))

    def test_publish_waits_for_file_input_mounted_after_tab_switch(self):
        from app.platforms.xhs.browser_writes import publish_xhs_browser

        async def scenario(media_path):
            page = _DelayedFileInputPage()
            manager = _Manager(page)
            outcome = await publish_xhs_browser(
                manager, self._identity(Path(media_path).parent), "images",
                "标题", "正文", [], [media_path])
            self.assertEqual(outcome.status, "success")
            self.assertGreater(page.file_queries, 4)
            self.assertEqual(page.file_input.files, [media_path])

        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "one.jpg"
            media.write_bytes(b"fixture")
            asyncio.run(scenario(str(media)))

    def test_publish_applies_private_visibility_before_submit(self):
        from app.platforms.xhs.browser_writes import publish_xhs_browser

        async def scenario(media_path):
            page = _Page()
            manager = _Manager(page)
            outcome = await publish_xhs_browser(
                manager, self._identity(Path(media_path).parent), "images",
                "标题", "正文", [], [media_path], visibility="private")
            self.assertEqual(outcome.status, "success")
            self.assertEqual(page.visibility_current.text, "仅自己可见")
            self.assertEqual(page.publish.click_count, 1)

        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "one.jpg"
            media.write_bytes(b"fixture")
            asyncio.run(scenario(str(media)))

    def test_visibility_skips_hidden_video_scaffolding(self):
        from app.platforms.xhs.browser_writes import publish_xhs_browser

        async def scenario(media_path):
            page = _HiddenVisibilityScaffoldPage()
            manager = _Manager(page)
            outcome = await publish_xhs_browser(
                manager, self._identity(Path(media_path).parent), "video",
                "标题", "正文", [], [media_path], visibility="private")
            self.assertEqual(outcome.status, "success")
            self.assertEqual(page.visibility_current.text, "仅自己可见")

        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "one.mp4"
            media.write_bytes(b"fixture")
            asyncio.run(scenario(str(media)))

    def test_publish_page_open_failure_stays_a_pre_submit_failure(self):
        from app.platforms.xhs.browser_writes import publish_xhs_browser

        class BrokenManager(_Manager):
            @asynccontextmanager
            async def visible_page(self, _identity, *, url=""):
                raise RuntimeError("fixture open failure")
                yield  # pragma: no cover

        async def scenario(media_path):
            manager = BrokenManager(_Page())
            outcome = await publish_xhs_browser(
                manager, self._identity(Path(media_path).parent), "images",
                "标题", "正文", [], [media_path])
            self.assertEqual(outcome.status, "failed")
            self.assertIn("fixture open failure", outcome.error)

        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "one.jpg"
            media.write_bytes(b"fixture")
            asyncio.run(scenario(str(media)))

    def test_static_published_navigation_is_not_success_evidence(self):
        from app.platforms.xhs.browser_writes import _visible_success

        class PageWithPublishedNavigation(_Page):
            def get_by_text(self, text, **_kwargs):
                if text == "已发布":
                    return _Locator(self, "published-navigation")
                return _MissingLocator(self)

        self.assertFalse(asyncio.run(_visible_success(
            PageWithPublishedNavigation())))

    def test_navigation_away_from_publish_form_is_success_evidence(self):
        from app.platforms.xhs.browser_writes import _visible_success

        page = _Page(publish_success_url=False)
        page.url = "https://creator.xiaohongshu.com/publish/manager"
        self.assertTrue(asyncio.run(_visible_success(page)))

        page.url = "https://creator.xiaohongshu.com/passport/login"
        self.assertFalse(asyncio.run(_visible_success(page)))

    def test_http_200_business_rejection_is_not_publish_success(self):
        from app.platforms.xhs.browser_writes import publish_xhs_browser

        async def scenario(media_path):
            page = _Page(
                publish_success_url=False,
                publish_response=_Response({"code": -1, "msg": "验证失败"}),
            )
            manager = _Manager(page)
            outcome = await publish_xhs_browser(
                manager, self._identity(Path(media_path).parent), "images",
                "标题", "正文", [], [media_path], timeout_seconds=1)
            self.assertEqual(outcome.status, "uncertain")
            self.assertEqual(page.publish.click_count, 1)

        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "one.jpg"
            media.write_bytes(b"fixture")
            asyncio.run(scenario(str(media)))

    def test_publish_dispatch_defaults_to_browser_without_api_fallback(self):
        from app.platforms.xhs import publish as publish_module
        from app.platforms.xhs.browser_writes import XhsWriteOutcome

        async def scenario(media_path):
            browser_result = XhsWriteOutcome(
                status="uncertain", error="结果待确认")
            with patch.object(
                    publish_module, "publish_xhs_browser",
                    AsyncMock(return_value=browser_result)) as browser, \
                    patch.object(
                        publish_module, "_publish_api_sync",
                        side_effect=AssertionError("browser mode must not use API")):
                result = await publish_module.publish_xhs(
                    _Manager(_Page()), self._identity(Path(media_path).parent),
                    '{"cookies":[]}',
                    "images", "标题", "正文", [media_path])
            self.assertFalse(result[0])
            self.assertTrue(result[2].startswith("write_uncertain:"))
            browser.assert_awaited_once()

        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "one.jpg"
            media.write_bytes(b"fixture")
            asyncio.run(scenario(str(media)))

    def test_reply_target_missing_never_falls_back_to_top_level_comment(self):
        from app.platforms.xhs.browser_writes import comment_xhs_browser

        async def scenario():
            page = _CommentPage(target_present=False)
            manager = _Manager(page)
            outcome = await comment_xhs_browser(
                manager, self._identity("."), "note1", "token", "回复内容",
                target_comment_id="comment404", target_text="目标原文")
            self.assertEqual(outcome.status, "failed")
            self.assertIn("未找到目标评论", outcome.error)
            self.assertEqual(page.send.click_count, 0)

        asyncio.run(scenario())

    def test_comment_disconnect_after_submit_is_uncertain_and_not_retried(self):
        from app.platforms.xhs.browser_writes import comment_xhs_browser

        async def scenario():
            page = _CommentPage(disconnect_after_submit=True)
            manager = _Manager(page)
            on_submit = AsyncMock()
            outcome = await comment_xhs_browser(
                manager, self._identity("."), "note1", "token", "评论内容",
                on_submit=on_submit)
            self.assertEqual(outcome.status, "uncertain")
            self.assertEqual(page.send.click_count, 1)
            on_submit.assert_awaited_once_with()

        asyncio.run(scenario())

    def test_preexisting_identical_comment_is_not_new_success_evidence(self):
        from app.platforms.xhs.browser_writes import comment_xhs_browser

        async def scenario():
            content = "页面原本已有的相同评论"
            page = _PreexistingCommentPage(content)
            manager = _Manager(page)
            outcome = await comment_xhs_browser(
                manager, self._identity("."), "note1", "token", content,
                timeout_seconds=1)
            self.assertEqual(outcome.status, "uncertain")
            self.assertEqual(page.send.click_count, 1)

        asyncio.run(scenario())

    def test_missing_publish_submit_reports_only_safe_selector_diagnostic(self):
        from app.platforms.xhs.browser_writes import publish_xhs_browser

        class MissingSubmitPage(_Page):
            def locator(self, selector):
                if selector in {
                        'button:has-text("发布")', "div.submit button"}:
                    return _MissingLocator(self)
                return super().locator(selector)

            def get_by_text(self, text, **kwargs):
                if text == "发布笔记":
                    return _MissingLocator(self)
                return super().get_by_text(text, **kwargs)

        class SensitiveUrlManager(_Manager):
            @asynccontextmanager
            async def visible_page(self, _identity, *, url=""):
                self.visible_leases += 1
                self.page.url = (
                    f"{url}&token=secret-value#private-fragment")
                yield self.page

        async def scenario(media_path):
            page = MissingSubmitPage(publish_success_url=False)
            manager = SensitiveUrlManager(page)
            outcome = await publish_xhs_browser(
                manager, self._identity(Path(media_path).parent), "images",
                "标题", "正文", [], [media_path], timeout_seconds=1)
            self.assertEqual(outcome.status, "failed")
            self.assertIn("selector_diag group=publish.submit", outcome.error)
            self.assertIn(
                "page=creator.xiaohongshu.com/publish/publish",
                outcome.error,
            )
            for secret in ("secret-value", "token=", "private-fragment"):
                self.assertNotIn(secret, outcome.error)

        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / "one.jpg"
            media.write_bytes(b"fixture")
            asyncio.run(scenario(str(media)))

    def test_missing_comment_editor_diagnostic_excludes_token_and_content(self):
        from app.platforms.xhs.browser_writes import comment_xhs_browser

        class MissingEditorPage(_CommentPage):
            def locator(self, selector):
                if "contenteditable" in selector or "textarea" in selector:
                    return _MissingLocator(self)
                return super().locator(selector)

        async def scenario():
            private_content = "private-message-content"
            page = MissingEditorPage()
            manager = _Manager(page)
            outcome = await comment_xhs_browser(
                manager, self._identity("."), "note1", "secret-xsec-token",
                private_content, timeout_seconds=1)
            self.assertEqual(outcome.status, "failed")
            self.assertIn("selector_diag group=comment.editor", outcome.error)
            self.assertIn(
                "page=www.xiaohongshu.com/explore/note1", outcome.error)
            self.assertNotIn("secret-xsec-token", outcome.error)
            self.assertNotIn(private_content, outcome.error)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
