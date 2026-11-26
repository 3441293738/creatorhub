"""Facebook 网页端原生发布器（支持个人主页、公共主页、群组与快拍；纯文本、多图与单视频）。

安全不变量：
- 仅在实际点击最终 `Post` / `发帖` / `发布` 提交按钮或监测到 GraphQL 发帖请求发出时才调用 `on_submit`；
- 使用 `DEFAULT_HUMAN_ENGINE` 执行拟人轨迹点击与输入，不自动点击任何验证码/检查点；
- 遇到 `/checkpoint`、锁定或 `[restricted]` 立即返回 `verification_required:` / 明确错误；
- 若以公共主页身份 (`page_id`) 发布，结束后在 `finally` 中安全恢复个人身份（清除 `i_user` Cookie）。
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
from .extract import extract_graphql_posts, parse_facebook_aweme, parse_ndjson_payloads
from .protocol import has_facebook_login_cookies
from .selectors import (
    COMPOSER_CHANGE_THUMB_SELECTORS,
    COMPOSER_CLOSE_BUTTON_SELECTORS,
    COMPOSER_DIALOG_FORM_SELECTORS,
    COMPOSER_DONE_BUTTON_SELECTORS,
    COMPOSER_FILE_INPUT_SELECTORS,
    COMPOSER_PHOTO_BUTTON_SELECTORS,
    COMPOSER_SAVE_DRAFT_SELECTORS,
    COMPOSER_SUBMIT_BUTTON_SELECTORS,
    COMPOSER_TEXTBOX_SELECTORS,
    COMPOSER_TRIGGER_SELECTORS,
    COMPOSER_UPLOAD_THUMB_SELECTORS,
    COMPOSER_VIDEO_EDIT_SELECTORS,
    PRIVACY_LABELS,
)
from .urls import (
    FACEBOOK_BASE,
    GQL_CREATE_POST_HINTS,
    GQL_ENDPOINT_HINTS,
    PROFILE_ME_URL,
    STORY_CREATE_URL,
    group_url,
)

VIDEO_EXTS = frozenset({".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"})
IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})
MAX_FACEBOOK_IMAGES = 20


def validate_facebook_publish_files(media_type: str, files: Sequence[str]) -> Tuple[List[Path], str]:
    """校验 Facebook 发布本地媒体文件：支持 0 文件（纯文本）、1~20 张图片或 1 个视频。"""
    paths: List[Path] = []
    for raw in files or []:
        if not raw:
            continue
        p = Path(raw)
        if not p.is_file():
            return [], f"本地媒体文件不存在: {raw}"
        if p.stat().st_size <= 0:
            return [], f"本地媒体文件为空: {p.name}"
        paths.append(p)

    if not paths:
        return [], ""

    mtype = (media_type or "").strip().lower()
    has_video = any(p.suffix.lower() in VIDEO_EXTS for p in paths)
    if mtype == "video" or has_video:
        if len(paths) > 1:
            return [], "Facebook 视频发布单次仅支持 1 个视频文件"
        if paths[0].suffix.lower() not in VIDEO_EXTS and paths[0].suffix.lower() != ".gif":
            return [], f"不支持的视频格式: {paths[0].suffix}"
        return paths, ""

    if len(paths) > MAX_FACEBOOK_IMAGES:
        return [], f"Facebook 图文发布单次最多支持 {MAX_FACEBOOK_IMAGES} 张图片"
    for p in paths:
        if p.suffix.lower() not in IMAGE_EXTS:
            return [], f"不支持的图片格式: {p.suffix}"
    return paths, ""


def build_facebook_caption(title: str, desc: str, topics: str = "") -> str:
    """将标题、正文与话题标签拼接为 Facebook 帖子文案。"""
    parts: List[str] = []
    t = str(title or "").strip()
    d = str(desc or "").strip()
    if t and d:
        parts.append(f"{t}\n\n{d}" if t not in d else d)
    elif t or d:
        parts.append(t or d)

    tags: List[str] = []
    for tok in str(topics or "").replace("，", ",").replace("#", " ").split():
        for piece in tok.split(","):
            w = piece.strip().lstrip("#")
            if w and f"#{w}" not in " ".join(parts):
                tags.append(f"#{w}")
    if tags:
        parts.append(" ".join(tags))
    return "\n\n".join(p for p in parts if p).strip()


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


async def _apply_privacy_range(page, privacy: str) -> None:
    """在个人主页发帖弹窗中尝试设置可见性（public / friends / private）。"""
    key = str(privacy or "public").strip().lower()
    if key == "self":
        key = "private"
    labels = PRIVACY_LABELS.get(key)
    if not labels:
        return
    try:
        # 定位弹窗顶部受众按钮（通常在作者名下方，包含当前受众文案）
        all_labels = [lbl for vals in PRIVACY_LABELS.values() for lbl in vals]
        trigger = None
        for lbl in all_labels:
            cand = page.locator(f'div[role="dialog"] form div[role="button"]:has-text("{lbl}")').first
            if await cand.count() and await cand.is_visible():
                trigger = cand
                break
        if trigger is None:
            return
        cur_txt = (await trigger.inner_text() or "").strip()
        if any(lbl.lower() in cur_txt.lower() for lbl in labels):
            return
        await DEFAULT_HUMAN_ENGINE.click_locator(trigger, page=page, timeout=3000)
        await human_wait(page, 1000)

        for lbl in labels:
            opt = page.locator(f'div[role="dialog"] div[role="radio"]:has-text("{lbl}")').first
            if await opt.count() and await opt.is_visible():
                await DEFAULT_HUMAN_ENGINE.click_locator(opt, page=page, timeout=3000)
                await human_wait(page, 600)
                break

        done_btn = await _find_first_visible(page, COMPOSER_DONE_BUTTON_SELECTORS, timeout_ms=3000)
        if done_btn is not None:
            await DEFAULT_HUMAN_ENGINE.click_locator(done_btn, page=page, timeout=3000)
            await human_wait(page, 800)
    except Exception:
        pass


async def _upload_media_files(page, paths: Sequence[Path], *, has_video: bool) -> None:
    """向 Facebook 发帖弹窗上传图片/视频并等待预览渲染。"""
    if not paths:
        return
    file_input = None
    for sel in COMPOSER_FILE_INPUT_SELECTORS:
        loc = page.locator(sel).first
        try:
            if await loc.count():
                file_input = loc
                break
        except Exception:
            continue

    if file_input is None:
        photo_btn = await _find_first_visible(page, COMPOSER_PHOTO_BUTTON_SELECTORS, timeout_ms=4000)
        if photo_btn is not None:
            await DEFAULT_HUMAN_ENGINE.click_locator(photo_btn, page=page, timeout=3000)
            await human_wait(page, 900)
        for sel in COMPOSER_FILE_INPUT_SELECTORS:
            loc = page.locator(sel).first
            try:
                if await loc.count():
                    file_input = loc
                    break
            except Exception:
                continue

    if file_input is None:
        raise RuntimeError("未找到 Facebook 发帖弹窗的文件上传控件")

    str_paths = [str(p.resolve()) for p in paths]
    if len(str_paths) == 1:
        await file_input.set_input_files(str_paths[0])
    else:
        try:
            await file_input.set_input_files(str_paths)
        except Exception:
            for sp in str_paths:
                await file_input.set_input_files(sp)
                await human_wait(page, 700)

    # 等待上传加载指示器结束 / 预览节点就绪
    wait_sec = 45 if has_video else 15
    deadline = asyncio.get_running_loop().time() + wait_sec
    await human_wait(page, 1800 if has_video else 1000)
    while asyncio.get_running_loop().time() < deadline:
        try:
            busy = await page.evaluate(
                """() => {
                    const dlg = document.querySelector('div[role="dialog"]');
                    if (!dlg) return false;
                    if (dlg.querySelector('img[src*="WEhNL1y2zoZ.gif"]')) return true;
                    const bar = dlg.querySelector('[role="progressbar"]');
                    if (bar) {
                        const v = bar.getAttribute('aria-valuenow');
                        if (v === null || Number(v) < 100) return true;
                    }
                    return false;
                }"""
            )
            if not busy:
                break
        except Exception:
            break
        await human_wait(page, 900)


async def _extract_latest_profile_post_link(page, target_url: str) -> str:
    """若 GraphQL 未返回新帖链接，则在个人主页/群组首帖尝试读取链接。"""
    try:
        if target_url and target_url not in str(page.url or ""):
            await page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            await human_wait(page, 2500)
        await page.evaluate("window.scrollBy({top: 600, behavior: 'smooth'})")
        await human_wait(page, 1500)
        href = await page.evaluate(
            """() => {
                const links = [...document.querySelectorAll('div[role="article"] a[href]')];
                for (const a of links) {
                    const h = a.href || '';
                    if (/\\/(posts|videos|reel|permalink\\.php|photo|story\\.php)/i.test(h)) {
                        return h;
                    }
                }
                return '';
            }"""
        )
        return str(href or "").strip()
    except Exception:
        return ""


async def _apply_facebook_video_cover(page, cover_path: str, on_stage=None) -> str:
    """尝试通过 Facebook 视频发帖弹窗的 Edit -> Change thumbnail -> Upload image 上传自定义封面。"""
    resolved = str(Path(cover_path).resolve())
    if not Path(resolved).is_file():
        return f"自定义封面文件不存在: {cover_path}"
    await _notify_stage(on_stage, "正在上传 Facebook 自定义视频缩略图…")
    fc_state = {"handled": 0}

    def _on_fc(fc):
        fc_state["handled"] += 1
        asyncio.ensure_future(fc.set_files(resolved))

    page.on("filechooser", _on_fc)
    try:
        edit_btn = await _find_first_visible(page, COMPOSER_VIDEO_EDIT_SELECTORS, timeout_ms=4000)
        if edit_btn is None:
            return ""
        await DEFAULT_HUMAN_ENGINE.click_locator(edit_btn, page=page, timeout=3500)
        await human_wait(page, 1200)

        change_btn = await _find_first_visible(page, COMPOSER_CHANGE_THUMB_SELECTORS, timeout_ms=4000)
        if change_btn is not None:
            await DEFAULT_HUMAN_ENGINE.click_locator(change_btn, page=page, timeout=3000)
            await human_wait(page, 900)

        uploaded = fc_state["handled"] > 0
        if not uploaded:
            up_btn = await _find_first_visible(page, COMPOSER_UPLOAD_THUMB_SELECTORS, timeout_ms=3000)
            if up_btn is not None:
                await DEFAULT_HUMAN_ENGINE.click_locator(up_btn, page=page, timeout=3000)
                await human_wait(page, 900)
                uploaded = fc_state["handled"] > 0

        if not uploaded:
            for sel in (
                'div[role="dialog"] input[type="file"][accept*="image"]',
                'input[type="file"][accept*="image"]',
            ):
                loc = page.locator(sel)
                for idx in range(await loc.count()):
                    cand = loc.nth(idx)
                    acc = str(await cand.get_attribute("accept") or "").lower()
                    if "video" in acc:
                        continue
                    await cand.set_input_files(resolved, timeout=10000)
                    uploaded = True
                    break
                if uploaded:
                    break

        await human_wait(page, 1200)
        done_btn = await _find_first_visible(page, COMPOSER_DONE_BUTTON_SELECTORS, timeout_ms=4000)
        if done_btn is not None:
            await DEFAULT_HUMAN_ENGINE.click_locator(done_btn, page=page, timeout=3500)
            await human_wait(page, 1000)
        return ""
    except Exception:
        return ""
    finally:
        try:
            page.remove_listener("filechooser", _on_fc)
        except Exception:
            pass


async def publish_facebook(
    mgr: Optional[BrowserManager],
    identity: Optional[Identity],
    storage_state: str,
    media_type: str,
    title: str,
    desc: str,
    files: Sequence[str],
    *,
    topics: str = "",
    visibility: str = "public",
    allow_comment: bool = True,
    cover_path: str = "",
    save_as_draft: bool = False,
    fb_options: Optional[Mapping[str, Any]] = None,
    headed: bool = True,
    timeout_seconds: int = 180,
    on_submit: Optional[Callable[[], Any]] = None,
    on_stage: Optional[Callable[[str], Any]] = None,
) -> Tuple[bool, str, str]:
    """执行 Facebook 帖子发布（支持视频自定义封面及支持页面的草稿保存），返回 `(ok, published_url_or_id, error)`。"""
    if mgr is None or identity is None:
        return False, "", "浏览器管理器未就绪，无法执行 Facebook 发布"

    opts = dict(fb_options or {})
    target_type = str(opts.get("target_type") or "profile").strip().lower()
    page_id = str(opts.get("page_id") or "").strip()
    target_group = str(opts.get("group_url") or "").strip()
    first_comment = str(opts.get("first_comment") or "").strip()
    privacy = str(opts.get("privacy") or visibility or "public").strip().lower()

    paths, val_err = validate_facebook_publish_files(media_type, files)
    if val_err:
        return False, "", val_err

    caption = build_facebook_caption(title, desc, topics)
    if not caption and not paths:
        return False, "", "Facebook 发布文案与媒体不能同时为空"

    if storage_state and not has_facebook_login_cookies(storage_state):
        return False, "", "logged_out:缺少 Facebook 登录态 Cookie (c_user / xs)"

    has_video = any(p.suffix.lower() in VIDEO_EXTS for p in paths)
    from ...browser.facebook_fetcher import (
        check_facebook_page_health,
        dismiss_facebook_overlays,
        switch_facebook_page_identity,
    )

    submitted = False
    create_request_sent = False
    captured_post_url = ""
    captured_post_id = ""
    switched_page = False

    async def _mark_submitted() -> None:
        nonlocal submitted
        if submitted:
            return
        submitted = True
        if on_submit is not None:
            res = on_submit()
            if inspect.isawaitable(res):
                await res

    ctx = await mgr.open_headed(identity) if headed else await mgr.context_for(identity)
    page = await ctx.new_page()

    def _on_request(req) -> None:
        nonlocal create_request_sent
        try:
            u = str(req.url or "")
            if not any(h in u for h in GQL_ENDPOINT_HINTS):
                return
            post_data = str(req.post_data or "")
            if any(h in post_data for h in GQL_CREATE_POST_HINTS):
                create_request_sent = True
        except Exception:
            pass

    async def _on_response(resp) -> None:
        nonlocal captured_post_url, captured_post_id
        try:
            u = str(resp.url or "")
            if not any(h in u for h in GQL_ENDPOINT_HINTS) or resp.status >= 400:
                return
            body = await resp.text()
            if not any(k in body for k in ("story_create", "permalink_url", "post_id", "legacy_fbid")):
                return
            for obj in parse_ndjson_payloads(body):
                for st in extract_graphql_posts(obj):
                    aw = parse_facebook_aweme(st)
                    if aw and aw.aweme_id:
                        captured_post_id = aw.aweme_id
                        if aw.source_url:
                            captured_post_url = aw.source_url
                        return
        except Exception:
            pass

    page.on("request", _on_request)
    page.on("response", _on_response)

    try:
        await _notify_stage(on_stage, "opening")
        # 1) 决定目标导航页
        if target_type == "story":
            nav_url = STORY_CREATE_URL
        elif target_type == "group" or target_group:
            if not target_group:
                return False, "", "群组发布需要提供目标群组链接 (group_url)"
            nav_url = group_url(target_group)
        else:
            nav_url = PROFILE_ME_URL

        # 2) 若指定公共主页身份，先加载首页并设置 i_user Cookie
        if page_id or target_type == "page":
            await page.goto(FACEBOOK_BASE, wait_until="domcontentloaded", timeout=40000)
            await human_wait(page, 1800)
            await dismiss_facebook_overlays(page)
            if page_id:
                ok_sw, sw_err = await switch_facebook_page_identity(ctx, page, page_id)
                if not ok_sw:
                    return False, "", sw_err or f"切换公共主页 ({page_id}) 失败"
                switched_page = True
            if nav_url == PROFILE_ME_URL and page_id:
                nav_url = f"{FACEBOOK_BASE}/profile.php?id={page_id}"

        await page.goto(nav_url, wait_until="domcontentloaded", timeout=45000)
        await human_wait(page, 2200)
        await dismiss_facebook_overlays(page)

        health_err = await check_facebook_page_health(page)
        if health_err:
            return False, "", health_err

        # ── 快拍 Story 独立分支 ──
        if target_type == "story":
            if save_as_draft:
                return False, "", "Facebook 快拍（Story）原生不支持平台草稿箱，请改用 CreatorHub 本地草稿"
            await _notify_stage(on_stage, "uploading")
            if paths:
                f_in = page.locator('input[type="file"]').first
                if await f_in.count():
                    await f_in.set_input_files(str(paths[0].resolve()))
                    await human_wait(page, 2500)
            elif caption:
                txt_card = page.locator('div[role="button"]:has-text("Create a text story"), div[role="button"]:has-text("创建文字快拍")').first
                if await txt_card.count() and await txt_card.is_visible():
                    await DEFAULT_HUMAN_ENGINE.click_locator(txt_card, page=page, timeout=4000)
                    await human_wait(page, 1000)
                tbox = page.locator('textarea, div[role="textbox"][contenteditable="true"]').first
                if await tbox.count():
                    await DEFAULT_HUMAN_ENGINE.type_text(tbox, caption, page=page, clear=False, timeout=4000)
                    await human_wait(page, 800)
            share_btn = await _find_first_visible(
                page,
                (
                    'div[role="button"][aria-label*="Share to story" i]:not([aria-disabled="true"])',
                    'div[role="button"]:has-text("Share to story"):not([aria-disabled="true"])',
                    'div[role="button"]:has-text("分享到快拍"):not([aria-disabled="true"])',
                ),
                timeout_ms=12000,
            )
            if share_btn is None:
                return False, "", "未找到 Facebook 快拍分享按钮"
            await _notify_stage(on_stage, "submitting")
            await DEFAULT_HUMAN_ENGINE.click_locator(share_btn, page=page, timeout=5000)
            await _mark_submitted()
            await human_wait(page, 3500)
            return True, STORY_CREATE_URL, ""

        # ── 个人主页 / 公共主页 / 群组发帖流程 ──
        await _notify_stage(on_stage, "composing")
        dialog_form = await _find_first_visible(page, COMPOSER_DIALOG_FORM_SELECTORS, timeout_ms=2500)
        if dialog_form is None:
            trigger = await _find_first_visible(page, COMPOSER_TRIGGER_SELECTORS, timeout_ms=10000)
            if trigger is None:
                # 兜底点击 ProfileComposer 区域内第一个按钮
                await page.evaluate("window.scrollBy(0, 260)")
                await human_wait(page, 800)
                trigger = await _find_first_visible(
                    page,
                    (
                        'div[data-pagelet="ProfileComposer"] div[role="button"]',
                        'div[role="main"] div[role="button"]:has(span)',
                    ),
                    timeout_ms=6000,
                )
            if trigger is None:
                return False, "", "未找到 Facebook 发帖入口按钮（可能无该群组/主页发帖权限）"
            await DEFAULT_HUMAN_ENGINE.click_locator(trigger, page=page, timeout=5000)
            dialog_form = await _find_first_visible(page, COMPOSER_DIALOG_FORM_SELECTORS, timeout_ms=12000)
            if dialog_form is None:
                return False, "", "未能打开发帖编辑弹窗"

        await human_wait(page, 1000)

        # 3) 填写正文文案
        if caption:
            textbox = await _find_first_visible(page, COMPOSER_TEXTBOX_SELECTORS, timeout_ms=10000)
            if textbox is None:
                return False, "", "未找到 Facebook 发帖正文输入框"
            await DEFAULT_HUMAN_ENGINE.type_text(textbox, caption, page=page, clear=False, timeout=5000)
            await human_wait(page, 900)

        # 4) 上传图片/视频附件
        if paths:
            await _notify_stage(on_stage, "uploading")
            await _upload_media_files(page, paths, has_video=has_video)
            if cover_path and has_video:
                await _apply_facebook_video_cover(page, cover_path, on_stage=on_stage)

        # 5) 个人主页可见性设置
        if target_type == "profile" and not target_group and not page_id:
            await _apply_privacy_range(page, privacy)

        # 5.5) 平台草稿箱模式：绝不点击 Post/发布，尝试关闭弹窗触发「Save draft / 保存草稿」
        if save_as_draft:
            await _notify_stage(on_stage, "正在尝试保存到 Facebook 平台草稿箱…")
            close_btn = await _find_first_visible(page, COMPOSER_CLOSE_BUTTON_SELECTORS, timeout_ms=5000)
            if close_btn is not None:
                await DEFAULT_HUMAN_ENGINE.click_locator(close_btn, page=page, timeout=4000)
            else:
                await page.keyboard.press("Escape")
            await human_wait(page, 1200)
            save_draft_btn = await _find_first_visible(page, COMPOSER_SAVE_DRAFT_SELECTORS, timeout_ms=4000)
            if save_draft_btn is not None:
                await _mark_submitted()
                await DEFAULT_HUMAN_ENGINE.click_locator(save_draft_btn, page=page, timeout=4000)
                await human_wait(page, 2200)
                return True, nav_url, ""
            return (
                False,
                "",
                "当前 Facebook 发帖弹窗未提供「Save draft / 保存草稿」入口，已安全中止（未公开发布）；请使用 CreatorHub「存为本地草稿」",
            )

        # 6) 点击发布提交（可能存在 Next -> Post 两步）
        await _notify_stage(on_stage, "submitting")
        submit_btn = await _find_first_visible(page, COMPOSER_SUBMIT_BUTTON_SELECTORS, timeout_ms=15000)
        if submit_btn is None:
            return False, "", "未找到可用的 Facebook 发布按钮（可能媒体仍在处理或字数受限）"

        btn_label = str(
            (await submit_btn.get_attribute("aria-label")) or (await submit_btn.inner_text()) or ""
        ).strip().lower()
        if btn_label in {"next", "下一步"}:
            await DEFAULT_HUMAN_ENGINE.click_locator(submit_btn, page=page, timeout=5000)
            await human_wait(page, 1500)
            submit_btn = await _find_first_visible(page, COMPOSER_SUBMIT_BUTTON_SELECTORS, timeout_ms=10000)
            if submit_btn is None:
                return False, "", "进入下一步后未找到最终发布按钮"

        try:
            await DEFAULT_HUMAN_ENGINE.click_locator(submit_btn, page=page, timeout=5000)
            await _mark_submitted()
        except Exception:
            if create_request_sent:
                await _mark_submitted()
            else:
                raise

        # 7) 等待发帖弹窗关闭并核验风控/限制弹窗
        deadline = asyncio.get_running_loop().time() + max(20.0, min(float(timeout_seconds), 120.0))
        while asyncio.get_running_loop().time() < deadline:
            health_err = await check_facebook_page_health(page)
            if health_err:
                return False, "", health_err
            still_open = await page.locator('div[role="dialog"] form').count()
            if not still_open:
                break
            await human_wait(page, 1000)

        await human_wait(page, 2500)
        final_link = captured_post_url or captured_post_id
        if not final_link:
            final_link = await _extract_latest_profile_post_link(page, nav_url)

        # 8) 可选首评追加（best-effort）
        if first_comment and final_link and final_link.startswith("http"):
            try:
                from ...browser.facebook_actions import post_facebook_comment
                await post_facebook_comment(
                    mgr,
                    identity,
                    final_link,
                    first_comment,
                    page_id=page_id,
                    headed=headed,
                )
            except Exception:
                pass

        return True, final_link or nav_url, ""
    except Exception as exc:
        prefix = "write_uncertain:" if submitted else ""
        return False, "", f"{prefix}Facebook 发布失败: {exc!r}"
    finally:
        if switched_page:
            try:
                await switch_facebook_page_identity(ctx, page, "")
            except Exception:
                pass
        try:
            page.remove_listener("request", _on_request)
            page.remove_listener("response", _on_response)
        except Exception:
            pass
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass
