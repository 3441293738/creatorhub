"""Facebook 网页端拟人写操作执行器。

覆盖能力：
- 7 种表情回应（Like / Love / Care / Haha / Wow / Sad / Angry，兼容普通帖与 Reels）
- 发表评论、按 `comment_id` 回复评论、带图评论及可选顺带点赞主帖
- 分享帖子到个人主页、公共主页或指定群组
- 关注 / 取关、公共主页点赞、发送好友请求与取消好友
- 加入 / 退出 Facebook 群组（支持基础入群问答与公共主页身份切换）
- 用户主页私信与 Messenger 会话文字 / 图片发送
"""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence, Tuple

from app.browser.facebook_fetcher import (
    check_facebook_page_health,
    dismiss_facebook_overlays,
    switch_facebook_page_identity,
)
from app.browser.human_interaction import DEFAULT_HUMAN_ENGINE, human_wait
from app.browser.identity import Identity
from app.browser.manager import BrowserManager
from app.platforms.facebook.protocol import validate_final_comment_text
from app.platforms.facebook.resolve import normalize_facebook_url
from app.platforms.facebook.selectors import (
    ADD_FRIEND_BUTTON_SELECTORS,
    COMMENT_OPEN_SELECTORS,
    COMMENT_SUBMIT_SELECTORS,
    COMMENT_TEXTBOX_SELECTORS,
    DM_COMPOSER_INPUT_SELECTORS,
    DM_FILE_INPUT_SELECTORS,
    DM_SEND_BUTTON_SELECTORS,
    FOLLOW_BUTTON_SELECTORS,
    FOLLOWING_BUTTON_SELECTORS,
    FRIENDS_MENU_BUTTON_SELECTORS,
    JOINED_GROUP_BUTTON_SELECTORS,
    JOIN_GROUP_BUTTON_SELECTORS,
    LIKE_BUTTON_SELECTORS,
    LIKE_PAGE_BUTTON_SELECTORS,
    PROFILE_MESSAGE_BUTTON_SELECTORS,
    REACTION_LABELS,
    SHARE_BUTTON_SELECTORS,
    UNLIKE_BUTTON_SELECTORS,
)
from app.platforms.facebook.urls import FACEBOOK_BASE, dm_thread_url, group_url, profile_url


async def _notify_submit(on_submit: Optional[Callable[[], Any]]) -> None:
    if on_submit is None:
        return
    res = on_submit()
    if inspect.isawaitable(res):
        await res


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


async def do_facebook_like(
    mgr: BrowserManager,
    identity: Identity,
    post_url_raw: str,
    *,
    reaction: str = "like",
    unlike: bool = False,
    page_id: str = "",
    headed: bool = True,
) -> Tuple[bool, str]:
    """对 Facebook 帖子 / 视频 / Reels 执行点赞或 7 种表情回应（支持以公共主页身份点赞）。"""
    target_url = normalize_facebook_url(post_url_raw)
    if not target_url.startswith(("http://", "https://")):
        return False, "无效的 Facebook 帖子链接"
    react_key = str(reaction or "like").strip().lower()
    if react_key not in REACTION_LABELS:
        react_key = "like"

    ctx = await mgr.open_headed(identity) if headed else await mgr.context_for(identity)
    page = await ctx.new_page()
    switched_page = False
    try:
        if page_id:
            await page.goto(FACEBOOK_BASE, wait_until="domcontentloaded", timeout=40000)
            await human_wait(page, 1500)
            await dismiss_facebook_overlays(page)
            ok_sw, sw_err = await switch_facebook_page_identity(ctx, page, page_id)
            if not ok_sw:
                return False, sw_err
            switched_page = True

        await page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
        await human_wait(page, 2600)
        await dismiss_facebook_overlays(page)
        health_err = await check_facebook_page_health(page)
        if health_err:
            return False, health_err

        if unlike:
            ubtn = await _find_first_visible(page, UNLIKE_BUTTON_SELECTORS, timeout_ms=5000)
            if ubtn is None:
                return True, ""
            await DEFAULT_HUMAN_ENGINE.click_locator(ubtn, page=page, timeout=4000)
            await human_wait(page, 1500)
            return True, ""

        # 已赞且仅需普通点赞 -> 幂等返回
        if react_key == "like":
            already = await _find_first_visible(page, UNLIKE_BUTTON_SELECTORS, timeout_ms=1500)
            if already is not None:
                return True, ""

        like_btn = await _find_first_visible(page, LIKE_BUTTON_SELECTORS, timeout_ms=10000)
        if like_btn is None:
            return False, "未找到 Facebook 点赞按钮"

        if react_key == "like":
            await DEFAULT_HUMAN_ENGINE.click_locator(like_btn, page=page, timeout=4000)
        else:
            # 悬停唤起 7 种表情回应浮层
            await like_btn.hover()
            await human_wait(page, 1200)
            clicked_react = False
            for lbl in REACTION_LABELS[react_key]:
                cand = page.locator(f'div[role="button"][aria-label="{lbl}"]').first
                try:
                    if await cand.count() and await cand.is_visible():
                        await DEFAULT_HUMAN_ENGINE.click_locator(cand, page=page, timeout=3000)
                        clicked_react = True
                        break
                except Exception:
                    continue
            if not clicked_react:
                await DEFAULT_HUMAN_ENGINE.click_locator(like_btn, page=page, timeout=4000)

        await human_wait(page, 1800)
        health_err = await check_facebook_page_health(page)
        if health_err:
            return False, health_err
        return True, ""
    except Exception as exc:
        return False, f"Facebook 点赞/表情回应失败: {exc!r}"
    finally:
        if switched_page:
            try:
                await switch_facebook_page_identity(ctx, page, "")
            except Exception:
                pass
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass


async def post_facebook_comment(
    mgr: BrowserManager,
    identity: Identity,
    post_url_raw: str,
    text: str,
    *,
    reply_to_comment_id: str = "",
    image_path: str = "",
    also_like: bool = False,
    reaction: str = "like",
    page_id: str = "",
    headed: bool = True,
    on_submit: Optional[Callable[[], Any]] = None,
) -> Tuple[bool, str]:
    """在 Facebook 帖子 / 视频 / Reels 下发表评论或按 `comment_id` 回复（支持附带图片与同步点赞）。"""
    target_url = normalize_facebook_url(post_url_raw)
    if not target_url.startswith(("http://", "https://")):
        return False, "无效的 Facebook 帖子链接"
    comment_text = str(text or "").strip()
    img_p = Path(image_path) if image_path else None
    if img_p is not None and not img_p.is_file():
        return False, f"评论附件图片不存在: {image_path}"
    if comment_text:
        val_err = validate_final_comment_text(comment_text)
        if val_err:
            return False, val_err
    elif img_p is None:
        return False, "评论文本与图片不能同时为空"

    # 若指定回复某条评论且 URL 未含 comment_id，则拼上查询参数直达定位
    if reply_to_comment_id and "comment_id=" not in target_url:
        sep = "&" if "?" in target_url else "?"
        target_url = f"{target_url}{sep}comment_id={reply_to_comment_id}"

    ctx = await mgr.open_headed(identity) if headed else await mgr.context_for(identity)
    page = await ctx.new_page()
    switched_page = False
    submitted = False

    try:
        if page_id:
            await page.goto(FACEBOOK_BASE, wait_until="domcontentloaded", timeout=40000)
            await human_wait(page, 1500)
            await dismiss_facebook_overlays(page)
            ok_sw, sw_err = await switch_facebook_page_identity(ctx, page, page_id)
            if not ok_sw:
                return False, sw_err
            switched_page = True

        await page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
        await human_wait(page, 2600)
        await dismiss_facebook_overlays(page)
        health_err = await check_facebook_page_health(page)
        if health_err:
            return False, health_err

        # 可选：评论同时顺带点赞主帖
        if also_like:
            like_btn = await _find_first_visible(page, LIKE_BUTTON_SELECTORS, timeout_ms=4000)
            if like_btn is not None:
                try:
                    await DEFAULT_HUMAN_ENGINE.click_locator(like_btn, page=page, timeout=3000)
                    await human_wait(page, 1200)
                except Exception:
                    pass

        # Reels 页若未展开评论侧栏则先点击评论图标
        if "/reel/" in str(page.url or "").lower():
            tbox_check = await _find_first_visible(page, COMMENT_TEXTBOX_SELECTORS, timeout_ms=2000)
            if tbox_check is None:
                cbtn = await _find_first_visible(page, COMMENT_OPEN_SELECTORS, timeout_ms=5000)
                if cbtn is not None:
                    await DEFAULT_HUMAN_ENGINE.click_locator(cbtn, page=page, timeout=3000)
                    await human_wait(page, 1600)

        textbox = None
        # 若为回复特定 comment_id，先定位该评论并点开其「回复」输入框
        if reply_to_comment_id:
            cid_block = page.locator(f'div[data-virtualized]:has(a[href*="{reply_to_comment_id}"])').first
            if await cid_block.count():
                for rep_lbl in ("Reply", "回复", "回覆", "Responder"):
                    rbtn = cid_block.locator(f'div[role="button"]:has-text("{rep_lbl}")').first
                    if await rbtn.count() and await rbtn.is_visible():
                        await DEFAULT_HUMAN_ENGINE.click_locator(rbtn, page=page, timeout=3000)
                        await human_wait(page, 1000)
                        break
                t_sub = cid_block.locator('form div[role="textbox"][contenteditable="true"]').first
                if await t_sub.count() and await t_sub.is_visible():
                    textbox = t_sub

        if textbox is None:
            textbox = await _find_first_visible(page, COMMENT_TEXTBOX_SELECTORS, timeout_ms=10000)
        if textbox is None:
            cbtn = await _find_first_visible(page, COMMENT_OPEN_SELECTORS, timeout_ms=4000)
            if cbtn is not None:
                await DEFAULT_HUMAN_ENGINE.click_locator(cbtn, page=page, timeout=3000)
                await human_wait(page, 1000)
                textbox = await _find_first_visible(page, COMMENT_TEXTBOX_SELECTORS, timeout_ms=8000)
        if textbox is None:
            return False, "未找到 Facebook 评论输入框（帖子可能已关闭评论权限）"

        # 若带评论图片，先上传图片
        if img_p is not None:
            f_in = page.locator('form input[type="file"]').first
            if await f_in.count():
                await f_in.set_input_files(str(img_p.resolve()))
                await human_wait(page, 2200)

        if comment_text:
            await DEFAULT_HUMAN_ENGINE.type_text(textbox, comment_text, page=page, clear=False, timeout=5000)
            await human_wait(page, 800)

        submit_btn = await _find_first_visible(page, COMMENT_SUBMIT_SELECTORS, timeout_ms=6000)
        if submit_btn is not None:
            await DEFAULT_HUMAN_ENGINE.click_locator(submit_btn, page=page, timeout=4000)
            submitted = True
            await _notify_submit(on_submit)
        else:
            await textbox.press("Enter")
            submitted = True
            await _notify_submit(on_submit)

        await human_wait(page, 2600)
        health_err = await check_facebook_page_health(page)
        if health_err:
            return False, health_err
        return True, ""
    except Exception as exc:
        prefix = "write_uncertain:" if submitted else ""
        return False, f"{prefix}Facebook 评论发送失败: {exc!r}"
    finally:
        if switched_page:
            try:
                await switch_facebook_page_identity(ctx, page, "")
            except Exception:
                pass
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass


async def do_facebook_share(
    mgr: BrowserManager,
    identity: Identity,
    post_url_raw: str,
    *,
    comment_text: str = "",
    group_url_target: str = "",
    page_id: str = "",
    headed: bool = True,
) -> Tuple[bool, str, str]:
    """分享 Facebook 帖子到个人主页、公共主页或指定群组。"""
    target_url = normalize_facebook_url(post_url_raw)
    if not target_url.startswith(("http://", "https://")):
        return False, "", "无效的 Facebook 帖子链接"

    ctx = await mgr.open_headed(identity) if headed else await mgr.context_for(identity)
    page = await ctx.new_page()
    switched_page = False
    try:
        if page_id:
            await page.goto(FACEBOOK_BASE, wait_until="domcontentloaded", timeout=40000)
            await human_wait(page, 1500)
            await dismiss_facebook_overlays(page)
            ok_sw, sw_err = await switch_facebook_page_identity(ctx, page, page_id)
            if not ok_sw:
                return False, "", sw_err
            switched_page = True

        await page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
        await human_wait(page, 2600)
        await dismiss_facebook_overlays(page)
        health_err = await check_facebook_page_health(page)
        if health_err:
            return False, "", health_err

        share_btn = await _find_first_visible(page, SHARE_BUTTON_SELECTORS, timeout_ms=10000)
        if share_btn is None:
            return False, "", "未找到 Facebook 分享按钮（帖子可能不允许分享）"
        await DEFAULT_HUMAN_ENGINE.click_locator(share_btn, page=page, timeout=4000)
        await human_wait(page, 1600)

        # 若指定分享至群组，点开「Share to a group」入口
        if group_url_target:
            for grp_lbl in ("Group", "群组", "小组", "社團"):
                gopt = page.locator(f'div[role="dialog"] div[role="button"]:has-text("{grp_lbl}")').first
                if await gopt.count() and await gopt.is_visible():
                    await DEFAULT_HUMAN_ENGINE.click_locator(gopt, page=page, timeout=3000)
                    await human_wait(page, 1500)
                    break

        if comment_text:
            tbox = await _find_first_visible(
                page,
                (
                    'div[role="dialog"] div[role="textbox"][contenteditable="true"]',
                    'div[role="dialog"] p[style*="text-align"]',
                ),
                timeout_ms=5000,
            )
            if tbox is not None:
                await DEFAULT_HUMAN_ENGINE.type_text(tbox, comment_text.strip(), page=page, clear=False, timeout=4000)
                await human_wait(page, 800)

        confirm = await _find_first_visible(
            page,
            (
                'div[role="dialog"] div[role="button"][aria-label="Share now" i]:not([aria-disabled="true"])',
                'div[role="dialog"] div[role="button"][aria-label="Share" i]:not([aria-disabled="true"])',
                'div[role="dialog"] div[role="button"]:has-text("Share now"):not([aria-disabled="true"])',
                'div[role="dialog"] div[role="button"]:has-text("立即分享"):not([aria-disabled="true"])',
                'div[role="dialog"] div[role="button"]:has-text("立即發佈"):not([aria-disabled="true"])',
                'div[role="dialog"] div[role="button"]:has-text("Post"):not([aria-disabled="true"])',
                'div[role="dialog"] div[role="button"]:has-text("发帖"):not([aria-disabled="true"])',
            ),
            timeout_ms=8000,
        )
        if confirm is None:
            return False, "", "未找到 Facebook 确认分享按钮"
        await DEFAULT_HUMAN_ENGINE.click_locator(confirm, page=page, timeout=4000)
        await human_wait(page, 2600)
        health_err = await check_facebook_page_health(page)
        if health_err:
            return False, "", health_err
        return True, target_url, ""
    except Exception as exc:
        return False, "", f"Facebook 分享失败: {exc!r}"
    finally:
        if switched_page:
            try:
                await switch_facebook_page_identity(ctx, page, "")
            except Exception:
                pass
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass


async def do_facebook_follow(
    mgr: BrowserManager,
    identity: Identity,
    target_ref: str,
    *,
    unfollow: bool = False,
    action_mode: str = "follow",
    headed: bool = True,
) -> Tuple[bool, str]:
    """执行 Facebook 关注/取关主页、点赞主页、发送好友请求或解除好友。

    `action_mode`:
    - `follow`（默认）：关注 / 取消关注（若页面无 Follow 按钮则兼容尝试公共主页 Like 或菜单内 Follow）
    - `like_page`：点赞公共主页
    - `add_friend`：添加好友（`unfollow=True` 时执行解除好友 Unfriend）
    """
    url = profile_url(target_ref)
    mode = str(action_mode or "follow").strip().lower()
    ctx = await mgr.open_headed(identity) if headed else await mgr.context_for(identity)
    page = await ctx.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await human_wait(page, 2500)
        await dismiss_facebook_overlays(page)
        health_err = await check_facebook_page_health(page)
        if health_err:
            return False, health_err

        # ── 加好友 / 解除好友分支 ──
        if mode == "add_friend":
            if unfollow:
                fbtn = await _find_first_visible(page, FRIENDS_MENU_BUTTON_SELECTORS, timeout_ms=6000)
                if fbtn is None:
                    return True, ""
                await DEFAULT_HUMAN_ENGINE.click_locator(fbtn, page=page, timeout=3000)
                await human_wait(page, 900)
                for lbl in ("Unfriend", "删除好友", "解除朋友關係", "Eliminar de mis amigos"):
                    item = page.locator(f'div[role="menuitem"]:has-text("{lbl}")').first
                    if await item.count() and await item.is_visible():
                        await DEFAULT_HUMAN_ENGINE.click_locator(item, page=page, timeout=3000)
                        await human_wait(page, 900)
                        break
                confirm = page.locator('div[role="dialog"] div[role="button"][aria-label*="Confirm" i], div[role="dialog"] div[role="button"]:has-text("确认"), div[role="dialog"] div[role="button"]:has-text("確定")').first
                if await confirm.count() and await confirm.is_visible():
                    await DEFAULT_HUMAN_ENGINE.click_locator(confirm, page=page, timeout=3000)
                await human_wait(page, 1800)
                return True, ""

            add_btn = await _find_first_visible(page, ADD_FRIEND_BUTTON_SELECTORS, timeout_ms=8000)
            if add_btn is None:
                if await _find_first_visible(page, FRIENDS_MENU_BUTTON_SELECTORS, timeout_ms=1500) is not None:
                    return True, ""
                return False, "未找到 Facebook 加为好友按钮"
            await DEFAULT_HUMAN_ENGINE.click_locator(add_btn, page=page, timeout=4000)
            await human_wait(page, 2000)
            return True, ""

        # ── 公共主页点赞分支 ──
        if mode == "like_page":
            lbtn = await _find_first_visible(page, LIKE_PAGE_BUTTON_SELECTORS, timeout_ms=8000)
            if lbtn is None:
                return False, "未找到公共主页点赞按钮"
            await DEFAULT_HUMAN_ENGINE.click_locator(lbtn, page=page, timeout=4000)
            await human_wait(page, 2000)
            return True, ""

        # ── 关注 / 取消关注分支 ──
        if unfollow:
            fbtn = await _find_first_visible(page, FOLLOWING_BUTTON_SELECTORS, timeout_ms=6000)
            if fbtn is None:
                return True, ""
            await DEFAULT_HUMAN_ENGINE.click_locator(fbtn, page=page, timeout=3000)
            await human_wait(page, 1000)
            for lbl in ("Unfollow", "取消关注", "取消追蹤", "Dejar de seguir"):
                item = page.locator(f'div[role="menuitem"]:has-text("{lbl}"), div[role="dialog"] div[role="button"]:has-text("{lbl}")').first
                if await item.count() and await item.is_visible():
                    await DEFAULT_HUMAN_ENGINE.click_locator(item, page=page, timeout=3000)
                    await human_wait(page, 900)
                    break
            done = page.locator('div[role="dialog"] div[role="button"]:has-text("Update"), div[role="dialog"] div[role="button"]:has-text("更新"), div[role="dialog"] div[role="button"]:has-text("确认")').first
            if await done.count() and await done.is_visible():
                await DEFAULT_HUMAN_ENGINE.click_locator(done, page=page, timeout=3000)
            await human_wait(page, 1800)
            return True, ""

        if await _find_first_visible(page, FOLLOWING_BUTTON_SELECTORS, timeout_ms=1500) is not None:
            return True, ""

        follow_btn = await _find_first_visible(page, FOLLOW_BUTTON_SELECTORS, timeout_ms=6000)
        if follow_btn is None:
            # 尝试右上角「更多 (`aria-haspopup="menu"`)」菜单内的 Follow 入口
            more_btn = page.locator('div[role="main"] div[aria-haspopup="menu"][role="button"]').first
            if await more_btn.count() and await more_btn.is_visible():
                await DEFAULT_HUMAN_ENGINE.click_locator(more_btn, page=page, timeout=3000)
                await human_wait(page, 900)
                for lbl in ("Follow", "关注", "追蹤", "Seguir"):
                    mi = page.locator(f'div[role="menu"] div[role="menuitem"]:has-text("{lbl}")').first
                    if await mi.count() and await mi.is_visible():
                        follow_btn = mi
                        break
        if follow_btn is None:
            follow_btn = await _find_first_visible(page, LIKE_PAGE_BUTTON_SELECTORS, timeout_ms=3000)
        if follow_btn is None:
            return False, "未找到 Facebook 关注/点赞主页按钮"

        await DEFAULT_HUMAN_ENGINE.click_locator(follow_btn, page=page, timeout=4000)
        await human_wait(page, 2200)
        health_err = await check_facebook_page_health(page)
        if health_err:
            return False, health_err
        return True, ""
    except Exception as exc:
        return False, f"Facebook 关系操作失败: {exc!r}"
    finally:
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass


async def do_facebook_group_membership(
    mgr: BrowserManager,
    identity: Identity,
    target_group: str,
    *,
    leave: bool = False,
    answers: Optional[Sequence[str]] = None,
    page_id: str = "",
    headed: bool = True,
) -> Tuple[bool, str]:
    """加入或退出 Facebook 群组（支持自动填写基础入群问答与公共主页身份加入）。"""
    url = group_url(target_group)
    ctx = await mgr.open_headed(identity) if headed else await mgr.context_for(identity)
    page = await ctx.new_page()
    switched_page = False
    try:
        if page_id:
            await page.goto(FACEBOOK_BASE, wait_until="domcontentloaded", timeout=40000)
            await human_wait(page, 1500)
            await dismiss_facebook_overlays(page)
            ok_sw, sw_err = await switch_facebook_page_identity(ctx, page, page_id)
            if not ok_sw:
                return False, sw_err
            switched_page = True

        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await human_wait(page, 2500)
        await dismiss_facebook_overlays(page)
        health_err = await check_facebook_page_health(page)
        if health_err:
            return False, health_err

        if leave:
            jbtn = await _find_first_visible(page, JOINED_GROUP_BUTTON_SELECTORS, timeout_ms=6000)
            if jbtn is None:
                return True, ""
            await DEFAULT_HUMAN_ENGINE.click_locator(jbtn, page=page, timeout=3000)
            await human_wait(page, 900)
            for lbl in ("Leave group", "退出小组", "退出社團", "退出群组", "Salir del grupo"):
                mi = page.locator(f'div[role="menuitem"]:has-text("{lbl}")').first
                if await mi.count() and await mi.is_visible():
                    await DEFAULT_HUMAN_ENGINE.click_locator(mi, page=page, timeout=3000)
                    await human_wait(page, 900)
                    break
            confirm = page.locator('div[role="dialog"] div[role="button"]:has-text("Leave group"), div[role="dialog"] div[role="button"]:has-text("退出")').first
            if await confirm.count() and await confirm.is_visible():
                await DEFAULT_HUMAN_ENGINE.click_locator(confirm, page=page, timeout=3000)
            await human_wait(page, 2000)
            return True, ""

        if await _find_first_visible(page, JOINED_GROUP_BUTTON_SELECTORS, timeout_ms=1500) is not None:
            return True, ""

        join_btn = await _find_first_visible(page, JOIN_GROUP_BUTTON_SELECTORS, timeout_ms=8000)
        if join_btn is None:
            return False, "未找到加入群组按钮（可能已提交申请或群组不可访问）"
        await DEFAULT_HUMAN_ENGINE.click_locator(join_btn, page=page, timeout=4000)
        await human_wait(page, 2000)

        # 若弹出入群问答对话框，按传入 answers 依次填写并勾选群规复选框
        dlg = page.locator('div[role="dialog"]').first
        if await dlg.count() and await dlg.is_visible():
            ans_list = [str(a or "").strip() for a in (answers or []) if str(a or "").strip()]
            textareas = dlg.locator("textarea")
            ta_count = await textareas.count()
            for idx in range(ta_count):
                if idx < len(ans_list):
                    await DEFAULT_HUMAN_ENGINE.type_text(textareas.nth(idx), ans_list[idx], page=page, clear=True, timeout=3000)
                    await human_wait(page, 500)
            checks = dlg.locator('input[type="checkbox"], div[role="checkbox"]')
            for idx in range(min(await checks.count(), 4)):
                try:
                    c = checks.nth(idx)
                    if await c.is_visible():
                        await DEFAULT_HUMAN_ENGINE.click_locator(c, page=page, timeout=2000)
                        await human_wait(page, 400)
                except Exception:
                    continue
            for sub_lbl in ("Submit", "提交", "送出", "Enviar"):
                sbtn = dlg.locator(f'div[role="button"]:has-text("{sub_lbl}"):not([aria-disabled="true"])').first
                if await sbtn.count() and await sbtn.is_visible():
                    await DEFAULT_HUMAN_ENGINE.click_locator(sbtn, page=page, timeout=3000)
                    await human_wait(page, 2000)
                    break

        health_err = await check_facebook_page_health(page)
        if health_err:
            return False, health_err
        return True, ""
    except Exception as exc:
        return False, f"Facebook 群组操作失败: {exc!r}"
    finally:
        if switched_page:
            try:
                await switch_facebook_page_identity(ctx, page, "")
            except Exception:
                pass
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass


async def send_facebook_dm(
    mgr: BrowserManager,
    identity: Identity,
    target_ref: str,
    text: str,
    *,
    conv_id: str = "",
    image_paths: Optional[Sequence[str]] = None,
    headed: bool = True,
    on_submit: Optional[Callable[[], Any]] = None,
) -> Tuple[bool, str]:
    """向 Facebook 用户或 Messenger 会话发送私信（支持文字与图片附件）。"""
    msg_text = str(text or "").strip()
    imgs: List[Path] = []
    for raw in (image_paths or []):
        if raw:
            p = Path(raw)
            if p.is_file():
                imgs.append(p)
    if not msg_text and not imgs:
        return False, "私信内容与图片不能同时为空"

    ctx = await mgr.open_headed(identity) if headed else await mgr.context_for(identity)
    page = await ctx.new_page()
    submitted = False
    try:
        # 优先使用已知 Messenger 会话 ID 直达 `/messages/t/<conv_id>`；否则打开用户主页点「发消息」
        if conv_id or (target_ref and "/messages/" in str(target_ref)):
            nav_url = dm_thread_url(conv_id or target_ref)
            await page.goto(nav_url, wait_until="domcontentloaded", timeout=45000)
            await human_wait(page, 2800)
            await dismiss_facebook_overlays(page)
        else:
            nav_url = profile_url(target_ref)
            await page.goto(nav_url, wait_until="domcontentloaded", timeout=45000)
            await human_wait(page, 2600)
            await dismiss_facebook_overlays(page)
            health_err = await check_facebook_page_health(page)
            if health_err:
                return False, health_err

            msg_btn = await _find_first_visible(page, PROFILE_MESSAGE_BUTTON_SELECTORS, timeout_ms=10000)
            if msg_btn is None:
                # 若直接拿到了数字 UID，兜底尝试直开 `/messages/t/<uid>`
                if str(target_ref or "").strip().isdigit():
                    await page.goto(dm_thread_url(target_ref), wait_until="domcontentloaded", timeout=45000)
                    await human_wait(page, 2600)
                    await dismiss_facebook_overlays(page)
                else:
                    return False, "未找到用户主页「发消息」按钮（对方可能限制陌生人私信）"
            else:
                await DEFAULT_HUMAN_ENGINE.click_locator(msg_btn, page=page, timeout=4000)
                await human_wait(page, 2200)

        health_err = await check_facebook_page_health(page)
        if health_err:
            return False, health_err

        # 1) 上传私信图片（如有）
        if imgs:
            file_in = None
            for sel in DM_FILE_INPUT_SELECTORS:
                loc = page.locator(sel).first
                if await loc.count():
                    file_in = loc
                    break
            if file_in is None:
                return False, "未找到 Messenger 图片上传控件"
            await file_in.set_input_files([str(p.resolve()) for p in imgs])
            await human_wait(page, 2500)

        # 2) 输入正文并发送
        textbox = await _find_first_visible(page, DM_COMPOSER_INPUT_SELECTORS, timeout_ms=10000)
        if textbox is None:
            return False, "未找到 Messenger 私信输入框"

        if msg_text:
            await DEFAULT_HUMAN_ENGINE.type_text(textbox, msg_text, page=page, clear=False, timeout=5000)
            await human_wait(page, 800)

        send_btn = await _find_first_visible(page, DM_SEND_BUTTON_SELECTORS, timeout_ms=4000)
        if send_btn is not None:
            await DEFAULT_HUMAN_ENGINE.click_locator(send_btn, page=page, timeout=4000)
            submitted = True
            await _notify_submit(on_submit)
        else:
            await textbox.press("Enter")
            submitted = True
            await _notify_submit(on_submit)

        await human_wait(page, 2500)
        health_err = await check_facebook_page_health(page)
        if health_err:
            return False, health_err
        return True, ""
    except Exception as exc:
        prefix = "write_uncertain:" if submitted else ""
        return False, f"{prefix}Facebook 私信发送失败: {exc!r}"
    finally:
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass
