import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

import app.db as db
import app.main as main
from app.browser import xhs_dots
from app.browser.identity import Identity
from app.browser.xhs_dots import (
    DOTS_REPLY_API,
    _await_answer,
    _DotsState,
    dots_reply_failure,
    dots_session_id,
    dots_url_failure,
    fetch_xhs_dots_ask,
    merge_sources,
    parse_dots_reply_body,
    source_count_from_text,
)
from app.browser.xhs_selectors import css_values, selector_candidates
from app.config import Config
from app.engine.monitor import MonitorEngine
from app.models import DouyinAccount
from app.risk import OperationKind, RiskCategory, classify_platform_error

REPLY_URL = (
    f"https://edith.xiaohongshu.com{DOTS_REPLY_API}"
    "?msg_id=m1&biz=xhs_dots_pc&session_id=sess-42")


class ParseReplyTests(unittest.TestCase):
    def test_whole_json_body_with_sources_and_session(self):
        body = json.dumps({
            "code": 0,
            "data": {
                "session_id": "sess-1",
                "content": "## 结论\n- 每天梳毛",
                "finish": True,
                "references": [
                    {"title": "比熊护理", "note_id": "abc123"},
                    {"name": "网页", "url": "https://example.com/p"},
                    {"title": "无链接"},
                ],
            },
        })
        parsed = parse_dots_reply_body(body)
        self.assertEqual(parsed["text"], "## 结论\n- 每天梳毛")
        self.assertTrue(parsed["finished"])
        self.assertEqual(parsed["session_id"], "sess-1")
        self.assertEqual(parsed["sources"], [
            {"title": "比熊护理",
             "url": "https://www.xiaohongshu.com/explore/abc123"},
            {"title": "网页", "url": "https://example.com/p"},
        ])

    def test_sse_incremental_chunks_are_concatenated(self):
        body = (
            'data: {"data":{"delta":"比熊"}}\n\n'
            'data: {"data":{"delta":"需要"}}\n\n'
            'data: {"data":{"delta":"梳毛","finish":false}}\n\n'
            ': keepalive\n'
            'data: {"data":{"delta":"","is_end":true}}\n\n'
            "data: [DONE]\n")
        parsed = parse_dots_reply_body(body)
        self.assertEqual(parsed["text"], "比熊需要梳毛")
        self.assertTrue(parsed["finished"])
        self.assertEqual(parsed["events"], 4)

    def test_cumulative_chunks_keep_the_latest_full_text(self):
        body = "\n".join(json.dumps({"content": text}, ensure_ascii=False)
                         for text in ("比", "比熊", "比熊要梳毛"))
        self.assertEqual(parse_dots_reply_body(body)["text"], "比熊要梳毛")

    def test_concatenated_json_objects_on_one_line(self):
        parsed = parse_dots_reply_body('{"text":"a"}{"text":"ab","done":true}')
        self.assertEqual(parsed["text"], "ab")
        self.assertTrue(parsed["finished"])

    def test_garbage_and_empty_bodies_yield_nothing(self):
        for body in ("", "   ", "not json at all", "data: {broken"):
            with self.subTest(body=body):
                parsed = parse_dots_reply_body(body)
                self.assertEqual(parsed["text"], "")
                self.assertFalse(parsed["finished"])
                self.assertEqual(parsed["sources"], [])

    def test_merge_sources_dedupes_by_url_and_fills_titles(self):
        merged = merge_sources(
            [{"title": "", "url": "https://a/1"}],
            [{"title": "标题", "url": "https://a/1"},
             {"title": "B", "url": "https://b/2"}, {"url": ""}])
        self.assertEqual(merged, [
            {"title": "标题", "url": "https://a/1"},
            {"title": "B", "url": "https://b/2"}])

    def test_session_id_and_source_count_helpers(self):
        self.assertEqual(dots_session_id(REPLY_URL), "sess-42")
        self.assertEqual(dots_session_id(
            "", "https://www.xiaohongshu.com/ai_chat/abcdef123456"),
            "abcdef123456")
        self.assertEqual(dots_session_id("", "https://x/ai_chat"), "")
        self.assertEqual(source_count_from_text("参考小红书与全网 12 篇内容"), 12)
        self.assertIsNone(source_count_from_text("没有数量"))


