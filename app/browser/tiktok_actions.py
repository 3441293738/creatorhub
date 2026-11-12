"""TikTok visible write actions with explicit uncertain outcomes."""

from __future__ import annotations

import inspect
from typing import Any

from ..platforms.tiktok.selectors import (
    COMMENT_INPUTS,
    COMMENT_SUBMITS,
    FOLLOW_BUTTONS,
    MESSAGE_BUTTONS,
    MESSAGE_IFRAME,
    MESSAGE_INPUTS,
)
from ..platforms.tiktok.urls import COMMENT_PUBLISH_HINTS, profile_url, video_url


async def _mark_submit(on_submit: Any) -> None:
    from app.licensing import require_active
    require_active()
    if on_submit is None:
        return
    result = on_submit()
    if inspect.isawaitable(result):
        await result


async def _visible(page, selectors):
    for selector in selectors:
        loc = page.locator(selector)
        for index in range(min(await loc.count(), 8)):
            candidate = loc.nth(index)
            try:
                if await candidate.is_visible():
                    return candidate
            except Exception:
                continue
    return None


async def post_tiktok_comment(
        mgr, identity, aweme_id: str, content: str, *, reply_to_text: str = "",
        require_reply: bool = False, on_submit: Any = None,
        headed: bool = True, author_unique_id: str = "",
        target_url: str = "") -> tuple[bool, str, str]:
    content = str(content or "").strip()
    if not content:
        return False, "", "空文案"
    if require_reply and not str(reply_to_text or "").strip():
        return False, "", "缺少目标评论原文，已跳过回复"
    ctx = await mgr.open_headed(identity) if headed else None
    page = await ctx.new_page() if ctx is not None else await mgr.new_page(identity, block_media=False)
    result = {"seen": False, "ok": False, "id": "", "message": ""}

    async def on_response(response):
        if result["seen"] or not any(hint in response.url for hint in COMMENT_PUBLISH_HINTS):
            return
        try:
            payload = await response.json()
        except Exception:
            return
        result["seen"] = True
        code = payload.get("status_code", payload.get("statusCode", payload.get("code")))
        result["ok"] = code in (0, "0", None) and not payload.get("error")
        comment = payload.get("comment") or payload.get("data") or {}
        if isinstance(comment, dict):
            result["id"] = str(comment.get("cid") or comment.get("id") or "")
        result["message"] = str(payload.get("status_msg") or payload.get("message") or "")

    page.on("response", on_response)
    submitted = False
    try:
        url = str(target_url or "").strip() or video_url(aweme_id, author_unique_id)
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(1800)
        if "/login" in str(page.url).lower():
            return False, "", "logged_out:账号未登录,无法发评论"
        editor = None
        if reply_to_text:
            item = page.locator('[data-e2e*="comment"]', has_text=reply_to_text[:24]).first
            try:
                if await item.count():
                    reply = item.get_by_text("Reply", exact=False).first
                    if await reply.count():
                        await reply.click(timeout=4000)
                        await page.wait_for_timeout(500)
            except Exception:
                if require_reply:
                    return False, "", "未找到目标评论回复区，未发送成顶层评论"
        editor = await _visible(page, COMMENT_INPUTS)
        if editor is None:
            return False, "", "未找到 TikTok 评论输入框"
        await editor.click(timeout=10000)
        try:
            await editor.fill(content)
        except Exception:
            await page.keyboard.type(content, delay=35)
        button = await _visible(page, COMMENT_SUBMITS)
        await _mark_submit(on_submit)
        if button is not None:
            from app.licensing import require_active
            require_active()
            await button.click(timeout=4000)
        else:
            from app.licensing import require_active
            require_active()
            await page.keyboard.press("Enter")
        submitted = True
        for _ in range(30):
            if result["seen"]:
                break
            await page.wait_for_timeout(300)
        if result["seen"] and result["ok"] and result["id"]:
            return True, result["id"], ""
        if result["seen"] and not result["ok"]:
            return False, "", f"TikTok 拒绝评论: {result['message'] or '未知原因'}"
        return False, "", "write_uncertain:评论已提交但未取得评论 ID，请到 TikTok 核对"
    except Exception as exc:
        if submitted:
            return False, "", f"write_uncertain:评论提交后页面异常，请人工核对: {exc}"
        return False, "", f"TikTok 发评论异常: {exc}"
    finally:
        try:
            if ctx is not None:
                await ctx.close()
            else:
                await page.close()
        except Exception:
            pass


async def do_tiktok_follow(
        mgr, identity, unique_id: str, *, unfollow: bool = False,
        on_submit: Any = None) -> tuple[bool, str]:
    page = None
    ctx = None
    clicked = False
    try:
        ctx = await mgr.open_headed(identity)
        page = await ctx.new_page()
        await page.goto(profile_url(unique_id), wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(1800)
        if "/login" in str(page.url).lower():
            return False, "logged_out:账号未登录"
        button = await _visible(page, FOLLOW_BUTTONS)
        if button is None:
            return False, "未找到 TikTok 关注按钮"
        text = (await button.inner_text()).strip().lower()
        following = "following" in text or "friends" in text
        if following == (not unfollow):
            return True, ""
        await _mark_submit(on_submit)
        from app.licensing import require_active
        require_active()
        await button.click(timeout=4000)
        clicked = True
        if unfollow:
            confirm = await _visible(page, ('button:has-text("Unfollow")', 'button:has-text("Confirm")'))
            if confirm is not None:
                from app.licensing import require_active
                require_active()
                await confirm.click(timeout=3000)
        for _ in range(15):
            await page.wait_for_timeout(400)
            current = await _visible(page, FOLLOW_BUTTONS)
            if current is None:
                continue
            now = (await current.inner_text()).strip().lower()
            if ("following" in now or "friends" in now) == (not unfollow):
                return True, ""
        return False, "write_uncertain:已点击关注状态按钮但未确认最终状态，请到 TikTok 核对"
    except Exception as exc:
        if clicked:
            return False, f"write_uncertain:关注操作提交后页面异常，请人工核对: {exc}"
        return False, f"TikTok 关注操作异常: {exc}"
    finally:
        try:
            if ctx is not None:
                await ctx.close()
            elif page is not None:
                await page.close()
        except Exception:
            pass


async def send_tiktok_dm(
        mgr, identity, unique_id: str, text: str, *,
        on_submit: Any = None) -> tuple[bool, str]:
    text = str(text or "").strip()
    if not text:
        return False, "空内容"
    page = None
    ctx = None
    submitted = False
    try:
        ctx = await mgr.open_headed(identity)
        page = await ctx.new_page()
        await page.goto(profile_url(unique_id), wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(1800)
        if "/login" in str(page.url).lower():
            return False, "logged_out:账号未登录"
        entry = await _visible(page, MESSAGE_BUTTONS)
        if entry is None:
            return False, "未找到 TikTok 私信入口（对方可能关闭私信）"
        await entry.click(timeout=4000)
        await page.wait_for_timeout(1200)
        target = page
        frame = page.locator(MESSAGE_IFRAME).first
        try:
            if await frame.count():
                handle = await frame.element_handle()
                child = await handle.content_frame() if handle is not None else None
                if child is not None:
                    target = child
        except Exception:
            target = page
        editor = await _visible(target, MESSAGE_INPUTS)
        if editor is None:
            return False, "未找到 TikTok 私信输入框"
        await editor.click(timeout=6000)
        try:
            await editor.fill(text)
        except Exception:
            await target.keyboard.type(text, delay=35)
        await _mark_submit(on_submit)
        from app.licensing import require_active
        require_active()
        await target.keyboard.press("Enter")
        submitted = True
        await page.wait_for_timeout(1000)
        return False, "write_uncertain:私信已提交但 TikTok Web 未提供明确回执，请到会话核对"
    except Exception as exc:
        if submitted:
            return False, f"write_uncertain:私信提交后页面异常，请人工核对: {exc}"
        return False, f"TikTok 发私信异常: {exc}"
    finally:
        try:
            if ctx is not None:
                await ctx.close()
            elif page is not None:
                await page.close()
        except Exception:
            pass
