"""Visible, page-native Rednote (海外小红书) publisher and work management.

Follows the recovered RPA publish architecture (`24388` -> `24457`):
  - Opens ``https://creator.rednote.com/publish/publish?source=official`` in the
    account's isolated browser profile (never using domestic ``edith.xiaohongshu.com``
    direct ``postCreate``).
  - Applies ``weighted_truncate(title, 20)`` (``exV``: up to 40 ASCII chars or
    20 CJK chars) and image-mode 900 UTF-16 validation.
  - Supports both Chinese and English creator-center UI labels on ``creator.rednote.com``.
  - Intercepts ``POST`` responses on ``webapi.rednote.com/web_api/sns/v2/note``
    (and ``/publish``, ``/note/create``), confirms on ``creator.rednote.com/new/note-manager``,
    and returns canonical ``https://www.rednote.com/zh/discovery/item/{note_id}`` URLs.
  - Enforces a strict single-click submission boundary with ``write_uncertain:``
    preservation when confirmation is inconclusive.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import ExitStack, suppress
import contextvars
from functools import partial
import inspect
import threading
from typing import Any, List, Sequence, Tuple
from urllib.parse import urlsplit

from ...browser.human_interaction import (
    DEFAULT_HUMAN_ENGINE,
    human_click_first,
    human_fill_first,
    human_wait,
)
from ...browser.identity import Identity
from ...browser.manager import BrowserManager
from ...browser.xhs_selectors import (
    SelectorCandidate,
    candidate_locator,
    selector_candidates,
    selector_diagnostic,
)
from .._submit import notify_stage as _notify_stage
from ..xhs.browser_writes import (
    XhsWriteOutcome as RednoteWriteOutcome,
    _PublishWebComponentLocator,
    _SubmitLocator,
    _click_closed_shadow_publish_button,
    _extract_publish_note_id,
    _publish_payload_accepted,
    _wait_until_enabled,
)
from ..xhs.deadline import PublishCancelEvent, deadline_after, remaining_seconds, timeout_error
from ..xhs.media import snapshot_publish_files, validate_publish_files
from .client import RednoteApiClient, RednoteApiError, cookie_str_from_state, has_a1
from .creator_api import RednoteCreatorApi, RednotePublishError, build_published_note_url
from .signer import extract_sign_context_from_state, refresh_rednote_sign_context
from .domains import (
    CREATOR_DOMAIN,
    EDIT_DOMAIN,
    HOME_PAGE_URL,
    PUBLISH_URL,
    is_rednote_host,
)
from .extract import parse_rednote_creator_user, parse_rednote_self_user
from .formatter import (
    MAX_DESC_WEIGHT,
    MAX_IMAGE_DESC_UTF16,
    MAX_TITLE_WEIGHT,
    MAX_TOPICS,
    utf16_code_units,
    weighted_truncate,
)
from .resolve import build_rednote_note_url

_SUCCESS_TEXTS = (
    "发布成功",
    "发布完成",
    "Published successfully",
    "Post published",
    "Published",
)
_VERIFY_KEYWORDS = (
    "安全验证",
    "扫码验证",
    "滑块验证",
    "请先完成验证",
    "短信验证码",
    "验证码",
    "Security Verification",
    "Verify",
    "Verification",
)
_OPERATOR_SELECTORS = (
    ".publish-page-content-media .operator",
    "#creator-publish-dom .cover-container .operator",
)

# Bilingual selector extensions for overseas creator.rednote.com UI
_EXTRA_TITLE_CANDIDATES: tuple[SelectorCandidate, ...] = (
    SelectorCandidate("title_en_placeholder", "css", 'input[placeholder*="Title" i]'),
    SelectorCandidate("title_en_textarea", "css", 'textarea[placeholder*="Title" i]'),
)
_EXTRA_BODY_CANDIDATES: tuple[SelectorCandidate, ...] = (
    SelectorCandidate(
        "body_en_placeholder",
        "css",
        '[contenteditable="true"][data-placeholder*="Write" i], [contenteditable="true"][data-placeholder*="Add" i], [contenteditable="true"][placeholder*="Write" i]',
    ),
    SelectorCandidate("body_en_textarea", "css", 'textarea[placeholder*="Write" i], textarea[placeholder*="Description" i]'),
)
_EXTRA_SUBMIT_CANDIDATES: tuple[SelectorCandidate, ...] = (
    SelectorCandidate("publish_en_button", "css", 'button:has-text("Publish")'),
    SelectorCandidate("post_en_button", "css", 'button:has-text("Post")'),
    SelectorCandidate("publish_en_text", "text", "Publish", exact=True),
)
_EXTRA_PROGRESS_CANDIDATES: tuple[SelectorCandidate, ...] = (
    SelectorCandidate("uploading_en_text", "text", "Uploading"),
    SelectorCandidate("processing_en_text", "text", "Processing"),
)

_VISIBILITY_LABEL_CANDIDATES: dict[str, tuple[str, ...]] = {
    "public": (
        "公开可见",
        "所有人可见",
        "公开",
        "公開可見",
        "所有人可見",
        "Public",
        "Everyone",
        "Visible to everyone",
    ),
    "private": (
        "仅自己可见",
        "私密",
        "僅自己可見",
        "仅自己",
        "Only me",
        "Private",
        "Only visible to me",
        "Self only",
    ),
    "friends": (
        "仅互关好友可见",
        "互关好友可见",
        "好友可见",
        "僅互關好友可見",
        "Friends",
        "Mutual followers",
        "Friends only",
        "Followers you follow back",
    ),
}
_ALL_VISIBILITY_LABELS: frozenset[str] = frozenset(
    lbl.lower() for group in _VISIBILITY_LABEL_CANDIDATES.values() for lbl in group
)
_VISIBILITY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "public": ("公开", "所有人", "公開", "public", "everyone"),
    "private": ("自己", "私密", "only me", "private", "only visible to me", "self only"),
    "friends": ("好友", "互关", "互關", "friend", "mutual"),
}


def _publish_url(media_type: str) -> str:
    target = "video" if str(media_type or "").lower() == "video" else "image"
    return f"{PUBLISH_URL}&from=tab_switch&target={target}"


def _candidates_for(group: str) -> tuple[SelectorCandidate, ...]:
    base = selector_candidates(group)
    if group == "publish.title":
        return base + _EXTRA_TITLE_CANDIDATES
    if group == "publish.body":
        return base[:5] + _EXTRA_BODY_CANDIDATES + base[5:]
    if group == "publish.submit":
        return base[:2] + _EXTRA_SUBMIT_CANDIDATES + base[2:]
    if group == "publish.progress":
        return base + _EXTRA_PROGRESS_CANDIDATES
    return base


async def _find_present_bilingual(page: Any, group: str) -> tuple[Any | None, str]:
    for candidate in _candidates_for(group):
        try:
            locator = candidate_locator(page, candidate)
            if int(await locator.count()) > 0:
                return locator, candidate.name
        except Exception:
            continue
    return None, ""


async def _find_visible_bilingual(page: Any, group: str) -> tuple[Any | None, str]:
    for candidate in _candidates_for(group):
        try:
            locator = candidate_locator(page, candidate)
            if int(await locator.count()) > 0 and await locator.is_visible():
                return locator, candidate.name
        except Exception:
            continue
    return None, ""


async def _wait_present(page: Any, group: str, interaction: Any, *, attempts: int = 60):
    for idx in range(max(1, attempts)):
        loc, name = await _find_present_bilingual(page, group)
        if loc is not None:
            return loc, name
        if idx + 1 < attempts:
            await interaction.pause(0.18, 0.32)
    return None, ""


async def _wait_visible(page: Any, group: str, interaction: Any, *, attempts: int = 40):
    for idx in range(max(1, attempts)):
        loc, name = await _find_visible_bilingual(page, group)
        if loc is not None:
            return loc, name
        if idx + 1 < attempts:
            await interaction.pause(0.18, 0.32)
    return None, ""


async def _upload_operator_text(page: Any) -> str:
    for sel in _OPERATOR_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                txt = str(await loc.inner_text() or "").replace("\n", " ").strip()
                if txt:
                    return txt
        except Exception:
            continue
    return ""


async def _upload_is_busy(page: Any) -> bool:
    op = await _upload_operator_text(page)
    if op and ("上传中" in op or "uploading" in op.lower()):
        return True
    for candidate in _candidates_for("publish.progress"):
        try:
            locator = candidate_locator(page, candidate)
            if await locator.count() and await locator.is_visible():
                return True
        except Exception:
            continue
    return False


async def _wait_upload_complete(
    page: Any,
    interaction: Any,
    *,
    attempts: int = 120,
    on_stage: Any = None,
    error_out: dict | None = None,
) -> bool:
    stable = 0
    last_op = ""
    for _ in range(max(1, attempts)):
        op = await _upload_operator_text(page)
        low = op.lower()
        if op and ("失败" in op or "failed" in low or "error" in low):
            if error_out is not None:
                error_out["error"] = f"海外小红书媒体上传失败: {op}"
            return False
        if op and ("上传中" in op or "uploading" in low) and op != last_op:
            last_op = op
            await _notify_stage(on_stage, f"正在上传媒体（{op}）…")
        if not await _upload_is_busy(page):
            stable += 1
            if stable >= 2:
                return True
        else:
            stable = 0
        await interaction.pause(0.25, 0.55)
    return False


def _matches_visibility_group(text: str, vis_key: str) -> bool:
    cleaned = " ".join(str(text or "").split()).strip()
    if not cleaned:
        return False
    low = cleaned.lower()
    desired_labels = _VISIBILITY_LABEL_CANDIDATES.get(vis_key, _VISIBILITY_LABEL_CANDIDATES["public"])
    if any(low == lbl.lower() or low.startswith(lbl.lower() + " ") for lbl in desired_labels):
        return True
    first_line = str(text or "").splitlines()[0].strip().lower()
    if any(first_line == lbl.lower() for lbl in desired_labels):
        return True
    return any(kw in low for kw in _VISIBILITY_KEYWORDS.get(vis_key, ()))


def _looks_like_any_visibility_label(text: str) -> bool:
    cleaned = " ".join(str(text or "").split()).strip()
    if not cleaned:
        return False
    first_line = str(text or "").splitlines()[0].strip().lower()
    if cleaned.lower() in _ALL_VISIBILITY_LABELS or first_line in _ALL_VISIBILITY_LABELS:
        return True
    return any(_matches_visibility_group(cleaned, k) for k in ("public", "private", "friends"))


async def _click_rednote_closed_shadow_publish_button(page: Any) -> None:
    """Dispatch one trusted human-curved click to Rednote's closed-shadow publish button."""
    await _click_closed_shadow_publish_button(page)


_VISIBILITY_OPTION_SELECTORS: tuple[str, ...] = (
    '.d-popover [role="option"]',
    '.d-popover .d-option',
    '.d-popover .d-select-option',
    '.d-popover .d-grid-item',
    '.d-popover .custom-option',
    '.d-select-dropdown [role="option"]',
    '.d-select-dropdown .d-option',
    '.d-select-dropdown .d-select-option',
    '[role="listbox"] [role="option"]',
    '.reds-popover [role="option"]',
    '.reds-popover .d-option',
    '[role="option"]',
    '.d-option',
    '.d-select-option',
    '.d-grid-item',
    '.custom-option',
    '.d-radio',
    '[role="radio"]',
)


async def _try_click_dropdown_option(
    page: Any,
    interaction: Any,
    vis_key: str,
    desired_labels: tuple[str, ...],
    trigger_text: str,
) -> bool:
    """Click a visible option inside an open dropdown/popover or inline option list."""
    # 1) Exact Playwright text locators (excluding the closed/open trigger text if identical)
    for exact in (True, False):
        for label in desired_labels:
            try:
                options = page.get_by_text(label, exact=exact)
                total = int(await options.count())
            except Exception:
                continue
            # Iterate from last to first because portal popovers are appended at the end of <body>
            for index in range(total - 1, -1, -1):
                option = options.nth(index)
                try:
                    if not await option.is_visible():
                        continue
                    cls = str(await option.get_attribute("class") or "")
                    if "d-select-description" in cls.split():
                        continue
                    opt_txt = str(await option.inner_text() or "").strip()
                    if trigger_text and opt_txt == trigger_text and exact is False:
                        continue
                    if not exact:
                        if not _matches_visibility_group(opt_txt, vis_key):
                            continue
                        if len(opt_txt) > 60:
                            continue
                    with suppress(Exception):
                        await option.scroll_into_view_if_needed(timeout=1500)
                    await DEFAULT_HUMAN_ENGINE.click_locator(option, page=page, timeout=3000)
                    await interaction.pause(0.18, 0.35)
                    return True
                except Exception:
                    continue

    # 2) Fallback: structured Playwright locators inside popover/dropdown/radio containers
    #    (strictly hardware-dispatched via click_locator — never untrusted JS el.click)
    for selector in _VISIBILITY_OPTION_SELECTORS:
        try:
            nodes = page.locator(selector)
            total = int(await nodes.count())
        except Exception:
            continue
        for index in range(total - 1, -1, -1):
            cand = nodes.nth(index)
            try:
                if not await cand.is_visible():
                    continue
                cls = str(await cand.get_attribute("class") or "")
                if "d-select-description" in cls.split():
                    continue
                raw = str(await cand.inner_text() or "").strip()
                if not raw or len(raw) > 48 or raw == trigger_text:
                    continue
                if _matches_visibility_group(raw, vis_key):
                    with suppress(Exception):
                        await cand.scroll_into_view_if_needed(timeout=1500)
                    await DEFAULT_HUMAN_ENGINE.click_locator(cand, page=page, timeout=3000)
                    await interaction.pause(0.18, 0.35)
                    return True
            except Exception:
                continue
    return False