class FailureClassificationTests(unittest.TestCase):
    def test_http_and_business_failures_use_explicit_prefixes(self):
        for status in (403, 429, 461, 471):
            with self.subTest(status=status):
                error = dots_reply_failure(status, {})
                self.assertTrue(error.startswith("captcha:"))
                self.assertEqual(
                    classify_platform_error(error)[0], RiskCategory.RISK)
        error = dots_reply_failure(401, {})
        self.assertTrue(error.startswith("logged_out:"))
        self.assertEqual(classify_platform_error(error)[0], RiskCategory.AUTH)
        self.assertTrue(dots_reply_failure(
            200, {"code": -100, "message": "请先登录", "text": ""}
        ).startswith("logged_out:"))
        self.assertTrue(dots_reply_failure(
            200, {"code": -9, "message": "操作频繁", "text": ""}
        ).startswith("captcha:"))
        self.assertIn("code=5", dots_reply_failure(
            200, {"code": 5, "message": "其他", "text": ""}))
        self.assertEqual(dots_reply_failure(
            200, {"code": 0, "message": "", "text": "ok"}), "")
        self.assertEqual(dots_reply_failure(
            200, {"code": 5, "message": "x", "text": "已有回答"}), "")

    def test_url_failures(self):
        self.assertTrue(dots_url_failure(
            "https://www.xiaohongshu.com/website-login/captcha?x=1"
        ).startswith("captcha:"))
        self.assertTrue(dots_url_failure(
            "https://www.xiaohongshu.com/login").startswith("logged_out:"))
        self.assertEqual(dots_url_failure(
            "https://www.xiaohongshu.com/ai_chat"), "")

    def test_selector_groups_exist_and_expose_css_values(self):
        for group in ("dots.input", "dots.send", "dots.generating",
                      "dots.answer", "dots.sources"):
            self.assertTrue(selector_candidates(group))
        self.assertIn("textarea", css_values("dots.input"))
        self.assertTrue(all(isinstance(v, str) for v in css_values("dots.answer")))


# ───────────────────────── 假页面 ─────────────────────────

class _Locator:
    def __init__(self, page, key):
        self.page = page
        self.key = key

    @property
    def first(self):
        return self

    @property
    def present(self):
        return self.key in self.page.present

    async def count(self):
        return 1 if self.present else 0

    async def is_visible(self, **_kwargs):
        return self.present

    async def is_enabled(self):
        return self.present

    async def press(self, key):
        if key == "Enter":
            self.page.enter_pressed = True
            await self.page.on_enter()

    async def inner_text(self, **_kwargs):
        return self.page.texts.get(self.key, "")

    async def click(self):
        self.page.clicked.append(self.key)

    async def wait_for(self, **_kwargs):
        return None

    async def scroll_into_view_if_needed(self, **_kwargs):
        return None

    async def hover(self):
        return None

    async def is_checked(self):
        return False


class _Response:
    def __init__(self, status, body, url=REPLY_URL):
        self.status = status
        self.url = url
        self._body = body

    async def text(self):
        return self._body


class _Page:
    """Minimal stand-in for a Playwright page on /ai_chat."""

    def __init__(self, *, present=None, reply=None, dom_answer="",
                 dom_links=None, url="https://www.xiaohongshu.com/ai_chat"):
        self.present = set(present if present is not None else {
            'div[role="textbox"][contenteditable="true"]',
            "参考小红书与全网"})
        self.texts = {"参考小红书与全网": "参考小红书与全网 3 篇内容"}
        self.reply = reply
        self.dom_answer = dom_answer
        self.dom_links = dom_links or []
        self.url = url
        self.redirected = url != "https://www.xiaohongshu.com/ai_chat"
        self.listeners = []
        self.enter_pressed = False
        self.clicked = []
        self.link_calls = 0

    def on(self, event, listener):
        if event == "response":
            self.listeners.append(listener)

    async def goto(self, url, **_kwargs):
        # 未登录时平台会把 /ai_chat 重定向走;假页面用初始 url 模拟落地页
        if not self.redirected:
            self.url = url

    async def on_enter(self):
        if self.reply is not None:
            for listener in list(self.listeners):
                listener(self.reply)

    def locator(self, selector):
        return _Locator(self, selector)

    def get_by_text(self, text, exact=False):
        return _Locator(self, text)

    async def evaluate(self, script, arg=None):
        if script is xhs_dots._DOTS_ANSWER_JS:
            return self.dom_answer if self.enter_pressed else ""
        if script is xhs_dots._DOTS_LINKS_JS:
            self.link_calls += 1
            # 第一次(点开前)只有导航链接;点开「参考」后多出来源链接
            return (self.dom_links if "参考小红书与全网" in self.clicked
                    else [{"title": "首页", "url": "https://www.xiaohongshu.com/"}])
        return ""

    def is_closed(self):
        return False


class _Interaction:
    def __init__(self, page):
        self.page = page
        self.typed = ""

    async def reading_pause(self, **_kwargs):
        return 0

    async def type_short(self, _locator, text):
        self.typed = text

    async def insert_long(self, _locator, text, page=None):
        self.typed = text

    async def click_visible(self, locator, **_kwargs):
        await locator.click()


class _Manager:
    def __init__(self, page):
        self.page = page
        self.xhs_interaction = _Interaction(page)

    @asynccontextmanager
    async def visible_page(self, _identity, **_kwargs):
        yield self.page


def _identity():
    return Identity(account_id=1, profile_dir="fixture", platform="xhs",
                    identity_mode="native")


async def _fast_sleep(_delay, *_args):
    await _REAL_SLEEP(0)

_REAL_SLEEP = asyncio.sleep


async def _quick_wait(event, _seconds):
    await _REAL_SLEEP(0)
    return event.is_set()


def _ask(page, question="比熊犬需要每天梳毛吗", timeout_s=30):
    manager = _Manager(page)
    with patch("app.browser.xhs_dots.asyncio.sleep", _fast_sleep),             patch("app.browser.xhs_dots._wait_event", _quick_wait):
        result = asyncio.run(fetch_xhs_dots_ask(
            manager, _identity(), question, timeout_s=timeout_s))
    return manager, result


