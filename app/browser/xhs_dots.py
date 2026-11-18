"""小红书「点点」AI 问答:用账号浏览器打开 /ai_chat,(默认)点「新对话」,填问题、发送,
监听页面自己发出的 agentspark 接口响应取回答(免签名):
  - message/reply?msg_id=<用户消息 id>:一次性 JSON,回答好了才返回,但引用只有笔记 id;
  - conversation/detail?session_id=...:整个会话的历史,AI 回答在 msg_id=ai-<用户消息 id> 的那条,
    正文里的引用标记 ::dqa:quote:{...}:: 带标题/作者/摘要。
回答只按 msg_id 精确取,绝不取历史里别的消息。接口读不到时才退回读 DOM 里渲染好的最后一条回答。
页面或接口改版时,改下面的常量和 xhs_selectors 的 dots.* 组。
"""
from __future__ import annotations

import asyncio
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Tuple

from .identity import Identity
from .manager import BrowserManager
from .xhs_fetcher import (
    _BASE, _note_url, _visible_page_scope, _xhs_page_failure, _xhs_reading_pause)
from .xhs_selectors import css_values, find_visible, selector_diagnostic

DOTS_URL = f"{_BASE}/ai_chat"
DOTS_REPLY_API = "/api/sns/agentspark/web/message/reply"
DOTS_DETAIL_API = "/api/sns/agentspark/web/conversation/detail"
# 点开「参考小红书与全网 N 篇内容」折叠条后页面拉的来源面板,card_id 就是 AI 消息的 msg_id
DOTS_PANEL_API = "/api/sns/agentspark/web/v1/dqa/complex/source"
PANEL_WAIT_S = 8
PANEL_SUMMARY_MAX = 200

DOTS_MAX_QUESTION = 800
# reply 回来后,等页面接着拉 detail(带完整引用)的最长时间;拉不到就用 reply 的结果
DETAIL_GRACE_S = 10
_RISK_STATUS = {403, 429, 461, 471}
_NOTE_ID = re.compile(r"[0-9a-f]{24}")
_MARKER = re.compile(r"::dqa:(quote|underline):")
_HIGHLIGHT_SPARK = "search_agent_highlight_summary"
_SOURCE_HEADER_SPARK = "dots_source_header"
# 零宽字符只清 U+200B/U+FEFF/U+2060;U+200D 是 emoji 连接符,不能删
_ZERO_WIDTH = dict.fromkeys(map(ord, "​﻿⁠"))
_CST = timezone(timedelta(hours=8))
_DECODER = json.JSONDecoder()


# ───────────────────────── 响应解析(纯函数) ─────────────────────────

def parse_dots_body(body: str) -> dict:
    """reply / detail 响应体 → {messages, code, message, session_id}。
    messages 是 data.messages 里带 msg_id 的条目;解析不了就是空。"""
    try:
        data = json.loads(str(body or "").strip())
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return {"messages": [], "code": None, "message": "", "session_id": ""}
    payload = data.get("data") if isinstance(data.get("data"), dict) else {}
    messages = [m for m in (payload.get("messages") or [])
                if isinstance(m, dict) and m.get("msg_id")]
    session_id = next((str(m["session_id"]) for m in messages
                       if m.get("session_id")), "")
    return {"messages": messages, "code": data.get("code"),
            "message": data.get("msg") if isinstance(data.get("msg"), str) else "",
            "session_id": session_id}


def _payloads(msg: dict) -> List[dict]:
    info = msg.get("msg_info")
    items = info.get("payloads") if isinstance(info, dict) else None
    return [p for p in items or [] if isinstance(p, dict)]


def _extra(msg: dict) -> dict:
    info = msg.get("msg_info")
    extra = info.get("extra") if isinstance(info, dict) else None
    return extra if isinstance(extra, dict) else {}


def _elements(payload: dict) -> List[dict]:
    items = [e for e in payload.get("elements") or [] if isinstance(e, dict)]
    if isinstance(payload.get("element"), dict):
        items.append(payload["element"])
    return items


def _user_text(msg: dict) -> str:
    for payload in _payloads(msg):
        if payload.get("message_type") != "user_input":
            continue
        for item in payload.get("inputs") or []:
            info = item.get("info") if isinstance(item, dict) else None
            if isinstance(info, dict) and isinstance(info.get("text"), str):
                return info["text"].strip()
    return ""


def _is_user_message(msg: dict) -> bool:
    return any(p.get("message_type") == "user_input" for p in _payloads(msg))


def _msg_done(msg: dict) -> bool:
    return bool(_extra(msg).get("finish_reason")) or msg.get("status") == 1


def source_count_from_text(text: str) -> Optional[int]:
    """「参考小红书与全网77篇内容」/「search and summarize 15 posts」→ 篇数。"""
    match = re.search(r"(\d+)\s*(?:篇|posts?\b|notes?\b)", str(text or ""), re.I)
    return int(match.group(1)) if match else None


def _join_pieces(pieces: List[str]) -> str:
    """各块(markdown 元素/负载)都是整段的块,块与块之间补一个空行。"""
    return "\n\n".join(piece.strip("\n") for piece in pieces if piece.strip())


def _answer_raw(msg: dict) -> Tuple[str, Optional[int]]:
    """AI 消息的 ai_answer 负载 → (带标记的 markdown 原文, 参考篇数)。
    新格式一个 content_type 8 负载;老格式每块一个 content_type 7 负载,
    结尾可能有 search_agent_highlight_summary(总结),它也是回答的一部分。"""
    pieces: List[str] = []
    count: Optional[int] = None
    for payload in _payloads(msg):
        if payload.get("message_type") != "ai_answer":
            continue
        for element in _elements(payload):
            markdown = element.get("markdown")
            if isinstance(markdown, dict) and isinstance(markdown.get("text"), str):
                pieces.append(markdown["text"])
            spark = element.get("spark")
            if not isinstance(spark, dict):
                continue
            data = spark.get("data") if isinstance(spark.get("data"), dict) else {}
            if spark.get("id") == _SOURCE_HEADER_SPARK:
                count = source_count_from_text(
                    data.get("sub_title") or data.get("queryTitle"))
            elif spark.get("id") == _HIGHLIGHT_SPARK:
                if isinstance(data.get("highlight_markdown"), str):
                    pieces.append(data["highlight_markdown"])
    return _join_pieces(pieces), count


def _iso(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, _CST).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _make_source(kind: str, ident: str, item: dict) -> Optional[dict]:
    note_id = ident if kind == "note" and _NOTE_ID.fullmatch(ident) else ""
    title = str(item.get("title") or "").strip()
    if not note_id and not title:
        return None
    return {
        "type": kind,
        "title": title,
        "author": str(item.get("author_nickname") or "").strip(),
        "site": str(item.get("site_name") or item.get("domain_name") or "").strip(),
        "note_id": note_id,
        "url": f"{_BASE}/explore/{note_id}" if note_id else "",
        "publish_time": _iso(item.get("publish_time")),
        "summary": re.sub(r"\s+", " ", str(item.get("content_summary") or "")).strip(),
        "origin": "quote",
    }


def _quote_sources(quote: dict) -> List[dict]:
    """一个引用标记 → 来源列表。items 没有 id,靠 entity_id(逗号分隔)按位置对应,
    数量对不上就不猜 id;整段没有 items(reply 里就是这样)时只能给出笔记 id。"""
    kind = str(quote.get("entity_type") or "note")
    ids = [i.strip() for i in urllib.parse.unquote(
        str(quote.get("entity_id") or "")).split(",") if i.strip()]
    items = [i for i in quote.get("items") or [] if isinstance(i, dict)]
    out: List[Optional[dict]] = []
    if items:
        aligned = len(items) == len(ids)
        for index, item in enumerate(items):
            out.append(_make_source(
                str(item.get("entity_type") or kind),
                ids[index] if aligned else "", item))
    elif kind == "note":
        out = [_make_source("note", ident, {}) for ident in ids]
    return [source for source in out if source]