async def _set_publish_visibility(page: Any, interaction: Any, visibility: str) -> None:
    vis_key = str(visibility or "public").strip().lower()
    desired_labels = _VISIBILITY_LABEL_CANDIDATES.get(vis_key, _VISIBILITY_LABEL_CANDIDATES["public"])

    # Dismiss any lingering #topic / @mention editor popover and scroll settings into view
    if hasattr(page, "keyboard"):
        with suppress(Exception):
            await page.keyboard.press("Escape")
            await interaction.pause(0.08, 0.16)
    for section_label in ("权限设置", "可见范围", "谁可以看", "Visibility", "Who can view", "Privacy"):
        try:
            sec_loc = page.get_by_text(section_label, exact=True)
            if int(await sec_loc.count()) > 0:
                await sec_loc.first.scroll_into_view_if_needed(timeout=1200)
                break
        except Exception:
            continue

    # Case A: Inline radio buttons where both public & private options are already visible on the form
    try:
        pub_vis = False
        priv_vis = False
        for lbl in _VISIBILITY_LABEL_CANDIDATES["public"][:4] + ("Public", "Everyone"):
            loc = page.get_by_text(lbl, exact=True)
            if int(await loc.count()) > 0 and await loc.first.is_visible():
                pub_vis = True
                break
        for lbl in _VISIBILITY_LABEL_CANDIDATES["private"][:4] + ("Only me", "Private"):
            loc = page.get_by_text(lbl, exact=True)
            if int(await loc.count()) > 0 and await loc.first.is_visible():
                priv_vis = True
                break
        if pub_vis and priv_vis:
            if vis_key == "public":
                # Still click public explicitly if non-default, or return cleanly
                await _try_click_dropdown_option(page, interaction, vis_key, desired_labels, "")
                return
            if await _try_click_dropdown_option(page, interaction, vis_key, desired_labels, ""):
                return
    except Exception:
        pass

    # Case B: `.d-select` / custom visibility dropdown — prefer a control whose text is a visibility label!
    async def _scan_visibility_trigger():
        cands = page.locator(".d-select-description")
        n_cands = int(await cands.count())
        matched = None
        first_vis = None
        for idx in range(n_cands):
            cand = cands.nth(idx)
            try:
                if hasattr(cand, "scroll_into_view_if_needed"):
                    with suppress(Exception):
                        await cand.scroll_into_view_if_needed(timeout=1200)
                if not await cand.is_visible():
                    continue
                if first_vis is None:
                    first_vis = cand
                txt = str(await cand.inner_text() or "").strip()
                if _looks_like_any_visibility_label(txt):
                    matched = cand
                    break
            except Exception:
                continue
        if matched is not None:
            return matched, first_vis
        # Also check non-`.d-select-description` triggers showing default public/visibility text
        for pub_lbl in ("公开可见", "所有人可见", "公開可見", "Public", "Everyone"):
            try:
                loc = page.get_by_text(pub_lbl, exact=True)
                for idx in range(int(await loc.count())):
                    item = loc.nth(idx)
                    if await item.is_visible():
                        return item, first_vis
            except Exception:
                continue
        return None, first_vis

    matched_visibility_select, first_visible_select = await _scan_visibility_trigger()
    if matched_visibility_select is None:
        # Expand "更多设置" / "高级设置" / "More settings" if collapsed on creator.rednote.com
        for more_lbl in ("更多设置", "高级设置", "更多选项", "More settings", "Advanced settings", "More options"):
            try:
                more_loc = page.get_by_text(more_lbl, exact=False)
                if int(await more_loc.count()) > 0 and await more_loc.first.is_visible():
                    await interaction.click_visible(more_loc.first)
                    await interaction.pause(0.2, 0.4)
                    # Re-check inline radio options after expanding
                    if await _try_click_dropdown_option(page, interaction, vis_key, desired_labels, ""):
                        return
                    matched_visibility_select, first_visible_select = await _scan_visibility_trigger()
                    break
            except Exception:
                continue

    current = matched_visibility_select or first_visible_select
    trigger_text = ""
    if current is not None:
        try:
            trigger_text = str(await current.inner_text() or "").strip()
            if _matches_visibility_group(trigger_text, vis_key):
                return
        except Exception:
            trigger_text = ""
    elif vis_key == "public":
        return
    else:
        raise RuntimeError("未找到可见范围设置")

    # Try up to 2 clicks on `current` (in case click #1 only dismissed an overlay)
    for attempt in range(2):
        await interaction.click_visible(current)
        await interaction.pause(0.22, 0.42)
        if await _try_click_dropdown_option(page, interaction, vis_key, desired_labels, trigger_text):
            try:
                readback = str(await current.inner_text() or "").strip()
            except Exception:
                readback = ""
            if not readback or _matches_visibility_group(readback, vis_key):
                return
            if attempt == 1:
                raise RuntimeError(f"可见范围设置后读回不一致（当前：{readback}）")

    # Collect diagnostic visible options if still not found
    seen_opts: list[str] = []
    if hasattr(page, "evaluate"):
        with suppress(Exception):
            seen_opts = await page.evaluate(
                """() => {
                  const out = [];
                  for (const el of document.querySelectorAll('.d-popover *, .d-select-dropdown *, [role="option"], .d-option, .d-radio')) {
                    const st = window.getComputedStyle(el);
                    if (!st || st.display === 'none' || st.visibility === 'hidden') continue;
                    const t = (el.innerText || '').trim();
                    if (t && t.length <= 24 && !out.includes(t)) out.push(t);
                    if (out.length >= 8) break;
                  }
                  return out;
                }"""
            ) or []
    hint = f"（页面可见选项：{', '.join(seen_opts)}）" if seen_opts else ""
    raise RuntimeError(f"未找到可见范围选项：{desired_labels[0]}{hint}")


def _publish_response_handler(evidence: dict, submitted: dict):
    def on_response(response):
        try:
            url = str(response.url or "")
            parsed = urlsplit(url)
            host = (parsed.hostname or "").lower()
            if host and not is_rednote_host(host):
                return
            request = response.request
            method = str(getattr(request, "method", "") or "").upper()
            status = int(response.status)
            if submitted["clicked"] and method == "POST" and any(
                marker in url.lower()
                for marker in ("/publish", "/note/create", "/web_api/sns/v2/note")
            ):
                if 200 <= status < 300:
                    evidence["responses"].append(response)
                else:
                    evidence["business_rejected"] = True
                    evidence["reject_msg"] = f"HTTP {status}"
                    if status in (461, 471):
                        evidence["verify_hint"] = f"海外小红书安全验证(HTTP {status})"
        except Exception:
            return

    return on_response


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
            note_id = _extract_publish_note_id(payload)
            if note_id:
                evidence["note_id"] = note_id
            return True
        evidence["business_rejected"] = True
        msg = str(payload.get("msg") or payload.get("message") or "").strip()
        if msg:
            evidence["reject_msg"] = msg
            if any(k in msg.lower() for k in ("验证", "滑块", "安全", "频繁", "异常", "verify", "captcha")):
                evidence["verify_hint"] = msg
    return bool(evidence["accepted"])


async def _visible_verify_text(page: Any) -> str:
    for kw in _VERIFY_KEYWORDS:
        try:
            loc = page.get_by_text(kw, exact=False).first
            if await loc.count() and await loc.is_visible():
                return kw
        except Exception:
            continue
    return ""


async def _visible_success(page: Any) -> bool:
    raw_url = str(getattr(page, "url", "") or "")
    lower_url = raw_url.lower()
    if any(marker in lower_url for marker in ("/publish/success", "publish_success")):
        return True
    try:
        parsed = urlsplit(raw_url)
        path = (parsed.path or "/").lower()
        if (
            (parsed.hostname or "").lower() == CREATOR_DOMAIN
            and "/publish/publish" not in path
            and not any(
                marker in lower_url
                for marker in ("login", "passport", "verify", "captcha", "error")
            )
        ):
            return True
    except Exception:
        pass
    for text in _SUCCESS_TEXTS:
        try:
            locator = page.get_by_text(text, exact=False).first
            if await locator.count() and await locator.is_visible():
                return True
        except Exception:
            continue
    return False


async def _confirm_published_title(
    page: Any,
    title: str,
    interaction: Any,
    *,
    attempts: int = 40,
    reload_every: int = 10,
) -> bool:
    clean_title = str(title or "").strip()
    if not clean_title:
        return False
    try:
        current = str(getattr(page, "url", "") or "")
        if "/new/note-manager" not in current:
            await page.goto(
                HOME_PAGE_URL,
                wait_until="domcontentloaded",
                timeout=40_000,
            )
    except Exception:
        return False
    interval = max(1, int(reload_every or 10))
    for index in range(max(1, attempts)):
        try:
            match = page.get_by_text(clean_title, exact=True).first
            if await match.count() and await match.is_visible():
                return True
        except Exception:
            pass
        if index + 1 < attempts:
            await interaction.pause(0.6, 1.0)
            if (index + 1) % interval == 0:
                with suppress(Exception):
                    await page.reload(wait_until="domcontentloaded", timeout=40_000)
    return False


async def publish_rednote_browser(
    mgr: BrowserManager,
    identity: Identity,
    media_type: str,
    title: str,
    desc: str,
    topics: Sequence[str],
    files: Sequence[str],
    *,
    timeout_seconds: int = 180,
    visibility: str = "public",
    on_submit: Any = None,
    on_stage: Any = None,
) -> RednoteWriteOutcome:
    """Submit one Rednote note via ``creator.rednote.com`` with single-click safety."""
    try:
        paths = validate_publish_files(media_type, files)
    except ValueError as exc:
        return RednoteWriteOutcome("failed", error=str(exc))

    normalized_title = weighted_truncate((title or "").strip(), MAX_TITLE_WEIGHT)
    raw_desc = str(desc or "")
    if str(media_type or "").lower() in {"image", "images"} and utf16_code_units(raw_desc) > MAX_IMAGE_DESC_UTF16:
        return RednoteWriteOutcome(
            "failed",
            error=f"海外小红书图文正文最多 {MAX_IMAGE_DESC_UTF16} 字符，请精简后重试",
        )

    tags = [str(t).strip().lstrip("#") for t in topics if str(t).strip()][:MAX_TOPICS]
    combined = (
        raw_desc + ("\n" + " ".join(f"#{tag}" for tag in tags) if tags else "")
    ).strip()
    body = weighted_truncate(combined, MAX_DESC_WEIGHT)
    if str(media_type or "").lower() in {"image", "images"} and utf16_code_units(body) > MAX_IMAGE_DESC_UTF16:
        # Keep UTF-16 code units within the 900-unit image-mode ceiling
        encoded = body.encode("utf-16-le")[: MAX_IMAGE_DESC_UTF16 * 2]
        body = encoded.decode("utf-16-le", errors="ignore").strip()

    interaction = mgr.xhs_interaction
    submitted = {"clicked": False}
    evidence: dict[str, Any] = {
        "accepted": False,
        "url": "",
        "note_id": "",
        "responses": [],
        "business_rejected": False,
        "reject_msg": "",
        "verify_hint": "",
    }
    on_response = _publish_response_handler(evidence, submitted)
    page = None

    try:
        await _notify_stage(on_stage, "正在打开海外小红书创作者中心并上传媒体…")
        async with mgr.visible_page(identity, url=_publish_url(media_type)) as page:
            cur_url = str(getattr(page, "url", "") or "").lower()
            if "login" in cur_url or "passport" in cur_url:
                return RednoteWriteOutcome(
                    "failed",
                    error="logged_out:海外小红书创作者中心未登录，请先完成登录",
                )

            file_input, _ = await _wait_present(page, "publish.file", interaction)
            if file_input is None:
                diagnostic = await selector_diagnostic(page, "publish.file")
                return RednoteWriteOutcome(
                    "failed",
                    error=f"未找到媒体上传控件(发布页可能改版或需重新登录)；{diagnostic}",
                )
            await file_input.set_input_files(paths, timeout=15_000)
            await _notify_stage(on_stage, "正在等待海外小红书媒体上传完成…")
            upload_err: dict[str, str] = {}
            if not await _wait_upload_complete(
                page, interaction, on_stage=on_stage, error_out=upload_err
            ):
                return RednoteWriteOutcome(
                    "failed",
                    error=upload_err.get("error") or "媒体上传在限定时间内未完成",
                )

            await _notify_stage(on_stage, "正在填写海外小红书标题、正文与可见范围…")
            if normalized_title:
                title_input, _ = await _wait_visible(page, "publish.title", interaction)
                if title_input is None:
                    diagnostic = await selector_diagnostic(page, "publish.title")
                    return RednoteWriteOutcome(
                        "failed", error=f"未找到标题输入框(发布页可能改版)；{diagnostic}"
                    )
                await interaction.type_short(title_input, normalized_title)
            if body:
                body_input, _ = await _wait_visible(page, "publish.body", interaction)
                if body_input is None:
                    diagnostic = await selector_diagnostic(page, "publish.body")
                    return RednoteWriteOutcome(
                        "failed", error=f"未找到正文输入框(发布页可能改版)；{diagnostic}"
                    )
                await interaction.insert_long(body_input, body, page=page)

            try:
                await _set_publish_visibility(page, interaction, visibility)
            except Exception as exc:
                return RednoteWriteOutcome("failed", error=f"设置可见范围失败: {exc}")

            await _notify_stage(on_stage, "正在准备点击「发布」…")
            publish_button, publish_selector = await _wait_visible(
                page, "publish.submit", interaction
            )
            if publish_button is None:
                diagnostic = await selector_diagnostic(page, "publish.submit")
                return RednoteWriteOutcome(
                    "failed", error=f"未找到发布按钮(发布页可能改版)；{diagnostic}"
                )
            if publish_selector == "publish_web_component":
                publish_button = _PublishWebComponentLocator(page, publish_button)
            if not await _wait_until_enabled(
                publish_button,
                interaction,
                attempts=max(4, min(120, timeout_seconds * 2)),
            ):
                return RednoteWriteOutcome(
                    "failed", error="发布按钮一直不可用，请检查页面提示"
                )

            with suppress(Exception):
                page.on("response", on_response)
            try:
                await interaction.click_visible(
                    _SubmitLocator(publish_button, submitted, on_submit)
                )
                await _notify_stage(on_stage, f"已点击「发布」，等待 {EDIT_DOMAIN} 确认…")
            except Exception as exc:
                if submitted["clicked"]:
                    return RednoteWriteOutcome(
                        "uncertain", error=f"发布已提交但连接中断: {exc!r}"
                    )
                return RednoteWriteOutcome("failed", error=f"发布按钮点击失败: {exc!r}")

            attempts = max(4, min(80, int(timeout_seconds * 2)))
            verify_notified = False
            for _ in range(attempts):
                await _consume_publish_responses(evidence)
                accepted_note_id = str(evidence.get("note_id") or "").strip()
                if (
                    evidence["accepted"]
                    and accepted_note_id
                    and not evidence["business_rejected"]
                ):
                    return RednoteWriteOutcome(
                        "success",
                        result=build_rednote_note_url(accepted_note_id),
                    )
                if not verify_notified:
                    verify_detail = evidence["verify_hint"] or await _visible_verify_text(page)
                    if verify_detail:
                        verify_notified = True
                        await _notify_stage(
                            on_stage,
                            f"需要人工安全验证（{verify_detail}），请在弹出的海外小红书窗口里完成验证",
                        )
                if await _visible_success(page):
                    await _consume_publish_responses(evidence)
                    note_id = str(evidence.get("note_id") or "").strip()
                    result = (
                        build_rednote_note_url(note_id)
                        if note_id
                        else str(getattr(page, "url", "") or evidence["url"] or HOME_PAGE_URL)
                    )
                    if evidence["accepted"] and note_id and not evidence["business_rejected"]:
                        return RednoteWriteOutcome("success", result=result)
                    if not normalized_title:
                        if evidence["accepted"] and not evidence["business_rejected"]:
                            return RednoteWriteOutcome("success", result=result)
                        return RednoteWriteOutcome(
                            "uncertain",
                            error="平台进入了发布成功页，但无标题笔记缺少接口确认回执，请到平台核对",
                        )
                    await _notify_stage(on_stage, "已提交，正在笔记管理页确认发布结果…")
                    await interaction.pause(0.8, 1.2)
                    confirmed = await _confirm_published_title(
                        page,
                        normalized_title,
                        interaction,
                        attempts=max(
                            4,
                            min(
                                120,
                                timeout_seconds
                                if str(media_type or "").lower() == "video"
                                else timeout_seconds // 3,
                            ),
                        ),
                    )
                    if confirmed:
                        return RednoteWriteOutcome("success", result=result)
                    if evidence["accepted"] and note_id and not evidence["business_rejected"]:
                        return RednoteWriteOutcome("success", result=result)
                    return RednoteWriteOutcome(
                        "uncertain",
                        error="平台进入了发布成功页，但笔记管理中未找到本次标题，请到平台核对",
                    )
                try:
                    if page.is_closed():
                        break
                except Exception:
                    break
                await interaction.pause(0.25, 0.55)

            reject_note = f"（{evidence['reject_msg']}）" if evidence.get("reject_msg") else ""
            detail = (
                f"平台返回了业务拒绝结果{reject_note}，但提交状态仍需核对"
                if evidence["business_rejected"]
                else "发布按钮已点击一次，但未取得明确成功证据"
            )
            return RednoteWriteOutcome("uncertain", error=f"{detail}，请到平台核对")
    except Exception as exc:
        status = "uncertain" if submitted["clicked"] else "failed"
        prefix = "发布已提交后页面异常" if submitted["clicked"] else "发布页面异常"
        return RednoteWriteOutcome(status, error=f"{prefix}: {exc!r}")
    finally:
        if page is not None:
            with suppress(Exception):
                page.remove_listener("response", on_response)


def _rednote_api_privacy_type(visibility: str) -> int:
    vis_map = {"public": 0, "private": 1}
    key = str(visibility or "public").strip().lower()
    if key not in vis_map:
        raise ValueError("海外小红书 API 发布可见性须为 public（公开）或 private（仅自己可见）")
    return vis_map[key]


def _publish_rednote_api_sync(
    cookie_str: str,
    storage_state_json: str,
    media_type: str,
    title: str,
    desc: str,
    files: List[str],
    topics: List[str],
    proxy: str = "",
    *,
    user_agent: str = "",
    locale: str = "en-US",
    timezone: str = "UTC",
    visibility: str = "public",
    on_submit=None,
    cancel_event: threading.Event | None = None,
    preserve_error: bool = False,
    deadline: float | None = None,
) -> Tuple[bool, str, str | RednoteApiError]:
    """Synchronous pure-protocol Rednote publisher (`XYS_` + `b1` + `//webapi.rednote.com` `x-rap-param`)."""
    try:
        privacy_type = _rednote_api_privacy_type(visibility)
    except ValueError as exc:
        return False, "", str(exc)

    submitted = False
    if deadline is None:
        deadline = deadline_after(180)

    def check_active():
        remaining_seconds(deadline, cancel_event)

    def mark_submitted():
        nonlocal submitted
        check_active()
        if on_submit is not None:
            on_submit()
        submitted = True
        check_active()

    def failure(message):
        if submitted:
            message = f"write_uncertain:海外小红书发布已提交，结果需到创作者中心核对；{message}"
        elif not (preserve_error and isinstance(message, RednoteApiError)):
            message = str(message)
        return False, "", message

    try:
        check_active()
        norm_kind = "video" if str(media_type or "").lower() == "video" else "image"
        with ExitStack() as stack:
            blobs = stack.enter_context(
                snapshot_publish_files(norm_kind, files, check_active=check_active)
            )
            api = RednoteCreatorApi(
                cookie_str,
                storage_state=storage_state_json,
                user_agent=user_agent or _LIST_UA,
                locale=locale,
                timezone=timezone,
                proxy=proxy,
                cancel_event=cancel_event,
                deadline=deadline,
            )
            stack.callback(api.close)
            if norm_kind == "video":
                ok, msg, resp_json = api.post_note(
                    media_type="video",
                    title=title,
                    desc=desc,
                    video_file=blobs[0],
                    topics=topics,
                    privacy_type=privacy_type,
                    on_submit=mark_submitted,
                )
            else:
                ok, msg, resp_json = api.post_note(
                    media_type="image",
                    title=title,
                    desc=desc,
                    image_files=blobs,
                    topics=topics,
                    privacy_type=privacy_type,
                    on_submit=mark_submitted,
                )
            check_active()
            if ok:
                return True, build_published_note_url(resp_json), ""
            err = msg or "海外小红书 API 发布失败"
            if any(k in err.lower() for k in ("登录", "过期", "expired", "login")):
                err += " —— 请在「账号」页重新完成海外小红书扫码或创作者登录"
            return failure(err)
    except (RednotePublishError, RednoteApiError) as exc:
        return failure(exc)
    except Exception as exc:
        return failure(f"海外小红书 API 发布异常: {exc!r}")


async def publish_rednote(
    mgr: BrowserManager | None,
    identity: Identity | None,
    storage_state_json: str,
    media_type: str,
    title: str,
    desc: str,
    media_paths: List[str],
    topics: str = "",
    *,
    headed: bool = True,
    mode: str = "browser",
    timeout_seconds: int = 180,
    visibility: str = "public",
    preserve_error: bool = False,
    on_submit=None,
    on_stage=None,
) -> Tuple[bool, str, str | RednoteApiError]:
    """Publish a Rednote note via browser RPA (`browser`) or experimental XYS+b1 API (`api`)."""
    _ = headed
    selected_mode = str(mode or "browser").strip().lower()
    if selected_mode not in {"browser", "api"}:
        selected_mode = "browser"
    tags = [t.strip().lstrip("#") for t in (topics or "").split(",") if t.strip()]

    if selected_mode == "browser":
        if mgr is None or identity is None:
            return False, "", "浏览器管理器或账号身份未就绪"
        outcome = await publish_rednote_browser(
            mgr,
            identity,
            media_type,
            title,
            desc,
            tags,
            media_paths,
            timeout_seconds=timeout_seconds,
            visibility=visibility,
            on_submit=on_submit,
            on_stage=on_stage,
        )
        return outcome.legacy()

    try:
        deadline = deadline_after(timeout_seconds)
        norm_kind = "video" if str(media_type or "").lower() == "video" else "images"
        files = validate_publish_files(norm_kind, media_paths)
        _rednote_api_privacy_type(visibility)
    except ValueError as exc:
        return False, "", str(exc)

    cookie_str = cookie_str_from_state(storage_state_json)
    if not has_a1(cookie_str):
        return False, "", "登录态缺少 rednote.com 的 a1 Cookie，请先完成海外小红书登录"

    effective_state = storage_state_json
    if (
        not extract_sign_context_from_state(effective_state).b1
        and mgr is not None
        and identity is not None
        and identity.key in getattr(mgr, "_contexts", {})
    ):
        with suppress(Exception):
            _, effective_state = await refresh_rednote_sign_context(
                mgr, identity, effective_state
            )

    await _notify_stage(on_stage, "正在通过海外小红书实验协议通道（XYS + b1）上传与发布…")

    proxy = getattr(identity, "proxy", "") if identity is not None else ""
    ua = getattr(identity, "ua", "") if identity is not None else _LIST_UA
    locale = getattr(identity, "locale", "en-US") if identity is not None else "en-US"
    tz = getattr(identity, "timezone_id", "UTC") if identity is not None else "UTC"

    cancel_event = PublishCancelEvent()
    loop = asyncio.get_running_loop()
    submit_future = None

    async def notify_submit():
        remaining_seconds(deadline, cancel_event)
        if on_submit is not None:
            result = on_submit()
            if inspect.isawaitable(result):
                await result

    def notify_from_worker():
        nonlocal submit_future
        remaining_seconds(deadline, cancel_event)
        submit_future = asyncio.run_coroutine_threadsafe(notify_submit(), loop)
        try:
            submit_future.result(timeout=remaining_seconds(deadline, cancel_event))
        except FutureTimeoutError:
            if submit_future.done():
                remaining_seconds(deadline, cancel_event)
                raise
            submit_future.cancel()
            raise timeout_error()
        except Exception:
            submit_future.cancel()
            remaining_seconds(deadline, cancel_event)
            raise

    def expire():
        cancel_event.expire()
        if submit_future is not None:
            submit_future.cancel()

    work = partial(
        _publish_rednote_api_sync,
        cookie_str,
        effective_state,
        media_type,
        title,
        desc,
        files,
        tags,
        proxy,
        user_agent=ua,
        locale=locale,
        timezone=tz,
        visibility=visibility,
        on_submit=notify_from_worker,
        cancel_event=cancel_event,
        preserve_error=preserve_error,
        deadline=deadline,
    )
    worker = loop.run_in_executor(None, contextvars.copy_context().run, work)
    try:
        delay = remaining_seconds(deadline)
    except Exception:
        delay = 0
    timeout_handle = loop.call_later(delay, expire)
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancel_event.set()
        if submit_future is not None:
            submit_future.cancel()
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            worker.exception()
        raise
    finally:
        timeout_handle.cancel()


_LIST_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)


async def rednote_creator_profile(
    storage_state_json: str,
    proxy: str = "",
    *,
    user_agent: str = _LIST_UA,
    locale: str = "en-US",
) -> dict | None:
    """Fetch Rednote account profile via ``webapi.rednote.com`` / ``creator.rednote.com``."""
    cookie_str = cookie_str_from_state(storage_state_json)
    if not has_a1(cookie_str):
        return None
    client = RednoteApiClient(
        cookie_str,
        user_agent or _LIST_UA,
        proxy=proxy,
        locale=locale,
        storage_state=storage_state_json,
    )
    async with client.session_scope():
        with suppress(Exception):
            creator_data = await client.creator_info()
            if creator_data:
                parsed = parse_rednote_creator_user(creator_data)
                if parsed.get("sec_uid") or parsed.get("nickname"):
                    return parsed
        with suppress(Exception):
            me = await client.self_info()
            if me and not me.get("guest"):
                uid = str(me.get("user_id") or "")
                other = await client.user_info(uid) if uid else {}
                merged = {**(other or {}), **me}
                parsed = parse_rednote_self_user(merged)
                if parsed.get("sec_uid") or parsed.get("nickname"):
                    return parsed
    return None


