"""Bounded comment traversal, adapted from Spider_XHS's root/reply split.

Only the explicit API path uses this module. It never changes login state,
switches transports or retries a rejected request. Both levels consume the
same request budget and expose incomplete traversal through ``has_more``.
"""
from __future__ import annotations

import asyncio

from .client import XhsApiError


def _rows(page: dict) -> list[dict]:
    if not isinstance(page, dict):
        raise XhsApiError(
            "评论分页响应结构异常", category="risk", signal="ambiguous_response")
    rows = page.get("comments")
    if rows is None:
        return []
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise XhsApiError(
            "评论列表响应结构异常", category="risk", signal="ambiguous_response")
    return rows


def _more(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        if value.lower() in {"1", "true"}:
            return True
        if value.lower() in {"0", "false"}:
            return False
    return None


def _id(row: dict) -> str:
    value = row.get("id") or row.get("comment_id")
    if isinstance(value, str) and value.strip() == value:
        return value
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return str(value)
    return ""


def _count(value) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 20:
        return int(value)
    return None


async def collect_note_comments(client, note_id: str, xsec_token: str = "", *,
                                xsec_source: str = "pc_feed",
                                max_comments: int = 200, max_requests: int = 20,
                                include_replies: bool = True,
                                request_interval: float = 0.5) -> dict:
    limit = max(0, min(2000, int(max_comments)))
    budget = max(0, min(100, int(max_requests)))
    interval = max(0.0, min(60.0, float(request_interval)))
    comments: list[dict] = []
    seen: set[str] = set()
    reply_ids: dict[str, set[str]] = {}
    expected_replies: dict[str, int] = {}
    incomplete = False
    requests = 0

    def append(row: dict, parent: str = "") -> bool:
        nonlocal incomplete
        cid = _id(row)
        if not cid:
            incomplete = True
            return False
        if cid in seen:
            return False
        if len(comments) >= limit:
            incomplete = True
            return False
        value = dict(row)
        value.pop("sub_comments", None)
        value.pop("subComments", None)
        if parent:
            target = value.get("target_comment") or {}
            if not isinstance(target, dict):
                raise XhsApiError(
                    "回复目标响应结构异常", category="risk", signal="ambiguous_response")
            if not (target.get("id") or target.get("comment_id")):
                value["target_comment"] = {**target, "id": parent}
            reply_ids.setdefault(parent, set()).add(cid)
        comments.append(value)
        seen.add(cid)
        return True

    async def read(method, *args, **kwargs):
        nonlocal requests
        if requests and interval:
            await asyncio.sleep(interval)
        requests += 1
        return await method(*args, **kwargs)

    if not limit or not budget:
        return {"comments": [], "has_more": True}
    async with client.session_scope():
        cursor = ""
        root_cursors = {cursor}
        while True:
            if requests >= budget or len(comments) >= limit:
                incomplete = True
                break
            page = await read(
                client.note_comments, note_id, xsec_token=xsec_token,
                cursor=cursor, xsec_source=xsec_source)
            roots = _rows(page)
            before_roots = len(seen)
            for root in roots:
                if len(comments) >= limit:
                    incomplete = True
                    break
                parent = _id(root)
                if not parent:
                    incomplete = True
                    continue
                append(root)
                if not include_replies:
                    continue
                embedded = root.get("sub_comments", root.get("subComments"))
                for reply in _rows({"comments": embedded}):
                    append(reply, parent)
                has_replies = _more(root.get("sub_comment_has_more"))
                expected = _count(root.get("sub_comment_count"))
                if expected is not None:
                    expected_replies[parent] = max(expected_replies.get(parent, 0), expected)
                if "sub_comment_count" in root and expected is None:
                    incomplete = True
                if has_replies is None and "sub_comment_has_more" in root:
                    incomplete = True
                if has_replies is None and expected is None and embedded:
                    incomplete = True
                reply_cursor = str(root.get("sub_comment_cursor") or "")
                reply_cursors = {reply_cursor}
                while has_replies is True:
                    if requests >= budget or len(comments) >= limit:
                        incomplete = True
                        break
                    before_replies = len(seen)
                    replies = await read(
                        client.note_sub_comments, note_id, parent,
                        xsec_token=xsec_token, cursor=reply_cursor,
                        page_size=min(10, limit - len(comments)))
                    for reply in _rows(replies):
                        append(reply, parent)
                    has_replies = _more(replies.get("has_more"))
                    if has_replies is not True:
                        incomplete = incomplete or has_replies is None
                        break
                    next_cursor = str(replies.get("cursor") or "")
                    if (not next_cursor or next_cursor in reply_cursors
                            or len(seen) == before_replies):
                        incomplete = True
                        break
                    reply_cursors.add(next_cursor)
                    reply_cursor = next_cursor
            has_roots = _more(page.get("has_more"))
            if has_roots is not True:
                incomplete = incomplete or has_roots is None
                break
            next_cursor = str(page.get("cursor") or "")
            if (not next_cursor or next_cursor in root_cursors
                    or len(seen) == before_roots):
                incomplete = True
                break
            root_cursors.add(next_cursor)
            cursor = next_cursor
    # Repeated root pages may add more replies later. Compare totals only after
    # traversal, using unique retained IDs instead of embedded-list lengths.
    incomplete = incomplete or any(
        expected > len(reply_ids.get(parent, set()))
        for parent, expected in expected_replies.items())
    return {"comments": comments, "has_more": incomplete}
