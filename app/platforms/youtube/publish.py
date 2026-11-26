"""YouTube Studio 网页端原生视频与 Shorts 发布器。

核心能力：
- 支持可选多频道切换 (`channel_name` 经 `/channel_switcher` 切换目标品牌频道)；
- 支持长视频 (`video`) 与短视频 (`shorts`，自动补全 `#Shorts` 标签)；
- 支持标题(≤100字)、简介与话题、儿童内容声明 (`made_for_kids`)、自定义封面上传 (`thumbnail_path`)、
  播放列表选择/自动新建 (`playlists`)、视频标签 (`tags`)、付费推广声明、嵌入与订阅通知开关、
  可见性 (`public` / `unlisted` / `private`) 与首映 (`premiere`)；
- 发布成功后支持自动追加首评 (`first_comment`)；
- 仅在最终点击 `#done-button` 发布提交时触发 `on_submit`，遇风控/验证立即返回 `verification_required:`，绝不自动绕过验证码。
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ...browser.human_interaction import DEFAULT_HUMAN_ENGINE, human_wait
from ...browser.identity import Identity
from ...browser.manager import BrowserManager
from .._submit import notify_stage as _notify_stage
from .protocol import has_youtube_login_cookies, normalize_yt_publish_options
from .resolve import normalize_youtube_url
from .selectors import (
    AUTH_BLOCKED_URL_TOKENS,
    AVATAR_BUTTON_SELECTORS,
    CREATE_CHANNEL_DIALOG_SELECTORS,
    DISMISS_BUTTON_SELECTORS,
    STUDIO_ALLOW_EMBED_CHECKBOX,
    STUDIO_DESC_INPUT_SELECTORS,
    STUDIO_DETAILS_EDITOR_SELECTORS,
    STUDIO_DIALOG_CLOSE_SELECTORS,
    STUDIO_DONE_BUTTON,
    STUDIO_FILE_INPUT_SELECTORS,
    STUDIO_MADE_FOR_KIDS_NO,
    STUDIO_MADE_FOR_KIDS_YES,
    STUDIO_NEXT_BUTTON,
    STUDIO_NOTIFY_SUBSCRIBERS_CHECKBOX,
    STUDIO_PAID_PROMOTION_CHECKBOX,
    STUDIO_PLAYLIST_CHECKBOXES,
    STUDIO_PLAYLIST_DONE_BUTTON,
    STUDIO_PLAYLIST_DROPDOWN,
    STUDIO_PLAYLIST_ITEMS,
    STUDIO_PREMIERE_CHECKBOX,
    STUDIO_SELECT_FILES_SELECTORS,
    STUDIO_SHARE_DIALOG_SELECTORS,
    STUDIO_SHOW_MORE_BUTTON,
    STUDIO_STEP_CHECKS,
    STUDIO_STEP_REVIEW,
    STUDIO_TAGS_INPUT,
    STUDIO_THUMBNAIL_FILE_INPUT,
    STUDIO_TITLE_INPUT_SELECTORS,
    STUDIO_UPLOAD_TRIGGER_SELECTORS,
    STUDIO_VIDEO_LINK_SELECTORS,
    STUDIO_VISIBILITY_RADIO,
)
from .urls import CHANNEL_SWITCHER_NEXT_URL, STUDIO_CHANNEL_URL, UPLOAD_URL

VIDEO_EXTS = frozenset({".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv", ".flv", ".wmv", ".mpeg", ".mpg"})
IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp"})
MAX_YOUTUBE_TITLE_LEN = 100
MAX_YOUTUBE_DESC_LEN = 5000


def validate_youtube_publish_files(
    media_type: str,
    files: Sequence[str],
    *,
    thumbnail_path: str = "",
) -> Tuple[List[Path], Optional[Path], str]:
    """校验 YouTube 发布本地媒体文件：必须为 1 个视频文件，可选附带 1 张自定义封面图。"""
    video_paths: List[Path] = []
    thumb: Optional[Path] = None

    for raw in files or []:
        if not raw:
            continue
        p = Path(raw)
        if not p.is_file():
            return [], None, f"本地媒体文件不存在: {raw}"
        if p.stat().st_size <= 0:
            return [], None, f"本地媒体文件为空: {p.name}"
        ext = p.suffix.lower()
        if ext in VIDEO_EXTS:
            video_paths.append(p)
        elif ext in IMAGE_EXTS and thumb is None:
            thumb = p
        else:
            return [], None, f"YouTube 不支持该媒体格式: {p.suffix}"

    if thumbnail_path:
        tp = Path(thumbnail_path)
        if not tp.is_file():
            return [], None, f"自定义缩略图文件不存在: {thumbnail_path}"
        if tp.suffix.lower() not in IMAGE_EXTS:
            return [], None, f"缩略图仅支持 JPG/PNG/WEBP 格式: {tp.suffix}"
        thumb = tp

    if not video_paths:
        return [], None, "YouTube 发布必须上传 1 个本地视频文件（支持 MP4 / MOV / MKV / WEBM）"
    if len(video_paths) > 1:
        return [], None, "YouTube 单次发布仅支持 1 个视频文件"
    return video_paths, thumb, ""


def build_youtube_metadata(
    title: str,
    desc: str,
    topics: str = "",
    *,
    post_kind: str = "video",
) -> Tuple[str, str, List[str]]:
    """构造 YouTube 标题 (≤100字)、简介 (≤5000字) 与标签列表。"""
    raw_title = str(title or "").strip()
    raw_desc = str(desc or "").strip()

    tags: List[str] = []
    seen_low: set[str] = set()
    for chunk in str(topics or "").replace("，", ",").replace("#", " ").split():
        for piece in chunk.split(","):
            w = piece.strip().lstrip("#")
            if w and w.lower() not in seen_low:
                seen_low.add(w.lower())
                tags.append(w)

    if not raw_title and raw_desc:
        first_line = raw_desc.splitlines()[0].strip()
        raw_title = first_line[:MAX_YOUTUBE_TITLE_LEN]

    if post_kind == "shorts":
        if "#shorts" not in raw_title.lower() and "#shorts" not in raw_desc.lower():
            if "shorts" not in seen_low:
                tags.insert(0, "Shorts")
                seen_low.add("shorts")

    final_title = raw_title[:MAX_YOUTUBE_TITLE_LEN].strip()
    desc_parts: List[str] = []
    if raw_desc:
        desc_parts.append(raw_desc)
    hashtags = [f"#{t}" for t in tags if f"#{t.lower()}" not in raw_desc.lower()]
    if hashtags:
        desc_parts.append(" ".join(hashtags[:15]))
    final_desc = "\n\n".join(p for p in desc_parts if p).strip()[:MAX_YOUTUBE_DESC_LEN]
    return final_title, final_desc, tags


async def _find_first_visible(page, selectors: Sequence[str], timeout_ms: int = 8000):
    deadline = asyncio.get_running_loop().time() + max(0.5, timeout_ms / 1000.0)
    while asyncio.get_running_loop().time() < deadline:
        for sel in selectors:
            loc = page.locator(sel).first
            try:
                if await loc.count() and await loc.is_visible():
                    return loc
            except Exception:
                continue
        await human_wait(page, 350)
    return None


async def _set_contenteditable_text(page, locator, text: str) -> None:
    await DEFAULT_HUMAN_ENGINE.click_locator(locator, page=page, timeout=5000)
    await human_wait(page, 250)
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Backspace")
    await human_wait(page, 180)
    try:
        await page.keyboard.insert_text(text)
    except Exception:
        await locator.evaluate(
            """(el, val) => {
                el.focus();
                document.execCommand('selectAll', false, null);
                document.execCommand('delete', false, null);
                document.execCommand('insertText', false, val);
                el.dispatchEvent(new Event('input', { bubbles: true }));
            }""",
            text,
        )
    await human_wait(page, 300)


async def _switch_channel_on_page(page, channel_name: str) -> Tuple[bool, str]:
    """经 `https://www.youtube.com/channel_switcher` 切换当前活动频道（兼容品牌名或 `@handle`）。"""
    target = str(channel_name or "").strip()
    if not target:
        return True, ""
    await page.goto(CHANNEL_SWITCHER_NEXT_URL, wait_until="domcontentloaded", timeout=40000)
    await human_wait(page, 1800)

    cur_url = str(page.url or "").lower()
    if any(tok in cur_url for tok in AUTH_BLOCKED_URL_TOKENS):
        return False, "logged_out:YouTube 登录态失效或触发 Google 安全验证"

    # 等待频道项出现
    for _ in range(15):
        cnt = await page.locator("ytd-account-item-renderer").count()
        if cnt > 0:
            break
        await human_wait(page, 600)

    target_low = target.lower().lstrip("@")
    result = await page.evaluate(
        """(needle) => {
            const items = Array.from(document.querySelectorAll('ytd-account-item-renderer'));
            for (const item of items) {
                const txt = (item.innerText || '').toLowerCase();
                const norm = txt.replace(/@/g, '');
                if (txt.includes(needle) || norm.includes(needle)) {
                    const selected = item.querySelector('#selected:not([hidden])');
                    if (selected) return { matched: true, already: true };
                    item.click();
                    return { matched: true, already: false };
                }
            }
            return { matched: false, already: false, total: items.length };
        }""",
        target_low,
    )
    if not isinstance(result, dict) or not result.get("matched"):
        return False, f"未在 channel_switcher 中找到目标频道: {target}"
    if not result.get("already"):
        await human_wait(page, 3000)
    return True, ""


async def _dismiss_onboarding_dialogs(page) -> None:
    for _ in range(3):
        dismissed = False
        for sel in CREATE_CHANNEL_DIALOG_SELECTORS:
            loc = page.locator(sel).first
            try:
                if await loc.count() and await loc.is_visible():
                    btn = page.locator("ytd-button-renderer#create-channel-button, #create-channel-button button").first
                    if await btn.count() and await btn.is_visible():
                        await DEFAULT_HUMAN_ENGINE.click_locator(btn, page=page, timeout=3000)
                        await human_wait(page, 2000)
                        dismissed = True
                        break
            except Exception:
                continue
        for sel in DISMISS_BUTTON_SELECTORS:
            loc = page.locator(sel).first
            try:
                if await loc.count() and await loc.is_visible():
                    await DEFAULT_HUMAN_ENGINE.click_locator(loc, page=page, timeout=2000)
                    await human_wait(page, 800)
                    dismissed = True
                    break
            except Exception:
                continue
        if not dismissed:
            break


async def _apply_playlists(page, playlists: Sequence[str]) -> None:
    if not playlists:
        return
    trigger = page.locator(STUDIO_PLAYLIST_DROPDOWN).first
    try:
        if not (await trigger.count() and await trigger.is_visible()):
            return
        await trigger.scroll_into_view_if_needed(timeout=3000)
        await DEFAULT_HUMAN_ENGINE.click_locator(trigger, page=page, timeout=4000)
        await human_wait(page, 1200)

        for name in playlists:
            want = str(name or "").strip()
            if not want:
                continue
            existing: List[str] = await page.evaluate(
                """(sel) => Array.from(document.querySelectorAll(sel))
                    .map(el => (el.innerText || '').replace(/[\\r\\n\\t]+/g, ' ').trim())
                    .filter(Boolean)""",
                STUDIO_PLAYLIST_ITEMS,
            )
            idx = next((i for i, v in enumerate(existing) if v.lower() == want.lower()), -1)
            if idx >= 0:
                await page.evaluate(
                    """({sel, idx}) => {
                        const boxes = document.querySelectorAll(sel);
                        if (boxes[idx] && !boxes[idx].checked) boxes[idx].click();
                    }""",
                    {"sel": STUDIO_PLAYLIST_CHECKBOXES, "idx": idx},
                )
                await human_wait(page, 600)
                continue

            # 若播放列表不存在，自动创建
            new_btn = page.locator(".new-playlist-button").first
            if await new_btn.count() and await new_btn.is_visible():
                await DEFAULT_HUMAN_ENGINE.click_locator(new_btn, page=page, timeout=3000)
                await human_wait(page, 800)
                menu_item = page.locator("tp-yt-paper-item[test-id='new_playlist']").first
                if await menu_item.count() and await menu_item.is_visible():
                    await DEFAULT_HUMAN_ENGINE.click_locator(menu_item, page=page, timeout=3000)
                    await human_wait(page, 800)
                pl_input = page.locator(".ytcp-playlist-creation-dialog #title-textarea #textbox").first
                if await pl_input.count() and await pl_input.is_visible():
                    await _set_contenteditable_text(page, pl_input, want)
                    create_btn = page.locator(".ytcp-playlist-creation-dialog #create-button, #create-button").first
                    if await create_btn.count() and await create_btn.is_visible():
                        await DEFAULT_HUMAN_ENGINE.click_locator(create_btn, page=page, timeout=3000)
                        await human_wait(page, 1500)

        done_btn = page.locator(STUDIO_PLAYLIST_DONE_BUTTON).first
        if await done_btn.count() and await done_btn.is_visible():
            await DEFAULT_HUMAN_ENGINE.click_locator(done_btn, page=page, timeout=3000)
            await human_wait(page, 800)
    except Exception:
        pass


async def _set_checkbox_state(page, selector: str, desired: bool) -> None:
    try:
        loc = page.locator(selector).first
        if not await loc.count():
            return
        cur_attr = await loc.get_attribute("aria-checked")
        if cur_attr is None:
            is_checked = await loc.evaluate("el => Boolean(el.checked)")
        else:
            is_checked = str(cur_attr).lower() == "true"
        if bool(is_checked) != bool(desired):
            await loc.scroll_into_view_if_needed(timeout=3000)
            await DEFAULT_HUMAN_ENGINE.click_locator(loc, page=page, timeout=3000)
            await human_wait(page, 400)
    except Exception:
        pass


async def _read_studio_video_url(page) -> str:
    for sel in STUDIO_VIDEO_LINK_SELECTORS:
        loc = page.locator(sel).first
        try:
            if await loc.count():
                href = await loc.get_attribute("href")
                if href and ("youtu.be/" in href or "youtube.com/" in href):
                    return normalize_youtube_url(href)
        except Exception:
            continue
    return ""


async def publish_youtube(
    mgr: BrowserManager,
    identity: Identity,
    *,
    media_type: str = "video",
    files: Sequence[str],
    title: str = "",
    desc: str = "",
    topics: str = "",
    visibility: str = "public",
    cover_path: str = "",
    save_as_draft: bool = False,
    yt_options: Optional[Mapping[str, Any]] = None,
    headed: bool = True,
    on_submit: Optional[Callable[[], Any]] = None,
    on_stage: Optional[Callable[[str], Any]] = None,
    upload_timeout_seconds: int = 180,
) -> Tuple[bool, str, str]:
    """在账号隔离浏览器中通过 YouTube Studio 上传并发布视频 / Shorts（支持自定义缩略图与自动存 Studio 草稿）。

    返回 `(ok, published_url, error)`。
    """
    opts = normalize_yt_publish_options(yt_options)
    effective_vis = opts["visibility"]
    if visibility in {"public", "unlisted", "private"} and not (yt_options or {}).get("visibility") and not (yt_options or {}).get("publish_as_private"):
        effective_vis = visibility

    eff_thumb = str(cover_path or opts["thumbnail_path"] or "").strip()
    video_paths, thumb_path, val_err = validate_youtube_publish_files(
        media_type,
        files,
        thumbnail_path=eff_thumb,
    )
    if val_err:
        return False, "", val_err

    has_state = any(
        has_youtube_login_cookies(st)
        for st in (getattr(identity, "bridge_states", None) or ())
    )
    has_profile = bool(getattr(identity, "profile_dir", "") and Path(identity.profile_dir).is_dir())
    if not has_state and not has_profile:
        return False, "", "logged_out:YouTube 账号缺少登录态，请先登录"

    final_title, final_desc, merged_tags = build_youtube_metadata(
        title,
        desc,
        topics,
        post_kind=opts["post_kind"],
    )
    for t in opts["tags"]:
        if t and t.lower() not in {x.lower() for x in merged_tags}:
            merged_tags.append(t)

    submit_notified = False

    async def _fire_on_submit() -> None:
        nonlocal submit_notified
        if submit_notified or on_submit is None:
            return
        submit_notified = True
        res = on_submit()
        if inspect.isawaitable(res):
            await res

    from ...browser.youtube_fetcher import open_youtube_page
    async with open_youtube_page(mgr, identity, headed=headed) as page:
        await _notify_stage(on_stage, "正在进入 YouTube Studio 上传界面...")

        # 1) 如指定多频道上下文，先切频道
        if opts["channel_name"]:
            sw_ok, sw_err = await _switch_channel_on_page(page, opts["channel_name"])
            if not sw_ok:
                return False, "", sw_err
            await page.goto(STUDIO_CHANNEL_URL, wait_until="domcontentloaded", timeout=45000)
            await human_wait(page, 2200)
            await _dismiss_onboarding_dialogs(page)
            trig = await _find_first_visible(page, STUDIO_UPLOAD_TRIGGER_SELECTORS, timeout_ms=10000)
            if trig is not None:
                await DEFAULT_HUMAN_ENGINE.click_locator(trig, page=page, timeout=4000)
                await human_wait(page, 1200)
            else:
                await page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=45000)
        else:
            await page.goto(UPLOAD_URL, wait_until="domcontentloaded", timeout=45000)

        await human_wait(page, 2200)
        cur_url = str(page.url or "").lower()
        if "accounts.google.com" in cur_url:
            return False, "", "logged_out:YouTube Studio 跳转登录页，登录态已失效"
        if "youtube.com/oops" in cur_url or "/disabled/" in cur_url:
            return False, "", "verification_required:YouTube 账号受限或被停用"

        await _dismiss_onboarding_dialogs(page)

        # 2) 等待文件选择器并注入本地视频文件
        await _notify_stage(on_stage, f"正在上传视频文件 {video_paths[0].name}...")
        picker_ready = await _find_first_visible(page, STUDIO_SELECT_FILES_SELECTORS, timeout_ms=25000)
        file_input = None
        for sel in STUDIO_FILE_INPUT_SELECTORS:
            loc = page.locator(sel).first
            try:
                if await loc.count():
                    file_input = loc
                    break
            except Exception:
                continue
        if file_input is None and picker_ready is None:
            return False, "", "未找到 YouTube Studio 视频上传入口，请检查是否已创建频道或登录失效"
        if file_input is None:
            file_input = page.locator('input[type="file"]').first

        await file_input.set_input_files(str(video_paths[0]))
        await human_wait(page, 2500)

        # 3) 等待元数据编辑器出现
        details_editor = await _find_first_visible(page, STUDIO_DETAILS_EDITOR_SELECTORS, timeout_ms=35000)
        if details_editor is None:
            return False, "", "上传文件后未进入 YouTube Studio 详情编辑页"

        await _notify_stage(on_stage, "正在填写标题、简介、观众与高级属性...")
        # 标题
        if final_title:
            title_box = await _find_first_visible(page, STUDIO_TITLE_INPUT_SELECTORS, timeout_ms=10000)
            if title_box is not None:
                await _set_contenteditable_text(page, title_box, final_title)

        # 简介
        if final_desc:
            desc_box = await _find_first_visible(page, STUDIO_DESC_INPUT_SELECTORS, timeout_ms=8000)
            if desc_box is not None:
                await _set_contenteditable_text(page, desc_box, final_desc)

        # 自定义缩略图（如提供）
        if thumb_path is not None:
            await _notify_stage(on_stage, "正在上传 YouTube 自定义封面缩略图...")
            thumb_uploaded = False
            try:
                thumb_input = page.locator(STUDIO_THUMBNAIL_FILE_INPUT).first
                if await thumb_input.count():
                    await thumb_input.set_input_files(str(thumb_path))
                    await human_wait(page, 2200)
                    thumb_uploaded = True
            except Exception as exc:
                return False, "", f"YouTube 自定义封面上传失败({exc!r})，已停止提交（注：频道需完成手机验证才可使用自定义缩略图）"
            if not thumb_uploaded:
                return False, "", "未找到 YouTube Studio 自定义缩略图上传控件（请确认该频道已开通自定义缩略图权限）"

        # 播放列表
        if opts["playlists"]:
            await _apply_playlists(page, opts["playlists"])

        # 观众设置（是否面向儿童）
        kids_sel = STUDIO_MADE_FOR_KIDS_YES if opts["made_for_kids"] else STUDIO_MADE_FOR_KIDS_NO
        kids_radio = page.locator(kids_sel).first
        if await kids_radio.count():
            try:
                await kids_radio.scroll_into_view_if_needed(timeout=3000)
                await DEFAULT_HUMAN_ENGINE.click_locator(kids_radio, page=page, timeout=4000)
                await human_wait(page, 600)
            except Exception:
                await page.evaluate("(s) => { const el = document.querySelector(s); if (el) el.click(); }", kids_sel)

        # 展开“展开更多”填写标签与高级开关
        need_more = bool(
            merged_tags
            or opts["paid_promotion"]
            or not opts["allow_embedding"]
            or not opts["notify_subscribers"]
            or opts["category"]
            or opts["language"]
        )
        if need_more:
            more_btn = page.locator(STUDIO_SHOW_MORE_BUTTON).first
            if await more_btn.count() and await more_btn.is_visible():
                try:
                    await more_btn.scroll_into_view_if_needed(timeout=3000)
                    await DEFAULT_HUMAN_ENGINE.click_locator(more_btn, page=page, timeout=3000)
                    await human_wait(page, 1000)
                except Exception:
                    pass

            if opts["paid_promotion"]:
                await _set_checkbox_state(page, STUDIO_PAID_PROMOTION_CHECKBOX, True)

            if merged_tags:
                tag_input = page.locator(STUDIO_TAGS_INPUT).first
                if await tag_input.count():
                    try:
                        await tag_input.scroll_into_view_if_needed(timeout=3000)
                        tag_str = ",".join(merged_tags[:30]) + ","
                        await tag_input.fill(tag_str)
                        await page.keyboard.press("Enter")
                        await human_wait(page, 600)
                    except Exception:
                        pass

            await _set_checkbox_state(page, STUDIO_ALLOW_EMBED_CHECKBOX, opts["allow_embedding"])
            await _set_checkbox_state(page, STUDIO_NOTIFY_SUBSCRIBERS_CHECKBOX, opts["notify_subscribers"])

            if opts["category"]:
                try:
                    cat_dd = page.locator("#category-container ytcp-dropdown-trigger").first
                    if await cat_dd.count():
                        await cat_dd.scroll_into_view_if_needed(timeout=3000)
                        await DEFAULT_HUMAN_ENGINE.click_locator(cat_dd, page=page, timeout=3000)
                        await human_wait(page, 600)
                        item = page.locator(f".tp-yt-paper-item.ytcp-text-menu[test-id='{opts['category']}']").first
                        if await item.count():
                            await DEFAULT_HUMAN_ENGINE.click_locator(item, page=page, timeout=3000)
                except Exception:
                    pass

        # 4) 切到检查页并等待视频链接与上传进度
        await _notify_stage(on_stage, "正在等待视频上传与版权检查...")
        video_link = await _read_studio_video_url(page)

        checks_tab = page.locator(STUDIO_STEP_CHECKS).first
        if await checks_tab.count() and await checks_tab.is_visible():
            await DEFAULT_HUMAN_ENGINE.click_locator(checks_tab, page=page, timeout=4000)
            await human_wait(page, 1200)

        # 等待视频上传完成（最久 upload_timeout_seconds 秒）
        deadline = asyncio.get_running_loop().time() + max(30, int(upload_timeout_seconds))
        while asyncio.get_running_loop().time() < deadline:
            if not video_link:
                video_link = await _read_studio_video_url(page)
            checks_done = await page.locator(
                'ytcp-video-upload-progress[checks-summary-status-v2="UPLOAD_CHECKS_DATA_SUMMARY_STATUS_COMPLETED"]'
            ).count()
            progress_text = await page.evaluate(
                """() => {
                    const el = document.querySelector('ytcp-video-upload-progress .progress-label');
                    return el ? (el.innerText || '') : '';
                }"""
            )
            pl = str(progress_text or "").lower()
            if checks_done > 0 or any(k in pl for k in ("checks complete", "finished processing", "处理完毕", "检查完毕", "未发现问题")):
                break
            # 若已生成视频链接且不再显示 Uploading 百分比，可进入 Visibility 步骤
            if video_link and "uploading" not in pl and "正在上传" not in pl and "%" not in pl:
                break
            await human_wait(page, 1800)

        # 4.5) 草稿模式：YouTube Studio 上传完毕后点击关闭弹窗即可自动保存为 Studio 草稿(Draft)，不点最终发布
        if save_as_draft:
            await _notify_stage(on_stage, "上传与属性填写已完成，正在关闭弹窗保存为 YouTube Studio 草稿...")
            close_btn = await _find_first_visible(page, STUDIO_DIALOG_CLOSE_SELECTORS, timeout_ms=8000)
            if close_btn is None:
                return False, "", "未找到 YouTube Studio 上传窗口关闭按钮，无法确认草稿保存"
            await _fire_on_submit()
            await DEFAULT_HUMAN_ENGINE.click_locator(close_btn, page=page, timeout=5000)
            await human_wait(page, 2500)
            return True, STUDIO_CHANNEL_URL, ""

        # 5) 切到“公开范围 (REVIEW)”步骤并设置可见性
        await _notify_stage(on_stage, f"正在设置视频可见性 ({effective_vis})...")
        review_tab = page.locator(STUDIO_STEP_REVIEW).first
        if await review_tab.count() and await review_tab.is_visible():
            await DEFAULT_HUMAN_ENGINE.click_locator(review_tab, page=page, timeout=4000)
            await human_wait(page, 1200)
        else:
            for _ in range(3):
                nxt = page.locator(STUDIO_NEXT_BUTTON).first
                if await nxt.count() and await nxt.is_visible():
                    await DEFAULT_HUMAN_ENGINE.click_locator(nxt, page=page, timeout=3000)
                    await human_wait(page, 800)
                if await page.locator("ytcp-uploads-review").count():
                    break

        vis_sel = STUDIO_VISIBILITY_RADIO.get(effective_vis, STUDIO_VISIBILITY_RADIO["public"])
        vis_radio = page.locator(vis_sel).first
        if await vis_radio.count():
            try:
                await DEFAULT_HUMAN_ENGINE.click_locator(vis_radio, page=page, timeout=4000)
            except Exception:
                await page.evaluate("(s) => { const el = document.querySelector(s); if (el) el.click(); }", vis_sel)
            await human_wait(page, 800)

        if opts["premiere"] and effective_vis == "public":
            await _set_checkbox_state(page, STUDIO_PREMIERE_CHECKBOX, True)

        if not video_link:
            video_link = await _read_studio_video_url(page)

        # 6) 点击最终发布按钮 `#done-button`
        done_btn = page.locator(STUDIO_DONE_BUTTON).first
        if not await done_btn.count():
            return False, "", "未找到 YouTube Studio 发布提交按钮 (#done-button)"

        await _notify_stage(on_stage, "正在提交发布到 YouTube...")
        await _fire_on_submit()
        await DEFAULT_HUMAN_ENGINE.click_locator(done_btn, page=page, timeout=6000)

        # 等待分享确认弹窗出现或上传对话关闭
        confirmed = False
        for _ in range(25):
            await human_wait(page, 1000)
            for ssel in STUDIO_SHARE_DIALOG_SELECTORS:
                try:
                    if await page.locator(ssel).count():
                        confirmed = True
                        if not video_link:
                            share_href = await page.locator("ytcp-video-share-dialog a[href*='youtu']").first.get_attribute("href")
                            if share_href:
                                video_link = normalize_youtube_url(share_href)
                        break
                except Exception:
                    continue
            if confirmed:
                break
            if not await page.locator("ytcp-uploads-dialog .dialog-content").count():
                confirmed = True
                break

        if not confirmed and not video_link:
            return False, "", "write_uncertain:已点击发布按钮但未捕获到完成确认弹窗，请在 YouTube Studio 核实避免重复发布"

        # 7) 如配置了首评，发布后前往视频页追加首评
        if opts["first_comment"] and video_link:
            try:
                await _notify_stage(on_stage, "视频发布成功，正在发送首评...")
                from ...browser.youtube_actions import post_youtube_comment_on_page

                await post_youtube_comment_on_page(page, video_link, opts["first_comment"])
            except Exception:
                pass

        return True, video_link, ""