class _SourceBook:
    """按出现顺序给来源编号并去重(note_id 优先,否则 类型+标题+作者/站点)。"""

    def __init__(self) -> None:
        self.items: List[dict] = []
        self._index: dict = {}

    def add(self, source: dict) -> int:
        key = source["note_id"] or (
            source["type"], source["title"], source["author"] or source["site"])
        if key not in self._index:
            self._index[key] = len(self.items)
            self.items.append(dict(source))
        else:
            known = self.items[self._index[key]]
            for field_name, value in source.items():
                if value and not known.get(field_name):
                    known[field_name] = value
        return self._index[key] + 1


def _strip_markers(text: str) -> Tuple[str, List[dict]]:
    """剥掉 ::dqa:quote:{json}:: / ::dqa:underline:{json}::。json 里可能有嵌套花括号,
    用 raw_decode 找结束边界。quote 换成 [n] 脚注并收集来源,underline 保留其文字。"""
    book = _SourceBook()
    out: List[str] = []
    pos = 0
    while True:
        match = _MARKER.search(text, pos)
        if not match:
            out.append(text[pos:])
            break
        out.append(text[pos:match.start()])
        try:
            obj, end = _DECODER.raw_decode(text, match.end())
        except ValueError:
            close = text.find("}::", match.end())
            pos = close + 3 if close >= 0 else len(text)
            continue
        pos = end + 2 if text.startswith("::", end) else end
        if not isinstance(obj, dict):
            continue
        if match.group(1) == "underline":
            out.append(str(obj.get("text") or ""))
            continue
        numbers = []
        for source in _quote_sources(obj):
            number = book.add(source)
            if number not in numbers:
                numbers.append(number)
        out.append("".join(f"[{n}]" for n in numbers))
    sources = book.items
    for number, source in enumerate(sources, 1):
        source["index"] = number
    return "".join(out), sources


def _tidy_bold(match: "re.Match[str]") -> str:
    inner = match.group(1).strip()
    return f"**{inner}**" if inner else ""


