"""TikTok Studio video publishing through an account-scoped visible browser."""
from __future__ import annotations

import asyncio
import inspect
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Iterable, List, Tuple

from ...browser.identity import Identity
from ...browser.manager import BrowserManager

PUBLISH_URL = "https://www.tiktok.com/creator-center/upload?lang=en"
PUBLISH_URLS = (
    PUBLISH_URL,
    "https://www.tiktok.com/tiktokstudio/upload?from=webapp",
)
MANAGE_URL = "https://www.tiktok.com/tiktokstudio/content"
UPLOAD_PAGE_TIMEOUT_SECONDS = 90

_VIDEO_INPUTS = (
    'input[type="file"][accept*="video"]',
    'input[type="file"][accept*="mp4"]',
    'input[type="file"]',
)
_CAPTION_SELECTORS = (
    '[data-e2e="caption-editor"] [contenteditable="true"]',
    '[data-e2e="video-caption"] [contenteditable="true"]',
    '[contenteditable="true"][role="textbox"]',
    '[contenteditable="true"][data-lexical-editor="true"]',
    '.public-DraftEditor-content[contenteditable="true"]',
    'div[contenteditable="true"][data-placeholder]',
    'div[contenteditable="true"][aria-label*="caption" i]',
    'div[contenteditable="true"]',
    'textarea[placeholder*="caption" i]',
)
_DISMISS_DIALOG_ACTIONS = (
    ("确定取消上传", "上传"),
    ("Discard this upload", "Continue uploading"),
    ("开启自动内容检查", "取消"),
    ("Turn on automatic content checks", "Not now"),
    ("在手机上预览你的视频", "知道了"),
    ("preview your video on mobile", "Got it"),
    ("全新编辑功能已上线", "知道了"),
    ("new editing features", "Got it"),
)
_SUCCESS_TEXT = (
    "Your video has been uploaded",
    "Video published",
    "Your post is being processed",
    "视频已发布",
    "发布成功",
    "已上传",
)
_VERIFY_TEXT = (
    "Verify to continue",
    "Security verification",
    "Complete verification",
    "验证码",
    "安全验证",
    "拖动滑块",
    "身份验证",
)
_REJECT_TEXT = (
    "Couldn't upload",
    "Upload failed",
    "Post failed",
    "上传失败",
    "发布失败",
)
_DEBUG_DIR = Path("./data/debug")


def _log(message: str) -> None:
    print(f"[tiktok-publish] {message}", flush=True)


def build_caption(title: str, desc: str, topics: str, limit: int = 2200) -> str:
    """Build one caption while keeping topic order and removing duplicates."""
    parts = []
    title = (title or "").strip()
    desc = (desc or "").strip()
    if title:
        parts.append(title)
    if desc and desc != title:
        parts.append(desc)
    tags, seen = [], set()
    for raw in (topics or "").replace("，", ",").split(","):
        tag = raw.strip().lstrip("#").strip()
        key = tag.casefold()
        if tag and key not in seen:
            seen.add(key)
            tags.append(f"#{tag}")
    if tags:
        parts.append(" ".join(tags))
    return "\n".join(parts).strip()[:limit]


def _video_file(media_type: str, media_paths: Iterable[str]) -> Tuple[str, str]:
    if media_type != "video":
        return "", "TikTok 网页发布当前仅支持单个视频"
    files = [str(Path(path).resolve()) for path in media_paths
             if path and Path(path).is_file()]
    if not files:
        return "", "没有可用的本地视频文件(路径不存在)"
    return files[0], ""


async def _dump(page, tag: str) -> str:
    try:
        _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = _DEBUG_DIR / f"tiktok_publish_{tag}_{stamp}"
        png = str(base.with_suffix(".png"))
        try:
            await page.screenshot(path=png, full_page=True)
        except Exception:
            png = ""
        try:
            text = await page.locator("body").inner_text()
        except Exception:
            text = ""
        base.with_suffix(".txt").write_text(
            f"url: {page.url}\n\n{text[:6000]}", encoding="utf-8")
        return png
    except Exception as exc:
        _log(f"保存诊断快照失败: {exc!r}")
        return ""


async def _visible_text(page, values) -> str:
    for value in values:
        try:
            candidates = page.get_by_text(value, exact=False)
            for index in range(await candidates.count()):
                if await candidates.nth(index).is_visible():
                    return value
        except Exception:
            continue
    return ""


async def _first_visible(page, selectors, timeout=1500):
    for selector in selectors:
        try:
            candidates = page.locator(selector)
            for index in range(await candidates.count()):
                candidate = candidates.nth(index)
                await candidate.wait_for(state="visible", timeout=timeout)
                if await candidate.is_visible():
                    return candidate
        except Exception:
            continue
    return None


async def _first_attached(page, selectors):
    """Return an attached control even when the page intentionally hides it."""
    for selector in selectors:
        try:
            candidates = page.locator(selector)
            for index in range(await candidates.count()):
                candidate = candidates.nth(index)
                try:
                    await candidate.wait_for(state="attached", timeout=500)
                except Exception:
                    continue
                if await candidate.get_attribute("disabled") is None:
                    return candidate
        except Exception:
            continue
    return None


def _page_state(url: str, body_text: str) -> Tuple[str, str]:
    current = (url or "").lower()
    text = (body_text or "").lower()
    if ("/login" in current or "log in to tiktok" in text
            or "登录 tiktok" in text):
        return "logged_out", ""
    for value in _VERIFY_TEXT:
        if value.lower() in text:
            return "verification", value
    for value in _REJECT_TEXT:
        if value.lower() in text:
            return "rejected", value
    return "loading", ""


async def _wait_for_upload_input(page, timeout_seconds: int):
    """Wait through the Studio shell until its hidden file input is attached."""
    deadline = asyncio.get_running_loop().time() + max(1, timeout_seconds)
    state, detail = "loading", ""
    while asyncio.get_running_loop().time() < deadline:
        upload = await _first_attached(page, _VIDEO_INPUTS)
        if upload is not None:
            return upload, "ready", ""
        try:
            body_text = await page.locator("body").inner_text(timeout=1500)
        except Exception:
            body_text = ""
        state, detail = _page_state(str(page.url or ""), body_text)
        if state != "loading":
            return None, state, detail
        await page.wait_for_timeout(750)
    return None, state, detail


