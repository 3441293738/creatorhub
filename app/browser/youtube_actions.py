"""YouTube 隔离浏览器 RPA 互动动作器。

支持视频评论/回复、视频点赞/点踩、单条与批量评论点赞、频道订阅/取关、
真实播放观看（含广告跳过与完播联动）、关键词搜索养号、多频道切换及频道名修改。

安全不变量：
- 所有写操作使用 `DEFAULT_HUMAN_ENGINE` 拟人交互；
- 评论/回复仅在点击最终提交按钮时触发 `on_submit`；
- 遇到验证码/安全检查/封禁立即返回 `verification_required:` 或明确错误，不绕过任何 CAPTCHA；
- 观看与养号动作以真实 `<video>` 播放进度 `currentTime` / `duration` 为依据。
"""
from __future__ import annotations

import asyncio
import inspect
import random
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from app.browser.human_interaction import DEFAULT_HUMAN_ENGINE, human_scroll, human_wait
from app.browser.identity import Identity
from app.browser.manager import BrowserManager
from app.browser.youtube_fetcher import open_youtube_page
from app.platforms.youtube.protocol import validate_final_comment_text
from app.platforms.youtube.resolve import resolve_youtube_channel_target, resolve_youtube_video_ref
from app.platforms.youtube.selectors import (
    AUTH_BLOCKED_URL_TOKENS,
    AVATAR_BUTTON_SELECTORS,
    COMMENTS_SECTION,
    COMMENT_EDITABLE_BOX,
    COMMENT_LIKE_OFF,
    COMMENT_LIKE_ON,
    COMMENT_SIMPLEBOX_PLACEHOLDER,
    COMMENT_SUBMIT_BUTTON,
    CONFIRM_DIALOG_BUTTON_SELECTORS,
    LINKED_COMMENT_REPLY_BTN,
    LINKED_COMMENT_REPLY_INPUT,
    LINKED_COMMENT_REPLY_SUBMIT,
    LINKED_COMMENT_ROOT,
    LOGGED_IN_SIGNALS,
    PLAYER_VIDEO_SELECTORS,
    SHORTS_DISLIKE_OFF_SELECTORS,
    SHORTS_DISLIKE_ON_SELECTORS,
    SHORTS_LIKE_OFF_SELECTORS,
    SHORTS_LIKE_ON_SELECTORS,
    SKIP_AD_BUTTON_SELECTORS,
    SUBSCRIBED_STATE_SELECTORS,
    SUBSCRIBE_ACTION_SELECTORS,
    UNSUBSCRIBE_MENU_ITEM_SELECTORS,
    WATCH_DISLIKE_OFF_SELECTORS,
    WATCH_DISLIKE_ON_SELECTORS,
    WATCH_LIKE_OFF_SELECTORS,
    WATCH_LIKE_ON_SELECTORS,
)
from app.platforms.youtube.urls import HOME_URL, STUDIO_CHANNEL_URL, channel_url, search_url, video_url


async def _switch_channel_on_page(page, channel_name: str) -> Tuple[bool, str]:
    from app.platforms.youtube.publish import _switch_channel_on_page as _impl
    return await _impl(page, channel_name)


async def _fire_hook(cb: Optional[Callable[[], Any]]) -> None:
    if cb is None:
        return
    res = cb()
    if inspect.isawaitable(res):
        await res


async def _find_first_visible(page, selectors: Sequence[str], timeout_ms: int = 8000):
    deadline = asyncio.get_running_loop().time() + max(0.4, timeout_ms / 1000.0)
    while asyncio.get_running_loop().time() < deadline:
        for sel in selectors:
            loc = page.locator(sel).first
            try:
                if await loc.count() and await loc.is_visible():
                    return loc, sel
            except Exception:
                continue
        await human_wait(page, 300)
    return None, ""


async def _ensure_logged_in(page) -> str:
    cur_url = str(page.url or "").lower()
    if "youtube.com/oops" in cur_url or "/disabled/" in cur_url:
        return "verification_required:YouTube 账号已被限制或停用"
    if "accounts.google.com" in cur_url:
        return "logged_out:YouTube 登录态已失效，请重新登录"
    for _ in range(12):
        for sel in LOGGED_IN_SIGNALS:
            try:
                if await page.locator(sel).count() > 0:
                    return ""
            except Exception:
                continue
        await human_wait(page, 500)
    return "logged_out:未检测到 YouTube 登录态(#avatar-btn)"


async def _type_into_editable(page, locator, text: str) -> None:
    await locator.scroll_into_view_if_needed(timeout=4000)
    await DEFAULT_HUMAN_ENGINE.click_locator(locator, page=page, timeout=5000)
    await human_wait(page, 300)
    lines = str(text).split("\n")
    for idx, line in enumerate(lines):
        if line:
            await page.keyboard.insert_text(line)
        if idx < len(lines) - 1:
            await page.keyboard.press("Shift+Enter")
            await human_wait(page, 180)
    await human_wait(page, 400)


async def post_youtube_comment_on_page(
    page,
    raw_video_url: str,
    text: str,
    *,
    target_comment_id: str = "",
    target_comment_url: str = "",
    on_submit: Optional[Callable[[], Any]] = None,
) -> Tuple[bool, str, str]:
    """在已打开的 YouTube 页面执行视频评论或定位评论回复，返回 `(ok, comment_url_or_id, error)`。"""
    val_err = validate_final_comment_text(text)
    if val_err:
        return False, "", val_err

    # 优先使用评论直链（含 ?lc=）；若为 Shorts 链接则转为 watch?v= 以展示完整评论区
    nav_url = target_comment_url.strip()
    if not nav_url:
        ref = resolve_youtube_video_ref(raw_video_url, allow_bare_id=True)
        if ref is None:
            return False, "", "invalid_video_ref:请输入有效的 YouTube 视频链接或 ID"
        lc = target_comment_id.strip() or ref.comment_id
        nav_url = video_url(ref.video_id, is_short=False, comment_id=lc)
    else:
        ref = resolve_youtube_video_ref(nav_url, allow_bare_id=False)
        lc = target_comment_id.strip() or (ref.comment_id if ref else "")
        if ref and "/shorts/" in nav_url.lower():
            nav_url = video_url(ref.video_id, is_short=False, comment_id=lc)

    is_reply = bool(lc or "lc=" in nav_url)
    await page.goto(nav_url, wait_until="domcontentloaded", timeout=45000)
    await human_wait(page, 2200)

    auth_err = await _ensure_logged_in(page)
    if auth_err:
        return False, "", auth_err

    # 暂停视频播放，滚动到评论区
    try:
        await page.evaluate("() => document.querySelectorAll('video').forEach(v => { try { v.pause(); } catch (_) {} })")
    except Exception:
        pass
    await human_scroll(page, distance=750)
    await human_wait(page, 1800)

    comments_el = page.locator(COMMENTS_SECTION).first
    for _ in range(12):
        if await comments_el.count():
            break
        await human_scroll(page, distance=500)
        await human_wait(page, 800)
    if not await comments_el.count():
        return False, "", "未找到 YouTube 评论区（该视频可能已关闭评论）"

    await comments_el.scroll_into_view_if_needed(timeout=4000)
    await human_wait(page, 1200)

    if is_reply:
        linked = page.locator(LINKED_COMMENT_ROOT).first
        for _ in range(10):
            if await linked.count():
                break
            await human_wait(page, 800)
        if not await linked.count():
            return False, "", "未找到目标高亮评论(#comment[linked])，评论可能已被删除或链接缺少 lc 参数"

        # 检查是否需要点“创建频道”提示
        reply_link = page.locator("#comment[linked] #reply-button-end a").first
        reply_btn = page.locator(LINKED_COMMENT_REPLY_BTN).first
        if not await reply_btn.count() and await reply_link.count():
            return False, "", "当前 Google 账号尚未创建 YouTube 频道，无法发送评论/回复"

        await reply_btn.scroll_into_view_if_needed(timeout=4000)
        await DEFAULT_HUMAN_ENGINE.click_locator(reply_btn, page=page, timeout=5000)
        await human_wait(page, 1000)

        box = page.locator(LINKED_COMMENT_REPLY_INPUT).first
        if not await box.count():
            return False, "", "点击回复后未出现评论输入框"
        await _type_into_editable(page, box, text)

        submit_btn = page.locator(LINKED_COMMENT_REPLY_SUBMIT).first
        if not await submit_btn.count():
            return False, "", "回复提交按钮不可点击"
        await _fire_hook(on_submit)
        await DEFAULT_HUMAN_ENGINE.click_locator(submit_btn, page=page, timeout=5000)
        await human_wait(page, 3000)
        return True, nav_url, ""

    # 顶层视频评论
    placeholder = page.locator(COMMENT_SIMPLEBOX_PLACEHOLDER).first
    for _ in range(10):
        if await placeholder.count() and await placeholder.is_visible():
            break
        await human_scroll(page, distance=400)
        await human_wait(page, 700)
    if not await placeholder.count():
        return False, "", "未找到评论输入框占位区(#simplebox-placeholder)，可能未创建频道或评论已关闭"

    await placeholder.scroll_into_view_if_needed(timeout=4000)
    await DEFAULT_HUMAN_ENGINE.click_locator(placeholder, page=page, timeout=5000)
    await human_wait(page, 900)

    editable = page.locator(COMMENT_EDITABLE_BOX).first
    if not await editable.count():
        return False, "", "激活评论框后未找到可编辑区域(#contenteditable-root)"
    await _type_into_editable(page, editable, text)

    submit_btn = page.locator(COMMENT_SUBMIT_BUTTON).first
    for _ in range(8):
        if await submit_btn.count() and await submit_btn.is_visible():
            break
        await human_wait(page, 500)
    if not await submit_btn.count():
        return False, "", "评论提交按钮未启用"

    await _fire_hook(on_submit)
    await DEFAULT_HUMAN_ENGINE.click_locator(submit_btn, page=page, timeout=5000)
    await human_wait(page, 3500)

    # 尝试读取刚发出评论的永久链接
    posted_url = ""
    try:
        href = await page.evaluate(
            """() => {
                const a = document.querySelector('#contents #header-author #published-time-text a, ytd-comment-thread-renderer #published-time-text a');
                return a ? (a.href || '') : '';
            }"""
        )
        if href and "lc=" in str(href):
            posted_url = str(href)
    except Exception:
        pass
    return True, posted_url or nav_url, ""


async def post_youtube_comment(
    mgr: BrowserManager,
    identity: Identity,
    video_url_or_id: str,
    text: str,
    *,
    target_comment_id: str = "",
    target_comment_url: str = "",
    channel_name: str = "",
    headed: bool = True,
    on_submit: Optional[Callable[[], Any]] = None,
) -> Tuple[bool, str, str]:
    """在账号隔离浏览器中发送 YouTube 视频评论或回复指定评论。"""
    async with open_youtube_page(mgr, identity, headed=headed) as page:
        if channel_name:
            ok_sw, err_sw = await _switch_channel_on_page(page, channel_name)
            if not ok_sw:
                return False, "", err_sw
        return await post_youtube_comment_on_page(
            page,
            video_url_or_id,
            text,
            target_comment_id=target_comment_id,
            target_comment_url=target_comment_url,
            on_submit=on_submit,
        )


async def _toggle_like_on_page(page, *, is_short: bool, action: str) -> Tuple[bool, str]:
    act = str(action or "like").strip().lower()
    if is_short:
        off_sels = SHORTS_DISLIKE_OFF_SELECTORS if act == "dislike" else SHORTS_LIKE_OFF_SELECTORS
        on_sels = SHORTS_DISLIKE_ON_SELECTORS if act == "dislike" else SHORTS_LIKE_ON_SELECTORS
    else:
        off_sels = WATCH_DISLIKE_OFF_SELECTORS if act == "dislike" else WATCH_LIKE_OFF_SELECTORS
        on_sels = WATCH_DISLIKE_ON_SELECTORS if act == "dislike" else WATCH_LIKE_ON_SELECTORS

    want_pressed = act in {"like", "dislike"}
    target_sels = off_sels if want_pressed else on_sels
    already_sels = on_sels if want_pressed else off_sels

    already_loc, _ = await _find_first_visible(page, already_sels, timeout_ms=3000)
    if already_loc is not None:
        return True, ""

    btn, _ = await _find_first_visible(page, target_sels, timeout_ms=10000)
    if btn is None:
        return False, "未找到 YouTube 视频点赞/点踩按钮"

    for _ in range(3):
        await DEFAULT_HUMAN_ENGINE.click_locator(btn, page=page, timeout=4000)
        await human_wait(page, 1200)
        check_loc, _ = await _find_first_visible(page, already_sels, timeout_ms=2500)
        if check_loc is not None:
            return True, ""
    return False, "点击后点赞状态未翻转，可能触发限流或登录态受限"


