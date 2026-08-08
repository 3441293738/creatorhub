"""Reviewed Xiaohongshu selector catalog with secret-free diagnostics."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Mapping
from urllib.parse import urlsplit


@dataclass(frozen=True)
class SelectorCandidate:
    name: str
    kind: Literal["css", "text"]
    value: str
    exact: bool = False


def _css(name: str, value: str) -> SelectorCandidate:
    return SelectorCandidate(name=name, kind="css", value=value)


def _text(name: str, value: str, *, exact: bool = False) -> SelectorCandidate:
    return SelectorCandidate(
        name=name, kind="text", value=value, exact=exact)


SELECTOR_GROUPS: Mapping[str, tuple[SelectorCandidate, ...]] = MappingProxyType({
    "publish.kind.image": (
        _text("image_upload_text", "上传图文"),
        _css("image_tab_role", '[role="tab"]:has-text("图文")'),
        _text("image_text", "图文"),
    ),
    "publish.kind.video": (
        _text("video_upload_text", "上传视频"),
        _css("video_tab_role", '[role="tab"]:has-text("视频")'),
        _text("video_text", "视频"),
    ),
    "publish.file": (
        _css("publish_file_input", 'input[type="file"]'),
    ),
    "publish.title": (
        _css("title_placeholder", 'input[placeholder*="标题"]'),
        _css("title_d_text_input", ".d-text input"),
        _css("title_c_input_inner", "input.c-input_inner"),
    ),
    "publish.body": (
        _css("body_ql_editor", ".ql-editor"),
        _css("body_post_textarea", "#post-textarea"),
        _css("body_contenteditable", '[contenteditable="true"]'),
        _css("body_textarea", "textarea"),
    ),
    "publish.submit": (
        _css("publish_button", 'button:has-text("发布")'),
        _css("publish_submit_button", "div.submit button"),
        _text("publish_note_text", "发布笔记"),
    ),
    "publish.progress": (
        _css(
            "upload_progress_class",
            '[class*="upload"][class*="progress"]',
        ),
        _css("upload_progress_role", '[role="progressbar"]'),
        _text("upload_in_progress_text", "上传中"),
        _text("upload_processing_text", "处理中"),
    ),
    "comment.editor": (
        _css(
            "comment_editor_contenteditable",
            '[contenteditable="true"][placeholder*="评论"]',
        ),
        _css("comment_editor_textarea", 'textarea[placeholder*="评论"]'),
        _css(
            "comment_input_contenteditable",
            '[class*="comment-input"] [contenteditable="true"]',
        ),
        _css("comment_textarea", '[class*="comment"] textarea'),
    ),
    "comment.submit": (
        _css("comment_send_button", 'button:has-text("发送")'),
        _css("comment_send_class", '[class*="comment-send"]'),
        _css("comment_submit_button", 'button[type="submit"]'),
        _text("comment_send_text", "发送"),
    ),
    "dm.entry": (
        _text("dm_entry_private_message", "私信"),
        _text("dm_entry_send_message", "发消息"),
        _text("dm_entry_send_private_message", "发私信"),
    ),
    "dm.editor": (
        _css("dm_editor_send_textarea", 'textarea[placeholder*="发送"]'),
        _css(
            "dm_editor_send_contenteditable",
            'div[contenteditable="true"][placeholder*="发送"]',
        ),
        _css("dm_editor_private_textarea", 'textarea[placeholder*="私信"]'),
        _css("dm_editor_contenteditable", 'div[contenteditable="true"]'),
        _css("dm_editor_textarea", "textarea"),
        _css("dm_editor_text_input", 'input[type="text"]'),
    ),
    "dm.submit": (
        _css("dm_submit_button", 'button:has-text("发送")'),
        _css("dm_submit_text", 'span:has-text("发送")'),
        _css("dm_submit_class", ".send-btn"),
    ),
})


def selector_candidates(group: str) -> tuple[SelectorCandidate, ...]:
    try:
        return SELECTOR_GROUPS[group]
    except KeyError as exc:
        raise KeyError(f"未知的小红书选择器语义组: {group}") from exc


def candidate_locator(page: Any, candidate: SelectorCandidate) -> Any:
    if candidate.kind == "text":
        return page.get_by_text(
            candidate.value, exact=candidate.exact).first
    return page.locator(candidate.value).first


async def find_present(page: Any, group: str) -> tuple[Any | None, str]:
    for candidate in selector_candidates(group):
        try:
            locator = candidate_locator(page, candidate)
            if int(await locator.count()) > 0:
                return locator, candidate.name
        except Exception:
            continue
    return None, ""


async def find_visible(
        page: Any, group: str, *,
        require_enabled: bool = False) -> tuple[Any | None, str]:
    for candidate in selector_candidates(group):
        try:
            locator = candidate_locator(page, candidate)
            if int(await locator.count()) <= 0 or not await locator.is_visible():
                continue
            if require_enabled and not await locator.is_enabled():
                continue
            return locator, candidate.name
        except Exception:
            continue
    return None, ""


def _safe_page_location(page: Any) -> str:
    try:
        raw_url = str(getattr(page, "url", "") or "")
        parsed = urlsplit(raw_url)
        host = parsed.hostname or ""
        path = parsed.path or "/"
        return f"{host}{path}" if host else path
    except Exception:
        return "?"


async def selector_diagnostic(page: Any, group: str) -> str:
    entries: list[str] = []
    for candidate in selector_candidates(group):
        count_text = "?"
        visible_text = "?"
        try:
            locator = candidate_locator(page, candidate)
            count = max(0, int(await locator.count()))
            count_text = "99+" if count > 99 else str(count)
            visible_text = (
                "true" if count > 0 and await locator.is_visible() else "false")
        except Exception:
            pass
        entries.append(
            f"{candidate.name}(count={count_text},visible={visible_text})")

    try:
        closed = "true" if bool(page.is_closed()) else "false"
    except Exception:
        closed = "?"
    diagnostic = (
        f"selector_diag group={group} page={_safe_page_location(page)} "
        f"closed={closed} candidates=[{','.join(entries)}]"
    )
    return diagnostic[:1200]