class FetchDotsAskTests(unittest.TestCase):
    def test_answer_comes_from_the_intercepted_reply_response(self):
        body = (
            'data: {"data":{"delta":"## 要\\n","session_id":"sess-42"}}\n\n'
            'data: {"data":{"delta":"- 每天梳毛","finish":true,'
            '"references":[{"title":"护理笔记","note_id":"n1"}]}}\n\n')
        page = _Page(
            reply=_Response(200, body),
            dom_links=[
                {"title": "首页", "url": "https://www.xiaohongshu.com/"},
                {"title": "梳毛指南",
                 "url": "https://www.xiaohongshu.com/explore/n2"},
                {"title": "页脚",
                 "url": "https://www.xiaohongshu.com/ai_chat"}])
        manager, (result, error) = _ask(page)
        self.assertEqual(error, "")
        self.assertEqual(manager.xhs_interaction.typed, "比熊犬需要每天梳毛吗")
        self.assertTrue(page.enter_pressed)
        self.assertEqual(result["via"], "api")
        self.assertTrue(result["complete"])
        self.assertEqual(result["answer"], "## 要\n- 每天梳毛")
        self.assertEqual(result["session_id"], "sess-42")
        self.assertEqual(result["source_count"], 3)
        self.assertEqual(
            [item["url"] for item in result["sources"]],
            ["https://www.xiaohongshu.com/explore/n1",
             "https://www.xiaohongshu.com/explore/n2"])

    def test_dom_is_used_when_no_reply_response_is_intercepted(self):
        markdown = "## 要不要梳毛\n\n- 每天一次\n- 换毛期更勤"
        page = _Page(dom_answer=markdown, dom_links=[
            {"title": "梳毛指南",
             "url": "https://www.xiaohongshu.com/explore/n2"}])
        # 发送键兜底:Enter 没触发回答请求时点击发送
        page.present.add('[aria-label*="发送"]')
        _manager, (result, error) = _ask(page)
        self.assertEqual(error, "")
        self.assertEqual(result["via"], "dom")
        self.assertEqual(result["answer"], markdown)
        self.assertIn('[aria-label*="发送"]', page.clicked)
        self.assertEqual(result["sources"][0]["title"], "梳毛指南")

    def test_text_present_before_sending_is_not_taken_as_the_answer(self):
        page = _Page()
        page.dom_answer = "欢迎来到点点"
        page.enter_pressed = True   # 让假页面在发送前就返回这段文字
        original = page.evaluate

        async def evaluate(script, arg=None):
            if script is xhs_dots._DOTS_ANSWER_JS:
                return "欢迎来到点点"
            return await original(script, arg)

        page.evaluate = evaluate
        page.enter_pressed = False
        result, error = asyncio.run(_await_answer(
            _Manager(page), page, _DotsState(), "问题", 0, True,
            baseline="欢迎来到点点"))
        self.assertIsNone(result)
        self.assertIn("没有给出回答", error)

    def test_long_question_uses_bulk_insert(self):
        page = _Page(reply=_Response(
            200, '{"data":{"content":"好","finish":true}}'))
        question = "比熊" * 40
        manager, (result, _error) = _ask(page, question=question)
        self.assertEqual(manager.xhs_interaction.typed, question)
        self.assertEqual(result["answer"], "好")

    def test_http_risk_status_is_reported_without_retry(self):
        page = _Page(reply=_Response(461, ""))
        _manager, (result, error) = _ask(page)
        self.assertIsNone(result)
        self.assertTrue(error.startswith("captcha:"))
        self.assertEqual(classify_platform_error(error)[0], RiskCategory.RISK)

    def test_captcha_redirect_during_wait_is_reported(self):
        page = _Page(reply=_Response(200, ""))

        async def on_enter():
            page.url = "https://www.xiaohongshu.com/website-login/captcha?x=1"
            for listener in list(page.listeners):
                listener(page.reply)

        page.on_enter = on_enter
        _manager, (result, error) = _ask(page)
        self.assertIsNone(result)
        self.assertTrue(error.startswith("captcha:"))

    def test_missing_input_on_login_page_is_logged_out(self):
        page = _Page(present=set(), url="https://www.xiaohongshu.com/login")
        with patch("app.browser.xhs_dots._poll_visible",
                   AsyncMock(return_value=(None, ""))):
            _manager, (result, error) = _ask(page)
        self.assertIsNone(result)
        self.assertTrue(error.startswith("logged_out:"))
        self.assertFalse(page.enter_pressed)

    def test_missing_input_on_unknown_page_reports_selector_diagnostic(self):
        page = _Page(present=set())
        with patch("app.browser.xhs_dots._poll_visible",
                   AsyncMock(return_value=(None, ""))):
            _manager, (result, error) = _ask(page)
        self.assertIsNone(result)
        self.assertIn("未找到点点输入框", error)
        self.assertIn("selector_diag group=dots.input", error)

    def test_timeout_without_any_answer_is_a_plain_business_error(self):
        page = _Page()
        page.enter_pressed = True
        result, error = asyncio.run(_await_answer(
            _Manager(page), page, _DotsState(), "问题", 0, True))
        self.assertIsNone(result)
        self.assertIn("没有给出回答", error)
        self.assertEqual(classify_platform_error(error)[0], RiskCategory.BUSINESS)

    def test_enter_without_reply_or_send_button_reports_unsent(self):
        page = _Page()
        with patch("app.browser.xhs_dots._wait_event",
                   AsyncMock(return_value=False)):
            result, error = asyncio.run(_await_answer(
                _Manager(page), page, _DotsState(), "问题", 0, False))
        self.assertIsNone(result)
        self.assertIn("没有看到点点发出回答请求", error)


# ───────────────────────── HTTP 接口 ─────────────────────────

class _BrowserStub:
    def __init__(self):
        self._locks = {}

    def lock_for(self, key):
        return self._locks.setdefault(key, asyncio.Lock())

    def identity_for(self, _account):
        return _identity()

    def anon_identity(self):
        return _identity()

    async def close_context(self, _key):
        return None


