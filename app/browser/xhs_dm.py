"""Exactly-once browser submission semantics for Xiaohongshu direct messages."""
from __future__ import annotations

import inspect
from typing import Any, Literal
from urllib.parse import urlsplit

from .xhs_selectors import find_visible, selector_diagnostic


_DM_WRITE_PATHS = (
    "/api/im/web/short_link/send_message",
    "/api/im/web/message/send",
    "/api/im/web/msg/send",
    "/api/im/web/chat/send",
    "/api/im/web/message/create",
    "/api/im/web/msg/create",
)


async def _mark_submit(on_submit: Any) -> None:
    if on_submit is None:
        return
    result = on_submit()
    if inspect.isawaitable(result):
        await result


class _SubmitLocator:
    """Record the durable boundary immediately before the sole click."""

    def __init__(self, locator: Any, submitted: dict, on_submit: Any):
        self._locator = locator
        self._submitted = submitted
        self._on_submit = on_submit

    def __getattr__(self, name: str):
        return getattr(self._locator, name)

    async def click(self, *args, **kwargs):
        await _mark_submit(self._on_submit)
        self._submitted["dispatched"] = True
        return await self._locator.click(*args, **kwargs)


def _is_xhs_dm_write_response(response: Any) -> bool:
    try:
        parsed = urlsplit(str(response.url or ""))
        host = str(parsed.hostname or "").lower()
        path = str(parsed.path or "").rstrip("/").lower()
        method = str(getattr(response.request, "method", "") or "").upper()
        status = int(response.status)
        return (
            (host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com"))
            and method == "POST"
            and 200 <= status < 300
            and path in _DM_WRITE_PATHS
        )
    except Exception:
        return False


def _dm_response_handler(evidence: dict, submitted: dict):
    def on_response(response):
        if submitted["dispatched"] and _is_xhs_dm_write_response(response):
            evidence["responses"].append(response)
    return on_response


def _payload_result(payload: Any) -> Literal["accepted", "rejected", "unknown"]:
    if not isinstance(payload, dict):
        return "unknown"
    root_success = payload.get("success")
    data = payload.get("data")
    data_success = data.get("success") if isinstance(data, dict) else None
    if root_success is False or data_success is False:
        return "rejected"
    code_results: list[bool] = []
    for key in ("code", "result_code"):
        if key not in payload:
            continue
        value = payload[key]
        accepted_code = not isinstance(value, bool) and (
            value == 0 or str(value).strip() == "0")
        code_results.append(accepted_code)
        if not accepted_code:
            return "rejected"
    if root_success is True or data_success is True or any(code_results):
        return "accepted"
    return "unknown"


async def _consume_responses(evidence: dict) -> bool:
    while evidence["responses"]:
        response = evidence["responses"].pop(0)
        try:
            result = _payload_result(await response.json())
        except Exception:
            result = "unknown"
        if result == "accepted":
            evidence["accepted"] = True
            return True
        if result == "rejected":
            evidence["business_rejected"] = True
    return bool(evidence["accepted"])


async def _exact_text_count(page: Any, text: str) -> int | None:
    try:
        return max(0, int(await page.get_by_text(text, exact=True).count()))
    except Exception:
        return None


async def _sent_bubble_count(page: Any, text: str) -> int | None:
    """Count rendered message bubbles without counting the editor itself."""
    try:
        return max(0, int(await page.evaluate(
            """value => [...document.querySelectorAll(
              '.xhs-im-bubble__text,[class*="xhs-im-bubble__text"]')]
              .filter(node => (node.textContent || '').trim() === value).length""",
            text)))
    except Exception:
        return None


async def _wait_visible(
        page: Any, interaction: Any, group: str, *,
        require_enabled: bool = False,
        attempts: int = 8) -> tuple[Any | None, str]:
    total_attempts = max(1, int(attempts))
    for attempt in range(total_attempts):
        found = await find_visible(
            page, group, require_enabled=require_enabled)
        if found[0] is not None:
            return found
        if attempt + 1 < total_attempts:
            await interaction.pause(0.15, 0.35)
    return None, ""


async def send_xhs_dm_page(
        mgr: Any, page: Any, text: str, *,
        on_submit: Any = None,
        timeout_seconds: int = 15) -> tuple[bool, str]:
    """Send one visible DM once and require explicit post-submit evidence."""
    text = str(text or "").strip()
    if not text:
        return False, "空内容"

    interaction = mgr.xhs_interaction
    submitted = {"dispatched": False}
    evidence = {
        "accepted": False,
        "business_rejected": False,
        "responses": [],
    }
    on_response = _dm_response_handler(evidence, submitted)
    listener_installed = False

    try:
        editor, _ = await _wait_visible(
            page, interaction, "dm.editor", attempts=5)
        # Direct /chat/{uid} routes already contain the editor.  Profile routes
        # still use the legacy entry button, preserving new-conversation support.
        if editor is None:
            entry, _ = await _wait_visible(
                page, interaction, "dm.entry", attempts=8)
            if entry is None:
                diagnostic = await selector_diagnostic(page, "dm.entry")
                return False, f"未找到私信入口(页面可能改版)；{diagnostic}"
            try:
                await interaction.click_visible(entry)
                await interaction.pause(0.25, 0.55)
            except Exception as exc:
                return False, f"打开私信入口失败: {exc!r}"
            editor, _ = await _wait_visible(
                page, interaction, "dm.editor", attempts=8)
        if editor is None:
            diagnostic = await selector_diagnostic(page, "dm.editor")
            return False, f"未找到私信输入框(页面可能改版)；{diagnostic}"

        try:
            if len(text) <= 40:
                await interaction.type_short(editor, text)
            else:
                await interaction.insert_long(editor, text, page=page)
        except Exception as exc:
            return False, f"私信内容输入失败: {exc!r}"
        existing_matches = await _exact_text_count(page, text)
        existing_bubbles = await _sent_bubble_count(page, text)
        await interaction.pause(0.7, 1.6)

        submit, _ = await _wait_visible(
            page, interaction, "dm.submit",
            require_enabled=True, attempts=4)
        try:
            page.on("response", on_response)
            listener_installed = True
        except Exception:
            pass

        try:
            if submit is not None:
                await interaction.click_visible(
                    _SubmitLocator(submit, submitted, on_submit))
            else:
                await _mark_submit(on_submit)
                submitted["dispatched"] = True
                await editor.press("Enter")
        except Exception as exc:
            if submitted["dispatched"]:
                return False, f"write_uncertain:私信已提交但页面连接中断: {exc!r}"
            return False, f"私信提交前失败: {exc!r}"

        attempts = max(2, min(60, max(1, int(timeout_seconds)) * 4))
        for _ in range(attempts):
            if await _consume_responses(evidence):
                return True, ""
            current_matches = await _exact_text_count(page, text)
            current_bubbles = await _sent_bubble_count(page, text)
            if existing_bubbles is not None and current_bubbles is not None \
                    and current_bubbles > existing_bubbles:
                return True, ""
            if existing_matches is not None and current_matches is not None \
                    and current_matches > existing_matches:
                return True, ""
            try:
                if page.is_closed():
                    break
            except Exception:
                break
            await interaction.pause(0.2, 0.45)

        detail = (
            "平台返回了业务拒绝结果，但提交状态仍需核对"
            if evidence["business_rejected"]
            else "私信已提交一次，但未取得明确成功证据"
        )
        return False, f"write_uncertain:{detail}，请到平台核对"
    except Exception as exc:
        if submitted["dispatched"]:
            return False, f"write_uncertain:私信提交后页面异常: {exc!r}"
        return False, f"私信页面异常: {exc!r}"
    finally:
        if listener_installed:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass
