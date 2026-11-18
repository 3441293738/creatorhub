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
    DOTS_DETAIL_API,
    DOTS_PANEL_API,
    DOTS_REPLY_API,
    _await_answer,
    _DotsState,
    _suggestions,
    build_dots_answer,
    clean_markdown,
    dots_reply_failure,
    dots_session_id,
    dots_url_failure,
    fetch_xhs_dots_ask,
    merge_panel_sources,
    parse_dots_body,
    parse_dots_panel,
    source_count_from_text,
)
from app.browser.xhs_selectors import css_values, selector_candidates
from app.config import Config
from app.engine.monitor import MonitorEngine
from app.models import DouyinAccount
from app.risk import OperationKind, RiskCategory, classify_platform_error

# 真实抓包(2026-11,一次提问「比熊多久洗一次澡比较合适」),已匿名化并只留相关几条:
# reply = message/reply 的响应(引用没有标题);detail = conversation/detail 的响应
# (整个会话历史,里面混着两条别的问题的回答,用来验证按 msg_id 精确取)。
CAPTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "xhs_dots_capture.json").read_text(
        encoding="utf-8"))
# 来源面板(complex/source)的响应,同样匿名化;card_id 已改成本次回答的 msg_id
PANEL = json.loads(
    (Path(__file__).parent / "fixtures" / "xhs_dots_panel.json").read_text(
        encoding="utf-8"))
USER_MSG = "msg_ad203013b7364ea4"
AI_MSG = f"ai-{USER_MSG}"
QUESTION = "比熊多久洗一次澡比较合适"
MAIN_SESSION = "xhs_dots-000000000000000000000001"
NEW_SESSION = f"{MAIN_SESSION}-new"
NOTE_A = f"{3:024x}"
NOTE_B = f"{5:024x}"
REPLY_URL = (
    f"https://edith.xiaohongshu.com{DOTS_REPLY_API}"
    f"?msg_id={USER_MSG}&biz=xhs_dots_pc&session_id={NEW_SESSION}")
DETAIL_URL = (
    f"https://edith.xiaohongshu.com{DOTS_DETAIL_API}"
    f"?session_id={NEW_SESSION}&biz=xhs_dots&count=20&in_order=false")
MAIN_DETAIL_URL = DETAIL_URL.replace(NEW_SESSION, MAIN_SESSION)
PANEL_URL = (
    f"https://edith.xiaohongshu.com{DOTS_PANEL_API}"
    f"?card_id=ai-{USER_MSG}&search_id=ai-{USER_MSG}&query=&tag="
    "&business_type=search_complex_onepage&page_from=0&size=10")
SOURCES_HEADER = "button.xhs-spark-source-header"
NEW_CHAT = 'button[aria-label="新对话"]'
INPUT = 'div[role="textbox"][contenteditable="true"]'


def _message(body, msg_id):
    return next(m for m in parse_dots_body(json.dumps(body))["messages"]
                if m["msg_id"] == msg_id)


def _in_session(body, session_id):
    body = json.loads(json.dumps(body))
    for msg in body["data"]["messages"]:
        msg["session_id"] = session_id
    return body


def _detail_body(*, with_new=True, session_id=MAIN_SESSION):
    """抓包里的 detail;with_new=False 时去掉本次提问的三条,只剩历史。"""
    body = json.loads(json.dumps(CAPTURE["detail"]))
    if not with_new:
        body["data"]["messages"] = [
            m for m in body["data"]["messages"] if USER_MSG not in m["msg_id"]]
    return _in_session(body, session_id)


def _md_message(msg_id, *pieces, extra=None):
    """手写一条老格式 AI 消息:每块一个 content_type 7 负载。"""
    return {"msg_id": msg_id, "reply_to": msg_id[3:], "status": 1,
            "msg_info": {"payloads": list(pieces),
                         "extra": extra or {"finish_reason": "stop"}}}


def _md_payload(text):
    return {"content_type": 7, "message_type": "ai_answer",
            "element": {"element_type": "markdown", "markdown": {"text": text}}}


def _quote(**fields):
    base = {"text_length": 5, "entity_type": "note", "entity_id": NOTE_A,
            "items": [{"entity_type": "note", "title": "标题甲",
                       "author_nickname": "作者", "content_summary": "摘要\n  甲",
                       "publish_time": 1771219140000}]}
    base.update(fields)
    return "::dqa:quote:" + json.dumps(base, ensure_ascii=False) + "::"


# ───────────────────────── 响应解析(基于真实抓包) ─────────────────────────

class ParseCaptureTests(unittest.TestCase):
    def test_detail_body_is_parsed_into_messages(self):
        parsed = parse_dots_body(json.dumps(CAPTURE["detail"]))
        ids = [m["msg_id"] for m in parsed["messages"]]
        self.assertIn(USER_MSG, ids)
        self.assertIn(AI_MSG, ids)
        self.assertIn("msg_b31bc9b737724026", ids)   # 历史里别的问题
        self.assertEqual(parsed["code"], 0)
        self.assertEqual(parsed["session_id"], MAIN_SESSION)

    def test_garbage_and_empty_bodies_yield_no_messages(self):
        for body in ("", "   ", "not json at all", "[1,2]", '{"data":[]}'):
            with self.subTest(body=body):
                self.assertEqual(parse_dots_body(body)["messages"], [])

    def test_detail_answer_has_clean_text_and_cited_sources(self):
        built = build_dots_answer(_message(CAPTURE["detail"], AI_MSG))
        answer = built["answer"]
        self.assertTrue(answer.startswith("**夏天7-10天一次，冬天10-14天一次**，"))
        self.assertIn("千万别天天洗。[1][2]", answer)
        self.assertIn("1. **洗前必须梳通**——", answer)
        self.assertNotIn("::dqa", answer)
        self.assertNotIn("​", answer)
        self.assertNotIn("<u>", answer)
        self.assertNotIn("***", answer)
        self.assertEqual(built["source_count"], 77)
        first, second = built["sources"]
        self.assertEqual(first["index"], 1)
        self.assertEqual(first["title"], "比熊洗澡频率&在家洗澡避坑指南")
        self.assertEqual(first["author"], "作者甲")
        self.assertEqual(first["note_id"], NOTE_A)
        self.assertEqual(
            first["url"], f"https://www.xiaohongshu.com/explore/{NOTE_A}")
        self.assertEqual(first["publish_time"], "2026-02-16T13:19:00+08:00")
        self.assertTrue(first["summary"].startswith("养比熊的家长都懂"))
        self.assertNotIn("\n", first["summary"])
        self.assertEqual(second["note_id"], NOTE_B)
        self.assertEqual(second["index"], 2)

    def test_reply_answer_has_same_text_but_only_note_ids(self):
        reply = build_dots_answer(_message(CAPTURE["reply"], AI_MSG))
        detail = build_dots_answer(_message(CAPTURE["detail"], AI_MSG))
        self.assertEqual(reply["answer"], detail["answer"])
        self.assertEqual(reply["source_count"], 77)
        self.assertEqual([s["note_id"] for s in reply["sources"]],
                         [NOTE_A, NOTE_B])
        self.assertTrue(all(s["title"] == "" for s in reply["sources"]))

    def test_history_answers_are_separate_messages(self):
        grooming = build_dots_answer(
            _message(CAPTURE["detail"], "ai-msg_b31bc9b737724026"))
        current = build_dots_answer(_message(CAPTURE["detail"], AI_MSG))
        self.assertNotEqual(grooming["answer"], current["answer"])
        self.assertEqual(grooming["source_count"], 83)

    def test_suggestions_come_from_the_follow_up_message_of_that_question(self):
        messages = parse_dots_body(json.dumps(CAPTURE["detail"]))["messages"]
        self.assertEqual(
            _suggestions(messages, USER_MSG),
            ["比熊洗澡用什么沐浴露比较好？", "比熊洗完澡怎么吹干才蓬松？"])
        self.assertEqual(_suggestions(messages, "msg_unknown"), [])


