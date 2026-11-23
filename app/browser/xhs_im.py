"""Xiaohongshu Web private-message read adapter.

The adapter strictly uses the signed-in, visible account browser and lets the
first-party Web client issue its normal signed requests (`X-s` / `X-t` /
`X-s-common`). It passively observes responses via `page.on("response", ...)`
and falls back to reading `window.__INITIAL_STATE__` / rendered DOM — never
firing unsigned `window.fetch` calls to `edith.xiaohongshu.com`.
"""
from __future__ import annotations

import asyncio
import json
import weakref
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from .human_interaction import DEFAULT_HUMAN_ENGINE, human_scroll, human_wait


CHAT_URL = "https://www.xiaohongshu.com/chat"
_CHATS_PATH = "/api/im/web/v3/chats"
_UNREAD_PATH = "/api/im/web/chat/get_unread"
_HISTORY_PATH = "/api/im/web/messages/history"

# Passive Python-side cache of observed XHS IM responses per Page instance
_PAGE_IM_CACHE: weakref.WeakKeyDictionary[Any, dict[str, Any]] = (
    weakref.WeakKeyDictionary()
)


def _page_cache(page: Any) -> dict[str, Any]:
    try:
        cache = _PAGE_IM_CACHE.get(page)
        if cache is None:
            cache = {"chats": [], "unread": None, "history": {}}
            _PAGE_IM_CACHE[page] = cache
        return cache
    except TypeError:
        return {"chats": [], "unread": None, "history": {}}


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except Exception:
        return value


def _seconds(value: Any) -> int:
    try:
        number = int(value or 0)
    except (TypeError, ValueError):
        return 0
    while number > 10_000_000_000:
        number //= 1000
    return max(0, number)


def _message_text(content: Any) -> tuple[str, str]:
    payload = _json(content)
    if isinstance(payload, str):
        return payload.strip(), "text"
    if not isinstance(payload, dict):
        return "", "unknown"
    raw = payload.get("content")
    if isinstance(raw, dict):
        text = str(raw.get("text") or raw.get("content") or "").strip()
    else:
        text = str(raw or payload.get("text") or "").strip()
    content_type = payload.get("content_type")
    kind = "text" if text else str(content_type or "unknown")
    return text, kind


def parse_conversations(chats_payload: Any, unread_payload: Any = None) -> list[dict]:
    """Normalize ``/v3/chats`` and ``get_unread`` payloads."""
    root = _dict(chats_payload)
    data = _dict(root.get("data"))
    chats = _list(data.get("chats"))

    unread_root = _dict(unread_payload)
    unread_data: Any = unread_root.get("data", unread_root)
    if isinstance(unread_data, dict):
        unread_map = (
            unread_data.get("unread")
            or unread_data.get("unread_map")
            or unread_data.get("result")
            or unread_data
        )
    else:
        unread_map = {}
    unread_map = unread_map if isinstance(unread_map, dict) else {}

    result: list[dict] = []
    seen: set[str] = set()
    for raw in chats:
        item = _dict(raw)
        peer_uid = str(item.get("chat_user_id") or item.get("peer_user_id") or "")
        conv_id = str(item.get("chat_id") or item.get("conversation_id") or peer_uid)
        if not conv_id or conv_id in seen:
            continue
        seen.add(conv_id)
        info = _dict(item.get("info"))
        last_text, _ = _message_text(item.get("last_msg_content"))
        unread = item.get("unread_count")
        if unread is None:
            unread = unread_map.get(peer_uid, unread_map.get(conv_id, 0))
        if isinstance(unread, dict):
            unread = unread.get("count") or unread.get("unread_count") or 0
        try:
            unread = max(0, int(unread or 0))
        except (TypeError, ValueError):
            unread = 0
        meta = {
            "self_uid": str(item.get("user_id") or ""),
            "last_sender_uid": str(item.get("last_sender_id") or ""),
            "start_store_id": int(item.get("start_store_id") or 0),
            "max_store_id": int(item.get("max_store_id") or 0),
            "chat_status": item.get("chat_status"),
            "group_chat": bool(item.get("group_chat") or item.get("is_group_chat")),
            "update_time": item.get("update_time"),
        }
        result.append({
            "conv_id": conv_id,
            "peer_uid": peer_uid,
            "peer_sec_uid": "",
            "peer_nickname": str(info.get("nickname") or info.get("name") or ""),
            "peer_avatar": str(info.get("avatar") or info.get("image") or ""),
            "last_text": last_text,
            "last_time": _seconds(item.get("last_msg_time") or item.get("update_time")),
            "unread_count": unread,
            "conv_short_id": "",
            "ticket": "",
            "raw_json": json.dumps(meta, ensure_ascii=False, separators=(",", ":")),
        })
    return result


def parse_history(payload: Any, *, peer_uid: str = "", self_uid: str = "") -> dict:
    """Normalize the Web history payload to CreatorHub's existing DM schema."""
    root = _dict(payload)
    data = _dict(root.get("data"))
    rows = _list(data.get("out_message_list") or data.get("message_list"))
    messages: list[dict] = []
    store_ids: list[int] = []
    for raw in rows:
        item = _dict(raw)
        sender = str(item.get("sender_id") or item.get("sender_uid") or "")
        receiver = str(item.get("receiver_id") or item.get("receiver_uid") or "")
        text, msg_type = _message_text(item.get("content"))
        store_id = int(item.get("store_id") or 0)
        if store_id:
            store_ids.append(store_id)
        msg_id = str(item.get("id") or item.get("uuid") or store_id or "")
        direction = "out" if self_uid and sender == self_uid else "in"
        if not self_uid and peer_uid:
            direction = "in" if sender == peer_uid else "out"
        messages.append({
            "server_msg_id": msg_id,
            "sender_uid": sender,
            "receiver_uid": receiver,
            "direction": direction,
            "text": text,
            "msg_type": msg_type,
            "create_time": _seconds(item.get("created_at") or item.get("create_time")),
            "store_id": store_id,
            "group_chat": bool(item.get("group_chat")),
            "content": _json(item.get("content")),
            "raw": item,
        })
    messages.sort(
        key=lambda row: (int(row.get("create_time") or 0), int(row.get("store_id") or 0))
    )
    return {
        "messages": messages,
        "next_cursor": min(store_ids) if store_ids else 0,
        "has_more": bool(rows) and len(rows) >= 50,
    }


def _path(response: Any) -> str:
    try:
        return str(urlsplit(str(response.url or "")).path or "").rstrip("/").lower()
    except Exception:
        return ""


def _query_param(response: Any, name: str) -> str:
    try:
        qs = parse_qs(urlsplit(str(response.url or "")).query)
        vals = qs.get(name) or []
        return str(vals[0] or "") if vals else ""
    except Exception:
        return ""


async def _safe_json(response: Any) -> Any:
    try:
        return await response.json()
    except Exception:
        return None


_READ_STATE_CONVS_JS = """() => {
    const unwrap = (v) => {
        let cur = v;
        for (let i = 0; i < 4 && cur && typeof cur === 'object'; i++) {
            if (cur._rawValue !== undefined) cur = cur._rawValue;
            else if (cur.value !== undefined && Object.keys(cur).length <= 3) cur = cur.value;
            else break;
        }
        return cur;
    };
    const st = window.__INITIAL_STATE__ || {};
    for (const key of ['im', 'chat', 'message', 'chatStore', 'imStore']) {
        const sub = unwrap(st[key]);
        if (!sub || typeof sub !== 'object') continue;
        for (const listKey of ['chats', 'chatList', 'conversations', 'sessionList']) {
            const arr = unwrap(sub[listKey]);
            if (Array.isArray(arr) && arr.length) {
                return { data: { chats: arr }, code: 0, success: true };
            }
        }
    }
    const domRows = [];
    const seen = new Set();
    const cards = document.querySelectorAll(
        '[data-chat-id],[data-user-id],a[href*="/chat/"],[class*="chat-item"],[class*="session-item"],[class*="conversation-item"]'
    );
    for (const el of cards) {
        let uid = el.getAttribute('data-user-id') || el.getAttribute('data-chat-id') || '';
        if (!uid) {
            const a = el.tagName === 'A' ? el : el.querySelector('a[href*="/chat/"]');
            const m = a && (a.getAttribute('href') || '').match(/\\/chat\\/([^/?#]+)/);
            if (m) uid = m[1];
        }
        if (!uid || seen.has(uid)) continue;
        seen.add(uid);
        const img = el.querySelector('img');
        const lines = (el.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
        const badgeEl = el.querySelector('[class*="badge"],[class*="unread"]');
        const unread = badgeEl ? (parseInt((badgeEl.innerText || '').trim(), 10) || 0) : 0;
        domRows.push({
            chat_id: uid,
            chat_user_id: uid,
            info: {
                nickname: lines[0] || uid,
                avatar: img ? (img.currentSrc || img.src || '') : '',
            },
            last_msg_content: lines.length > 1 ? lines[lines.length - 1] : '',
            unread_count: unread,
        });
    }
    if (domRows.length) {
        return { data: { chats: domRows }, code: 0, success: true };
    }
    return null;
}"""


_READ_DOM_HISTORY_JS = """(peerUid) => {
    const unwrap = (v) => {
        let cur = v;
        for (let i = 0; i < 4 && cur && typeof cur === 'object'; i++) {
            if (cur._rawValue !== undefined) cur = cur._rawValue;
            else if (cur.value !== undefined && Object.keys(cur).length <= 3) cur = cur.value;
            else break;
        }
        return cur;
    };
    const st = window.__INITIAL_STATE__ || {};
    for (const key of ['im', 'chat', 'message', 'chatStore', 'imStore']) {
        const sub = unwrap(st[key]);
        if (!sub || typeof sub !== 'object') continue;
        for (const msgKey of ['messages', 'messageList', 'currentMessages', 'out_message_list']) {
            const arr = unwrap(sub[msgKey]);
            if (Array.isArray(arr) && arr.length) {
                return { data: { message_list: arr }, code: 0, success: true };
            }
        }
    }
    const bubbles = document.querySelectorAll('.xhs-im-bubble__text,[class*="xhs-im-bubble__text"]');
    const list = [];
    bubbles.forEach((node, idx) => {
        const text = (node.textContent || '').trim();
        if (!text) return;
        const row = node.closest('[class*="self"],[class*="mine"],[class*="right"]');
        list.push({
            id: 'dom_' + idx,
            store_id: idx + 1,
            sender_id: row ? '' : String(peerUid || ''),
            content: { text, content_type: 'text' },
            created_at: 0,
        });
    });
    if (list.length) {
        return { data: { message_list: list }, code: 0, success: true };
    }
    return null;
}"""


_FIND_CHAT_SIDEBAR_BOX_JS = """() => {
    const sels = [
        '[class*="chat-list"]',
        '[class*="session-list"]',
        '[class*="conversation-list"]',
        'aside',
    ];
    for (const sel of sels) {
        for (const el of document.querySelectorAll(sel)) {
            const r = el.getBoundingClientRect();
            const s = getComputedStyle(el);
            if (
                r.width > 120 && r.height > 160 &&
                (s.overflowY === 'auto' || s.overflowY === 'scroll' || el.scrollHeight > el.clientHeight + 20)
            ) {
                return { x: r.x, y: r.y, width: r.width, height: r.height };
            }
        }
    }
    return null;
}"""


async def _scroll_chat_sidebar(page: Any) -> None:
    try:
        box = await page.evaluate(_FIND_CHAT_SIDEBAR_BOX_JS)
        if isinstance(box, dict) and box.get("width") and box.get("height"):
            tx, ty = DEFAULT_HUMAN_ENGINE.sample_box_point(box)
            await DEFAULT_HUMAN_ENGINE.move_mouse(page, tx, ty)
    except Exception:
        pass
    await human_scroll(page, distance=560)


async def _click_conversation_in_sidebar(
    page: Any, conv_id: str, peer_uid: str = ""
) -> bool:
    targets = [t for t in dict.fromkeys([conv_id, peer_uid]) if t]
    selectors: list[str] = []
    for target in targets:
        selectors.extend([
            f'[data-chat-id="{target}"]',
            f'[data-user-id="{target}"]',
            f'a[href*="/chat/{target}"]',
        ])
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                await DEFAULT_HUMAN_ENGINE.click_locator(loc, page=page, timeout=3000)
                return True
        except Exception:
            continue
    return False


async def fetch_conversations_in_page(
    page: Any, *, pages: int = 1
) -> tuple[list[dict], str]:
    """Passively read conversations from the signed-in `/chat` page without raw `window.fetch`."""
    cache = _page_cache(page)
    captured_chats: list[dict] = []
    unread_payload: Any = cache.get("unread")
    chat_event = asyncio.Event()

    async def on_response(response: Any) -> None:
        nonlocal unread_payload
        path = _path(response)
        if path == _CHATS_PATH and int(getattr(response, "status", 0) or 0) == 200:
            body = await _safe_json(response)
            if isinstance(body, dict):
                captured_chats.append(body)
                cache["chats"] = [body]
                chat_event.set()
        elif path == _UNREAD_PATH and int(getattr(response, "status", 0) or 0) == 200:
            body = await _safe_json(response)
            if isinstance(body, dict):
                unread_payload = body
                cache["unread"] = body

    page.on("response", on_response)
    try:
        cur_url = str(getattr(page, "url", "") or "")
        if "passport" in cur_url or "/login" in cur_url:
            return [], "logged_out:账号未登录"

        on_chat = "xiaohongshu.com/chat" in cur_url
        if not on_chat:
            await page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=30000)
            if "passport" in str(page.url or "") or "/login" in str(page.url or ""):
                return [], "logged_out:账号未登录"
            try:
                await asyncio.wait_for(chat_event.wait(), timeout=7.5)
            except asyncio.TimeoutError:
                pass
        else:
            # Wait briefly in case a native WebSocket push just triggered XHS's own signed XHR
            try:
                await asyncio.wait_for(chat_event.wait(), timeout=1.2)
            except asyncio.TimeoutError:
                pass

        total_pages = max(1, min(5, int(pages)))
        for _ in range(max(0, total_pages - 1)):
            chat_event.clear()
            await _scroll_chat_sidebar(page)
            try:
                await asyncio.wait_for(chat_event.wait(), timeout=2.5)
            except asyncio.TimeoutError:
                break

        payloads = list(captured_chats)
        if not payloads:
            try:
                state_payload = await page.evaluate(_READ_STATE_CONVS_JS)
                if isinstance(state_payload, dict):
                    payloads.append(state_payload)
            except Exception:
                pass
        if not payloads and cache.get("chats"):
            payloads = list(cache["chats"])
        if not payloads and on_chat:
            # Reload `/chat` once so XHS's first-party runtime issues signed `/v3/chats`
            chat_event.clear()
            await page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=30000)
            try:
                await asyncio.wait_for(chat_event.wait(), timeout=7.5)
            except asyncio.TimeoutError:
                pass
            payloads = list(captured_chats)

        combined: list[dict] = []
        seen: set[str] = set()
        for payload in payloads:
            if payload.get("success") is False or str(payload.get("code", "0")) not in {"0", ""}:
                return [], f"私信会话接口返回异常 code={payload.get('code')}"
            for item in parse_conversations(payload, unread_payload):
                key = str(item.get("conv_id") or "")
                if key and key not in seen:
                    seen.add(key)
                    combined.append(item)
        return combined, ""
    except Exception as exc:
        return [], f"小红书私信轻量同步失败: {exc!r}"
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass


async def fetch_history_in_page(
    page: Any,
    conv_id: str,
    *,
    peer_uid: str = "",
    self_uid: str = "",
    last_id: int = 0,
    start_id: int = 0,
    count: int = 50,
) -> tuple[dict, str]:
    """Passively capture one conversation's history via natural UI selection or route navigation."""
    _ = (last_id, start_id)
    chat_user_id = str(peer_uid or conv_id or "")
    if not chat_user_id:
        return {}, "缺会话 id"

    cache = _page_cache(page)
    capture: dict[str, Any] = {}
    event = asyncio.Event()

    async def on_response(response: Any) -> None:
        if _path(response) != _HISTORY_PATH or int(getattr(response, "status", 0) or 0) != 200:
            return
        q_uid = _query_param(response, "chat_user_id")
        if q_uid and q_uid not in {chat_user_id, str(conv_id or "")}:
            return
        body = await _safe_json(response)
        if isinstance(body, dict):
            capture["payload"] = body
            cache.setdefault("history", {})[chat_user_id] = body
            event.set()

    page.on("response", on_response)
    try:
        cur_url = str(getattr(page, "url", "") or "")
        clicked = False
        if "xiaohongshu.com/chat" in cur_url:
            clicked = await _click_conversation_in_sidebar(page, str(conv_id or ""), chat_user_id)
            if clicked:
                try:
                    await asyncio.wait_for(event.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass

        if "payload" not in capture:
            target_url = f"{CHAT_URL}/{quote(str(conv_id or chat_user_id), safe='')}"
            await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            if "passport" in str(page.url or "") or "/login" in str(page.url or ""):
                return {}, "logged_out:账号未登录"
            try:
                await asyncio.wait_for(event.wait(), timeout=8.0)
            except asyncio.TimeoutError:
                pass
            await human_wait(page, 420)

        payload = capture.get("payload")
        if payload is None:
            try:
                payload = await page.evaluate(_READ_DOM_HISTORY_JS, chat_user_id)
            except Exception:
                payload = None
        if payload is None:
            payload = cache.get("history", {}).get(chat_user_id)
        if payload is None:
            return {}, "未观察到小红书 Web 私信历史接口"

        root = _dict(payload)
        if root.get("success") is False or str(root.get("code", "0")) not in {"0", ""}:
            return {}, f"私信历史接口返回异常 code={root.get('code')}"
        parsed = parse_history(root, peer_uid=chat_user_id, self_uid=self_uid)
        parsed["has_more"] = len(parsed.get("messages", [])) >= max(1, int(count))
        return parsed, ""
    except Exception as exc:
        return {}, f"小红书私信历史轻量同步失败: {exc!r}"
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass


@asynccontextmanager
async def _background_page(mgr: Any, identity: Any):
    """Reuse the account-owned tab without bringing Chrome to the foreground."""
    try:
        lease = mgr.visible_page(identity, foreground=False)
    except TypeError:  # compatibility for small manager stubs
        lease = mgr.visible_page(identity)
    async with lease as page:
        yield page


async def fetch_conversations(
    mgr: Any,
    identity: Any,
    *,
    settle_ms: int = 2600,
    _visible: bool = False,
    _page: Any = None,
) -> tuple[list[dict], str]:
    if not _visible:
        async with _background_page(mgr, identity) as page:
            return await fetch_conversations(
                mgr, identity, settle_ms=settle_ms, _visible=True, _page=page
            )
    page = _page
    owns_page = page is None
    captures: dict[str, Any] = {}
    event = asyncio.Event()

    async def on_response(response: Any) -> None:
        path = _path(response)
        if path == _CHATS_PATH:
            body = await _safe_json(response)
            captures["chats"] = body
            if page is not None and isinstance(body, dict):
                _page_cache(page)["chats"] = [body]
            event.set()
        elif path == _UNREAD_PATH:
            body = await _safe_json(response)
            captures["unread"] = body
            if page is not None and isinstance(body, dict):
                _page_cache(page)["unread"] = body

    try:
        if page is None:
            page = await mgr.new_page(identity, block_media=False)
        page.on("response", on_response)
        await page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=30000)
        if "passport" in page.url or "/login" in page.url:
            return [], "logged_out:账号未登录"
        try:
            await asyncio.wait_for(
                event.wait(), timeout=max(6.0, settle_ms / 1000 + 4.0)
            )
        except asyncio.TimeoutError:
            pass
        await mgr.xhs_interaction.pause(0.35, 0.8)
        if "chats" not in captures:
            try:
                state_chats = await page.evaluate(_READ_STATE_CONVS_JS)
                if isinstance(state_chats, dict):
                    captures["chats"] = state_chats
            except Exception:
                pass
        if "chats" not in captures:
            return [], "未观察到小红书 Web 私信会话接口，页面可能改版或账号尚未开通"
        payload = _dict(captures.get("chats"))
        if payload.get("success") is False or str(payload.get("code", "0")) not in {"0", ""}:
            return [], f"私信会话接口返回异常 code={payload.get('code')}"
        return parse_conversations(payload, captures.get("unread")), ""
    except Exception as exc:
        return [], f"小红书私信同步失败: {exc!r}"
    finally:
        if page is not None:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass
            if owns_page:
                try:
                    await page.close()
                except Exception:
                    pass


async def fetch_history(
    mgr: Any,
    identity: Any,
    conv_id: str,
    *,
    peer_uid: str = "",
    self_uid: str = "",
    count: int = 50,
    _visible: bool = False,
    _page: Any = None,
) -> tuple[dict, str]:
    if not _visible:
        async with _background_page(mgr, identity) as page:
            return await fetch_history(
                mgr,
                identity,
                conv_id,
                peer_uid=peer_uid,
                self_uid=self_uid,
                count=count,
                _visible=True,
                _page=page,
            )
    if not conv_id:
        return {}, "缺会话 id"
    page = _page
    owns_page = page is None
    capture: dict[str, Any] = {}
    event = asyncio.Event()

    async def on_response(response: Any) -> None:
        if _path(response) == _HISTORY_PATH:
            body = await _safe_json(response)
            capture["payload"] = body
            if page is not None and isinstance(body, dict):
                _page_cache(page).setdefault("history", {})[str(peer_uid or conv_id)] = body
            event.set()

    try:
        if page is None:
            page = await mgr.new_page(identity, block_media=False)
        page.on("response", on_response)
        clicked = False
        if "xiaohongshu.com/chat" in str(getattr(page, "url", "") or ""):
            clicked = await _click_conversation_in_sidebar(
                page, str(conv_id), str(peer_uid or "")
            )
            if clicked:
                try:
                    await asyncio.wait_for(event.wait(), timeout=5.5)
                except asyncio.TimeoutError:
                    pass
        if "payload" not in capture:
            url = f"{CHAT_URL}/{quote(str(conv_id), safe='')}"
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if "passport" in page.url or "/login" in page.url:
                return {}, "logged_out:账号未登录"
            try:
                await asyncio.wait_for(event.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                pass
            await mgr.xhs_interaction.pause(0.3, 0.7)
        payload = capture.get("payload")
        if payload is None:
            try:
                payload = await page.evaluate(
                    _READ_DOM_HISTORY_JS, str(peer_uid or conv_id)
                )
            except Exception:
                payload = None
        if payload is None:
            return {}, "未观察到小红书 Web 私信历史接口"
        root = _dict(payload)
        if root.get("success") is False or str(root.get("code", "0")) not in {"0", ""}:
            return {}, f"私信历史接口返回异常 code={root.get('code')}"
        parsed = parse_history(payload, peer_uid=peer_uid, self_uid=self_uid)
        parsed["has_more"] = len(parsed.get("messages", [])) >= max(1, int(count))
        return parsed, ""
    except Exception as exc:
        return {}, f"小红书私信历史同步失败: {exc!r}"
    finally:
        if page is not None:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass
            if owns_page:
                try:
                    await page.close()
                except Exception:
                    pass