async def _open_upload_page(page, timeout_seconds: int):
    """Open Studio with one route fallback and wait for the SPA to become ready."""
    total = max(30, int(timeout_seconds))
    primary_wait = max(20, min(60, total - 20))
    waits = (primary_wait, max(20, total - primary_wait))
    last_state, last_detail = "loading", ""
    for index, url in enumerate(PUBLISH_URLS):
        try:
            await page.goto(
                url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            _log(f"发布页导航尚未完成，继续等待页面控件: {exc!r}")
        upload, state, detail = await _wait_for_upload_input(
            page, waits[min(index, len(waits) - 1)])
        if upload is not None:
            return upload, "ready", ""
        last_state, last_detail = state, detail
        if state in {"logged_out", "verification", "rejected"}:
            break
        _log("当前发布入口持续加载，切换备用 Studio 入口")
    return None, last_state, last_detail


async def _set_caption(page, text: str) -> bool:
    if not text:
        return True
    editor = await _first_visible(page, _CAPTION_SELECTORS, timeout=4000)
    if editor is None:
        return False
    try:
        await editor.click(timeout=3000)
        try:
            await editor.fill(text, timeout=5000)
        except Exception:
            await page.keyboard.press("Control+A")
            await page.keyboard.press("Backspace")
            await page.keyboard.insert_text(text)
        return True
    except Exception:
        return False


async def _dismiss_upload_dialogs(page) -> int:
    """Dismiss optional Studio onboarding without changing account settings."""
    dismissed = 0
    for _ in range(len(_DISMISS_DIALOG_ACTIONS)):
        clicked = False
        for marker, action in _DISMISS_DIALOG_ACTIONS:
            try:
                markers = page.get_by_text(marker, exact=False)
                for marker_index in range(await markers.count()):
                    visible_marker = markers.nth(marker_index)
                    if not await visible_marker.is_visible():
                        continue
                    container = visible_marker.locator(
                        "xpath=ancestor::*[(self::div or self::section) and .//button][1]")
                    if not await container.count():
                        continue
                    buttons = container.get_by_role(
                        "button", name=action, exact=True)
                    for button_index in range(await buttons.count()):
                        button = buttons.nth(button_index)
                        if not await button.is_visible():
                            continue
                        await button.click(timeout=2500)
                        await page.wait_for_timeout(400)
                        dismissed += 1
                        clicked = True
                        break
                    if clicked:
                        break
            except Exception:
                continue
            if clicked:
                break
        if not clicked:
            break
    return dismissed


async def _privacy_button(page):
    for label in ("Who can watch this video", "Who can view this post",
                  "Who can view", "谁可以看"):
        try:
            markers = page.get_by_text(label, exact=True)
            for index in range(await markers.count()):
                marker = markers.nth(index)
                if not await marker.is_visible():
                    continue
                container = marker.locator(
                    "xpath=ancestor::*[.//*[@role='combobox']][1]")
                if not await container.count():
                    continue
                controls = container.locator('[role="combobox"]')
                for control_index in range(await controls.count()):
                    control = controls.nth(control_index)
                    if await control.is_visible():
                        return control
        except Exception:
            continue
    selectors = (
        '[data-e2e="video_visibility_container"] [role="combobox"]',
        '[data-e2e="privacy-level"] [role="combobox"]',
        'button[role="combobox"][aria-label*="view" i]',
        'button[role="combobox"][aria-label*="privacy" i]',
        '[role="combobox"]',
    )
    return await _first_visible(page, selectors)


async def _set_visibility(page, visibility: str) -> bool:
    """Non-public intent is strict; public may keep TikTok's default."""
    if visibility == "public":
        return True
    labels = {
        "friends": ("Friends", "Mutual followers", "好友", "好友可见",
                    "互相关注的朋友", "互相关注的好友"),
        "private": ("Only you", "Private", "仅自己可见", "仅你可见",
                    "仅限自己", "仅自己", "私密"),
    }.get(visibility, ())
    button = await _privacy_button(page)
    if button is None or not labels:
        return False
    try:
        await button.click(timeout=3000)
        await page.wait_for_timeout(400)
        for label in labels:
            for role in ("option", "radio", "menuitem"):
                options = page.get_by_role(role, name=label, exact=False)
                for index in range(await options.count()):
                    option = options.nth(index)
                    if await option.is_visible():
                        await option.click(timeout=3000)
                        return True
            options = page.get_by_text(label, exact=False)
            for index in range(await options.count()):
                option = options.nth(index)
                if await option.is_visible():
                    await option.click(timeout=3000)
                    return True
    except Exception:
        pass
    return False


async def _set_toggle(page, labels, desired: bool) -> bool:
    for label in labels:
        for role in ("checkbox", "switch"):
            try:
                options = page.get_by_role(role, name=label, exact=False)
                for index in range(await options.count()):
                    option = options.nth(index)
                    if not await option.is_visible():
                        continue
                    try:
                        checked = await option.is_checked()
                    except Exception:
                        checked = await option.get_attribute("aria-checked") == "true"
                    if checked != desired:
                        await option.click(timeout=2500)
                    return True
            except Exception:
                continue
    # TikTok defaults these controls to enabled. Missing controls are only an
    # error when the requested state differs from that default.
    return desired


async def _apply_interactions(page, allow_comment: bool, allow_duet: bool,
                              allow_stitch: bool) -> str:
    settings = (
        (("Comment", "Allow comments", "评论", "允许评论"), allow_comment, "评论"),
        (("Duet", "Allow Duet", "合拍", "允许合拍"), allow_duet, "Duet"),
        (("Stitch", "Allow Stitch", "拼接", "允许拼接"), allow_stitch, "Stitch"),
    )
    missing = []
    for labels, desired, display in settings:
        if not await _set_toggle(page, labels, desired):
            missing.append(display)
    return "、".join(missing)


async def _publish_button(page):
    selectors = (
        'button[data-e2e="post_video_button"]',
        'button[data-e2e="publish-button"]',
        'button[type="submit"]',
        'button',
    )
    for selector in selectors:
        try:
            candidates = page.locator(selector)
            for index in range(await candidates.count() - 1, -1, -1):
                candidate = candidates.nth(index)
                if not await candidate.is_visible():
                    continue
                text = ((await candidate.inner_text()) or "").strip().lower()
                data_e2e = (await candidate.get_attribute("data-e2e") or "").lower()
                if ("post_video_button" not in data_e2e
                        and "publish-button" not in data_e2e
                        and text not in {"post", "publish", "post now", "发布", "立即发布"}):
                    continue
                if await candidate.get_attribute("aria-disabled") == "true":
                    continue
                try:
                    if not await candidate.is_enabled():
                        continue
                except Exception:
                    pass
                return candidate
        except Exception:
            continue
    return None


async def _notify_submit(callback: Callable[[], object] | None) -> None:
    from app.licensing import require_active
    require_active()
    if callback is None:
        return
    result = callback()
    if inspect.isawaitable(result):
        await result


async def publish_tiktok(
        mgr: BrowserManager, identity: Identity, storage_state_json: str,
        media_type: str, title: str, desc: str, media_paths: List[str],
        topics: str = "", visibility: str = "public",
        allow_comment: bool = True, allow_duet: bool = True,
        allow_stitch: bool = True, headed: bool = True,
        timeout_seconds: int = 180,
        on_submit: Callable[[], object | Awaitable[object]] | None = None,
        ) -> Tuple[bool, str, str]:
    """Publish one video and return ``(ok, result_url, error)``."""
    _ = storage_state_json, headed
    video, validation_error = _video_file(media_type, media_paths)
    if validation_error:
        return False, "", validation_error
    caption = build_caption(title, desc, topics)

    ctx = await mgr.open_headed(identity)
    page = await ctx.new_page()
    clicked = False
    try:
        upload, page_state, page_detail = await _open_upload_page(
            page, min(UPLOAD_PAGE_TIMEOUT_SECONDS, max(30, int(timeout_seconds))))
        if page_state == "logged_out":
            await _dump(page, "logged-out")
            return False, "", "logged_out:TikTok 登录态已失效，请先重新登录"
        if page_state == "verification":
            png = await _dump(page, "verification")
            return False, "", ("TikTok 发布页要求安全验证，请在账号浏览器完成验证后重试。"
                                f"页面提示:{page_detail}；诊断截图:{png or 'data/debug'}")
        if page_state == "rejected":
            await _dump(page, "upload-rejected")
            return False, "", f"TikTok 发布页拒绝继续:{page_detail}"
        if upload is None:
            png = await _dump(page, "no-upload-input")
            return False, "", ("TikTok Studio 持续加载，等待后仍未出现视频上传入口；"
                                "请检查该账号代理访问 TikTok Studio 是否完整。"
                                f"诊断截图:{png or 'data/debug'}")
        await upload.set_input_files(video, timeout=15000)

        editor = None
        deadline = asyncio.get_running_loop().time() + 90
        while asyncio.get_running_loop().time() < deadline:
            editor = await _first_visible(page, _CAPTION_SELECTORS, timeout=500)
            if editor is not None:
                break
            rejected = await _visible_text(page, _REJECT_TEXT)
            if rejected:
                await _dump(page, "upload-rejected")
                return False, "", f"TikTok 拒绝上传:{rejected}"
            await page.wait_for_timeout(750)
        if editor is None:
            png = await _dump(page, "no-editor")
            return False, "", ("视频已选择，但上传后未进入编辑状态；请核对格式、时长或转码状态。"
                                f"诊断截图:{png or 'data/debug'}")

        dismissed = await _dismiss_upload_dialogs(page)
        if dismissed:
            _log(f"已关闭 {dismissed} 个发布页引导弹窗")
        if not await _set_caption(page, caption):
            await _dump(page, "caption")
            return False, "", "已上传视频，但未找到 TikTok 说明输入框"
        if not await _set_visibility(page, visibility):
            png = await _dump(page, "visibility")
            return False, "", ("未能设置 TikTok 可见范围，已停止提交以保留你的权限选择。"
                                f"诊断截图:{png or 'data/debug'}")
        missing = await _apply_interactions(
            page, allow_comment, allow_duet, allow_stitch)
        if missing:
            return False, "", f"未能设置 TikTok 互动权限:{missing}，已停止提交"

        button = None
        deadline = asyncio.get_running_loop().time() + 150
        while asyncio.get_running_loop().time() < deadline:
            button = await _publish_button(page)
            if button is not None:
                break
            rejected = await _visible_text(page, _REJECT_TEXT)
            if rejected:
                return False, "", f"TikTok 拒绝上传:{rejected}"
            await page.wait_for_timeout(1000)
        if button is None:
            png = await _dump(page, "not-ready")
            return False, "", ("视频转码后仍未出现可用的发布按钮。"
                                f"诊断截图:{png or 'data/debug'}")

        await button.scroll_into_view_if_needed(timeout=3000)
        await _notify_submit(on_submit)
        clicked = True
        await button.click(timeout=5000)
        _log("已点击发布，等待平台确认")

        deadline = asyncio.get_running_loop().time() + max(90, int(timeout_seconds))
        while asyncio.get_running_loop().time() < deadline:
            current = str(page.url or "").lower()
            if any(part in current for part in (
                    "/tiktokstudio/content", "/creator-center/content",
                    "/creator-center/manage")):
                return True, str(page.url or MANAGE_URL), ""
            success = await _visible_text(page, _SUCCESS_TEXT)
            if success:
                return True, MANAGE_URL, ""
            rejected = await _visible_text(page, _REJECT_TEXT)
            if rejected:
                return False, "", f"write_uncertain:TikTok 已接收发布操作，但页面显示:{rejected}"
            verification = await _visible_text(page, _VERIFY_TEXT)
            if verification:
                _log(f"等待用户在可见窗口完成“{verification}”")
            await page.wait_for_timeout(1000)

        png = await _dump(page, "unconfirmed")
        return False, "", ("write_uncertain:已点击 TikTok 发布，但未收到最终确认；"
                            "请先在 TikTok Studio 核对，系统不会自动重发。"
                            f"诊断截图:{png or 'data/debug'}")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        try:
            await _dump(page, "exception")
        except Exception:
            pass
        prefix = "write_uncertain:" if clicked else ""
        return False, "", f"{prefix}TikTok 发布异常:{exc!r}"
    finally:
        try:
            await ctx.close()
        except Exception:
            pass