class MarkerAndTextTests(unittest.TestCase):
    def _built(self, text):
        return build_dots_answer(_md_message("ai-m1", _md_payload(text)))

    def test_quote_json_with_nested_braces_is_cut_at_its_real_end(self):
        quote = _quote(params={"a": {"b": "}::"}, "c": [{"d": 1}]})
        built = self._built(f"前文{quote}后文")
        self.assertEqual(built["answer"], "前文[1]后文")
        self.assertEqual(built["sources"][0]["note_id"], NOTE_A)

    def test_underline_keeps_its_text_and_unparsable_marker_is_dropped(self):
        underline = '::dqa:underline:{"text":"蒲公英","params":{}}::'
        built = self._built(f"在{underline}接单::dqa:quote:{{坏的 json}}::，好")
        self.assertEqual(built["answer"], "在蒲公英接单，好")
        self.assertEqual(built["sources"], [])

    def test_same_source_cited_twice_is_numbered_once(self):
        again = _quote(items=[{"entity_type": "note", "title": "标题甲",
                               "author_nickname": "作者"}])
        built = self._built(f"甲{_quote()}乙{again}丙")
        self.assertEqual(built["answer"], "甲[1]乙[1]丙")
        self.assertEqual(len(built["sources"]), 1)
        self.assertEqual(built["sources"][0]["publish_time"],
                         "2026-02-16T13:19:00+08:00")

    def test_item_ids_are_not_guessed_when_counts_differ(self):
        quote = _quote(entity_id=f"{NOTE_A},{NOTE_B}")
        source = self._built(f"x{quote}")["sources"][0]
        self.assertEqual(source["note_id"], "")
        self.assertEqual(source["url"], "")
        self.assertEqual(source["title"], "标题甲")

    def test_percent_encoded_ids_and_web_items(self):
        web_quote = _quote(
            entity_type="web", entity_id="e8a585ee##5",
            items=[{"entity_type": "web", "title": "网页", "site_name": "某站",
                    "domain_name": "某站官网", "content_summary": "摘要"}])
        web = self._built(f"x{web_quote}")["sources"][0]
        self.assertEqual((web["type"], web["site"], web["url"]),
                         ("web", "某站", ""))
        encoded = _quote(items=[], entity_id=f"{NOTE_A}%2C{NOTE_B}")
        sources = self._built(f"x{encoded}")["sources"]
        self.assertEqual([s["note_id"] for s in sources], [NOTE_A, NOTE_B])

    def test_old_format_chunks_highlight_summary_and_english_header(self):
        header = {"content_type": 7, "message_type": "ai_answer", "element": {
            "element_type": "spark", "spark": {
                "id": "dots_source_header",
                "data": {"sub_title": "search and summarize 15 posts"}}}}
        summary = {"content_type": 7, "message_type": "ai_answer", "element": {
            "spark": {"id": "search_agent_highlight_summary",
                      "data": {"highlight_markdown": "\n#### 总结\n\n- 要点"}}}}
        built = build_dots_answer(_md_message(
            "ai-m1", header, _md_payload("正文第一段"), _md_payload("\n第二段"),
            summary))
        self.assertEqual(
            built["answer"], "正文第一段\n\n第二段\n\n#### 总结\n\n- 要点")
        self.assertEqual(built["source_count"], 15)

    def test_clean_markdown(self):
        z = "​"
        cases = {
            f"{z}***{z}粗斜{z}***{z}，后": "**粗斜**，后",
            f"a{z}****{z}b": "ab",
            f"{z}**{z}没闭合": "没闭合",
            "** 空格 ** 与 ****": "**空格** 与",
            "<u>下划线</u>文字": "下划线文字",
            "👩‍💻 程序员﻿": "👩‍💻 程序员",
            "a\n\n\n\nb  \n": "a\n\nb",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(clean_markdown(raw), expected)

    def test_source_count_helper(self):
        self.assertEqual(source_count_from_text("参考小红书与全网77篇内容"), 77)
        self.assertEqual(source_count_from_text("参考小红书与全网 12 篇内容"), 12)
        self.assertEqual(source_count_from_text("search and summarize 24 posts"), 24)
        self.assertIsNone(source_count_from_text("没有数量"))
        self.assertIsNone(source_count_from_text(None))

    def test_session_id_helper(self):
        self.assertEqual(dots_session_id(REPLY_URL), NEW_SESSION)
        self.assertEqual(dots_session_id(
            "", "https://www.xiaohongshu.com/ai_chat/abcdef123456"),
            "abcdef123456")
        self.assertEqual(dots_session_id("", "https://x/ai_chat"), "")


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
            200, {"code": -100, "message": "请先登录", "messages": []}
        ).startswith("logged_out:"))
        self.assertTrue(dots_reply_failure(
            200, {"code": -9, "message": "操作频繁", "messages": []}
        ).startswith("captcha:"))
        self.assertIn("code=5", dots_reply_failure(
            200, {"code": 5, "message": "其他", "messages": []}))
        self.assertEqual(dots_reply_failure(
            200, {"code": 0, "message": "", "messages": [{"msg_id": "a"}]}), "")
        self.assertEqual(dots_reply_failure(
            200, {"code": 5, "message": "x", "messages": [{"msg_id": "a"}]}), "")

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
                      "dots.answer", "dots.new_chat", "dots.sources"):
            self.assertTrue(selector_candidates(group))
        self.assertIn("textarea", css_values("dots.input"))
        self.assertIn(NEW_CHAT, css_values("dots.new_chat"))
        self.assertIn(SOURCES_HEADER, css_values("dots.sources"))
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
        return ""

    async def click(self):
        self.page.clicked.append(self.key)
        self.page._deliver(self.page.on_click.get(self.key, ()))

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
        self._body = body if isinstance(body, str) else json.dumps(
            body, ensure_ascii=False)

    async def text(self):
        return self._body


class _Page:
    """Minimal stand-in for a Playwright page on /ai_chat。
    goto 时先送出 pre(主会话的 detail),按 Enter 后送出 on_send 里的响应。"""

    def __init__(self, *, present=None, on_send=(), pre=(), dom_answer="",
                 on_click=None, url="https://www.xiaohongshu.com/ai_chat"):
        self.present = set(present if present is not None else {INPUT, NEW_CHAT})
        self.on_send = list(on_send)
        self.pre = list(pre)
        self.on_click = dict(on_click or {})
        self.dom_answer = dom_answer
        self.url = url
        self.redirected = url != "https://www.xiaohongshu.com/ai_chat"
        self.listeners = []
        self.enter_pressed = False
        self.clicked = []
        self.order = []

    def on(self, event, listener):
        if event == "response":
            self.listeners.append(listener)

    def _deliver(self, responses):
        for response in responses:
            for listener in list(self.listeners):
                listener(response)

    async def goto(self, url, **_kwargs):
        # 未登录时平台会把 /ai_chat 重定向走;假页面用初始 url 模拟落地页
        if not self.redirected:
            self.url = url
        self._deliver(self.pre)

    async def on_enter(self):
        self.order.append("enter")
        self._deliver(self.on_send)

    def locator(self, selector):
        return _Locator(self, selector)

    def get_by_text(self, text, exact=False):
        return _Locator(self, text)

    async def evaluate(self, script, arg=None):
        if script is xhs_dots._DOTS_ANSWER_JS:
            return self.dom_answer if self.enter_pressed else ""
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
        self.page.order.append("type")
        self.typed = text

    async def insert_long(self, _locator, text, page=None):
        self.page.order.append("type")
        self.typed = text

    async def click_visible(self, locator, **_kwargs):
        if locator.key == NEW_CHAT:
            self.page.order.append("new_chat")
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


_REAL_SLEEP = asyncio.sleep


async def _fast_sleep(_delay, *_args):
    await _REAL_SLEEP(0)


async def _quick_wait(event, _seconds):
    await _REAL_SLEEP(0)
    return event.is_set()


def _ask(page, question=QUESTION, timeout_s=30, **kwargs):
    manager = _Manager(page)
    with patch("app.browser.xhs_dots.asyncio.sleep", _fast_sleep), \
            patch("app.browser.xhs_dots._wait_event", _quick_wait), \
            patch("app.browser.xhs_dots.DETAIL_GRACE_S", 0):
        result = asyncio.run(fetch_xhs_dots_ask(
            manager, _identity(), question, timeout_s=timeout_s, **kwargs))
    return manager, result


def _main_history():
    """打开页面时 detail 返回主会话的历史(只有历史,没有本次提问)。"""
    return [_Response(200, _detail_body(with_new=False), MAIN_DETAIL_URL)]


def _new_turn(*, reply=True, detail=True, session_id=NEW_SESSION):
    """按下发送后页面发出的请求:reply(带 msg_id)和随后的 detail。"""
    responses = []
    if reply:
        responses.append(_Response(
            200, _in_session(CAPTURE["reply"], session_id),
            REPLY_URL.replace(NEW_SESSION, session_id)))
    if detail:
        responses.append(_Response(
            200, _detail_body(session_id=session_id),
            DETAIL_URL.replace(NEW_SESSION, session_id)))
    return responses


class FetchDotsAskTests(unittest.TestCase):
    def test_answer_comes_from_detail_matched_by_msg_id(self):
        page = _Page(pre=_main_history(), on_send=_new_turn())
        manager, (result, error) = _ask(page)
        self.assertEqual(error, "")
        self.assertEqual(manager.xhs_interaction.typed, QUESTION)
        self.assertEqual(page.order, ["new_chat", "type", "enter"])
        self.assertEqual(result["via"], "detail")
        self.assertTrue(result["complete"])
        self.assertEqual(result["msg_id"], AI_MSG)
        self.assertEqual(result["session_id"], NEW_SESSION)
        self.assertEqual(result["session_mode"], "new")
        self.assertNotIn("session_note", result)
        self.assertTrue(result["answer"].startswith("**夏天7-10天一次"))
        grooming = build_dots_answer(   # 历史里别的问题的回答,不能取到
            _message(CAPTURE["detail"], "ai-msg_b31bc9b737724026"))
        self.assertNotEqual(result["answer"], grooming["answer"])
        self.assertEqual(
            result["answer"],
            build_dots_answer(_message(CAPTURE["detail"], AI_MSG))["answer"])
        self.assertEqual(result["source_count"], 77)
        self.assertEqual([s["title"] for s in result["sources"]],
                         ["比熊洗澡频率&在家洗澡避坑指南",
                          "🐶比熊多久洗一次澡？新手必看🛁"])
        self.assertEqual(result["suggestions"],
                         ["比熊洗澡用什么沐浴露比较好？",
                          "比熊洗完澡怎么吹干才蓬松？"])

    def test_reply_alone_is_used_when_detail_never_arrives(self):
        page = _Page(pre=_main_history(), on_send=_new_turn(detail=False))
        _manager, (result, error) = _ask(page)
        self.assertEqual(error, "")
        self.assertEqual(result["via"], "reply")
        self.assertTrue(result["complete"])
        self.assertEqual([s["note_id"] for s in result["sources"]],
                         [NOTE_A, NOTE_B])
        self.assertEqual(result["source_count"], 77)

    def test_user_message_is_found_in_detail_when_reply_is_not_intercepted(self):
        page = _Page(pre=_main_history(), on_send=_new_turn(reply=False))
        _manager, (result, error) = _ask(page)
        self.assertEqual(error, "")
        self.assertEqual(result["via"], "detail")
        self.assertEqual(result["msg_id"], AI_MSG)

    def test_new_session_false_stays_in_main_session(self):
        page = _Page(pre=_main_history(), on_send=_new_turn())
        _manager, (result, error) = _ask(page, new_session=False)
        self.assertEqual(error, "")
        self.assertNotIn("new_chat", page.order)
        self.assertEqual(result["session_mode"], "main")

    def test_missing_new_chat_entry_falls_back_to_main_with_a_note(self):
        page = _Page(present={INPUT}, pre=_main_history(), on_send=_new_turn())
        _manager, (result, error) = _ask(page)
        self.assertEqual(error, "")
        self.assertEqual(result["session_mode"], "main")
        self.assertIn("没找到「新对话」入口", result["session_note"])

    def test_new_chat_that_did_not_take_effect_is_reported(self):
        page = _Page(pre=_main_history(),
                     on_send=_new_turn(session_id=MAIN_SESSION))
        _manager, (result, error) = _ask(page)
        self.assertEqual(error, "")
        self.assertEqual(result["session_mode"], "main")
        self.assertIn("仍落在主会话", result["session_note"])

    def test_session_mode_is_unknown_without_a_baseline_session(self):
        page = _Page(on_send=_new_turn())
        _manager, (result, error) = _ask(page)
        self.assertEqual(error, "")
        self.assertEqual(result["session_mode"], "unknown")

    def test_dom_is_the_last_resort_when_no_api_response_is_seen(self):
        markdown = "## 要不要梳毛\n\n- 每天一次\n- 换毛期更勤"
        page = _Page(dom_answer=markdown)
        # 发送键兜底:Enter 没触发回答请求时点击发送
        page.present.add('[aria-label*="发送"]')
        _manager, (result, error) = _ask(page, new_session=False)
        self.assertEqual(error, "")
        self.assertEqual(result["via"], "dom")
        self.assertEqual(result["answer"], markdown)
        self.assertEqual(result["sources"], [])
        self.assertIn('[aria-label*="发送"]', page.clicked)

    def test_history_is_never_returned_as_the_answer(self):
        # 本次提问的 AI 消息始终没出现;detail 里只有别的问题的回答
        state = _DotsState()
        state.user_msg_id = "msg_not_in_detail"
        history = parse_dots_body(json.dumps(_detail_body(with_new=False)))
        for msg in history["messages"]:
            state.detail_msgs[msg["msg_id"]] = msg
        page = _Page()
        page.enter_pressed = True
        result, error = asyncio.run(_await_answer(
            _Manager(page), page, state, QUESTION, 0, True))
        self.assertIsNone(result)
        self.assertIn("没有给出回答", error)

    def test_answer_text_present_before_sending_is_not_taken_as_the_answer(self):
        page = _Page()
        original = page.evaluate

        async def evaluate(script, arg=None):
            if script is xhs_dots._DOTS_ANSWER_JS:
                return "欢迎来到点点"
            return await original(script, arg)

        page.evaluate = evaluate
        result, error = asyncio.run(_await_answer(
            _Manager(page), page, _DotsState(), "问题", 0, True,
            baseline="欢迎来到点点"))
        self.assertIsNone(result)
        self.assertIn("没有给出回答", error)

    def test_incomplete_answer_is_returned_on_timeout_with_complete_false(self):
        state = _DotsState()
        state.user_msg_id = USER_MSG
        partial = json.loads(json.dumps(_message(CAPTURE["reply"], AI_MSG)))
        partial["status"] = 0
        partial["msg_info"]["extra"].pop("finish_reason")
        state.reply_msgs[AI_MSG] = partial
        page = _Page()
        result, error = asyncio.run(_await_answer(
            _Manager(page), page, state, QUESTION, 0, True))
        self.assertEqual(error, "")
        self.assertFalse(result["complete"])
        self.assertTrue(result["answer"].startswith("**夏天7-10天一次"))

    def test_long_question_uses_bulk_insert(self):
        question = "比熊" * 40
        page = _Page(pre=_main_history(), on_send=_new_turn())
        manager, (_result, _error) = _ask(page, question=question)
        self.assertEqual(manager.xhs_interaction.typed, question)

    def test_http_risk_status_is_reported_without_retry(self):
        page = _Page(on_send=[_Response(461, "")])
        _manager, (result, error) = _ask(page)
        self.assertIsNone(result)
        self.assertTrue(error.startswith("captcha:"))
        self.assertEqual(classify_platform_error(error)[0], RiskCategory.RISK)

    def test_captcha_redirect_during_wait_is_reported(self):
        page = _Page(on_send=[_Response(200, "")])
        original = page.on_enter

        async def on_enter():
            page.url = "https://www.xiaohongshu.com/website-login/captcha?x=1"
            await original()

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


def _panel_page(panel_body=None, panel_url=PANEL_URL, **kwargs):
    responses = [] if panel_body is False else [
        _Response(200, PANEL if panel_body is None else panel_body, panel_url)]
    return _Page(
        present={INPUT, NEW_CHAT, SOURCES_HEADER}, pre=_main_history(),
        on_send=_new_turn(), on_click={SOURCES_HEADER: responses}, **kwargs)


class PanelSourcesTests(unittest.TestCase):
    def test_panel_body_is_parsed(self):
        parsed = parse_dots_panel(json.dumps(PANEL))
        self.assertEqual(parsed["card_id"], AI_MSG)
        self.assertEqual(parsed["total"], 3)
        first, second, third = parsed["sources"]
        self.assertEqual((first["type"], first["note_id"], first["origin"]),
                         ("note", NOTE_A, "panel"))
        self.assertEqual(first["author"], "作者甲")
        self.assertEqual(first["url"], f"https://www.xiaohongshu.com/explore/{NOTE_A}")
        self.assertEqual(first["publish_time"], "2026-02-16T13:19:00+08:00")
        self.assertEqual(first["xsec_token"], "tok-fake")
        self.assertEqual(len(first["summary"]), 200)
        self.assertNotIn("\n", first["summary"])
        self.assertEqual(second["summary"], "#比熊[话题]# #洗澡[话题]#")
        self.assertEqual(third["author"], "")
        self.assertEqual(third["summary"], "吹风机开低档 边吹边梳")
        for bad in ("", "x", "{}", '{"data":{"info":[]}}'):
            self.assertEqual(parse_dots_panel(bad)["sources"], [])

    def test_merge_keeps_quote_sources_first_and_dedupes_panel_ones(self):
        quote = build_dots_answer(_message(CAPTURE["detail"], AI_MSG))["sources"]
        panel = parse_dots_panel(json.dumps(PANEL))["sources"]
        merged = merge_panel_sources(quote, panel)
        self.assertEqual([s["note_id"] for s in merged],
                         [NOTE_A, NOTE_B, f"{7:024x}", f"{9:024x}"])
        self.assertEqual([s["index"] for s in merged], [1, 2, None, None])
        self.assertEqual([s["origin"] for s in merged],
                         ["quote", "quote", "panel", "panel"])
        self.assertEqual(merged[0]["title"], quote[0]["title"])   # 重复项不被覆盖
        self.assertEqual(len(quote), 2)                           # 入参没被改动

    def test_merge_adds_xsec_token_to_note_links(self):
        quote = [{"type": "note", "title": "t", "author": "", "site": "",
                  "note_id": f"{1:024x}", "url": "", "publish_time": "",
                  "summary": "", "index": 1, "origin": "quote"}]
        panel = [dict(quote[0], xsec_token="tok=", index=None, origin="panel"),
                 dict(quote[0], note_id=f"{2:024x}", title="u",
                      xsec_token="", index=None, origin="panel")]
        merged = merge_panel_sources(quote, panel)
        self.assertIn("xsec_token=tok%3D", merged[0]["url"])
        self.assertIn("xsec_source=pc_dots", merged[0]["url"])
        self.assertEqual(merged[1]["url"], "")   # 没有 token 时不改

    def test_merge_matches_quote_sources_without_ids_by_title(self):
        quote = [{"type": "note", "title": "比熊洗澡频率&在家洗澡避坑指南",
                  "author": "", "site": "", "note_id": "", "url": "",
                  "publish_time": "", "summary": "", "index": 1,
                  "origin": "quote"}]
        merged = merge_panel_sources(
            quote, parse_dots_panel(json.dumps(PANEL))["sources"])
        self.assertEqual(len(merged), 3)
        self.assertEqual(merged[0]["note_id"], NOTE_A)   # 补齐了空字段
        self.assertEqual(merged[0]["origin"], "quote")

    def test_new_session_opens_the_panel_and_merges_its_sources(self):
        page = _panel_page()
        _manager, (result, error) = _ask(page)
        self.assertEqual(error, "")
        self.assertIn(SOURCES_HEADER, page.clicked)
        self.assertEqual(result["session_mode"], "new")
        self.assertEqual(result["source_count"], 77)
        self.assertEqual([s["origin"] for s in result["sources"]],
                         ["quote", "quote", "panel", "panel"])
        self.assertIn("[1][2]", result["answer"])

    def test_panel_for_another_card_is_ignored(self):
        other = json.loads(json.dumps(PANEL))
        other["data"]["info"]["card_id"] = "ai-msg_other"
        for body, url in ((other, PANEL_URL),
                          (PANEL, PANEL_URL.replace(AI_MSG, "ai-msg_other"))):
            with self.subTest(url=url[-60:]):
                page = _panel_page(body, url)
                _manager, (result, error) = _ask(page)
                self.assertEqual(error, "")
                self.assertEqual([s["origin"] for s in result["sources"]],
                                 ["quote", "quote"])

    def test_missing_panel_response_is_skipped_silently(self):
        page = _panel_page(False)
        _manager, (result, error) = _ask(page)
        self.assertEqual(error, "")
        self.assertIn(SOURCES_HEADER, page.clicked)
        self.assertEqual(len(result["sources"]), 2)
        self.assertTrue(result["complete"])

    def test_failing_click_is_skipped_silently(self):
        page = _panel_page()

        async def broken_click():
            raise RuntimeError("detached")

        original = page.locator

        def locator(selector):
            loc = original(selector)
            if selector == SOURCES_HEADER:
                loc.click = broken_click
            return loc

        page.locator = locator
        _manager, (result, error) = _ask(page)
        self.assertEqual(error, "")
        self.assertEqual(len(result["sources"]), 2)

    def test_panel_is_not_opened_outside_a_new_session(self):
        page = _panel_page()
        _manager, (result, error) = _ask(page, new_session=False)
        self.assertEqual(error, "")
        self.assertEqual(result["session_mode"], "main")
        self.assertNotIn(SOURCES_HEADER, page.clicked)
        self.assertEqual(len(result["sources"]), 2)

    def test_panel_already_fetched_by_the_page_is_used_without_clicking(self):
        page = _panel_page()
        page.on_send = page.on_send + [_Response(200, PANEL, PANEL_URL)]
        _manager, (result, error) = _ask(page)
        self.assertEqual(error, "")
        self.assertNotIn(SOURCES_HEADER, page.clicked)
        self.assertEqual(len(result["sources"]), 4)


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
        self.assertTrue(main.XhsDotsAskIn(account_id=1, question="问").new_session)

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
            "source_count": 1, "session_id": "sess-1", "session_mode": "new",
            "msg_id": "ai-m1", "suggestions": ["再问"], "complete": True,
            "via": "detail"}
        with patch("app.main.fetch_xhs_dots_ask",
                   AsyncMock(return_value=(payload, ""))) as fetcher:
            result = self._call()
        self.assertEqual(result["question"], "比熊犬需要每天梳毛吗")
        self.assertEqual(result["answer"], "每天梳")
        self.assertEqual(result["session_id"], "sess-1")
        self.assertEqual(result["session_mode"], "new")
        self.assertEqual(result["suggestions"], ["再问"])
        self.assertEqual(result["sources"], payload["sources"])
        args, kwargs = fetcher.await_args
        self.assertEqual(args[2], "比熊犬需要每天梳毛吗")
        self.assertEqual(kwargs["timeout_s"], 90)
        self.assertTrue(kwargs["new_session"])
        self.assertEqual(
            self._events(), [(OperationKind.READ_HEAVY.value, "success")])

    def test_new_session_flag_is_forwarded(self):
        with patch("app.main.fetch_xhs_dots_ask",
                   AsyncMock(return_value=({"answer": "a"}, ""))) as fetcher:
            self._call(new_session=False)
        self.assertFalse(fetcher.await_args.kwargs["new_session"])

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
