"""Visible, page-native Xiaohongshu writes with explicit ambiguity semantics."""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Sequence
from urllib.parse import urlencode

from ...browser.identity import Identity
from ...browser.manager import BrowserManager
from ...browser.xhs_selectors import (
    candidate_locator,
    find_present,
    find_visible,
    selector_candidates,
    selector_diagnostic,
)


PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish?source=official"
_SUCCESS_TEXTS = ("发布成功", "发布完成")


@dataclass(frozen=True)
class XhsWriteOutcome:
    status: Literal["success", "failed", "uncertain"]
    result: str = ""
    error: str = ""
    method: str = "browser"

    @property
    def ok(self) -> bool:
        return self.status == "success"

    def legacy(self) -> tuple[bool, str, str]:
        if self.status == "success":
            return True, self.result, ""
        if self.status == "uncertain":
            detail = self.error or "页面提交后未取得明确成功证据，请先到平台核对"
            return False, "", f"write_uncertain:{detail}"
        return False, "", self.error or "页面操作失败"


async def _wait_until_enabled(locator: Any, interaction: Any,
                              *, attempts: int = 80) -> bool:
    for _ in range(max(1, attempts)):
        try:
            if await locator.count() and await locator.is_visible() \
                    and await locator.is_enabled():
                return True
        except Exception:
            pass
        await interaction.pause(0.2, 0.45)
    return False


async def _upload_is_busy(page: Any) -> bool:
    for candidate in selector_candidates("publish.progress"):
        try:
            locator = candidate_locator(page, candidate)
            if await locator.count() and await locator.is_visible():
                return True
        except Exception:
            continue
    return False


async def _wait_upload_complete(page: Any, interaction: Any,
                                *, attempts: int = 120) -> bool:
    stable = 0
    for _ in range(max(1, attempts)):
        if not await _upload_is_busy(page):
            stable += 1
            if stable >= 2:
                return True
        else:
            stable = 0
        await interaction.pause(0.25, 0.55)
    return False


class _SubmitLocator:
    """Mark the exact boundary where Patchright dispatches the sole submit click."""

    def __init__(self, locator: Any, state: dict, on_submit: Any = None):
        self._locator = locator
        self._state = state
        self._on_submit = on_submit

    def __getattr__(self, name: str):
        return getattr(self._locator, name)

    async def click(self, *args, **kwargs):
        if self._on_submit is not None:
            result = self._on_submit()
            if inspect.isawaitable(result):
                await result
        self._state["clicked"] = True
        return await self._locator.click(*args, **kwargs)


def _publish_response_handler(evidence: dict, submitted: dict):
    def on_response(response):
        try:
            url = str(response.url or "")
            request = response.request
            method = str(getattr(request, "method", "") or "").upper()
            status = int(response.status)
            if submitted["clicked"] and method == "POST" \
                    and 200 <= status < 300 and any(
                    marker in url.lower() for marker in (
                        "/publish", "/note/create", "/web_api/sns/v2/note")):
                evidence["responses"].append(response)
        except Exception:
            return
    return on_response