async def rednote_creator_check(
    storage_state_json: str,
    proxy: str = "",
    *,
    user_agent: str = _LIST_UA,
    locale: str = "en-US",
) -> bool | None:
    """Check Rednote creator/web session validity. Returns True, False, or None."""
    cookie_str = cookie_str_from_state(storage_state_json)
    if not has_a1(cookie_str):
        return False
    client = RednoteApiClient(
        cookie_str,
        user_agent or _LIST_UA,
        proxy=proxy,
        locale=locale,
        storage_state=storage_state_json,
    )
    try:
        async with client.session_scope():
            me = await client.self_info()
            if isinstance(me, dict):
                if me.get("guest") is True:
                    return False
                if me.get("user_id") or me.get("red_id"):
                    return True
        return None
    except RednoteApiError as exc:
        if exc.category == "auth":
            return False
        return None
    except Exception:
        return None


async def list_published_rednote(
    storage_state_json: str,
    proxy: str = "",
    *,
    mgr: BrowserManager | None = None,
    identity: Identity | None = None,
    user_agent: str = _LIST_UA,
    locale: str = "en-US",
    read_mode: str = "hybrid",
) -> Tuple[bool, str, list]:
    """List own published Rednote notes (API-first in ``hybrid``/``api``, browser fallback in ``hybrid``/``browser``)."""
    mode = str(read_mode or "hybrid").strip().lower()
    if mode not in {"hybrid", "api", "browser"}:
        mode = "hybrid"

    cookie_str = cookie_str_from_state(storage_state_json)
    api_err = ""
    if mode in {"hybrid", "api"}:
        if not has_a1(cookie_str):
            api_err = "登录态缺少 rednote.com 的 a1 Cookie，请先登录海外小红书账号"
            if mode == "api":
                return False, api_err, []
        else:
            client = RednoteApiClient(
                cookie_str,
                user_agent or _LIST_UA,
                proxy=proxy,
                locale=locale,
                storage_state=storage_state_json,
            )
            try:
                async with client.session_scope():
                    with suppress(Exception):
                        creator_notes = await client.published_notes(max_pages=5)
                        if creator_notes:
                            return True, "ok", creator_notes
                    me = await client.self_info()
                    uid = str((me or {}).get("user_id") or "")
                    if uid:
                        posted = await client.notes_by_creator(uid)
                        return True, "ok", posted.get("notes") or []
                    api_err = "未能获取海外小红书账号 user_id，请先刷新资料或重新登录"
            except RednoteApiError as exc:
                api_err = str(exc)
                if mode == "api" or exc.category == "auth":
                    return False, api_err, []
            except Exception as exc:
                api_err = f"{exc!r}"
                if mode == "api":
                    return False, api_err, []

    if mode in {"hybrid", "browser"} and mgr is not None and identity is not None:
        from ...browser.rednote_fetcher import fetch_rednote_creator_published

        notes, berr = await fetch_rednote_creator_published(mgr, identity)
        if notes or not berr:
            return True, "ok", notes
        if berr.startswith("logged_out:"):
            return False, berr.split(":", 1)[1], []
        return False, berr, []

    return False, api_err or "未读取到海外小红书已发布作品", []


async def delete_rednote_work(
    storage_state_json: str,
    note_id: str,
    *,
    proxy: str = "",
    user_agent: str = _LIST_UA,
    locale: str = "en-US",
) -> Tuple[bool, str]:
    """Delete an own Rednote note via ``webapi.rednote.com``."""
    nid = str(note_id or "").strip()
    if not nid:
        return False, "缺少笔记 ID"
    cookie_str = cookie_str_from_state(storage_state_json)
    if not has_a1(cookie_str):
        return False, "登录态缺少 a1，请先登录海外小红书账号"
    client = RednoteApiClient(
        cookie_str,
        user_agent or _LIST_UA,
        proxy=proxy,
        locale=locale,
        storage_state=storage_state_json,
    )
    try:
        async with client.session_scope():
            await client.delete_note(nid)
        return True, ""
    except Exception as exc:
        return False, str(exc)


async def update_rednote_visibility(
    storage_state_json: str,
    note_id: str,
    visibility: str,
    *,
    proxy: str = "",
    user_agent: str = _LIST_UA,
    locale: str = "en-US",
) -> Tuple[bool, str]:
    """Update an own Rednote note's visibility (`public` -> 0, `private` -> 1)."""
    nid = str(note_id or "").strip()
    if not nid:
        return False, "缺少笔记 ID"
    vis_map = {"public": 0, "private": 1}
    vis_key = str(visibility or "").strip().lower()
    if vis_key not in vis_map:
        return False, "海外小红书可见性仅支持 public（公开）或 private（仅自己可见）"
    cookie_str = cookie_str_from_state(storage_state_json)
    if not has_a1(cookie_str):
        return False, "登录态缺少 a1，请先登录海外小红书账号"
    client = RednoteApiClient(
        cookie_str,
        user_agent or _LIST_UA,
        proxy=proxy,
        locale=locale,
        storage_state=storage_state_json,
    )
    try:
        async with client.session_scope():
            await client.set_note_privacy(nid, vis_map[vis_key])
        return True, ""
    except Exception as exc:
        return False, str(exc)