class DotsAskEndpointTests(unittest.TestCase):
    def setUp(self):
        self.previous_engine = db._engine
        self.previous_main_engine = main.engine
        self.previous_browser = main.browser
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(str(Path(self.tmp.name) / "dots.db"))
        self.browser = _BrowserStub()
        main.browser = self.browser
        main.engine = MonitorEngine(Config(), self.browser)
        with db.get_session() as session:
            account = DouyinAccount(
                platform="xhs", nickname="fixture", status="active",
                storage_state=json.dumps({"cookies": [
                    {"name": "a1", "value": "x"},
                    {"name": "web_session", "value": "y"}]}))
            douyin = DouyinAccount(
                platform="douyin", nickname="dy", status="active",
                storage_state='{"cookies":[]}')
            logged_out = DouyinAccount(
                platform="xhs", nickname="creator-only", status="active",
                storage_state=json.dumps({"cookies": [
                    {"name": "a1", "value": "x"}]}))
            session.add_all([account, douyin, logged_out])
            session.commit()
            for row in (account, douyin, logged_out):
                session.refresh(row)
            self.account_id = account.id
            self.douyin_id = douyin.id
            self.no_web_session_id = logged_out.id

    def tearDown(self):
        main.engine = self.previous_main_engine
        main.browser = self.previous_browser
        if db._engine is not None:
            db._engine.dispose()
        db._engine = self.previous_engine
        self.tmp.cleanup()

    def _call(self, **overrides):
        values = {"account_id": self.account_id,
                  "question": " 比熊犬需要每天梳毛吗 ", "timeout_s": 90}
        values.update(overrides)
        return asyncio.run(main.xhs_dots_ask(main.XhsDotsAskIn(**values)))

    def _events(self):
        from sqlmodel import select
        from app.models import RiskEvent
        with db.get_session() as session:
            return [(e.operation_kind, e.outcome) for e in session.exec(
                select(RiskEvent).where(
                    RiskEvent.account_id == self.account_id)).all()]

    def test_body_validation(self):
        for bad in ({"question": ""}, {"question": "x" * 801},
                    {"timeout_s": 5}, {"timeout_s": 301}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    main.XhsDotsAskIn(account_id=1, **{"question": "问", **bad})

    def test_blank_question_and_invalid_accounts_are_rejected(self):
        with self.assertRaises(HTTPException) as caught:
            self._call(question="   ")
        self.assertEqual(caught.exception.status_code, 422)
        for account_id in (self.douyin_id, self.no_web_session_id, 9999):
            with self.subTest(account_id=account_id):
                with self.assertRaises(HTTPException) as caught:
                    self._call(account_id=account_id)
                self.assertEqual(caught.exception.status_code, 400)

    def test_success_returns_answer_and_counts_one_heavy_read(self):
        payload = {
            "answer": "每天梳", "sources": [{"title": "T", "url": "https://u"}],
            "source_count": 1, "session_id": "sess-1", "complete": True,
            "via": "api"}
        with patch("app.main.fetch_xhs_dots_ask",
                   AsyncMock(return_value=(payload, ""))) as fetcher:
            result = self._call()
        self.assertEqual(result["question"], "比熊犬需要每天梳毛吗")
        self.assertEqual(result["answer"], "每天梳")
        self.assertEqual(result["session_id"], "sess-1")
        self.assertEqual(result["sources"], payload["sources"])
        args, kwargs = fetcher.await_args
        self.assertEqual(args[2], "比熊犬需要每天梳毛吗")
        self.assertEqual(kwargs["timeout_s"], 90)
        self.assertEqual(
            self._events(), [(OperationKind.READ_HEAVY.value, "success")])

    def test_verification_required_maps_to_409_and_is_not_retried(self):
        fetcher = AsyncMock(return_value=(None, "captcha:小红书要求安全验证"))
        with patch("app.main.fetch_xhs_dots_ask", fetcher):
            with self.assertRaises(HTTPException) as caught:
                self._call()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.detail["code"], "verification_required")
        self.assertEqual(fetcher.await_count, 1)
        # 失败已计入风控,账号随即进入冷却,下一次直接被拒绝而不再打开页面
        with patch("app.main.fetch_xhs_dots_ask", fetcher):
            with self.assertRaises(HTTPException) as deferred:
                self._call()
        self.assertEqual(deferred.exception.status_code, 429)
        self.assertEqual(fetcher.await_count, 1)

    def _failure(self, error):
        with patch("app.main.fetch_xhs_dots_ask",
                   AsyncMock(return_value=(None, error))):
            with self.assertRaises(HTTPException) as caught:
                self._call()
        return caught.exception

    def test_logged_out_maps_to_400(self):
        exc = self._failure("logged_out:小红书登录态已失效")
        self.assertEqual(exc.status_code, 400)
        self.assertEqual(exc.detail["code"], "logged_out")

    def test_timeout_maps_to_504(self):
        exc = self._failure("点点在 90 秒内没有给出回答")
        self.assertEqual(exc.status_code, 504)
        self.assertEqual(exc.detail["code"], "timeout")

    def test_generic_failure_maps_to_502(self):
        exc = self._failure("未找到点点输入框（页面结构已调整）")
        self.assertEqual(exc.status_code, 502)
        self.assertEqual(exc.detail["code"], "read_failed")

    def test_engine_not_ready_is_503(self):
        main.engine = None
        with self.assertRaises(HTTPException) as caught:
            self._call()
        self.assertEqual(caught.exception.status_code, 503)


if __name__ == "__main__":
    unittest.main()