def clean_markdown(text: str) -> str:
    """去零宽字符和 <u>,把 ***x*** 规整成 **x**,去掉空/残缺的加粗。"""
    text = str(text or "").translate(_ZERO_WIDTH).replace("\r\n", "\n")
    text = re.sub(r"</?u>", "", text)
    text = re.sub(r"\*{4,}", "", text)
    text = re.sub(r"\*{3}", "**", text)
    lines = []
    for line in text.split("\n"):
        if line.count("**") % 2:
            line = line.replace("**", "")
        lines.append(re.sub(r"\*\*(.*?)\*\*", _tidy_bold, line).rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _suggestions(messages: List[dict], user_msg_id: str) -> List[str]:
    words: List[str] = []
    for msg in messages:
        if msg.get("reply_to") != user_msg_id:
            continue
        for payload in _payloads(msg):
            if payload.get("message_type") != "ai_suggest":
                continue
            for element in _elements(payload):
                spark = element.get("spark")
                data = spark.get("data") if isinstance(spark, dict) else None
                for entity in (data or {}).get("entities") or []:
                    word = str(entity.get("word") or "").strip() \
                        if isinstance(entity, dict) else ""
                    if word and word not in words:
                        words.append(word)
    return words


def build_dots_answer(ai_msg: dict) -> dict:
    """一条 AI 消息 → {answer, sources, source_count, status}。
    source_count 是点点参考的总篇数,sources 只是正文里引用到的那几条。"""
    raw, count = _answer_raw(ai_msg)
    text, sources = _strip_markers(raw)
    return {"answer": clean_markdown(text), "sources": sources,
            "source_count": count if count is not None else (len(sources) or None)}


def parse_dots_panel(body: str) -> dict:
    """来源面板响应(complex/source)→ {card_id, total, sources}。
    每项 {id, type(note/video/...), note_info{title, paragraph_text, publish_time,
    xsec_token}, user{nick_name}};第一页即可,不翻页。"""
    try:
        data = json.loads(str(body or "").strip())
    except ValueError:
        data = None
    payload = data.get("data") if isinstance(data, dict) else None
    info = payload.get("info") if isinstance(payload, dict) else None
    if not isinstance(info, dict):
        return {"card_id": "", "total": None, "sources": []}
    sources: List[dict] = []
    for item in info.get("sources") or []:
        if not isinstance(item, dict):
            continue
        note = item.get("note_info") if isinstance(item.get("note_info"), dict) else {}
        user = item.get("user") if isinstance(item.get("user"), dict) else {}
        kind = "note" if str(item.get("type") or "note") in (
            "note", "video", "normal") else "web"
        ident = str(item.get("id") or note.get("id") or "")
        note_id = ident if kind == "note" and _NOTE_ID.fullmatch(ident) else ""
        title = str(note.get("title") or "").strip()
        link = next((str(note[k]) for k in ("url", "link") if str(
            note.get(k) or "").startswith("http")), "")
        if not note_id and not title:
            continue
        summary = re.sub(r"\s+", " ", str(note.get("paragraph_text") or "")).strip()
        sources.append({
            "type": kind,
            "title": title,
            "author": str(user.get("nick_name") or "").strip(),
            "site": str(note.get("site_name") or "").strip(),
            "note_id": note_id,
            "url": f"{_BASE}/explore/{note_id}" if note_id else link,
            "publish_time": _iso(note.get("publish_time")),
            "summary": summary[:PANEL_SUMMARY_MAX],
            "xsec_token": str(note.get("xsec_token") or ""),
            "index": None,
            "origin": "panel",
        })
    return {"card_id": str(info.get("card_id") or ""),
            "total": payload.get("total"), "sources": sources}


def merge_panel_sources(quote_sources: List[dict], panel_sources: List[dict]) -> List[dict]:
    """quote 来源在前(保留 index),面板来源去重后接在后面;重复的只补齐空字段。"""
    merged = [dict(source) for source in quote_sources]
    by_id = {s["note_id"]: s for s in merged if s.get("note_id")}
    by_title = {s["title"]: s for s in merged if s.get("title")}
    for source in panel_sources:
        known = (by_id.get(source.get("note_id") or "")
                 or by_title.get(source.get("title") or ""))
        if known is not None:
            for key, value in source.items():
                if value and not known.get(key) and key not in ("index", "origin"):
                    known[key] = value
            continue
        entry = dict(source)
        merged.append(entry)
        if entry["note_id"]:
            by_id[entry["note_id"]] = entry
        if entry["title"]:
            by_title[entry["title"]] = entry
    for entry in merged:   # 网页版打开笔记要带 xsec_token,否则跳登录/404
        if entry.get("note_id") and entry.get("xsec_token"):
            entry["url"] = _note_url(entry["note_id"], entry["xsec_token"], "pc_dots")
    return merged


def dots_reply_failure(status: int, parsed: dict) -> str:
    """HTTP 状态或业务 code 能证明风控/登录失效时,返回 captcha:/logged_out: 前缀的错误。"""
    if status in _RISK_STATUS:
        return f"captcha:小红书点点接口被风控或要求安全验证(http {status})"
    if status == 401:
        return "logged_out:小红书登录态已失效"
    code = parsed.get("code")
    message = str(parsed.get("message") or "")
    if code not in (None, 0, "0", 200) and not parsed.get("messages"):
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


def _query_param(url: str, *keys: str) -> str:
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(str(url or "")).query)
    for key in keys:
        if query.get(key):
            return query[key][0]
    return ""


def dots_session_id(reply_url: str, page_url: str = "") -> str:
    for url in (reply_url, page_url):
        found = _query_param(url, "session_id", "sessionId")
        if found:
            return found
    match = re.search(r"/ai_chat/([0-9A-Za-z_-]{6,})", str(page_url or ""))
    return match.group(1) if match else ""


# ───────────────────────── 页面端 JS ─────────────────────────

