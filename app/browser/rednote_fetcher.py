"""海外小红书（Rednote）页面拦截与 SSR 状态提取器。

严格使用 ``www.rednote.com`` / ``webapi.rednote.com`` / ``creator.rednote.com``，
由真实登录浏览器自动完成请求签名，兼容中英文界面元素与 SSR ``window.__INITIAL_STATE__``。
"""
from __future__ import annotations

import json
import urllib.parse
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlsplit

from ..platforms.rednote.domains import (
    FRONT_HOME_PAGE_URL,
    HOME_PAGE_URL,
    WWW_URL,
    is_rednote_host,
)
from ..platforms.rednote.resolve import (
    build_rednote_note_url,
    build_rednote_profile_url,
)
from .human_interaction import (
    DEFAULT_HUMAN_ENGINE,
    human_click_first,
    human_fill_first,
    human_scroll,
    human_wait,
)
from .identity import Identity
from .manager import BrowserManager
from .xhs_fetcher import (
    COMMENT_API,
    FAVORITE_ALL_API,
    FAVORITE_BOARDS_API,
    FAVORITE_BOARD_NOTES_API,
    FEED_API,
    OTHERINFO_API,
    USER_ME_API,
    USER_POSTED_API,
    _ResponseInbox,
    _SSR_NOTES_JS,
    _SSR_NOTE_COMMENTS_JS,
    _SSR_NOTE_DETAIL_JS,
    _SSR_PROFILE_AUTHOR_JS,
    _decamel,
    _favorite_notes,
    _is_search_notes_response,
    _scroll_collection,
    _visible_page_scope,
    _xhs_reading_pause,
    _xhs_search_value,
)

CREATOR_POSTED_HINTS = (
    "/api/galaxy/v2/creator/note/user/posted",
    "/api/galaxy/creator/note/user/posted",
    "/web_api/sns/v5/creator/note/user/posted",
)


_PAGE_DIAG_JS = """() => {
  const isVisible = el => {
    if (!el) return false;
    const style = window.getComputedStyle(el);
    if (!style || style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) {
      return false;
    }
    const rect = el.getBoundingClientRect();
    return rect.width > 4 && rect.height > 4;
  };
  const anyVisible = selectors => {
    for (const sel of selectors) {
      for (const el of document.querySelectorAll(sel)) {
        if (isVisible(el)) return (el.innerText || el.textContent || sel).trim().slice(0, 160);
      }
    }
    return '';
  };
  const hasContent = Boolean(
    anyVisible([
      '#noteContainer',
      '.note-container',
      '.note-scroller',
      '.feeds-container .note-item',
      '.user-page',
      '.search-layout',
    ])
  );
  const captchaUi = anyVisible([
    '.red-captcha',
    '.captcha-container',
    '[class*="captcha-modal"]',
    '[class*="verify-modal"]',
  ]);
  const loginModal = anyVisible([
    '.login-container',
    '.login-modal',
    'button.login-btn',
    '.side-bar .login-btn',
  ]);
  const toastText = anyVisible([
    '.reds-toast',
    '.d-toast',
    '.toast',
    '.error-wrapper',
    '.access-limit',
    '.limit-container',
  ]);
  const bodyShort = hasContent
    ? ''
    : ((document.body && document.body.innerText) || '').replace(/\\s+/g, ' ').trim().slice(0, 400);
  return { hasContent, captchaUi, loginModal, toastText, bodyShort };
}"""


async def _rednote_page_failure(page) -> str:
    """Return an explicit auth/risk signal only when ``rednote.com`` proves it.

    Never substring-match user-generated note captions, comments, or feed cards
    when normal page content (`#noteContainer`, `.feeds-container`, `.user-page`)
    is already mounted; inspect dedicated captcha/login modals, platform toasts,
    and standalone error pages instead.
    """
    current_url = str(getattr(page, "url", "") or "").lower()
    if "xiaohongshu.com" in current_url:
        return "redirected:海外小红书页面被跳转至国内小红书，请检查海外代理网络与登录态"
    if "/website-login/captcha" in current_url or "error_code=300012" in current_url:
        return "captcha:海外小红书要求完成安全验证"
    if "passport" in current_url or "/login" in current_url:
        return "logged_out:海外小红书登录态已失效"

    diag: dict = {}
    try:
        res = await page.evaluate(_PAGE_DIAG_JS)
        if isinstance(res, dict):
            diag = res
    except Exception:
        diag = {}

    if diag.get("captchaUi"):
        return f"captcha:海外小红书要求完成安全验证：{str(diag['captchaUi'])[:80]}"

    risk_surface = " ".join(
        part for part in (str(diag.get("toastText") or ""), str(diag.get("bodyShort") or "")) if part
    ).strip()
    risk_low = risk_surface.lower()
    if risk_surface and (
        any(
            marker in risk_surface
            for marker in ("请求太频繁", "请求频繁", "操作频繁", "访问频繁", "扫码验证身份", "请完成安全验证")
        )
        or (
            not diag.get("hasContent")
            and len(risk_surface) <= 260
            and "安全验证" in risk_surface
        )
        or any(
            marker in risk_low
            for marker in ("too many requests", "security verification", "access too frequent")
        )
    ):
        return f"captcha:海外小红书平台频控/安全验证：{risk_surface[:120]}"

    if diag.get("loginModal"):
        return "logged_out:海外小红书登录态已失效"

    if not diag and not diag.get("hasContent"):
        for login_label in ("登录", "Log in", "Sign in"):
            try:
                if await page.get_by_text(login_label, exact=True).first.is_visible(timeout=500):
                    return "logged_out:海外小红书登录态已失效"
            except Exception:
                continue
    return ""


def _is_rednote_api_url(url: str) -> bool:
    try:
        host = urlsplit(str(url or "")).hostname or ""
    except ValueError:
        return False
    return is_rednote_host(host)


async def fetch_rednote_notes(
    mgr: BrowserManager,
    identity: Identity,
    user_id: str,
    known_ids: Set[str],
    xsec_token: str = "",
    xsec_source: str = "",
    max_scrolls: int = 8,
    settle_ms: int = 1800,
    block_media: bool = True,
    open_url: str = "",
    ssr_fallback: bool = True,
    keep_context: bool = False,
) -> Tuple[List[dict], Optional[dict], str]:
    """Open a Rednote creator profile on ``www.rednote.com`` and capture posted notes."""
    _ = block_media
    collected: Dict[str, dict] = {}
    author: Optional[dict] = None
    error = ""
    page_failure = ""

    async def _merge_ssr_notes(page) -> None:
        if not ssr_fallback:
            return
        try:
            ssr_items = json.loads(await page.evaluate(_SSR_NOTES_JS) or "[]")
            for it in ssr_items:
                it = _decamel(it)
                nid = str(
                    it.get("note_id")
                    or it.get("id")
                    or (it.get("note_card") or {}).get("note_id")
                    or ""
                )
                if nid and nid not in collected:
                    collected[nid] = it
        except Exception:
            pass

    async def on_response(resp):
        nonlocal author
        url = str(resp.url or "")
        if not _is_rednote_api_url(url):
            return
        try:
            if USER_POSTED_API in url:
                data = (await resp.json()).get("data") or {}
                for it in data.get("notes") or []:
                    nid = str(it.get("note_id") or it.get("id") or "")
                    if nid:
                        collected[nid] = it
            elif OTHERINFO_API in url and author is None:
                data = (await resp.json()).get("data") or {}
                if data:
                    author = data
        except Exception:
            pass

    try:
        async with _visible_page_scope(mgr, identity, keep_context) as page:
            page.on("response", on_response)
            responses = _ResponseInbox(
                page,
                lambda r: _is_rednote_api_url(r.url) and USER_POSTED_API in r.url and r.status == 200,
            )
            await page.goto(
                open_url or build_rednote_profile_url(user_id, xsec_token, xsec_source),
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            await responses.wait(max(1500, min(5000, settle_ms)), on_response)
            await _merge_ssr_notes(page)
            if not collected:
                page_failure = await _rednote_page_failure(page)
            if not page_failure:
                await _xhs_reading_pause(
                    mgr, content_length=min(1200, 120 + len(collected) * 90)
                )
                await _scroll_collection(mgr, page, collected, max_scrolls, known_ids)
            await _merge_ssr_notes(page)
            if author is None:
                try:
                    ssr_author = _decamel(
                        json.loads(await page.evaluate(_SSR_PROFILE_AUTHOR_JS) or "{}")
                    )
                    if ssr_author:
                        ssr_author.setdefault("user_id", user_id)
                        author = ssr_author
                except Exception:
                    pass
            if not collected:
                page_failure = page_failure or await _rednote_page_failure(page)
        if not collected and not error:
            error = page_failure or "未拦截到海外小红书笔记（创作者可能暂无公开笔记，或页面接口已调整）"
    except Exception as exc:
        error = f"打开海外小红书创作者主页失败: {exc!r}"

    new_items = [it for nid, it in collected.items() if nid not in known_ids]
    return new_items, author, error


async def fetch_rednote_search(
    mgr: BrowserManager,
    identity: Identity,
    keyword: str,
    known_ids: Set[str],
    max_scrolls: int = 6,
    settle_ms: int = 1800,
    block_media: bool = True,
    keep_context: bool = False,
    stagnant_limit: int = 2,
    search_sort: str = "general",
    content_type: str = "all",
) -> Tuple[List[dict], str]:
    """Search notes on ``www.rednote.com`` and intercept ``search/notes`` responses."""
    _ = (settle_ms, block_media)
    collected: Dict[str, dict] = {}
    error = ""
    page_failure = ""

    async def on_response(resp):
        url = str(resp.url or "")
        if not _is_rednote_api_url(url) or not _is_search_notes_response(url):
            return
        try:
            data = (await resp.json()).get("data") or {}
        except Exception:
            return
        for it in data.get("items") or []:
            if it.get("model_type") not in (None, "note"):
                continue
            nid = str(it.get("id") or (it.get("note_card") or {}).get("note_id") or "")
            if nid:
                collected[nid] = it

    try:
        async with _visible_page_scope(mgr, identity, keep_context) as page:
            page.on("response", on_response)
            responses = _ResponseInbox(
                page,
                lambda r: (
                    _is_rednote_api_url(r.url)
                    and _is_search_notes_response(r.url)
                    and r.status == 200
                ),
            )
            query = urllib.parse.urlencode({
                "keyword": keyword,
                "source": "web_explore_feed",
                "type": "51",
                **(
                    {
                        "sort": {
                            "latest": "time_descending",
                            "most_liked": "popularity_descending",
                        }[search_sort]
                    }
                    if search_sort in {"latest", "most_liked"}
                    else {}
                ),
                **(
                    {"note_type": {"video": "video", "images": "image"}[content_type]}
                    if content_type in {"video", "images"}
                    else {}
                ),
            })
            search_url = f"{WWW_URL}/zh/search_result?{query}"
            filtered_search = search_sort in {"latest", "most_liked"} or content_type in {
                "video",
                "images",
            }
            response = None
            typed = False
            if filtered_search:
                await page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
                response = await responses.wait(12_000, on_response)
            else:
                await page.goto(
                    FRONT_HOME_PAGE_URL, wait_until="domcontentloaded", timeout=30_000
                )
                box = None
                for sel in (
                    "#search-input",
                    'input[placeholder*="搜索"]',
                    'input[placeholder*="Search" i]',
                    ".search-input input",
                    "input.search-input",
                ):
                    try:
                        box = page.locator(sel).first
                        await box.wait_for(state="visible", timeout=2500)
                        await mgr.xhs_interaction.type_short(box, keyword)
                        typed = True
                        break
                    except Exception:
                        continue
                if typed and box is not None:
                    await box.press("Enter")
                    response = await responses.wait(4000, on_response)
            if response is None and not filtered_search:
                await page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)
                await responses.wait(12_000, on_response)
            if not collected:
                page_failure = await _rednote_page_failure(page)
            if not page_failure:
                await _xhs_reading_pause(
                    mgr, content_length=min(1000, len(keyword) * 35 + len(collected) * 70)
                )
                await _scroll_collection(
                    mgr, page, collected, max_scrolls, stagnant_limit=stagnant_limit
                )
            if not collected:
                page_failure = page_failure or await _rednote_page_failure(page)
        if not collected and not error:
            error = page_failure or "未拦截到海外小红书搜索结果（关键词可能无结果，或页面接口已调整）"
    except Exception as exc:
        error = f"打开海外小红书搜索页失败: {exc!r}"

    new_items = [it for nid, it in collected.items() if nid not in known_ids]
    if search_sort in {"latest", "most_liked"}:
        field = "time" if search_sort == "latest" else "likes"
        new_items.sort(key=lambda item: _xhs_search_value(item, field), reverse=True)
    return new_items, error


async def fetch_rednote_favorite_boards(
    mgr: BrowserManager,
    identity: Identity,
    user_id: str,
    settle_ms: int = 2500,
) -> Tuple[List[dict], str]:
    boards: Dict[str, dict] = {}
    error = ""

    async def on_response(resp):
        if not _is_rednote_api_url(resp.url) or FAVORITE_BOARDS_API not in resp.url:
            return
        try:
            data = (await resp.json()).get("data") or {}
        except Exception:
            return
        for value in data.get("boards") or []:
            board_id = str(value.get("id") or value.get("board_id") or "")
            if board_id:
                boards[board_id] = value

    try:
        async with _visible_page_scope(mgr, identity, False) as page:
            page.on("response", on_response)
            responses = _ResponseInbox(
                page,
                lambda r: _is_rednote_api_url(r.url)
                and FAVORITE_BOARDS_API in r.url
                and r.status == 200,
            )
            await page.goto(
                build_rednote_profile_url(user_id),
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            for label in ("收藏", "收藏夹", "Saved", "Collects", "Favorites"):
                try:
                    control = page.get_by_text(label, exact=True).first
                    if await control.is_visible(timeout=1000):
                        await DEFAULT_HUMAN_ENGINE.click_locator(control, page=page, timeout=3000)
                        break
                except Exception:
                    continue
            await responses.wait(max(1500, min(10_000, settle_ms)), on_response)
            if not boards:
                error = await _rednote_page_failure(page)
    except Exception as exc:
        error = f"打开海外小红书收藏夹失败: {exc!r}"
    if not boards and not error:
        error = "未读取到收藏夹列表（账号可能没有自建收藏夹，或页面接口已调整）"
    return list(boards.values()), error


async def fetch_rednote_favorites(
    mgr: BrowserManager,
    identity: Identity,
    user_id: str,
    *,
    board_id: str = "",
    board_name: str = "",
    max_scrolls: int = 12,
    stagnant_limit: int = 3,
) -> Tuple[List[dict], str]:
    collected: Dict[str, dict] = {}
    error = ""
    endpoint = FAVORITE_BOARD_NOTES_API if board_id else FAVORITE_ALL_API

    async def on_response(resp):
        if not _is_rednote_api_url(resp.url) or endpoint not in resp.url:
            return
        try:
            data = (await resp.json()).get("data") or {}
        except Exception:
            return
        for value in _favorite_notes(data):
            note_id = str(
                value.get("note_id")
                or value.get("id")
                or (value.get("note_card") or {}).get("note_id")
                or ""
            )
            if note_id:
                value.setdefault("xsec_source", "pc_user")
                collected[note_id] = value

    try:
        async with _visible_page_scope(mgr, identity, False) as page:
            page.on("response", on_response)
            responses = _ResponseInbox(
                page,
                lambda r: _is_rednote_api_url(r.url) and endpoint in r.url and r.status == 200,
            )
            await page.goto(
                build_rednote_profile_url(user_id),
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            for label in ("收藏", "收藏夹", "Saved", "Collects", "Favorites"):
                try:
                    control = page.get_by_text(label, exact=True).first
                    if await control.is_visible(timeout=1000):
                        await DEFAULT_HUMAN_ENGINE.click_locator(control, page=page, timeout=3000)
                        break
                except Exception:
                    continue
            if board_id:
                opened = False
                if board_name:
                    try:
                        board = page.get_by_text(board_name, exact=True).first
                        if await board.is_visible(timeout=2500):
                            await DEFAULT_HUMAN_ENGINE.click_locator(board, page=page, timeout=3000)
                            opened = True
                    except Exception:
                        pass
                if not opened:
                    for selector in (f'[data-board-id="{board_id}"]', f'[href*="{board_id}"]'):
                        try:
                            board = page.locator(selector).first
                            if await board.is_visible(timeout=1200):
                                await DEFAULT_HUMAN_ENGINE.click_locator(board, page=page, timeout=3000)
                                break
                        except Exception:
                            continue
            await responses.wait(10_000, on_response)
            if not collected:
                error = await _rednote_page_failure(page)
            if not error:
                await _scroll_collection(
                    mgr, page, collected, max_scrolls, stagnant_limit=stagnant_limit
                )
    except Exception as exc:
        error = f"读取海外小红书收藏笔记失败: {exc!r}"
    if not collected and not error:
        error = "未读取到收藏笔记（收藏夹可能为空，或页面接口已调整）"
    return list(collected.values()), error


async def fetch_rednote_note_detail(
    mgr: BrowserManager,
    identity: Identity,
    note_id: str,
    xsec_token: str = "",
    xsec_source: str = "pc_feed",
    settle_ms: int = 1800,
    block_media: bool = True,
    keep_context: bool = False,
) -> Tuple[Optional[dict], str]:
    _ = (settle_ms, block_media)
    result: dict = {}
    error = ""
    page_failure = ""

    async def on_response(resp):
        if not _is_rednote_api_url(resp.url) or FEED_API not in resp.url:
            return
        try:
            data = (await resp.json()).get("data") or {}
        except Exception:
            return
        for it in data.get("items") or []:
            card = it.get("note_card") or {}
            if str(card.get("note_id") or it.get("id") or "") == note_id or not result:
                result.update(card)

    try:
        async with _visible_page_scope(mgr, identity, keep_context) as page:
            page.on("response", on_response)
            responses = _ResponseInbox(
                page,
                lambda r: _is_rednote_api_url(r.url) and FEED_API in r.url and r.status == 200,
            )
            await page.goto(
                build_rednote_note_url(note_id, xsec_token, xsec_source or "pc_feed"),
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            try:
                ssr_card = _decamel(
                    json.loads(await page.evaluate(_SSR_NOTE_DETAIL_JS, note_id) or "{}")
                )
                if ssr_card:
                    result.update(ssr_card)
            except Exception:
                pass
            if not result:
                page_failure = await _rednote_page_failure(page)
            if not result and not page_failure:
                await responses.wait(8000, on_response)
            if result:
                content_length = sum(
                    len(str(result.get(name) or ""))
                    for name in ("title", "display_title", "desc", "description")
                )
                await _xhs_reading_pause(mgr, content_length=max(120, content_length))
            if not result:
                page_failure = page_failure or await _rednote_page_failure(page)
        if not result:
            error = page_failure or "页面已打开，但未读取到海外小红书笔记详情媒体"
    except Exception as exc:
        error = f"打开海外小红书笔记详情失败: {exc!r}"
    return (result or None), error


async def _focus_note_comment_pane(page) -> bool:
    """Move cursor smoothly into the right-hand scrollable pane of `#noteContainer`."""
    for sel in (
        "#noteContainer .note-scroller",
        ".note-scroller",
        "#noteContainer .interaction-container",
        ".interaction-container",
        ".comments-container",
        "#noteContainer",
    ):
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=400):
                box = await loc.bounding_box()
                if isinstance(box, dict) and box.get("width") and box.get("height"):
                    tx, ty = DEFAULT_HUMAN_ENGINE.sample_box_point(box)
                    await DEFAULT_HUMAN_ENGINE.move_mouse(page, tx, ty)
                    return True
        except Exception:
            continue
    return False


async def _nudge_note_scroller_js(page) -> bool:
    """Scroll `.note-scroller` via human inertia wheel after aiming cursor over the scroller."""
    try:
        box = await page.evaluate(
            """() => {
              const sels = [
                '#noteContainer .note-scroller',
                '.note-scroller',
                '#noteContainer .interaction-container',
                '.interaction-container',
                '.comments-container'
              ];
              for (const sel of sels) {
                const el = document.querySelector(sel);
                if (el && el.scrollHeight > el.clientHeight + 8) {
                  const r = el.getBoundingClientRect();
                  if (r.width > 40 && r.height > 40) {
                    return { x: r.x, y: r.y, width: r.width, height: r.height };
                  }
                }
              }
              return null;
            }"""
        )
        if not isinstance(box, dict) or not box.get("width"):
            return False
        tx, ty = DEFAULT_HUMAN_ENGINE.sample_box_point(box)
        await DEFAULT_HUMAN_ENGINE.move_mouse(page, tx, ty)
        await human_scroll(page, distance=560, settle=False)
        return True
    except Exception:
        return False


async def fetch_rednote_comments(
    mgr: BrowserManager,
    identity: Identity,
    note_id: str,
    known_cids: Set[str],
    xsec_token: str = "",
    xsec_source: str = "pc_feed",
    max_scrolls: int = 6,
    settle_ms: int = 1600,
    block_media: bool = True,
    keep_context: bool = False,
    include_replies: bool = True,
) -> Tuple[List[dict], str]:
    _ = block_media
    collected: Dict[str, dict] = {}
    error = ""
    page_failure = ""
    saw_comment_response = False
    has_more_comments = True
    sub_comment_api = "/api/sns/web/v2/comment/sub/page"

    async def on_response(resp):
        nonlocal saw_comment_response, has_more_comments
        if not _is_rednote_api_url(resp.url):
            return
        is_root = COMMENT_API in resp.url
        is_sub = sub_comment_api in resp.url
        if not is_root and not is_sub:
            return
        try:
            payload = await resp.json()
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        if is_root:
            if resp.status == 200 and (payload.get("success") is True or payload.get("code") == 0):
                saw_comment_response = True
                if "has_more" in data:
                    has_more_comments = bool(data.get("has_more"))
            for c in data.get("comments") or []:
                cid = str(c.get("id") or "")
                if cid:
                    prev = collected.get(cid)
                    if prev and prev.get("sub_comments") and not c.get("sub_comments"):
                        c = {**c, "sub_comments": prev["sub_comments"]}
                    collected[cid] = c
        elif is_sub:
            parsed_q = urllib.parse.parse_qs(urlsplit(resp.url).query)
            root_id = (parsed_q.get("root_comment_id") or [""])[0]
            sub_rows = data.get("comments") or []
            if root_id and root_id in collected and isinstance(sub_rows, list):
                existing = list(collected[root_id].get("sub_comments") or [])
                seen_sub = {str(s.get("id") or "") for s in existing if isinstance(s, dict)}
                for sub in sub_rows:
                    sid = str(sub.get("id") or "")
                    if sid and sid not in seen_sub:
                        existing.append(sub)
                        seen_sub.add(sid)
                collected[root_id]["sub_comments"] = existing
            else:
                for sub in sub_rows:
                    sid = str(sub.get("id") or "")
                    if sid:
                        collected[sid] = sub

    try:
        async with _visible_page_scope(mgr, identity, keep_context) as page:
            page.on("response", on_response)
            responses = _ResponseInbox(
                page,
                lambda r: _is_rednote_api_url(r.url) and COMMENT_API in r.url and r.status == 200,
            )
            await page.goto(
                build_rednote_note_url(note_id, xsec_token, xsec_source or "pc_feed"),
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            try:
                ssr_comments = _decamel(
                    json.loads(await page.evaluate(_SSR_NOTE_COMMENTS_JS, note_id) or "[]")
                )
                for comment in ssr_comments:
                    cid = str(comment.get("id") or comment.get("comment_id") or "")
                    if cid:
                        collected[cid] = comment
            except Exception:
                pass

            initial_wait_ms = max(5000, min(10_000, int(settle_ms or 1600) * 4))
            if not collected and not saw_comment_response:
                await responses.wait(initial_wait_ms, on_response)

            await _focus_note_comment_pane(page)
            if not collected and not saw_comment_response:
                # Tall captions can keep `.comments-container` below the fold until scrolled.
                if await _nudge_note_scroller_js(page):
                    await responses.wait(3000, on_response)

            if not collected and not saw_comment_response:
                page_failure = await _rednote_page_failure(page)

            if not page_failure:
                stagnant = 0
                for _ in range(max(0, int(max_scrolls))):
                    if known_cids and (known_cids & set(collected)):
                        break
                    if saw_comment_response and not has_more_comments:
                        break
                    before = len(collected)
                    await _focus_note_comment_pane(page)
                    await mgr.xhs_interaction.scroll_step(page)
                    await _nudge_note_scroller_js(page)
                    await responses.wait(1800, on_response)
                    if len(collected) == before:
                        stagnant += 1
                        if stagnant >= 2:
                            break
                    else:
                        stagnant = 0

                if include_replies and collected:
                    for _ in range(min(3, max(1, max_scrolls // 2))):
                        try:
                            more_btns = page.locator(".show-more")
                            cnt = await more_btns.count()
                            if cnt <= 0:
                                break
                            clicked_any = False
                            for idx in range(min(cnt, 4)):
                                btn = more_btns.nth(idx)
                                if await btn.is_visible(timeout=300):
                                    await DEFAULT_HUMAN_ENGINE.click_locator(btn, page=page, timeout=2000)
                                    clicked_any = True
                                    await mgr.xhs_interaction.pause(0.35, 0.65)
                            if not clicked_any:
                                break
                        except Exception:
                            break

            if not collected and not saw_comment_response:
                page_failure = page_failure or await _rednote_page_failure(page)
                if not page_failure:
                    error = (
                        "未读取到该笔记评论区响应（如仅填写纯笔记 ID，建议改填带 xsec_token 的完整海外小红书笔记链接）"
                        if not xsec_token
                        else "未读取到海外小红书评论接口响应，请稍后重试"
                    )
        if not collected and page_failure:
            error = page_failure
    except Exception as exc:
        error = f"打开海外小红书评论区失败: {exc!r}"

    new_comments = [c for cid, c in collected.items() if cid not in known_cids]
    return new_comments, error


async def fetch_rednote_self_profile(
    mgr: BrowserManager,
    identity: Identity,
    settle_ms: int = 2500,
    block_media: bool = True,
) -> Tuple[Optional[dict], str]:
    """Open ``www.rednote.com/zh`` and inspect ``user/me`` + ``otherinfo``."""
    _ = block_media
    me_data: dict = {}
    other_data: dict = {}
    error = ""

    async def on_response(resp):
        url = str(resp.url or "")
        if not _is_rednote_api_url(url):
            return
        try:
            if USER_ME_API in url:
                data = (await resp.json()).get("data") or {}
                if isinstance(data, dict):
                    me_data.update(data)
            elif OTHERINFO_API in url:
                data = (await resp.json()).get("data") or {}
                if isinstance(data, dict):
                    other_data.update(data)
        except Exception:
            pass

    try:
        async with _visible_page_scope(mgr, identity, False) as page:
            page.on("response", on_response)
            inbox = _ResponseInbox(
                page,
                lambda r: _is_rednote_api_url(r.url) and USER_ME_API in r.url and r.status == 200,
            )
            await page.goto(FRONT_HOME_PAGE_URL, wait_until="domcontentloaded", timeout=30_000)
            await inbox.wait(max(1500, min(6000, settle_ms)), on_response)
            if me_data.get("guest") is True:
                return None, "logged_out"
            uid = str(me_data.get("user_id") or "")
            if uid and not other_data:
                other_inbox = _ResponseInbox(
                    page,
                    lambda r: _is_rednote_api_url(r.url)
                    and OTHERINFO_API in r.url
                    and r.status == 200,
                )
                await page.goto(
                    build_rednote_profile_url(uid),
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                await other_inbox.wait(3500, on_response)
                if not other_data:
                    try:
                        ssr_author = _decamel(
                            json.loads(await page.evaluate(_SSR_PROFILE_AUTHOR_JS) or "{}")
                        )
                        if ssr_author:
                            other_data.update(ssr_author)
                    except Exception:
                        pass
            if not me_data and not other_data:
                pf = await _rednote_page_failure(page)
                if pf.startswith("logged_out"):
                    return None, "logged_out"
                error = pf or "未能从海外小红书页面读取到账号资料"
    except Exception as exc:
        return None, f"打开海外小红书首页失败: {exc!r}"

    merged = {**other_data, **me_data}
    if not merged:
        return None, error or "empty"
    return merged, ""


async def fetch_rednote_creator_published(
    mgr: BrowserManager,
    identity: Identity,
    settle_ms: int = 3500,
) -> Tuple[List[dict], str]:
    """Open ``creator.rednote.com/new/note-manager`` and capture published notes."""
    notes: List[dict] = []
    error = ""

    async def on_response(resp):
        url = str(resp.url or "")
        if not _is_rednote_api_url(url):
            return
        if not any(hint in url for hint in CREATOR_POSTED_HINTS):
            return
        try:
            data = (await resp.json()).get("data") or {}
        except Exception:
            return
        for row in (
            data.get("notes")
            or data.get("note_infos")
            or data.get("noteList")
            or []
        ):
            if isinstance(row, dict):
                notes.append(row)

    try:
        async with _visible_page_scope(mgr, identity, False) as page:
            page.on("response", on_response)
            inbox = _ResponseInbox(
                page,
                lambda r: _is_rednote_api_url(r.url)
                and any(h in r.url for h in CREATOR_POSTED_HINTS)
                and r.status == 200,
            )
            await page.goto(HOME_PAGE_URL, wait_until="domcontentloaded", timeout=35_000)
            cur = str(getattr(page, "url", "") or "").lower()
            if "/login" in cur or "passport" in cur:
                return [], "logged_out:海外小红书创作者中心未登录"
            await inbox.wait(max(2000, min(8000, settle_ms)), on_response)
    except Exception as exc:
        error = f"打开海外小红书创作者中心失败: {exc!r}"
    return notes, error
