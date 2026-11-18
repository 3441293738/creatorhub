"""小红书「点点」AI 问答:用账号浏览器打开 /ai_chat 新对话,填问题、发送,
监听页面自己发出的 agentspark reply 接口响应取回答(免签名);截不到时读 DOM
里渲染好的最后一条回答。页面或接口改版时,改下面的常量和 xhs_selectors 的 dots.* 组。
"""
from __future__ import annotations

import asyncio
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

from .identity import Identity
from .manager import BrowserManager
from .xhs_fetcher import (
    _BASE, _visible_page_scope, _xhs_page_failure, _xhs_reading_pause)
from .xhs_selectors import css_values, find_visible, selector_diagnostic

DOTS_URL = f"{_BASE}/ai_chat"
DOTS_REPLY_API = "/api/sns/agentspark/web/message/reply"
DOTS_API_MARKER = "/api/sns/agentspark/"

DOTS_MAX_QUESTION = 800
_RISK_STATUS = {403, 429, 461, 471}
_REPLY_TEXT_KEYS = ("content", "text", "answer", "delta", "reply_content",
                    "msg_content", "reply", "message")
_REPLY_NEST_KEYS = ("data", "msg", "message", "result", "reply", "content",
                    "body", "payload", "response")
_FINISH_KEYS = ("finish", "finished", "is_finish", "is_finished", "is_end",
                "end", "done", "is_done", "finish_reason")
_FINISH_WORDS = {"finish", "finished", "done", "end", "ended", "complete",
                 "completed", "stop", "stopped"}
_SOURCE_LIST_KEYS = ("references", "reference", "refs", "sources", "citations",
                     "reference_list", "search_results", "web_pages", "notes")
_ID_KEYS = ("session_id", "sessionId", "conversation_id", "conversationId")


# ───────────────────────── 响应解析(纯函数) ─────────────────────────

def _json_events(body: str) -> List[Any]:
    """整段 JSON、SSE(data: ...)、NDJSON、粘连的多个 JSON 都拆成事件列表。"""
    text = str(body or "").strip()
    if not text:
        return []
    try:
        return [json.loads(text)]
    except ValueError:
        pass
    events: List[Any] = []
    decoder = json.JSONDecoder()
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith((":", "event:", "id:", "retry:")):
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            continue
        index = 0
        while index < len(line):
            try:
                value, end = decoder.raw_decode(line, index)
            except ValueError:
                break
            events.append(value)
            index = end
            while index < len(line) and line[index] in " \t,":
                index += 1
    return events


def _event_text(node: Any, depth: int = 0) -> str:
    if depth > 6:
        return ""
    if isinstance(node, list):
        return "".join(_event_text(item, depth + 1) for item in node)
    if not isinstance(node, dict):
        return ""
    for key in _REPLY_TEXT_KEYS:
        value = node.get(key)
        if isinstance(value, str) and value:
            return value
    for key in _REPLY_NEST_KEYS:
        value = node.get(key)
        if isinstance(value, (dict, list)):
            found = _event_text(value, depth + 1)
            if found:
                return found
    return ""


def _event_finished(node: Any, depth: int = 0) -> bool:
    if depth > 6:
        return False
    if isinstance(node, list):
        return any(_event_finished(item, depth + 1) for item in node)
    if not isinstance(node, dict):
        return False
    for key in _FINISH_KEYS:
        value = node.get(key)
        if value is True:
            return True
        if isinstance(value, str) and value.strip().lower() in _FINISH_WORDS:
            return True
    return any(_event_finished(value, depth + 1) for value in node.values()
               if isinstance(value, (dict, list)))


def _find_ids(node: Any, out: dict, depth: int = 0) -> None:
    if depth > 6:
        return
    if isinstance(node, list):
        for item in node:
            _find_ids(item, out, depth + 1)
    elif isinstance(node, dict):
        for key in _ID_KEYS:
            if node.get(key) and not out.get("session_id"):
                out["session_id"] = str(node[key])
        for value in node.values():
            if isinstance(value, (dict, list)):
                _find_ids(value, out, depth + 1)


def _source_from_dict(item: dict) -> Optional[dict]:
    title = ""
    for key in ("title", "name", "note_title", "display_title", "page_title"):
        if isinstance(item.get(key), str) and item[key].strip():
            title = item[key].strip()
            break
    url = ""
    for key in ("url", "link", "jump_url", "href", "page_url", "source_url"):
        if isinstance(item.get(key), str) and item[key].startswith("http"):
            url = item[key]
            break
    note_id = str(item.get("note_id") or "")
    if not url and note_id:
        url = f"{_BASE}/explore/{note_id}"
    if not url:
        return None
    return {"title": title, "url": url}


def _collect_sources(node: Any, out: list, depth: int = 0) -> None:
    if depth > 6:
        return
    if isinstance(node, list):
        for item in node:
            _collect_sources(item, out, depth + 1)
    elif isinstance(node, dict):
        for key, value in node.items():
            if key in _SOURCE_LIST_KEYS and isinstance(value, list):
                for item in value:
                    source = _source_from_dict(item) if isinstance(item, dict) else None
                    if source:
                        out.append(source)
            elif isinstance(value, (dict, list)):
                _collect_sources(value, out, depth + 1)


def _merge_text(current: str, piece: str) -> str:
    """累计式(新文本以旧文本开头)取新值,增量式拼接,过期的较短文本忽略。"""
    if not piece:
        return current
    if not current or piece.startswith(current):
        return piece
    if current.startswith(piece):
        return current
    return current + piece


def parse_dots_reply_body(body: str) -> dict:
    """解析一次 reply 响应体 → {text, finished, sources, session_id, code, message}。
    结构按「JSON / SSE / NDJSON」宽松处理;拿不到的字段保持空。"""
    events = _json_events(body)
    text = ""
    finished = False
    sources: list = []
    ids: dict = {}
    code: Any = None
    message = ""
    for event in events:
        text = _merge_text(text, _event_text(event))
        finished = finished or _event_finished(event)
        _collect_sources(event, sources)
        _find_ids(event, ids)
        if isinstance(event, dict):
            if code is None and "code" in event:
                code = event.get("code")
            if not message and isinstance(event.get("msg"), str):
                message = event["msg"]
    return {"text": text, "finished": finished, "sources": sources,
            "session_id": ids.get("session_id", ""), "code": code,
            "message": message, "events": len(events)}


def dots_reply_failure(status: int, parsed: dict) -> str:
    """HTTP 状态或业务 code 能证明风控/登录失效时,返回 captcha:/logged_out: 前缀的错误。"""
    if status in _RISK_STATUS:
        return f"captcha:小红书点点接口被风控或要求安全验证(http {status})"
    if status == 401:
        return "logged_out:小红书登录态已失效"
    code = parsed.get("code")
    message = str(parsed.get("message") or "")
    if code not in (None, 0, "0", 200) and not parsed.get("text"):
        if any(word in message for word in ("登录", "未登录")):
            return f"logged_out:小红书登录态已失效：{message[:80]}"
        if any(word in message for word in ("验证", "频繁", "风控", "异常")):
            return f"captcha:小红书点点要求安全验证：{message[:80]}"
        return f"点点接口返回错误: code={code} msg={message[:80]}"
    return ""


def dots_url_failure(url: str) -> str:
    lowered = str(url or "").lower()
    if "/website-login/captcha" in lowered or "error_code=300012" in lowered:
        return "captcha:小红书要求安全验证"
    if "passport" in lowered or "/login" in lowered:
        return "logged_out:小红书登录态已失效"
    return ""


def dots_session_id(reply_url: str, page_url: str = "") -> str:
    for url in (reply_url, page_url):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(str(url or "")).query)
        for key in ("session_id", "sessionId"):
            if query.get(key):
                return query[key][0]
    match = re.search(r"/ai_chat/([0-9A-Za-z_-]{6,})", str(page_url or ""))
    return match.group(1) if match else ""