# 接口读不到时的兜底:取最后一条回答并转成 markdown(标题/列表/加粗/链接/代码/表格);
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


# ───────────────────────── 抓取 ─────────────────────────

@dataclass
class _DotsState:
    reply_seen: asyncio.Event = field(default_factory=asyncio.Event)
    pending: int = 0
    armed: bool = False            # 点发送前为 False:此时的 detail 只用来记基线
    user_msg_id: str = ""
    reply_msgs: dict = field(default_factory=dict)    # msg_id → 消息(reply 响应)
    detail_msgs: dict = field(default_factory=dict)   # msg_id → 消息(detail 响应)
    baseline_user_ids: set = field(default_factory=set)
    panels: dict = field(default_factory=dict)        # card_id → 来源面板第一页的解析结果
    main_session_id: str = ""      # 打开页面时 detail 里的会话(主会话)
    reply_url: str = ""
    session_id: str = ""
    failure: str = ""
    reply_status: int = 0
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


async def _wait_event(event: asyncio.Event, seconds: float) -> bool:
    try:
        await asyncio.wait_for(event.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return event.is_set()


async def _open_new_chat(mgr, page) -> bool:
    """点页面上的「新对话」(折叠侧栏里是铅笔图标按钮)。找不到入口返回 False。"""
    button, _name = await find_visible(page, "dots.new_chat")
    if button is None:
        return False
    try:
        click = getattr(getattr(mgr, "xhs_interaction", None), "click_visible", None)
        if callable(click):
            await click(button)
        else:
            await button.click()
    except Exception:
        return False
    await asyncio.sleep(1.0)
    return True


def _resolve_user_msg_id(state: _DotsState, question: str) -> str:
    """用户消息 id:优先取 reply 请求里的 msg_id;没截到 reply 时,
    在 detail 里找发送前不存在、文字等于提问的最新一条用户消息。"""
    if state.user_msg_id:
        return state.user_msg_id
    candidates = [
        m for m in state.detail_msgs.values()
        if _is_user_message(m) and _user_text(m) == question
        and m["msg_id"] not in state.baseline_user_ids]
    if not candidates:
        return ""
    return str(max(candidates, key=lambda m: m.get("timestamp") or 0)["msg_id"])


async def fetch_xhs_dots_ask(
        mgr: BrowserManager, identity: Identity, question: str,
        timeout_s: int = 120, keep_context: bool = False,
        new_session: bool = True,
) -> Tuple[Optional[dict], str]:
    """向点点提一个问题。返回 ({answer, sources, source_count, session_id, session_mode,
    msg_id, suggestions, complete, via}, error);错误以 captcha:/logged_out: 前缀标明风控/登录失效。
    new_session=True 时先点「新对话」,避免主会话的历史影响回答;找不到入口或没生效就退回主会话,
    session_mode 标 main(没生效时还带 session_note)。"""
    question = str(question or "").strip()
    timeout_s = max(10, min(300, int(timeout_s or 120)))
    state = _DotsState()
    result: Optional[dict] = None
    error = ""
    new_chat_clicked = False

    async def read_api(resp, is_reply: bool):
        state.pending += 1
        try:
            status = int(getattr(resp, "status", 0) or 0)
            if is_reply:
                state.reply_status = status
            try:
                body = await resp.text()
            except Exception:
                body = ""
            parsed = parse_dots_body(body)
            failure = dots_reply_failure(status, parsed) if (
                is_reply or status in _RISK_STATUS or status == 401) else ""
            if failure:
                state.failure = failure
            target = state.reply_msgs if is_reply else state.detail_msgs
            for msg in parsed["messages"]:
                target[str(msg["msg_id"])] = msg
                if not is_reply and not state.armed and _is_user_message(msg):
                    state.baseline_user_ids.add(str(msg["msg_id"]))
            if parsed["session_id"] and not is_reply and not state.armed:
                state.main_session_id = state.main_session_id or parsed["session_id"]
            if is_reply:
                state.session_id = state.session_id or parsed["session_id"]
        finally:
            state.pending -= 1

    async def read_panel(resp, url: str):
        try:
            if int(getattr(resp, "status", 0) or 0) != 200:
                return
            parsed = parse_dots_panel(await resp.text())
        except Exception:
            return
        card_id = _query_param(url, "card_id")
        if parsed["card_id"] and card_id and parsed["card_id"] != card_id:
            return
        card_id = card_id or parsed["card_id"]
        if card_id and parsed["sources"]:
            state.panels.setdefault(card_id, parsed)   # 只要第一页

    def on_response(resp):
        url = str(getattr(resp, "url", "") or "")
        if DOTS_PANEL_API in url:
            task = asyncio.ensure_future(read_panel(resp, url))
            state.tasks.add(task)
            task.add_done_callback(state.tasks.discard)
            return
        if DOTS_REPLY_API in url:
            if not state.armed:      # 发送前页面不会、也不该有 reply
                return
            state.reply_url = url
            state.user_msg_id = state.user_msg_id or _query_param(url, "msg_id")
            state.session_id = state.session_id or _query_param(url, "session_id")
            state.reply_seen.set()
            kind = True
        elif DOTS_DETAIL_API in url:
            if not state.armed and not state.main_session_id:
                state.main_session_id = _query_param(url, "session_id")
            kind = False
        else:
            return
        task = asyncio.ensure_future(read_api(resp, kind))
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
                    if new_session:
                        new_chat_clicked = await _open_new_chat(mgr, page)
                        if new_chat_clicked:   # 点完页面会重绘,重新取输入框
                            again, _name = await _poll_visible(
                                page, "dots.input", 10000)
                            box = again or box
                    await _xhs_reading_pause(mgr, content_length=200)
                    baseline = await _dots_dom_answer(page, "")
                    interaction = mgr.xhs_interaction
                    if len(question) > 60:
                        await interaction.insert_long(box, question, page=page)
                    else:
                        await interaction.type_short(box, question)
                    state.armed = True
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
                    if result is not None:
                        _mark_session(result, state, new_session, new_chat_clicked)
                        if result.get("session_mode") == "new":
                            await _attach_panel_sources(mgr, page, state, result)
            finally:
                for task in list(state.tasks):
                    task.cancel()
    except Exception as exc:
        error = error or f"点点问答失败: {exc!r}"
    return result, error


async def _attach_panel_sources(mgr, page, state: _DotsState, result: dict) -> None:
    """新会话里页面上只有本条回答:点开它的「参考…篇内容」折叠条,取来源面板第一页,
    card_id 必须等于本条回答的 msg_id 才采用。点击失败或 PANEL_WAIT_S 秒内没截到都静默跳过。"""
    msg_id = result.get("msg_id") or ""
    if not msg_id:
        return
    try:
        if msg_id not in state.panels:
            toggle, _name = await find_visible(page, "dots.sources")
            if toggle is None:
                return
            click = getattr(getattr(mgr, "xhs_interaction", None),
                            "click_visible", None)
            if callable(click):
                await click(toggle)
            else:
                await toggle.click()
            for _ in range(int(PANEL_WAIT_S / 0.5)):
                if msg_id in state.panels:
                    break
                await asyncio.sleep(0.5)
        panel = state.panels.get(msg_id)
        if panel:
            result["sources"] = merge_panel_sources(
                result.get("sources") or [], panel["sources"])
    except Exception:
        return


def _mark_session(result: dict, state: _DotsState, requested: bool,
                  clicked: bool) -> None:
    """session_mode:new(确认落在新会话)/ main(主会话)/ unknown(点了新对话但无法比对)。"""
    session_id = result.get("session_id") or ""
    if not requested:
        result["session_mode"] = "main"
        return
    if not clicked:
        result["session_mode"] = "main"
        result["session_note"] = "没找到「新对话」入口,已在主会话里提问,回答可能受历史对话影响"
    elif session_id and state.main_session_id:
        if session_id != state.main_session_id:
            result["session_mode"] = "new"
        else:
            result["session_mode"] = "main"
            result["session_note"] = "点了「新对话」但提问仍落在主会话,回答可能受历史对话影响"
    else:
        result["session_mode"] = "unknown"


def _api_result(state: _DotsState, uid: str, ai_id: str, via: str,
                complete: bool) -> dict:
    msg = (state.detail_msgs.get(ai_id) if via == "detail"
           else state.reply_msgs.get(ai_id)) or {}
    built = build_dots_answer(msg)
    everything = list(state.detail_msgs.values()) + list(state.reply_msgs.values())
    return {
        "answer": built["answer"],
        "sources": built["sources"],
        "source_count": built["source_count"],
        "session_id": str(msg.get("session_id") or state.session_id
                          or dots_session_id(state.reply_url)),
        "msg_id": ai_id,
        "suggestions": _suggestions(everything, uid),
        "complete": complete,
        "via": via,
    }


async def _await_answer(mgr, page, state: _DotsState, question: str,
                        timeout_s: int, sent: bool,
                        baseline: str = "") -> Tuple[Optional[dict], str]:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    reply_done_at: Optional[float] = None
    last_dom: Any = None
    stable = 0
    dom_text = ""
    while loop.time() < deadline:
        await asyncio.sleep(1.0)
        if state.failure:
            return None, state.failure
        url_failure = dots_url_failure(getattr(page, "url", ""))
        if url_failure:
            return None, url_failure
        uid = _resolve_user_msg_id(state, question)
        if uid:
            ai_id = f"ai-{uid}"
            reply_ai = state.reply_msgs.get(ai_id)
            detail_ai = state.detail_msgs.get(ai_id)
            reply_done = reply_ai is not None and _msg_done(reply_ai)
            if reply_done and reply_done_at is None:
                reply_done_at = loop.time()
            detail_done = detail_ai is not None and (
                bool(_extra(detail_ai).get("finish_reason"))
                or (reply_done and detail_ai.get("status") == 1))
            if detail_done and state.pending == 0:
                return _api_result(state, uid, ai_id, "detail", True), ""
            if (reply_done and state.pending == 0 and reply_done_at is not None
                    and loop.time() - reply_done_at >= DETAIL_GRACE_S):
                return _api_result(state, uid, ai_id, "reply", True), ""
            continue
        # 没有用户消息 id(没截到 reply、detail 里也没找到)才用 DOM 兜底
        dom_text = await _dots_dom_answer(page, question)
        if dom_text == baseline:   # 发送前页面上就有的文字(欢迎语/推荐问题)不算回答
            dom_text = ""
        stable = stable + 1 if dom_text == last_dom else 0
        last_dom = dom_text
        if dom_text and stable >= 5 and state.pending == 0 \
                and not await _dots_generating(page):
            return _dom_result(state, page, dom_text, True), ""

    uid = _resolve_user_msg_id(state, question)
    ai_id = f"ai-{uid}" if uid else ""
    for via, store in (("detail", state.detail_msgs), ("reply", state.reply_msgs)):
        if ai_id and ai_id in store and build_dots_answer(store[ai_id])["answer"]:
            return _api_result(state, uid, ai_id, via, False), ""
    dom_final = await _dots_dom_answer(page, question)
    dom_final = "" if dom_final == baseline else (dom_final or dom_text)
    if dom_final and not uid:
        return _dom_result(state, page, dom_final, False), ""
    failure = dots_url_failure(page.url) or await _xhs_page_failure(page)
    if failure:
        return None, failure
    if not sent:
        return None, "已填写问题但没有看到点点发出回答请求（发送键或页面结构可能已调整）"
    return None, f"点点在 {timeout_s} 秒内没有给出回答"


def _dom_result(state: _DotsState, page, text: str, complete: bool) -> dict:
    return {
        "answer": clean_markdown(text),
        "sources": [],
        "source_count": None,
        "session_id": state.session_id or dots_session_id(
            state.reply_url, getattr(page, "url", "")),
        "msg_id": "",
        "suggestions": [],
        "complete": complete,
        "via": "dom",
    }