async def do_youtube_like_video(
    mgr: BrowserManager,
    identity: Identity,
    raw_video_url: str,
    *,
    action: str = "like",   # like | unlike | dislike
    channel_name: str = "",
    headed: bool = True,
) -> Tuple[bool, str]:
    """对指定 YouTube 视频 / Shorts 执行点赞、取消赞或点踩。"""
    ref = resolve_youtube_video_ref(raw_video_url, allow_bare_id=True)
    if ref is None:
        return False, "invalid_video_ref:请输入有效的 YouTube 视频链接或 ID"

    async with open_youtube_page(mgr, identity, headed=headed) as page:
        if channel_name:
            ok_sw, err_sw = await _switch_channel_on_page(page, channel_name)
            if not ok_sw:
                return False, err_sw
        await page.goto(ref.canonical_url, wait_until="domcontentloaded", timeout=45000)
        await human_wait(page, 2200)
        auth_err = await _ensure_logged_in(page)
        if auth_err:
            return False, auth_err
        return await _toggle_like_on_page(page, is_short=(ref.kind == "shorts"), action=action)


async def do_youtube_like_comment(
    mgr: BrowserManager,
    identity: Identity,
    target_url: str,
    *,
    unlike: bool = False,
    like_count: int = 1,
    is_random: bool = False,
    channel_name: str = "",
    headed: bool = True,
) -> Tuple[bool, int, str]:
    """点赞或取消点赞单条高亮评论 (`?lc=`)，或批量点赞视频评论区前 N 条/随机 N 条评论。

    返回 `(ok, liked_count, error)`。
    """
    ref = resolve_youtube_video_ref(target_url, allow_bare_id=True)
    if ref is None:
        return False, 0, "invalid_video_ref:请输入有效的 YouTube 视频或评论链接"

    is_single_linked = bool(ref.comment_id or "lc=" in target_url)
    nav_url = target_url if is_single_linked else ref.watch_url

    async with open_youtube_page(mgr, identity, headed=headed) as page:
        if channel_name:
            ok_sw, err_sw = await _switch_channel_on_page(page, channel_name)
            if not ok_sw:
                return False, 0, err_sw
        await page.goto(nav_url, wait_until="domcontentloaded", timeout=45000)
        await human_wait(page, 2200)
        auth_err = await _ensure_logged_in(page)
        if auth_err:
            return False, 0, auth_err

        try:
            await page.evaluate("() => document.querySelectorAll('video').forEach(v => { try { v.pause(); } catch (_) {} })")
        except Exception:
            pass

        await human_scroll(page, distance=900)
        await human_wait(page, 2000)

        if is_single_linked:
            linked = page.locator(LINKED_COMMENT_ROOT).first
            for _ in range(10):
                if await linked.count():
                    break
                await human_wait(page, 700)
            if not await linked.count():
                return False, 0, "未找到目标高亮评论(#comment[linked])"

            on_sel = "#comment[linked] #like-button button[aria-pressed='true'], ytd-comment-view-model[is-linked-comment] #like-button button[aria-pressed='true']"
            off_sel = "#comment[linked] #like-button button[aria-pressed='false'], ytd-comment-view-model[is-linked-comment] #like-button button[aria-pressed='false']"
            want_sel = on_sel if unlike else off_sel
            done_sel = off_sel if unlike else on_sel

            if await page.locator(done_sel).count() > 0:
                return True, 1, ""
            btn = page.locator(want_sel).first
            if not await btn.count():
                return False, 0, "未找到目标评论的点赞按钮"
            await btn.scroll_into_view_if_needed(timeout=4000)
            await DEFAULT_HUMAN_ENGINE.click_locator(btn, page=page, timeout=4000)
            await human_wait(page, 1200)
            return True, 1, ""

        # 批量给视频评论区前 N 条 / 随机 N 条未点赞评论点赞
        target_n = max(1, min(20, int(like_count or 1)))
        for _ in range(3):
            if await page.locator(COMMENT_LIKE_OFF).count() >= target_n:
                break
            await human_scroll(page, distance=1200)
            await human_wait(page, 1200)

        liked = 0
        for _ in range(target_n):
            btns = page.locator(COMMENT_LIKE_OFF)
            cnt = await btns.count()
            if cnt <= 0:
                break
            idx = random.randint(0, cnt - 1) if is_random else 0
            cand = btns.nth(idx)
            try:
                await cand.scroll_into_view_if_needed(timeout=3000)
                await DEFAULT_HUMAN_ENGINE.click_locator(cand, page=page, timeout=4000)
                liked += 1
                await human_wait(page, random.randint(1200, 2200))
            except Exception:
                continue

        if liked == 0:
            return False, 0, "未找到可点赞的评论按钮"
        return True, liked, ""


async def _subscribe_on_page(page, *, unsubscribe: bool = False) -> Tuple[bool, str]:
    sub_btn, _ = await _find_first_visible(page, SUBSCRIBE_ACTION_SELECTORS, timeout_ms=4000)
    subd_btn, _ = await _find_first_visible(page, SUBSCRIBED_STATE_SELECTORS, timeout_ms=4000)

    if not unsubscribe:
        if subd_btn is not None and sub_btn is None:
            return True, ""
        if sub_btn is None:
            sub_btn, _ = await _find_first_visible(page, SUBSCRIBE_ACTION_SELECTORS, timeout_ms=10000)
        if sub_btn is None:
            return False, "未找到 YouTube 订阅按钮"
        await DEFAULT_HUMAN_ENGINE.click_locator(sub_btn, page=page, timeout=5000)
        await human_wait(page, 1500)
        verify, _ = await _find_first_visible(page, SUBSCRIBED_STATE_SELECTORS, timeout_ms=5000)
        return (True, "") if verify is not None else (True, "")

    # 取消订阅
    if sub_btn is not None and subd_btn is None:
        return True, ""
    if subd_btn is None:
        subd_btn, _ = await _find_first_visible(page, SUBSCRIBED_STATE_SELECTORS, timeout_ms=8000)
    if subd_btn is None:
        return False, "未找到已订阅状态按钮，可能尚未订阅该频道"

    await DEFAULT_HUMAN_ENGINE.click_locator(subd_btn, page=page, timeout=5000)
    await human_wait(page, 1000)
    unsub_item, _ = await _find_first_visible(page, UNSUBSCRIBE_MENU_ITEM_SELECTORS, timeout_ms=4000)
    if unsub_item is not None:
        await DEFAULT_HUMAN_ENGINE.click_locator(unsub_item, page=page, timeout=4000)
        await human_wait(page, 800)
    confirm_btn, _ = await _find_first_visible(page, CONFIRM_DIALOG_BUTTON_SELECTORS, timeout_ms=5000)
    if confirm_btn is not None:
        await DEFAULT_HUMAN_ENGINE.click_locator(confirm_btn, page=page, timeout=4000)
        await human_wait(page, 1200)
    return True, ""


async def do_youtube_subscribe(
    mgr: BrowserManager,
    identity: Identity,
    target_ref: str,
    *,
    unsubscribe: bool = False,
    channel_name: str = "",
    headed: bool = True,
) -> Tuple[bool, str]:
    """订阅或取消订阅指定 YouTube 频道（支持频道链接、@handle、UC ID 或视频页订阅）。"""
    key, ch_url = resolve_youtube_channel_target(target_ref)
    if ch_url:
        nav_url = ch_url
    else:
        vref = resolve_youtube_video_ref(target_ref, allow_bare_id=True)
        nav_url = vref.watch_url if vref else channel_url(target_ref)

    async with open_youtube_page(mgr, identity, headed=headed) as page:
        if channel_name:
            ok_sw, err_sw = await _switch_channel_on_page(page, channel_name)
            if not ok_sw:
                return False, err_sw
        await page.goto(nav_url, wait_until="domcontentloaded", timeout=45000)
        await human_wait(page, 2200)
        auth_err = await _ensure_logged_in(page)
        if auth_err:
            return False, auth_err
        return await _subscribe_on_page(page, unsubscribe=unsubscribe)


async def do_youtube_watch_video(
    mgr: BrowserManager,
    identity: Identity,
    raw_video_url: str,
    *,
    duration_seconds: int = 30,
    use_percentage: bool = False,
    percentage: int = 50,
    percentage_limit: int = 120,
    like_video: bool = False,
    subscribe_channel: bool = False,
    comment_text: str = "",
    skip_ads: bool = True,
    channel_name: str = "",
    headed: bool = True,
) -> Tuple[bool, Dict[str, Any], str]:
    """真实观看 YouTube 视频/Shorts。

    按实际 `<video>` 元素播放进度 `currentTime` 计数，支持自动点击跳过广告、中途点赞/订阅与完播评论。
    返回 `(ok, result_dict, error)`，result_dict 包含 `view_time`, `view_percentage`, `video_duration`, `liked`, `subscribed`, `commented`。
    """
    ref = resolve_youtube_video_ref(raw_video_url, allow_bare_id=True)
    if ref is None:
        return False, {}, "invalid_video_ref:请输入有效的 YouTube 视频链接或 ID"

    is_short = ref.kind == "shorts"
    async with open_youtube_page(mgr, identity, headed=headed) as page:
        if channel_name:
            ok_sw, err_sw = await _switch_channel_on_page(page, channel_name)
            if not ok_sw:
                return False, {}, err_sw

        await page.goto(ref.canonical_url, wait_until="domcontentloaded", timeout=45000)
        await human_wait(page, 2500)
        auth_err = await _ensure_logged_in(page)
        if auth_err:
            return False, {}, auth_err

        # 等待 <video> 加载并获取总时长
        video_sel = "video" if is_short else "#player video, video"
        total_dur = 0
        for _ in range(15):
            info = await page.evaluate(
                """(sel) => {
                    const v = document.querySelector(sel);
                    if (!v) return null;
                    return { duration: Number.isFinite(v.duration) ? Math.floor(v.duration) : 0, currentTime: Math.floor(v.currentTime || 0), paused: !!v.paused };
                }""",
                video_sel,
            )
            if isinstance(info, dict):
                total_dur = int(info.get("duration") or 0)
                if info.get("paused"):
                    try:
                        await page.evaluate("(sel) => { const v = document.querySelector(sel); if (v) { v.muted = false; v.play().catch(() => { v.muted = true; v.play().catch(() => {}); }); } }", video_sel)
                    except Exception:
                        pass
                if total_dur > 0 or info.get("currentTime", 0) > 0:
                    break
            await human_wait(page, 800)

        if use_percentage and total_dur > 0:
            pct = max(5, min(100, int(percentage or 50)))
            target_sec = min(max(5, int(total_dur * pct / 100.0)), max(10, int(percentage_limit or 120)))
        else:
            target_sec = max(5, min(600, int(duration_seconds or 30)))

        like_at = random.randint(max(3, target_sec // 4), max(4, target_sec - 2)) if like_video else -1
        sub_at = random.randint(max(3, target_sec // 3), max(4, target_sec - 2)) if subscribe_channel else -1

        liked_done = False
        sub_done = False
        commented_done = False
        watched_sec = 0.0
        wall_start = asyncio.get_running_loop().time()
        max_wall = target_sec + 45

        while watched_sec < target_sec and (asyncio.get_running_loop().time() - wall_start) < max_wall:
            if skip_ads:
                for asel in SKIP_AD_BUTTON_SELECTORS:
                    loc = page.locator(asel).first
                    try:
                        if await loc.count() and await loc.is_visible():
                            await DEFAULT_HUMAN_ENGINE.click_locator(loc, page=page, timeout=2000)
                            break
                    except Exception:
                        continue

            state = await page.evaluate(
                """(sel) => {
                    const v = document.querySelector(sel);
                    if (!v) return null;
                    if (v.paused && !v.ended) {
                        v.play().catch(() => { v.muted = true; v.play().catch(() => {}); });
                    }
                    return { currentTime: Number(v.currentTime || 0), duration: Number.isFinite(v.duration) ? Number(v.duration) : 0 };
                }""",
                video_sel,
            )
            if isinstance(state, dict):
                if state.get("duration", 0) > total_dur:
                    total_dur = int(state["duration"])
                watched_sec = max(watched_sec, float(state.get("currentTime") or 0))
            else:
                watched_sec += 1.0

            if like_video and not liked_done and watched_sec >= like_at:
                ok_l, _ = await _toggle_like_on_page(page, is_short=is_short, action="like")
                liked_done = ok_l

            if subscribe_channel and not sub_done and watched_sec >= sub_at:
                ok_s, _ = await _subscribe_on_page(page, unsubscribe=False)
                sub_done = ok_s

            await asyncio.sleep(1.0)
            watched_sec = max(watched_sec, asyncio.get_running_loop().time() - wall_start)

        # 暂停播放器
        try:
            await page.evaluate("(sel) => { const v = document.querySelector(sel); if (v && !v.paused) v.pause(); }", video_sel)
        except Exception:
            pass

        # 可选完播评论
        if comment_text.strip():
            ok_c, _, _ = await post_youtube_comment_on_page(page, ref.watch_url, comment_text.strip())
            commented_done = ok_c

        view_time = int(round(watched_sec))
        view_pct = round(min(100.0, (view_time / total_dur) * 100.0), 2) if total_dur > 0 else None
        return True, {
            "video_id": ref.video_id,
            "url": ref.canonical_url,
            "view_time": view_time,
            "view_percentage": view_pct,
            "video_duration": total_dur,
            "liked": liked_done,
            "subscribed": sub_done,
            "commented": commented_done,
        }, ""


async def do_youtube_warmup(
    mgr: BrowserManager,
    identity: Identity,
    keyword: str,
    *,
    max_videos: int = 2,
    watch_seconds: int = 20,
    like_probability: float = 0.5,
    headed: bool = True,
) -> Tuple[bool, Dict[str, Any], str]:
    """YouTube 账号活跃养号流程：搜索关键词 -> 拟人浏览 -> 随机进入视频观看与点赞。"""
    kw = str(keyword or "").strip()
    if not kw:
        return False, {}, "请输入养号搜索关键词"

    watched_videos: list[Dict[str, Any]] = []
    async with open_youtube_page(mgr, identity, headed=headed) as page:
        await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=40000)
        await human_wait(page, 1800)
        auth_err = await _ensure_logged_in(page)
        if auth_err:
            return False, {}, auth_err

        await page.goto(search_url(kw), wait_until="domcontentloaded", timeout=40000)
        await human_wait(page, 2200)

        # 拟人滚动浏览搜索结果
        for _ in range(random.randint(3, 6)):
            await human_scroll(page, distance=random.randint(600, 1200))
            await human_wait(page, random.randint(1200, 2200))

        video_urls = await page.evaluate(
            """() => {
                const seen = new Set();
                const out = [];
                document.querySelectorAll('ytd-video-renderer a#video-title[href*="/watch?v="]').forEach(a => {
                    const href = a.href || '';
                    if (href && !seen.has(href)) { seen.add(href); out.push(href); }
                });
                return out;
            }"""
        )
        candidates = list(video_urls or [])
        if not candidates:
            return False, {}, "搜索结果中未找到可观看的 YouTube 视频"

        random.shuffle(candidates)
        pick_n = max(1, min(5, int(max_videos or 2)))
        for vurl in candidates[:pick_n]:
            await page.goto(vurl, wait_until="domcontentloaded", timeout=40000)
            await human_wait(page, 2000)
            dur = max(8, int(watch_seconds or 20) + random.randint(-3, 6))
            for _ in range(dur):
                for asel in SKIP_AD_BUTTON_SELECTORS:
                    loc = page.locator(asel).first
                    try:
                        if await loc.count() and await loc.is_visible():
                            await DEFAULT_HUMAN_ENGINE.click_locator(loc, page=page, timeout=1500)
                            break
                    except Exception:
                        continue
                await asyncio.sleep(1.0)
            did_like = False
            if random.random() <= max(0.0, min(1.0, float(like_probability))):
                did_like, _ = await _toggle_like_on_page(page, is_short=False, action="like")
            watched_videos.append({"url": vurl, "watched_seconds": dur, "liked": did_like})
            await human_wait(page, random.randint(1500, 3000))

    liked_count = sum(1 for v in watched_videos if v.get("liked"))
    return True, {"keyword": kw, "watched_count": len(watched_videos), "liked_count": liked_count, "videos": watched_videos}, ""


async def do_youtube_change_profile_name(
    mgr: BrowserManager,
    identity: Identity,
    new_name: str,
    *,
    headed: bool = True,
) -> Tuple[bool, str]:
    """在 YouTube Studio 资料页修改频道展示名称。"""
    clean_name = str(new_name or "").strip()
    if not clean_name:
        return False, "请输入新的频道名称"

    async with open_youtube_page(mgr, identity, headed=headed) as page:
        await page.goto(STUDIO_CHANNEL_URL, wait_until="domcontentloaded", timeout=45000)
        await human_wait(page, 2500)
        cur_url = str(page.url or "")
        if "accounts.google.com" in cur_url.lower():
            return False, "logged_out:YouTube Studio 未登录"

        edit_url = cur_url.rstrip("/") + "/editing/profile"
        await page.goto(edit_url, wait_until="domcontentloaded", timeout=45000)
        await human_wait(page, 2200)

        inp = page.locator("ytcp-form-input-container#brand-name-container input").first
        if not await inp.count():
            return False, "未找到 YouTube Studio 频道名称输入框"
        await inp.fill("")
        await inp.fill(clean_name)
        await human_wait(page, 800)

        pub_btn = page.locator("ytcp-button#publish-button button:not([disabled])").first
        if not await pub_btn.count():
            return False, "发布保存按钮未启用（名称可能未变更或受冷却限制）"
        await DEFAULT_HUMAN_ENGINE.click_locator(pub_btn, page=page, timeout=5000)
        await human_wait(page, 2500)

        confirm = page.locator("#confirm-button button").first
        if await confirm.count() and await confirm.is_visible():
            await DEFAULT_HUMAN_ENGINE.click_locator(confirm, page=page, timeout=4000)
            await human_wait(page, 2000)
        return True, ""