def merge_sources(*groups: list) -> List[dict]:
    seen: dict = {}
    for group in groups:
        for item in group or []:
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            title = str(item.get("title") or "").strip()
            if url not in seen:
                seen[url] = {"title": title, "url": url}
            elif title and not seen[url]["title"]:
                seen[url]["title"] = title
    return list(seen.values())


def source_count_from_text(text: str) -> Optional[int]:
    match = re.search(r"参考[^\d]{0,12}(\d+)\s*篇", str(text or ""))
    return int(match.group(1)) if match else None


# ───────────────────────── 页面端 JS ─────────────────────────

# 取最后一条回答并转成 markdown(标题/列表/加粗/链接/代码/表格);
# 排除提问气泡和「参考…篇内容」折叠条。参数 [选择器列表, 问题文本]。
_DOTS_ANSWER_JS = """([selectors, question]) => {
    const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
    const q = norm(question);
    const visible = el => {
      const st = getComputedStyle(el);
      return st.display !== 'none' && st.visibility !== 'hidden';
    };
    const skipTag = new Set(['BUTTON', 'SVG', 'STYLE', 'SCRIPT', 'TEXTAREA', 'INPUT', 'IMG']);
    const md = (node, ctx) => {
      if (node.nodeType === 3) return node.textContent;
      if (node.nodeType !== 1 || skipTag.has(node.tagName.toUpperCase())) return '';
      if (!visible(node)) return '';
      const tag = node.tagName.toLowerCase();
      const kids = (c) => Array.from(node.childNodes).map(n => md(n, c || ctx)).join('');
      if (/^h[1-6]$/.test(tag)) return '\\n\\n' + '#'.repeat(+tag[1]) + ' ' + kids().trim() + '\\n\\n';
      if (tag === 'br') return '\\n';
      if (tag === 'strong' || tag === 'b') { const t = kids().trim(); return t ? '**' + t + '**' : ''; }
      if (tag === 'em' || tag === 'i') { const t = kids().trim(); return t ? '*' + t + '*' : ''; }
      if (tag === 'code') return '`' + node.textContent + '`';
      if (tag === 'pre') return '\\n\\n```\\n' + node.textContent.replace(/\\n+$/, '') + '\\n```\\n\\n';
      if (tag === 'a') {
        const t = kids().trim(); const href = node.href || '';
        return t && /^https?:/.test(href) ? '[' + t + '](' + href + ')' : t;
      }
      if (tag === 'ul' || tag === 'ol') {
        let i = 0;
        const items = Array.from(node.children).map(li => {
          i += 1;
          const body = md(li, {}).trim().replace(/\\n/g, '\\n  ');
          return (tag === 'ol' ? i + '. ' : '- ') + body;
        });
        return '\\n' + items.join('\\n') + '\\n\\n';
      }
      if (tag === 'li') return kids();
      if (tag === 'tr') return '| ' + Array.from(node.children).map(c => md(c, {}).trim()).join(' | ') + ' |\\n';
      if (tag === 'table') return '\\n\\n' + kids() + '\\n';
      if (tag === 'p') return '\\n\\n' + kids().trim() + '\\n\\n';
      const display = getComputedStyle(node).display;
      const block = /^(block|flex|grid|list-item|table)/.test(display);
      const inner = kids();
      return block ? '\\n' + inner + '\\n' : inner;
    };
    const matched = Array.from(document.querySelectorAll(selectors.join(','))).filter(visible);
    const outer = matched.filter(el => !matched.some(o => o !== el && o.contains(el)));
    const bubbles = outer.map(el => ({el, text: norm(el.innerText)}))
      .filter(b => b.text && !(q && (b.text === q || (b.text.startsWith(q) && b.text.length < q.length + 8))));
    const last = bubbles[bubbles.length - 1];
    if (!last) return '';
    return md(last.el, {})
      .replace(/^\\s*参考[^\\n]{0,24}\\d+\\s*篇[^\\n]*$/gm, '')
      .replace(/[ \\t]+\\n/g, '\\n').replace(/\\n{3,}/g, '\\n\\n').trim();
}"""

_DOTS_LINKS_JS = """() => Array.from(document.querySelectorAll('a[href]'))
    .filter(a => /^https?:/.test(a.href))
    .map(a => ({
      title: ((a.innerText || a.title || a.getAttribute('aria-label') || '')
              .replace(/\\s+/g, ' ').trim()).slice(0, 200),
      url: a.href,
    }))"""

_DOTS_SOURCE_NOISE = ("xiaohongshu.com/ai_chat", "xiaohongshu.com/login",
                      "beian", "miit.gov.cn", "xiaohongshu.com/explore?",
                      "xiaohongshu.com/user/profile")


# ───────────────────────── 抓取 ─────────────────────────

@dataclass
class _DotsState:
    reply_seen: asyncio.Event = field(default_factory=asyncio.Event)
    pending: int = 0
    api_text: str = ""
    api_finished: bool = False
    api_sources: list = field(default_factory=list)
    reply_url: str = ""
    session_id: str = ""
    failure: str = ""
    reply_status: int = 0
    events: int = 0
    tasks: set = field(default_factory=set)


async def _poll_visible(page, group: str, wait_ms: int):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait_ms / 1000
    while True:
        locator, name = await find_visible(page, group)
        if locator is not None:
            return locator, name
        if loop.time() >= deadline:
            return None, ""
        await asyncio.sleep(0.5)


async def _dots_dom_answer(page, question: str) -> str:
    try:
        return str(await page.evaluate(
            _DOTS_ANSWER_JS, [css_values("dots.answer"), question]) or "")
    except Exception:
        return ""


async def _dots_generating(page) -> bool:
    locator, _name = await find_visible(page, "dots.generating")
    return locator is not None


async def _dots_collect_sources(mgr, page, answer_links_before: set) -> Tuple[list, Optional[int]]:
    """点开「参考小红书与全网 N 篇内容」,返回新出现的链接(标题+url)和篇数。"""
    toggle, _name = await find_visible(page, "dots.sources")
    if toggle is None:
        return [], None
    count = None
    try:
        count = source_count_from_text(await toggle.inner_text(timeout=1500))
    except Exception:
        pass
    try:
        click = getattr(getattr(mgr, "xhs_interaction", None), "click_visible", None)
        if callable(click):
            await click(toggle)
        else:
            await toggle.click()
        await asyncio.sleep(1.5)
        links = await page.evaluate(_DOTS_LINKS_JS) or []
    except Exception:
        return [], count
    fresh = [item for item in links
             if item["url"] not in answer_links_before
             and not any(noise in item["url"] for noise in _DOTS_SOURCE_NOISE)]
    return fresh, count


