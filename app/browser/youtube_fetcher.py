"""YouTube 隔离浏览器采集器（Hybrid 回退通道：网络层 Innertube 拦截 + `ytInitialData` + DOM 兜底）。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from app.browser.human_interaction import DEFAULT_HUMAN_ENGINE, human_scroll, human_wait
from app.browser.identity import Identity
from app.browser.manager import BrowserManager
from app.platforms.youtube.extract import (
    extract_channel_profile,
    extract_channel_videos,
    extract_comments,
    extract_managed_channels,
    extract_search_channels,
    extract_search_videos,
    extract_subscriptions,
    extract_video_detail,
    parse_youtube_user,
)
from app.platforms.youtube.protocol import parse_youtube_count
from app.platforms.youtube.resolve import clean_youtube_handle, resolve_youtube_channel_target, resolve_youtube_video_ref
from app.platforms.youtube.selectors import (
    ACCOUNT_MENU_HANDLE_SELECTORS,
    ACCOUNT_MENU_NAME_SELECTORS,
    AUTH_BLOCKED_URL_TOKENS,
    AVATAR_BUTTON_SELECTORS,
    LOGGED_IN_SIGNALS,
    SIGNIN_BUTTON_SELECTORS,
    STUDIO_METRIC_SUBSCRIBERS,
)
from app.platforms.youtube.urls import (
    CHANNEL_SWITCHER_URL,
    HOME_URL,
    STUDIO_CHANNEL_URL,
    SUBSCRIPTIONS_URL,
    YOUTUBE_BASE,
    channel_search_url,
    channel_url,
    search_url,
    video_url,
)

_log = logging.getLogger("creatorhub.browser.youtube")


@asynccontextmanager
async def open_youtube_page(
    mgr: BrowserManager,
    identity: Identity,
    *,
    headed: bool = False,
    block_media: bool = False,
):
    """兼容 BrowserManager 的统一 YouTube 页面上下文（有头走 open_headed，无头走 new_page）。"""
    if headed:
        ctx = await mgr.open_headed(identity)
        page = await ctx.new_page()
    else:
        page = await mgr.new_page(identity, block_media=block_media)
    try:
        yield page
    finally:
        with suppress(Exception):
            await page.close()


def _is_email_str(val: str) -> bool:
    s = str(val or "").strip()
    return "@" in s and not s.startswith("@") and "." in s.split("@", 1)[1]


async def _check_page_auth_block(page) -> str:
    cur_url = str(page.url or "").lower()
    if "youtube.com/oops" in cur_url or "/disabled/" in cur_url:
        return f"verification_required:YouTube 账号已被限制或停用({page.url})"
    if "accounts.google.com/signin/challenge" in cur_url or "accounts.google.com/v3/signin/challenge" in cur_url:
        return f"verification_required:Google 账号触发安全校验({page.url})"
    if "accounts.google.com" in cur_url:
        return "logged_out:YouTube 跳转到 Google 登录页，登录态已失效"
    return ""


async def _page_logged_in(page) -> bool:
    for sel in LOGGED_IN_SIGNALS:
        try:
            if await page.locator(sel).count() > 0:
                return True
        except Exception:
            continue
    return False


async def _read_page_yt_initial(page) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    try:
        res = await page.evaluate(
            """() => ({
                initialData: window.ytInitialData || null,
                playerResponse: window.ytInitialPlayerResponse || null,
            })"""
        )
        if isinstance(res, dict):
            return (
                res.get("initialData") if isinstance(res.get("initialData"), dict) else {},
                res.get("playerResponse") if isinstance(res.get("playerResponse"), dict) else {},
            )
    except Exception:
        pass
    return {}, {}


async def _dismiss_consent_if_present(page) -> None:
    cur_url = str(page.url or "").lower()
    if "consent.youtube.com" not in cur_url and "consent.google.com" not in cur_url:
        return
    for sel in (
        'button[aria-label*="Accept all" i]',
        'button:has-text("Accept all")',
        'button:has-text("全部接受")',
        'form[action*="consent"] button[type="submit"]',
    ):
        loc = page.locator(sel).first
        try:
            if await loc.count() and await loc.is_visible():
                await DEFAULT_HUMAN_ENGINE.click_locator(loc, page=page, timeout=3000)
                await human_wait(page, 2000)
                return
        except Exception:
            continue


async def fetch_youtube_self_profile(
    mgr: BrowserManager,
    identity: Identity,
    *,
    headed: bool = False,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """在账号隔离浏览器中校验 YouTube 登录态并读取昵称、@handle、头像、频道列表与订阅数。"""
    async with open_youtube_page(mgr, identity, headed=headed) as page:
        await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=40000)
        await _dismiss_consent_if_present(page)
        await human_wait(page, 2200)

        auth_err = await _check_page_auth_block(page)
        if auth_err:
            return None, auth_err

        logged = False
        for _ in range(10):
            if await _page_logged_in(page):
                logged = True
                break
            # 若明确看到登录按钮，说明未登录
            for ssel in SIGNIN_BUTTON_SELECTORS:
                try:
                    if await page.locator(ssel).count() > 0:
                        return None, "logged_out:YouTube 首页显示未登录状态，请重新登录"
                except Exception:
                    pass
            await human_wait(page, 800)

        if not logged:
            return None, "logged_out:未检测到 YouTube 已登录头像按钮(#avatar-btn)"

        profile: Dict[str, Any] = {}
        # 1) 点击头像打开菜单读取 #account-name / #channel-handle / #channel-container a / #avatar img
        try:
            avatar_btn = page.locator("button#avatar-btn").first
            if await avatar_btn.count() and await avatar_btn.is_visible():
                await DEFAULT_HUMAN_ENGINE.click_locator(avatar_btn, page=page, timeout=4000)
                await human_wait(page, 1400)
                menu_info = await page.evaluate(
                    """() => {
                        const nameEl = document.querySelector('#account-name');
                        const handleEl = document.querySelector('#channel-handle');
                        const chLink = document.querySelector('#channel-container a, ytd-active-account-header-renderer a[href*="/@"], ytd-active-account-header-renderer a[href*="/channel/"]');
                        const avImg = document.querySelector('ytd-active-account-header-renderer #avatar img[src], button#avatar-btn img[src]');
                        return {
                            name: nameEl ? ((nameEl.getAttribute('title') || nameEl.innerText || '').trim()) : '',
                            handle: handleEl ? (handleEl.innerText || '').trim() : '',
                            profile_url: chLink ? (chLink.href || '') : '',
                            avatar: avImg ? (avImg.src || '') : '',
                        };
                    }"""
                )
                if isinstance(menu_info, dict):
                    if menu_info.get("name"):
                        profile["nickname"] = str(menu_info["name"]).strip()
                    if menu_info.get("handle"):
                        h_raw = str(menu_info["handle"]).strip()
                        if h_raw and not _is_email_str(h_raw):
                            profile["unique_id"] = h_raw
                    if menu_info.get("avatar"):
                        profile["avatar"] = str(menu_info["avatar"]).strip()
                    if menu_info.get("profile_url"):
                        hk = clean_youtube_handle(menu_info["profile_url"])
                        if hk.startswith("UC"):
                            profile["uid"] = hk
                        elif hk.startswith("@") and not profile.get("unique_id"):
                            profile["unique_id"] = hk
        except Exception as exc:
            _log.debug("youtube avatar menu read failed: %s", exc)

        # 2) 读取 channel_switcher 获取多频道列表
        try:
            await page.goto(CHANNEL_SWITCHER_URL, wait_until="domcontentloaded", timeout=35000)
            await human_wait(page, 1800)
            init_sw, _ = await _read_page_yt_initial(page)
            channels = extract_managed_channels(init_sw)
            if channels:
                profile["channels"] = channels
                sel = next((c for c in channels if c.get("is_selected")), channels[0])
                profile.setdefault("nickname", sel.get("name") or "")
                if not profile.get("unique_id") and sel.get("handle") and not _is_email_str(sel.get("handle")):
                    profile["unique_id"] = sel["handle"]
                if not profile.get("uid") and sel.get("channel_id"):
                    profile["uid"] = sel["channel_id"]
                if not profile.get("avatar") and sel.get("avatar"):
                    profile["avatar"] = sel["avatar"]
                if sel.get("subscribers_count"):
                    profile["follower_count"] = max(int(profile.get("follower_count") or 0), int(sel["subscribers_count"]))
        except Exception:
            pass

        # 3) 访问频道主页或 Studio 补齐订阅数与视频数
        ch_ref = str(profile.get("unique_id") or profile.get("uid") or "").strip()
        if ch_ref:
            try:
                await page.goto(channel_url(ch_ref), wait_until="domcontentloaded", timeout=35000)
                await human_wait(page, 1800)
                ch_init, _ = await _read_page_yt_initial(page)
                ch_prof = extract_channel_profile(ch_init)
                if ch_prof:
                    for k in ("uid", "sec_uid", "unique_id", "douyin_id", "nickname", "avatar", "signature"):
                        if ch_prof.get(k) and not profile.get(k):
                            profile[k] = ch_prof[k]
                    profile["follower_count"] = max(int(profile.get("follower_count") or 0), int(ch_prof.get("follower_count") or 0))
                    profile["aweme_count"] = max(int(profile.get("aweme_count") or 0), int(ch_prof.get("aweme_count") or 0))
            except Exception:
                pass

        if not profile.get("follower_count"):
            try:
                await page.goto(STUDIO_CHANNEL_URL, wait_until="domcontentloaded", timeout=30000)
                for _ in range(8):
                    await human_wait(page, 900)
                    txt = await page.evaluate(
                        """(sel) => {
                            const el = document.querySelector(sel);
                            return el ? (el.innerText || '').trim() : '';
                        }""",
                        STUDIO_METRIC_SUBSCRIBERS,
                    )
                    if txt:
                        profile["follower_count"] = parse_youtube_count(txt)
                        break
            except Exception:
                pass

        if not (profile.get("nickname") or profile.get("unique_id") or profile.get("uid")):
            return None, "profile_incomplete:已登录但暂未读取到 YouTube 频道资料"
        return parse_youtube_user(profile), ""


async def fetch_youtube_managed_channels(
    mgr: BrowserManager,
    identity: Identity,
) -> Tuple[List[Dict[str, Any]], str]:
    """经浏览器打开 `/channel_switcher` 同步当前账号下全部可选频道。"""
    async with open_youtube_page(mgr, identity, headed=False) as page:
        await page.goto(CHANNEL_SWITCHER_URL, wait_until="domcontentloaded", timeout=35000)
        await _dismiss_consent_if_present(page)
        await human_wait(page, 2000)
        auth_err = await _check_page_auth_block(page)
        if auth_err:
            return [], auth_err

        init_data, _ = await _read_page_yt_initial(page)
        channels = extract_managed_channels(init_data)
        if channels:
            return channels, ""

        # DOM 兜底
        dom_rows = await page.evaluate(
            """() => Array.from(document.querySelectorAll('ytd-account-item-renderer')).map(el => {
                const name = (el.querySelector('#channel-title')?.innerText || '').trim();
                const meta = Array.from(el.querySelectorAll('yt-formatted-string')).map(s => (s.innerText || '').trim()).filter(Boolean);
                const handle = meta.find(t => t.startsWith('@')) || '';
                const subs = meta.find(t => /subscriber|订阅|訂閱/i.test(t)) || '';
                const img = el.querySelector('img[src]')?.src || '';
                const selected = !!el.querySelector('#selected:not([hidden])');
                return { name, handle, subscribers_text: subs, avatar: img, is_selected: selected };
            }).filter(r => r.name || r.handle)"""
        )
        out: List[Dict[str, Any]] = []
        for r in (dom_rows or []):
            h = str(r.get("handle") or "").strip()
            n = str(r.get("name") or h.lstrip("@")).strip()
            out.append({
                "name": n,
                "handle": h,
                "channel_id": "",
                "link": channel_url(h) if h else YOUTUBE_BASE,
                "subscribers_text": str(r.get("subscribers_text") or ""),
                "subscribers_count": parse_youtube_count(r.get("subscribers_text") or ""),
                "avatar": str(r.get("avatar") or ""),
                "is_selected": bool(r.get("is_selected", False)),
            })
        return out, ("" if out else "empty")


async def fetch_youtube_channel_videos(
    mgr: BrowserManager,
    identity: Identity,
    channel_ref: str,
    *,
    tab: str = "videos",
    max_scrolls: int = 6,
    limit: int = 30,
    include_shorts: bool = False,
    block_media: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any], str]:
    """在账号隔离浏览器中抓取指定频道的视频（被动拦截 `/youtubei/v1/browse` + `ytInitialData`）。"""
    key, base_u = resolve_youtube_channel_target(channel_ref)
    target_url = channel_url(key or base_u or channel_ref, tab=tab or "videos")
    collected: Dict[str, Dict[str, Any]] = {}
    profile: Dict[str, Any] = {}

    async with open_youtube_page(mgr, identity, headed=False, block_media=block_media) as page:
        async def on_response(resp):
            if "/youtubei/v1/browse" not in resp.url or resp.status >= 400:
                return
            try:
                payload = await resp.json()
            except Exception:
                return
            if not isinstance(payload, dict):
                return
            rows, _ = extract_channel_videos(
                payload,
                default_author=str(profile.get("nickname") or ""),
                default_channel_id=str(profile.get("uid") or ""),
                default_handle=str(profile.get("unique_id") or ""),
                default_avatar=str(profile.get("avatar") or ""),
            )
            for r in rows:
                collected.setdefault(r["video_id"], r)

        page.on("response", on_response)
        await page.goto(target_url, wait_until="domcontentloaded", timeout=40000)
        await _dismiss_consent_if_present(page)
        await human_wait(page, 2200)

        auth_err = await _check_page_auth_block(page)
        if auth_err:
            return [], {}, auth_err

        init_data, _ = await _read_page_yt_initial(page)
        if init_data:
            profile = extract_channel_profile(init_data)
            rows, _ = extract_channel_videos(
                init_data,
                default_author=str(profile.get("nickname") or ""),
                default_channel_id=str(profile.get("uid") or ""),
                default_handle=str(profile.get("unique_id") or ""),
                default_avatar=str(profile.get("avatar") or ""),
            )
            for r in rows:
                collected.setdefault(r["video_id"], r)

        prev_n = len(collected)
        stale = 0
        for _ in range(max(0, int(max_scrolls))):
            if len(collected) >= limit:
                break
            await human_scroll(page, distance=1800)
            await human_wait(page, 1400)
            if len(collected) == prev_n:
                stale += 1
                if stale >= 2:
                    break
            else:
                stale = 0
                prev_n = len(collected)

        # DOM 兜底补充
        if not collected:
            dom_items = await page.evaluate(
                """() => {
                    const out = [];
                    const seen = new Set();
                    document.querySelectorAll('a#video-title-link, a#video-title, ytd-rich-item-renderer a[href*="/watch?v="], ytd-rich-item-renderer a[href*="/shorts/"]').forEach(a => {
                        const href = a.href || '';
                        const m = href.match(/(?:v=|\\/shorts\\/)([A-Za-z0-9_-]{11})/);
                        if (!m || seen.has(m[1])) return;
                        seen.add(m[1]);
                        const title = (a.getAttribute('title') || a.innerText || '').trim();
                        out.push({ video_id: m[1], title, desc: title, source_url: href, is_short: href.includes('/shorts/') });
                    });
                    return out;
                }"""
            )
            for r in (dom_items or []):
                vid = str(r.get("video_id") or "").strip()
                if vid and vid not in collected:
                    collected[vid] = {
                        **r,
                        "cover": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
                        "author_name": str(profile.get("nickname") or ""),
                        "channel_id": str(profile.get("uid") or ""),
                        "channel_handle": str(profile.get("unique_id") or ""),
                        "avatar": str(profile.get("avatar") or ""),
                    }

        if include_shorts and tab == "videos" and len(collected) < limit:
            shorts_u = channel_url(key or base_u or channel_ref, tab="shorts")
            try:
                await page.goto(shorts_u, wait_until="domcontentloaded", timeout=35000)
                await human_wait(page, 1800)
                s_init, _ = await _read_page_yt_initial(page)
                s_rows, _ = extract_channel_videos(
                    s_init,
                    default_author=str(profile.get("nickname") or ""),
                    default_channel_id=str(profile.get("uid") or ""),
                    default_handle=str(profile.get("unique_id") or ""),
                    default_avatar=str(profile.get("avatar") or ""),
                )
                for r in s_rows:
                    collected.setdefault(r["video_id"], r)
            except Exception:
                pass

    items = list(collected.values())[:limit]
    return items, profile, ("" if items else "empty")


async def fetch_youtube_search(
    mgr: BrowserManager,
    identity: Identity,
    keyword: str,
    *,
    max_scrolls: int = 5,
    limit: int = 30,
    upload_date: str = "",
    duration: str = "",
    sort_by: str = "",
    search_type: str = "video",
    block_media: bool = False,
) -> Tuple[List[Dict[str, Any]], str]:
    """在账号隔离浏览器中执行 YouTube 关键词搜索并滚动翻页采集。"""
    target_url = search_url(
        keyword,
        search_type=search_type,
        upload_date=upload_date,
        duration=duration,
        sort_by=sort_by,
    )
    collected: Dict[str, Dict[str, Any]] = {}

    async with open_youtube_page(mgr, identity, headed=False, block_media=block_media) as page:
        async def on_response(resp):
            if "/youtubei/v1/search" not in resp.url or resp.status >= 400:
                return
            try:
                payload = await resp.json()
            except Exception:
                return
            if not isinstance(payload, dict):
                return
            rows, _ = extract_search_videos(payload)
            for r in rows:
                collected.setdefault(r["video_id"], r)

        page.on("response", on_response)
        await page.goto(target_url, wait_until="domcontentloaded", timeout=40000)
        await _dismiss_consent_if_present(page)
        await human_wait(page, 2200)

        auth_err = await _check_page_auth_block(page)
        if auth_err:
            return [], auth_err

        init_data, _ = await _read_page_yt_initial(page)
        if init_data:
            rows, _ = extract_search_videos(init_data)
            for r in rows:
                collected.setdefault(r["video_id"], r)

        prev_n = len(collected)
        stale = 0
        for _ in range(max(0, int(max_scrolls))):
            if len(collected) >= limit:
                break
            await human_scroll(page, distance=2000)
            await human_wait(page, 1400)
            if len(collected) == prev_n:
                stale += 1
                if stale >= 2:
                    break
            else:
                stale = 0
                prev_n = len(collected)

    items = list(collected.values())[:limit]
    return items, ("" if items else "empty")


async def fetch_youtube_channels_search(
    mgr: BrowserManager,
    identity: Identity,
    keyword: str,
    *,
    max_scrolls: int = 4,
    limit: int = 30,
    block_media: bool = False,
) -> Tuple[List[Dict[str, Any]], str]:
    """在隔离浏览器中按关键词搜索 YouTube 频道列表。"""
    target_url = channel_search_url(keyword)
    collected: Dict[str, Dict[str, Any]] = {}

    async with open_youtube_page(mgr, identity, headed=False, block_media=block_media) as page:
        async def on_response(resp):
            if "/youtubei/v1/search" not in resp.url or resp.status >= 400:
                return
            try:
                payload = await resp.json()
            except Exception:
                return
            if not isinstance(payload, dict):
                return
            rows, _ = extract_search_channels(payload)
            for r in rows:
                collected.setdefault(r["sec_uid"], r)

        page.on("response", on_response)
        await page.goto(target_url, wait_until="domcontentloaded", timeout=40000)
        await _dismiss_consent_if_present(page)
        await human_wait(page, 2000)

        auth_err = await _check_page_auth_block(page)
        if auth_err:
            return [], auth_err

        init_data, _ = await _read_page_yt_initial(page)
        if init_data:
            rows, _ = extract_search_channels(init_data)
            for r in rows:
                collected.setdefault(r["sec_uid"], r)

        for _ in range(max(0, int(max_scrolls))):
            if len(collected) >= limit:
                break
            await human_scroll(page, distance=1800)
            await human_wait(page, 1200)

    items = list(collected.values())[:limit]
    return items, ("" if items else "empty")


async def fetch_youtube_video_detail(
    mgr: BrowserManager,
    identity: Identity,
    raw_url: str,
    *,
    block_media: bool = False,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """在账号隔离浏览器中打开视频页并提取完整 `ytInitialData` + `ytInitialPlayerResponse`。"""
    ref = resolve_youtube_video_ref(raw_url, allow_bare_id=True)
    if ref is None:
        return None, "invalid_video_ref:请输入有效的 YouTube 视频链接或 ID"

    async with open_youtube_page(mgr, identity, headed=False, block_media=block_media) as page:
        await page.goto(ref.watch_url, wait_until="domcontentloaded", timeout=40000)
        await _dismiss_consent_if_present(page)
        await human_wait(page, 2200)

        auth_err = await _check_page_auth_block(page)
        if auth_err:
            return None, auth_err

        init_data, player_resp = await _read_page_yt_initial(page)
        detail, cont = extract_video_detail(init_data, player_resp, source_url=ref.canonical_url)
        if detail is None:
            return None, "empty"
        detail["comments_continuation"] = cont
        return detail, ""


async def fetch_youtube_comments(
    mgr: BrowserManager,
    identity: Identity,
    raw_url: str,
    *,
    max_scrolls: int = 6,
    limit: int = 100,
    block_media: bool = False,
) -> Tuple[List[Dict[str, Any]], str]:
    """在账号隔离浏览器中滚动加载并拦截 `/youtubei/v1/next` 提取视频评论列表。"""
    ref = resolve_youtube_video_ref(raw_url, allow_bare_id=True)
    if ref is None:
        return [], "invalid_video_ref:请输入有效的 YouTube 视频链接或 ID"

    collected: Dict[str, Dict[str, Any]] = {}

    async with open_youtube_page(mgr, identity, headed=False, block_media=block_media) as page:
        async def on_response(resp):
            if "/youtubei/v1/next" not in resp.url or resp.status >= 400:
                return
            try:
                payload = await resp.json()
            except Exception:
                return
            if not isinstance(payload, dict):
                return
            batch, _ = extract_comments(payload, video_id=ref.video_id)
            for row in batch:
                collected.setdefault(row["comment_id"], row)

        page.on("response", on_response)
        await page.goto(ref.watch_url, wait_until="domcontentloaded", timeout=40000)
        await _dismiss_consent_if_present(page)
        await human_wait(page, 2200)

        auth_err = await _check_page_auth_block(page)
        if auth_err:
            return [], auth_err

        # 暂停视频播放，专注滚动评论区
        try:
            await page.evaluate("() => document.querySelectorAll('video').forEach(v => { try { v.pause(); } catch (_) {} })")
        except Exception:
            pass

        # 滚至评论区触发加载
        await human_scroll(page, distance=900)
        await human_wait(page, 2000)

        prev_n = len(collected)
        stale = 0
        for _ in range(max(1, int(max_scrolls))):
            if len(collected) >= limit:
                break
            await human_scroll(page, distance=1800)
            await human_wait(page, 1500)
            if len(collected) == prev_n:
                stale += 1
                if stale >= 2:
                    break
            else:
                stale = 0
                prev_n = len(collected)

        # DOM 兜底
        if not collected:
            dom_rows = await page.evaluate(
                """() => {
                    const out = [];
                    document.querySelectorAll('ytd-comment-thread-renderer').forEach(el => {
                        const text = (el.querySelector('#content-text')?.innerText || '').trim();
                        if (!text) return;
                        const author = (el.querySelector('#author-text')?.innerText || '').trim();
                        const timeA = el.querySelector('#published-time-text a');
                        const href = timeA ? (timeA.getAttribute('href') || '') : '';
                        const m = href.match(/[?&]lc=([^&]+)/);
                        const cid = m ? m[1] : '';
                        const likes = (el.querySelector('#vote-count-middle')?.innerText || '').trim();
                        out.push({ comment_id: cid, text, user_nickname: author, like_count: likes });
                    });
                    return out;
                }"""
            )
            for r in (dom_rows or []):
                text = str(r.get("text") or "").strip()
                if not text:
                    continue
                cid = str(r.get("comment_id") or "").strip() or f"dom_{len(collected)}"
                collected.setdefault(cid, {
                    "comment_id": cid,
                    "text": text,
                    "user_nickname": str(r.get("user_nickname") or "").strip(),
                    "user_sec_uid": str(r.get("user_nickname") or "").strip(),
                    "ip_label": "",
                    "like_count": parse_youtube_count(r.get("like_count") or 0),
                    "create_time": 0,
                    "reply_to": "",
                    "comment_url": video_url(ref.video_id, comment_id=cid),
                })

    items = list(collected.values())[:limit]
    return items, ("" if items else "empty")


async def fetch_youtube_subscriptions(
    mgr: BrowserManager,
    identity: Identity,
    *,
    max_scrolls: int = 5,
    limit: int = 200,
    block_media: bool = False,
) -> Tuple[List[Dict[str, Any]], str]:
    """在账号隔离浏览器中抓取当前账号已订阅频道列表 (`/feed/channels`)。"""
    collected: Dict[str, Dict[str, Any]] = {}
    async with open_youtube_page(mgr, identity, headed=False, block_media=block_media) as page:
        await page.goto(SUBSCRIPTIONS_URL, wait_until="domcontentloaded", timeout=40000)
        await _dismiss_consent_if_present(page)
        await human_wait(page, 2200)

        auth_err = await _check_page_auth_block(page)
        if auth_err:
            return [], auth_err

        # `/feed/channels` 未登录时可能停留在原 URL 并展示 Sign in 引导页而非跳转 Google 登录页
        if not await _page_logged_in(page):
            await human_wait(page, 1200)
            if not await _page_logged_in(page):
                for ssel in SIGNIN_BUTTON_SELECTORS:
                    try:
                        if await page.locator(ssel).count() > 0:
                            return [], "logged_out:YouTube 订阅列表页显示未登录，请重新登录"
                    except Exception:
                        pass

        init_data, _ = await _read_page_yt_initial(page)
        if init_data:
            rows, _ = extract_subscriptions(init_data)
            for r in rows:
                collected.setdefault(r["sec_uid"], r)

        for _ in range(max(0, int(max_scrolls))):
            if len(collected) >= limit:
                break
            await human_scroll(page, distance=1800)
            await human_wait(page, 1100)

        # 补充 DOM 列表
        dom_rows = await page.evaluate(
            """() => Array.from(document.querySelectorAll('ytd-channel-renderer')).map(el => {
                const a = el.querySelector('a#main-link, a.channel-link');
                const href = a ? (a.href || '') : '';
                const name = (el.querySelector('#channel-title, yt-formatted-string#text')?.innerText || '').trim();
                const subs = (el.querySelector('#subscribers')?.innerText || '').trim();
                const img = el.querySelector('#avatar img[src]')?.src || '';
                return { href, name, subs, img };
            }).filter(r => r.href || r.name)"""
        )
        for r in (dom_rows or []):
            hk = clean_youtube_handle(r.get("href") or "")
            if not hk or hk in collected:
                continue
            collected[hk] = {
                "uid": hk if hk.startswith("UC") else "",
                "sec_uid": hk,
                "unique_id": hk if hk.startswith("@") else "",
                "douyin_id": hk,
                "nickname": str(r.get("name") or hk.lstrip("@")),
                "avatar": str(r.get("img") or ""),
                "signature": "",
                "follower_count": parse_youtube_count(r.get("subs") or 0),
                "aweme_count": 0,
                "url": channel_url(hk),
            }

    items = list(collected.values())[:limit]
    return items, ("" if items else "empty")
