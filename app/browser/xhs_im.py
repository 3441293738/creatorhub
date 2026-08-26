"""Xiaohongshu Web private-message read adapter.

The adapter deliberately uses the signed-in, visible account browser and lets
the first-party Web client issue its normal requests.  It only observes the
documented-in-code response shapes; it does not synthesize request signatures.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote, urlsplit


CHAT_URL = "https://www.xiaohongshu.com/chat"
_CHATS_PATH = "/api/im/web/v3/chats"
_UNREAD_PATH = "/api/im/web/chat/get_unread"
_HISTORY_PATH = "/api/im/web/messages/history"
_API_ORIGIN = "https://edith.xiaohongshu.com"


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
        unread_map = unread_data.get("unread") or unread_data.get("unread_map") \
            or unread_data.get("result") or unread_data
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
    messages.sort(key=lambda row: (int(row.get("create_time") or 0),
                                   int(row.get("store_id") or 0)))
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


async def _safe_json(response: Any) -> Any:
    try:
        return await response.json()
    except Exception:
        return None


async def _page_json(page: Any, url: str) -> Any:
    """Issue a normal first-party fetch inside the already signed-in page.

    This avoids reloading ``/chat`` (and reconnecting its native WebSocket) for
    every push. No request signature or second socket is synthesized.
    """
    return await page.evaluate(
        """async (url) => {
            const response = await fetch(url, {
                method: 'GET', credentials: 'include', cache: 'no-store'
            });
            let body = null;
            try { body = await response.json(); } catch (_) {}
            return {status: response.status, body};
        }""",
        url,
    )


async def fetch_conversations_in_page(page: Any, *, pages: int = 1
                                      ) -> tuple[list[dict], str]:
    """Fetch the account-wide conversation frontier without page navigation."""
    payloads: list[dict] = []
    unread_payload: Any = None
    try:
        unread_result = await _page_json(
            page, f"{_API_ORIGIN}{_UNREAD_PATH}")
        if int(_dict(unread_result).get("status") or 0) == 200:
            unread_payload = _dict(unread_result).get("body")
        for number in range(max(1, min(5, int(pages)))):
            url = (f"{_API_ORIGIN}{_CHATS_PATH}?limit=100&complete=true"
                   f"&page={number}&source=pc")
            result = _dict(await _page_json(page, url))
            if int(result.get("status") or 0) != 200:
                return [], f"私信会话接口 HTTP {result.get('status')}"
            payload = _dict(result.get("body"))
            if payload.get("success") is False or str(payload.get("code", "0")) not in {"0", ""}:
                return [], f"私信会话接口返回异常 code={payload.get('code')}"
            payloads.append(payload)
            rows = _list(_dict(payload.get("data")).get("chats"))
            if not rows or len(rows) < 100:
                break
        combined: list[dict] = []
        seen: set[str] = set()
        for payload in payloads:
            for item in parse_conversations(payload, unread_payload):
                key = str(item.get("conv_id") or "")
                if key and key not in seen:
                    seen.add(key)
                    combined.append(item)
        return combined, ""
    except Exception as exc:
        return [], f"小红书私信轻量同步失败: {exc!r}"


async def fetch_history_in_page(page: Any, conv_id: str, *, peer_uid: str = "",
                                self_uid: str = "", last_id: int = 0,
                                start_id: int = 0, count: int = 50
                                ) -> tuple[dict, str]:
    """Read one changed conversation without navigating or marking it read."""
    chat_user_id = str(peer_uid or conv_id or "")
    if not chat_user_id:
        return {}, "缺会话 id"
    url = (f"{_API_ORIGIN}{_HISTORY_PATH}?chat_user_id="
           f"{quote(chat_user_id, safe='')}&last_id={max(0, int(last_id))}"
           f"&start_id={max(0, int(start_id))}&limit="
           f"{max(1, min(100, int(count)))}")
    try:
        result = _dict(await _page_json(page, url))
        if int(result.get("status") or 0) != 200:
            return {}, f"私信历史接口 HTTP {result.get('status')}"
        payload = _dict(result.get("body"))
        if payload.get("success") is False or str(payload.get("code", "0")) not in {"0", ""}:
            return {}, f"私信历史接口返回异常 code={payload.get('code')}"
        parsed = parse_history(payload, peer_uid=chat_user_id, self_uid=self_uid)
        parsed["has_more"] = len(parsed.get("messages", [])) >= max(1, int(count))
        return parsed, ""
    except Exception as exc:
        return {}, f"小红书私信历史轻量同步失败: {exc!r}"


@asynccontextmanager
async def _background_page(mgr: Any, identity: Any):
    """Reuse the account-owned tab without bringing Chrome to the foreground."""
    try:
        lease = mgr.visible_page(identity, foreground=False)
    except TypeError:  # compatibility for small test/third-party manager stubs
        lease = mgr.visible_page(identity)
    async with lease as page:
        yield page


async def fetch_conversations(mgr: Any, identity: Any, *, settle_ms: int = 2600,
                              _visible: bool = False, _page: Any = None
                              ) -> tuple[list[dict], str]:
    if not _visible:
        async with _background_page(mgr, identity) as page:
            return await fetch_conversations(
                mgr, identity, settle_ms=settle_ms, _visible=True, _page=page)
    page = _page
    owns_page = page is None
    captures: dict[str, Any] = {}
    event = asyncio.Event()

    async def on_response(response: Any) -> None:
        path = _path(response)
        if path == _CHATS_PATH:
            captures["chats"] = await _safe_json(response)
            event.set()
        elif path == _UNREAD_PATH:
            captures["unread"] = await _safe_json(response)

    try:
        if page is None:
            page = await mgr.new_page(identity, block_media=False)
        page.on("response", on_response)
        await page.goto(CHAT_URL, wait_until="domcontentloaded", timeout=30000)
        if "passport" in page.url or "/login" in page.url:
            return [], "logged_out:账号未登录"
        try:
            await asyncio.wait_for(event.wait(), timeout=max(6.0, settle_ms / 1000 + 4.0))
        except asyncio.TimeoutError:
            pass
        await mgr.xhs_interaction.pause(0.35, 0.8)
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


async def fetch_history(mgr: Any, identity: Any, conv_id: str, *,
                        peer_uid: str = "", self_uid: str = "", count: int = 50,
                        _visible: bool = False, _page: Any = None
                        ) -> tuple[dict, str]:
    if not _visible:
        async with _background_page(mgr, identity) as page:
            return await fetch_history(
                mgr, identity, conv_id, peer_uid=peer_uid, self_uid=self_uid,
                count=count, _visible=True, _page=page)
    if not conv_id:
        return {}, "缺会话 id"
    page = _page
    owns_page = page is None
    capture: dict[str, Any] = {}
    event = asyncio.Event()

    async def on_response(response: Any) -> None:
        if _path(response) == _HISTORY_PATH:
            capture["payload"] = await _safe_json(response)
            event.set()

    try:
        if page is None:
            page = await mgr.new_page(identity, block_media=False)
        page.on("response", on_response)
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