async def fetch_xhs_dots_ask(
        mgr: BrowserManager, identity: Identity, question: str,
        timeout_s: int = 120, keep_context: bool = False,
) -> Tuple[Optional[dict], str]:
    """向点点提一个问题。返回 ({answer, sources, source_count, session_id,
    complete, via}, error);错误以 captcha:/logged_out: 前缀标明风控/登录失效。"""
    question = str(question or "").strip()
    timeout_s = max(10, min(300, int(timeout_s or 120)))
    state = _DotsState()
    result: Optional[dict] = None
    error = ""

    async def read_reply(resp):
        state.pending += 1
        try:
            status = int(getattr(resp, "status", 0) or 0)
            state.reply_status = status
            try:
                body = await resp.text()
            except Exception:
                body = ""
            parsed = parse_dots_reply_body(body)
            state.events += parsed["events"]
            failure = dots_reply_failure(status, parsed)
            if failure:
                state.failure = failure
            state.api_text = _merge_text(state.api_text, parsed["text"])
            state.api_finished = state.api_finished or parsed["finished"]
            state.api_sources.extend(parsed["sources"])
            state.session_id = state.session_id or parsed["session_id"]
        finally:
            state.pending -= 1

    def on_response(resp):
        url = str(getattr(resp, "url", "") or "")
        if DOTS_REPLY_API not in url:
            if DOTS_API_MARKER in url and not state.session_id:
                state.session_id = dots_session_id(url)
            return
        state.reply_url = url
        state.session_id = state.session_id or dots_session_id(url)
        state.reply_seen.set()
        task = asyncio.ensure_future(read_reply(resp))
        state.tasks.add(task)
        task.add_done_callback(state.tasks.discard)

    try:
        async with _visible_page_scope(mgr, identity, keep_context) as page:
            page.on("response", on_response)
            try:
                await page.goto(
                    DOTS_URL, wait_until="domcontentloaded", timeout=30000)
                box, _name = await _poll_visible(page, "dots.input", 15000)
                if box is None:
                    failure = (dots_url_failure(page.url)
                               or await _xhs_page_failure(page))
                    error = failure or (
                        "未找到点点输入框（登录失效或页面结构已调整） "
                        + await selector_diagnostic(page, "dots.input"))
                else:
                    await _xhs_reading_pause(mgr, content_length=200)
                    baseline = await _dots_dom_answer(page, "")
                    interaction = mgr.xhs_interaction
                    if len(question) > 60:
                        await interaction.insert_long(box, question, page=page)
                    else:
                        await interaction.type_short(box, question)
                    await box.press("Enter")
                    sent = await _wait_event(state.reply_seen, 8)
                    if not sent:
                        send, _name = await find_visible(
                            page, "dots.send", require_enabled=True)
                        if send is not None:
                            await interaction.click_visible(send)
                            sent = await _wait_event(state.reply_seen, 8)
                    result, error = await _await_answer(
                        mgr, page, state, question, timeout_s, sent, baseline)
            finally:
                for task in list(state.tasks):
                    task.cancel()
    except Exception as exc:
        error = error or f"点点问答失败: {exc!r}"
    return result, error


async def _wait_event(event: asyncio.Event, seconds: float) -> bool:
    try:
        await asyncio.wait_for(event.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return event.is_set()


async def _await_answer(mgr, page, state: _DotsState, question: str,
                        timeout_s: int, sent: bool,
                        baseline: str = "") -> Tuple[Optional[dict], str]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    last_sig: Any = None
    stable = 0
    dom_text = ""
    done = False
    while loop.time() < deadline:
        await asyncio.sleep(1.0)
        if state.failure:
            return None, state.failure
        url_failure = dots_url_failure(getattr(page, "url", ""))
        if url_failure:
            return None, url_failure
        dom_text = await _dots_dom_answer(page, question)
        if dom_text == baseline:   # 发送前页面上就有的文字(欢迎语/推荐问题)不算回答
            dom_text = ""
        sig = (len(state.api_text), dom_text)
        stable = stable + 1 if sig == last_sig else 0
        last_sig = sig
        has_text = bool(state.api_text or dom_text)
        if state.pending == 0 and has_text and state.api_finished:
            done = True
        elif (state.pending == 0 and has_text and stable >= 5
              and not await _dots_generating(page)):
            done = True
        if done:
            break
    if not (state.api_text or dom_text):
        failure = dots_url_failure(page.url) or await _xhs_page_failure(page)
        if failure:
            return None, failure
        if not sent:
            return None, "已填写问题但没有看到点点发出回答请求（发送键或页面结构可能已调整）"
        return None, f"点点在 {timeout_s} 秒内没有给出回答"

    dom_final = await _dots_dom_answer(page, question)
    if dom_final == baseline:
        dom_final = ""
    dom_final = dom_final or dom_text
    api_ok = bool(state.api_text) and state.api_finished
    answer = state.api_text if api_ok else (dom_final or state.api_text)
    via = "api" if answer and answer == state.api_text else "dom"

    links_before: set = set()
    try:
        links_before = {item["url"] for item in
                        (await page.evaluate(_DOTS_LINKS_JS) or [])}
    except Exception:
        pass
    dom_sources, count = await _dots_collect_sources(mgr, page, links_before)
    sources = merge_sources(state.api_sources, dom_sources)
    return {
        "answer": answer.strip(),
        "sources": sources,
        "source_count": count if count is not None else (len(sources) or None),
        "session_id": state.session_id or dots_session_id(
            state.reply_url, getattr(page, "url", "")),
        "complete": done,
        "via": via,
    }, ""