def _publish_payload_accepted(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("success") is False:
        return False
    if payload.get("success") is True:
        return True
    for key in ("code", "result_code"):
        if key not in payload:
            continue
        value = payload[key]
        if value == 0 or str(value).strip().lower() in {"0", "ok", "success"}:
            return True
    data = payload.get("data")
    return bool(isinstance(data, dict) and data.get("success") is True)


async def _consume_publish_responses(evidence: dict) -> bool:
    while evidence["responses"]:
        response = evidence["responses"].pop(0)
        try:
            payload = await response.json()
        except Exception:
            continue
        if _publish_payload_accepted(payload):
            evidence["accepted"] = True
            evidence["url"] = str(getattr(response, "url", "") or "")
            return True
        evidence["business_rejected"] = True
    return bool(evidence["accepted"])


async def _visible_success(page: Any) -> bool:
    if any(marker in str(getattr(page, "url", "")).lower()
           for marker in ("/publish/success", "publish_success")):
        return True
    for text in _SUCCESS_TEXTS:
        try:
            locator = page.get_by_text(text, exact=False).first
            if await locator.count() and await locator.is_visible():
                return True
        except Exception:
            continue
    return False


async def publish_xhs_browser(
        mgr: BrowserManager, identity: Identity, media_type: str,
        title: str, desc: str, topics: Sequence[str], files: Sequence[str],
        *, timeout_seconds: int = 180,
        on_submit: Any = None) -> XhsWriteOutcome:
    """Submit one note once; never retry or switch transport after submission."""
    paths = [str(Path(path)) for path in files if path and Path(path).exists()]
    if not paths:
        return XhsWriteOutcome("failed", error="没有可用的本地媒体文件(路径不存在)")
    if media_type == "video":
        paths = paths[:1]
    else:
        paths = paths[:18]
    title = (title or "").strip()[:20]
    tags = [str(topic).strip().lstrip("#") for topic in topics if str(topic).strip()]
    body = ((desc or "") + (
        "\n" + " ".join(f"#{tag}" for tag in tags) if tags else ""
    )).strip()[:1000]
    interaction = mgr.xhs_interaction
    submitted = {"clicked": False}
    evidence = {
        "accepted": False,
        "url": "",
        "responses": [],
        "business_rejected": False,
    }
    on_response = _publish_response_handler(evidence, submitted)

    try:
        async with mgr.visible_page(identity, url=PUBLISH_URL) as page:
            if "login" in page.url or "passport" in page.url:
                return XhsWriteOutcome("failed", error="logged_out:创作平台未登录")

            tab, _ = await find_visible(
                page,
                "publish.kind.video" if media_type == "video"
                else "publish.kind.image",
            )
            if tab is not None:
                await interaction.click_visible(tab)
                await interaction.pause(0.2, 0.45)

            file_input, _ = await find_present(page, "publish.file")
            if file_input is None:
                diagnostic = await selector_diagnostic(page, "publish.file")
                return XhsWriteOutcome(
                    "failed", error=f"未找到媒体上传控件(发布页可能改版)；{diagnostic}")
            await file_input.set_input_files(paths, timeout=15_000)
            if not await _wait_upload_complete(page, interaction):
                return XhsWriteOutcome("failed", error="媒体上传在限定时间内未完成")

            if title:
                title_input, _ = await find_visible(page, "publish.title")
                if title_input is None:
                    diagnostic = await selector_diagnostic(
                        page, "publish.title")
                    return XhsWriteOutcome(
                        "failed", error=f"未找到标题输入框(发布页可能改版)；{diagnostic}")
                await interaction.type_short(title_input, title)
            if body:
                body_input, _ = await find_visible(page, "publish.body")
                if body_input is None:
                    diagnostic = await selector_diagnostic(
                        page, "publish.body")
                    return XhsWriteOutcome(
                        "failed", error=f"未找到正文输入框(发布页可能改版)；{diagnostic}")
                await interaction.insert_long(body_input, body, page=page)

            publish_button, _ = await find_visible(page, "publish.submit")
            if publish_button is None:
                diagnostic = await selector_diagnostic(page, "publish.submit")
                return XhsWriteOutcome(
                    "failed", error=f"未找到发布按钮(发布页可能改版)；{diagnostic}")
            if not await _wait_until_enabled(
                    publish_button, interaction,
                    attempts=max(4, min(120, timeout_seconds * 2))):
                return XhsWriteOutcome("failed", error="发布按钮一直不可用，请检查页面提示")

            try:
                page.on("response", on_response)
            except Exception:
                pass
            try:
                # This wrapper records the precise click-dispatch boundary.  There
                # is intentionally no second click and no API fallback below it.
                await interaction.click_visible(
                    _SubmitLocator(publish_button, submitted, on_submit))
            except Exception as exc:
                if submitted["clicked"]:
                    return XhsWriteOutcome(
                        "uncertain", error=f"发布已提交但连接中断: {exc!r}")
                return XhsWriteOutcome("failed", error=f"发布按钮点击失败: {exc!r}")

            attempts = max(4, min(80, int(timeout_seconds * 2)))
            for _ in range(attempts):
                if await _consume_publish_responses(evidence) \
                        or await _visible_success(page):
                    result = str(getattr(page, "url", "") or evidence["url"])
                    return XhsWriteOutcome("success", result=result)
                try:
                    if page.is_closed():
                        break
                except Exception:
                    break
                await interaction.pause(0.25, 0.55)
            detail = (
                "平台返回了业务拒绝结果，但提交状态仍需核对"
                if evidence["business_rejected"]
                else "发布按钮已点击一次，但未取得明确成功证据"
            )
            return XhsWriteOutcome(
                "uncertain", error=f"{detail}，请到平台核对")
    except Exception as exc:
        status = "uncertain" if submitted["clicked"] else "failed"
        prefix = "发布已提交后页面异常" if submitted["clicked"] else "发布页面异常"
        return XhsWriteOutcome(status, error=f"{prefix}: {exc!r}")
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass


async def _locator_visible(locator: Any) -> bool:
    try:
        return bool(await locator.count() and await locator.is_visible())
    except Exception:
        return False


async def _find_reply_target(page: Any, comment_id: str, target_text: str):
    if comment_id:
        safe_id = str(comment_id).replace('"', '\\"')
        locator = page.locator(f'[data-comment-id="{safe_id}"]').first
        if await _locator_visible(locator):
            return locator
    if target_text:
        locator = page.get_by_text(target_text, exact=True).first
        if await _locator_visible(locator):
            return locator
    return None


async def _find_comment_input(page: Any):
    locator, _ = await find_visible(page, "comment.editor")
    return locator


def _comment_response_handler(evidence: dict):
    def on_response(response):
        try:
            url = str(response.url or "").lower()
            request = response.request
            method = str(getattr(request, "method", "") or "").upper()
            status = int(response.status)
            if method == "POST" and 200 <= status < 300 and any(
                    marker in url for marker in (
                        "/comment/post", "/comment/create", "/comment/add")):
                evidence["accepted"] = True
        except Exception:
            return
    return on_response


async def comment_xhs_browser(
        mgr: BrowserManager, identity: Identity, note_id: str, xsec_token: str,
        content: str, *, target_comment_id: str = "",
        target_text: str = "", max_scrolls: int = 16,
        timeout_seconds: int = 30,
        on_submit: Any = None) -> XhsWriteOutcome:
    """Post one visible comment/reply once, preserving an uncertain result."""
    note_id = str(note_id or "").strip()
    content = str(content or "").strip()
    if not note_id:
        return XhsWriteOutcome("failed", error="缺少目标笔记 ID")
    if not content:
        return XhsWriteOutcome("failed", error="评论内容为空")
    query = urlencode({
        "xsec_token": xsec_token or "", "xsec_source": "pc_comment"})
    url = f"https://www.xiaohongshu.com/explore/{note_id}"
    if xsec_token:
        url += f"?{query}"
    interaction = mgr.xhs_interaction
    submitted = {"clicked": False}
    evidence = {"accepted": False}
    on_response = _comment_response_handler(evidence)
    page = None

    try:
        async with mgr.visible_page(identity, url=url) as page:
            if "login" in page.url or "passport" in page.url:
                return XhsWriteOutcome("failed", error="logged_out:账号未登录")

            if target_comment_id:
                target = await _find_reply_target(
                    page, target_comment_id, target_text)
                for _ in range(max(0, int(max_scrolls))):
                    if target is not None:
                        break
                    await interaction.scroll_step(page)
                    target = await _find_reply_target(
                        page, target_comment_id, target_text)
                if target is None:
                    return XhsWriteOutcome(
                        "failed", error="未找到目标评论，已停止且不会降级为顶层评论")

                reply = None
                try:
                    reply = target.get_by_text("回复", exact=True).first
                    if not await _locator_visible(reply):
                        reply = None
                except Exception:
                    reply = None
                if reply is None:
                    try:
                        reply = target.locator(
                            'button:has-text("回复"),[class*="reply"]').first
                        if not await _locator_visible(reply):
                            reply = None
                    except Exception:
                        reply = None
                if reply is None:
                    return XhsWriteOutcome(
                        "failed", error="已找到目标评论，但未找到该评论的回复入口")
                await interaction.click_visible(reply)
                await interaction.pause(0.2, 0.45)

            editor = await _find_comment_input(page)
            for _ in range(max(0, int(max_scrolls))):
                if editor is not None:
                    break
                await interaction.scroll_step(page)
                editor = await _find_comment_input(page)
            if editor is None:
                diagnostic = await selector_diagnostic(page, "comment.editor")
                return XhsWriteOutcome(
                    "failed", error=f"未找到评论输入框(页面可能改版)；{diagnostic}")
            if len(content) <= 80:
                await interaction.type_short(editor, content)
            else:
                await interaction.insert_long(editor, content, page=page)

            send, _ = await find_visible(page, "comment.submit")
            if send is None:
                diagnostic = await selector_diagnostic(page, "comment.submit")
                return XhsWriteOutcome(
                    "failed", error=f"未找到评论发送按钮(页面可能改版)；{diagnostic}")
            if not await _wait_until_enabled(
                    send, interaction,
                    attempts=max(4, min(60, timeout_seconds * 2))):
                return XhsWriteOutcome("failed", error="评论发送按钮当前不可用")
            existing_matches = 0
            try:
                existing_matches = int(await page.get_by_text(
                    content, exact=True).count())
            except Exception:
                pass
            try:
                page.on("response", on_response)
            except Exception:
                pass
            try:
                await interaction.click_visible(
                    _SubmitLocator(send, submitted, on_submit))
            except Exception as exc:
                if submitted["clicked"]:
                    return XhsWriteOutcome(
                        "uncertain", error=f"评论已提交但连接中断: {exc!r}")
                return XhsWriteOutcome("failed", error=f"评论发送失败: {exc!r}")

            for _ in range(max(4, min(60, timeout_seconds * 2))):
                own_text_added = False
                try:
                    own_text = page.get_by_text(content, exact=True)
                    own_text_added = int(await own_text.count()) > existing_matches
                except Exception:
                    pass
                if own_text_added:
                    return XhsWriteOutcome("success", result="ok")
                try:
                    if page.is_closed():
                        break
                except Exception:
                    break
                await interaction.pause(0.2, 0.45)
            detail = ("接口已接受但页面未显示新评论" if evidence["accepted"]
                      else "评论按钮已点击一次，但未取得明确成功证据")
            return XhsWriteOutcome(
                "uncertain", error=f"{detail}，请到平台核对")
    except Exception as exc:
        status = "uncertain" if submitted["clicked"] else "failed"
        prefix = "评论提交后页面异常" if submitted["clicked"] else "评论页面异常"
        return XhsWriteOutcome(status, error=f"{prefix}: {exc!r}")
    finally:
        if page is not None:
            try:
                page.remove_listener("response", on_response)
            except Exception:
                pass
