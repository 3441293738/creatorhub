"""TikTok Web reads executed inside the account's logged-in page context."""

from __future__ import annotations

import re
import time
from typing import Dict, List, Tuple

from .human_interaction import (
    DEFAULT_HUMAN_ENGINE,
    human_click_first,
    human_fill_first,
    human_scroll,
    human_wait,
)
from .identity import Identity
from .manager import BrowserManager
from ..platforms.tiktok.extract import (
    flatten_tiktok_comments,
    parse_tiktok_app_context,
    parse_tiktok_user,
)
from ..platforms.tiktok.urls import (
    COMMENT_LIST_API,
    COMMENT_REPLY_API,
    SEARCH_USER_API,
    SEARCH_VIDEO_API,
    user_search_url,
    video_search_url,
    video_url,
    profile_url,
)
from ..platforms.tiktok.protocol import (
    protocol_summary,
    signed_request_evidence,
)


_VIDEO_PATH_RE = re.compile(r"/@([^/?#]+)/video/(\d+)", re.I)


def _walk_lists(value, keys: tuple[str, ...]) -> list[dict]:
    """Return dict rows only from arrays whose field name is explicitly allowed."""
    output: list[dict] = []
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, list):
            for row in current:
                if isinstance(row, (dict, list)):
                    stack.append(row)
        elif isinstance(current, dict):
            for key, child in current.items():
                if key in keys and isinstance(child, list):
                    output.extend(row for row in child if isinstance(row, dict))
                elif isinstance(child, (dict, list)):
                    stack.append(child)
    return output


def _tiktok_item(value: dict) -> dict:
    if not isinstance(value, dict):
        return {}
    item = value.get("item") or value.get("aweme_info") \
        or value.get("itemStruct") or value
    return item if isinstance(item, dict) else {}


def _tiktok_item_id(value: dict) -> str:
    item = _tiktok_item(value)
    return str(item.get("id") or item.get("aweme_id")
               or item.get("itemId") or "")


def _search_items_from_payload(payload) -> list[dict]:
    """Extract video-shaped items from API and hydration payload variants."""
    output: dict[str, dict] = {}
    stack = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, list):
            stack.extend(current)
            continue
        if not isinstance(current, dict):
            continue
        item = _tiktok_item(current)
        item_id = _tiktok_item_id(item)
        author = item.get("author") if isinstance(item, dict) else None
        if item_id and isinstance(author, dict):
            output.setdefault(item_id, current)
        stack.extend(child for child in current.values()
                     if isinstance(child, (dict, list)))
    return list(output.values())


async def _page_hydration_payloads(page) -> list:
    try:
        values = await page.evaluate("""() => {
          const out = [];
          const ids = ['__UNIVERSAL_DATA_FOR_REHYDRATION__', 'SIGI_STATE',
                       '__NEXT_DATA__'];
          for (const id of ids) {
            const el = document.getElementById(id);
            if (!el || !el.textContent) continue;
            try { out.push(JSON.parse(el.textContent)); } catch (_) {}
          }
          return out;
        }""")
        return values if isinstance(values, list) else []
    except Exception:
        return []


async def _page_video_cards(page) -> list[dict]:
    """Read rendered cards when TikTok changes or suppresses search XHR."""
    try:
        rows = await page.evaluate(r"""() => {
          const rows = [];
          const seen = new Set();
          for (const anchor of document.querySelectorAll('a[href*=\"/video/\"]')) {
            const href = anchor.href || '';
            const match = href.match(/\/@([^/?#]+)\/video\/(\d+)/i);
            if (!match || seen.has(match[2])) continue;
            seen.add(match[2]);
            const card = anchor.closest('[data-e2e*=\"search\"], article, li, div') || anchor;
            const video = card.querySelector('video');
            const image = card.querySelector('img');
            const text = (anchor.getAttribute('aria-label') || card.innerText || '').trim();
            rows.push({
              item: {
                id: match[2], desc: text.slice(0, 500),
                author: { uniqueId: decodeURIComponent(match[1]) },
                stats: {},
                video: {
                  playAddr: { urlList: video && video.src ? [video.src] : [] },
                  cover: { urlList: image && image.src ? [image.src] : [] }
                }
              }
            });
          }
          return rows;
        }""")
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


async def _trigger_video_search(page, keyword: str) -> bool:
    for selector in (
            'input[data-e2e="search-user-input"]',
            'input[type="search"]',
            'input[placeholder*="Search"]'):
        try:
            field = page.locator(selector).first
            if await field.count() and await field.is_visible():
                await DEFAULT_HUMAN_ENGINE.type_text(field, keyword, page=page, timeout=4000)
                await human_wait(page, 180)
                await field.press("Enter")
                await human_wait(page, 1500)
                return True
        except Exception:
            continue
    return False


async def _merge_page_items(page, collected: Dict[str, dict]) -> int:
    before = len(collected)
    for payload in await _page_hydration_payloads(page):
        for raw in _search_items_from_payload(payload):
            item_id = _tiktok_item_id(raw)
            if item_id:
                collected.setdefault(item_id, raw)
    for raw in await _page_video_cards(page):
        item_id = _tiktok_item_id(raw)
        if item_id:
            collected.setdefault(item_id, raw)
    return len(collected) - before


async def _wait_for_tiktok_search_results(
        page, collected: Dict[str, dict], *, timeout_ms: int = 20000) -> bool:
    """Wait for slow proxy/render paths without sleeping the full timeout."""
    deadline = time.monotonic() + max(0, int(timeout_ms)) / 1000
    while time.monotonic() < deadline:
        await _merge_page_items(page, collected)
        if collected:
            return True
        await human_wait(page, 750)
    await _merge_page_items(page, collected)
    return bool(collected)


def _request_headers(request) -> dict:
    headers = getattr(request, "headers", {}) or {}
    return headers if isinstance(headers, dict) else {}


async def _page_signer_capabilities(page) -> dict:
    try:
        value = await page.evaluate("""() => ({
          byted_acrawler: !!window.byted_acrawler,
          frontierSign: typeof window.byted_acrawler?.frontierSign === 'function',
          webmssdk: !!window._webmssdk,
          universalHydration: !!document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__')
        })""")
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


async def _page_failure(page) -> str:
    current = str(getattr(page, "url", "") or "").lower()
    if "/login" in current or "login?" in current:
        return "logged_out:登录态失效,请重新登录"
    try:
        body = (await page.locator("body").inner_text(timeout=1500)).lower()
    except Exception:
        body = ""
    if any(token in body for token in ("captcha", "verify to continue", "security verification")):
        return "captcha: TikTok 要求安全验证"
    return ""


async def fetch_tiktok_search(
        mgr: BrowserManager, identity: Identity, keyword: str, *,
        max_results: int = 20, max_scrolls: int = 12, stagnant_limit: int = 3,
        block_media: bool = True, diagnostics: dict | None = None,
        initial_wait_ms: int = 20000) -> Tuple[List[dict], str]:
    collected: Dict[str, dict] = {}
    seen_responses = 0
    seen_search_paths: set[str] = set()
    signed_requests: dict[str, dict] = {}
    page = await mgr.new_page(identity, block_media)

    def on_request(request):
        low = str(getattr(request, "url", "") or "").lower()
        if "/api/search/" not in low:
            return
        evidence = signed_request_evidence(request.url, _request_headers(request))
        key = evidence.get("path") or request.url.split("?", 1)[0]
        previous = signed_requests.get(key)
        if evidence.get("signed") or previous is None:
            signed_requests[key] = evidence

    async def on_response(response):
        nonlocal seen_responses
        low = str(response.url or "").lower()
        if "/api/search/" in low:
            seen_search_paths.add(low.split("?", 1)[0])
        if (SEARCH_VIDEO_API not in response.url
                and "/api/search/item/" not in low
                and "/api/search/general/" not in low) \
                or response.status != 200:
            return
        seen_responses += 1
        try:
            payload = await response.json()
        except Exception:
            return
        rows = _search_items_from_payload(payload)
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            aweme_id = _tiktok_item_id(raw)
            if aweme_id:
                collected.setdefault(aweme_id, raw)

    page.on("request", on_request)
    page.on("response", on_response)
    try:
        await page.goto(video_search_url(keyword), wait_until="domcontentloaded", timeout=30000)
        failure = await _page_failure(page)
        if failure:
            return [], failure
        await _wait_for_tiktok_search_results(
            page, collected, timeout_ms=initial_wait_ms)
        # The preview endpoint can answer successfully with an empty shell before
        # the search UI commits its query.  A response is therefore not evidence
        # that results have been requested; actively submit the visible search
        # field whenever the first adaptive wait produced no cards.
        if not collected:
            await _trigger_video_search(page, keyword)
            failure = await _page_failure(page)
            if failure:
                return [], failure
            await _wait_for_tiktok_search_results(
                page, collected, timeout_ms=min(18000, initial_wait_ms))
        stagnant = 0
        for _ in range(max(1, min(40, int(max_scrolls or 1)))):
            before = len(collected)
            await human_scroll(page, settle=False)
            await human_wait(page, 900)
            await _merge_page_items(page, collected)
            if len(collected) >= max_results:
                break
            stagnant = stagnant + 1 if len(collected) == before else 0
            if stagnant >= max(1, int(stagnant_limit or 1)):
                break
        if collected:
            error = ""
        elif seen_responses:
            error = "TikTok 搜索已响应，但本次结果为空"
        else:
            paths = "、".join(sorted(seen_search_paths)[:4])
            error = ("TikTok 搜索页未返回作品；已尝试接口、页面内嵌数据和可见作品卡"
                     + (f"（观察到：{paths}）" if paths else ""))
        return list(collected.values())[:max_results], error
    except Exception as exc:
        return list(collected.values())[:max_results], f"TikTok 搜索失败: {exc}"
    finally:
        if diagnostics is not None:
            diagnostics.update(protocol_summary())
            diagnostics.update({
                "keyword": str(keyword or ""),
                "observed_search_paths": sorted(seen_search_paths),
                "search_response_count": seen_responses,
                "result_count": len(collected),
                "signed_requests": list(signed_requests.values()),
                "page_signer": await _page_signer_capabilities(page),
            })
        try:
            await page.close()
        except Exception:
            pass


async def probe_tiktok_protocol(
        mgr: BrowserManager, identity: Identity, keyword: str = "claude", *,
        timeout_ms: int = 20000) -> dict:
    diagnostics: dict = {}
    items, error = await fetch_tiktok_search(
        mgr, identity, keyword, max_results=3, max_scrolls=1,
        stagnant_limit=1, block_media=True, diagnostics=diagnostics,
        initial_wait_ms=timeout_ms)
    diagnostics["ok"] = bool(items) and not error
    diagnostics["error"] = error
    diagnostics["sample_item_ids"] = [
        _tiktok_item_id(item) for item in items if _tiktok_item_id(item)
    ][:3]
    return diagnostics


async def fetch_tiktok_profile_works(
        mgr: BrowserManager, identity: Identity, unique_id: str, *,
        max_results: int = 100, max_scrolls: int = 14,
        block_media: bool = True) -> Tuple[List[dict], str]:
    """Read the logged-in account's public profile works with DOM fallback."""
    unique_id = str(unique_id or "").strip().lstrip("@")
    if not unique_id:
        return [], "missing_uid:TikTok 账号缺 unique_id，请先刷新账号资料"
    collected: Dict[str, dict] = {}
    seen_post_response = False
    page = await mgr.new_page(identity, block_media)

    async def on_response(response):
        nonlocal seen_post_response
        low = str(response.url or "").lower()
        if ("/api/post/item_list/" not in low
                and "/api/user/list/" not in low) or response.status != 200:
            return
        seen_post_response = True
        try:
            payload = await response.json()
        except Exception:
            return
        for raw in _search_items_from_payload(payload):
            item_id = _tiktok_item_id(raw)
            if item_id:
                collected.setdefault(item_id, raw)

    page.on("response", on_response)
    try:
        await page.goto(profile_url(unique_id), wait_until="domcontentloaded", timeout=30000)
        await human_wait(page, 1400)
        failure = await _page_failure(page)
        if failure:
            return [], failure
        await _merge_page_items(page, collected)
        stagnant = 0
        for _ in range(max(1, min(40, int(max_scrolls or 1)))):
            before = len(collected)
            await human_scroll(page, settle=False)
            await human_wait(page, 850)
            await _merge_page_items(page, collected)
            if len(collected) >= max_results:
                break
            stagnant = stagnant + 1 if len(collected) == before else 0
            if stagnant >= 3:
                break
        if collected:
            return list(collected.values())[:max_results], ""
        if seen_post_response:
            return [], "TikTok 本账号主页已响应，但没有已发布作品"
        return [], "TikTok 本账号主页未读取到作品卡片或作品接口"
    except Exception as exc:
        return list(collected.values())[:max_results], f"TikTok 本账号作品读取失败: {exc}"
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def fetch_tiktok_users(
        mgr: BrowserManager, identity: Identity, keyword: str, *,
        max_results: int = 50, max_scrolls: int = 12,
        block_media: bool = True) -> Tuple[List[dict], str]:
    collected: Dict[str, dict] = {}
    page = await mgr.new_page(identity, block_media)

    async def on_response(response):
        if SEARCH_USER_API not in response.url or response.status != 200:
            return
        try:
            payload = await response.json()
        except Exception:
            return
        rows = payload.get("user_list") or payload.get("users") or []
        if not isinstance(rows, list):
            rows = _walk_lists(payload, ("user_list", "users"))
        for raw in rows:
            user = parse_tiktok_user(raw)
            if user:
                key = user["sec_uid"] or user["uid"] or user["unique_id"]
                collected.setdefault(key, user)

    page.on("response", on_response)
    try:
        await page.goto(user_search_url(keyword), wait_until="domcontentloaded", timeout=30000)
        await human_wait(page, 1400)
        failure = await _page_failure(page)
        if failure:
            return [], failure
        for _ in range(max(1, min(40, int(max_scrolls or 1)))):
            before = len(collected)
            await human_scroll(page, settle=False)
            await human_wait(page, 850)
            if len(collected) >= max_results or (collected and len(collected) == before):
                break
        return list(collected.values())[:max_results], ""
    except Exception as exc:
        return list(collected.values())[:max_results], f"TikTok 用户搜索失败: {exc}"
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def fetch_tiktok_self_profile(
        mgr: BrowserManager, identity: Identity, *,
        block_media: bool = True) -> Tuple[dict | None, str]:
    """Read the authenticated account from TikTok hydration, never fuzzy-search it."""
    page = await mgr.new_page(identity, block_media)
    try:
        await page.goto("https://www.tiktok.com/", wait_until="domcontentloaded", timeout=30000)
        await human_wait(page, 1200)
        failure = await _page_failure(page)
        if failure:
            return None, failure
        context = await page.evaluate("""() => {
          const el = document.getElementById('__UNIVERSAL_DATA_FOR_REHYDRATION__');
          if (!el || !el.textContent) return null;
          try { return JSON.parse(el.textContent).__DEFAULT_SCOPE__?.['webapp.app-context'] || null; }
          catch (_) { return null; }
        }""")
        profile = parse_tiktok_app_context(context or {})
        return (profile, "") if profile else (None, "TikTok 首页未读取到登录账号资料")
    except Exception as exc:
        return None, f"TikTok 本人资料读取失败: {exc}"
    finally:
        try:
            await page.close()
        except Exception:
            pass


async def _expand_reply_threads(page, max_clicks: int = 12) -> None:
    selectors = (
        'button:has-text("View more replies")',
        'button:has-text("View replies")',
        '[data-e2e*="view-more-repl"]',
    )
    clicked = 0
    for selector in selectors:
        locator = page.locator(selector)
        try:
            count = min(await locator.count(), max_clicks - clicked)
        except Exception:
            count = 0
        for index in range(count):
            try:
                button = locator.nth(index)
                if await button.is_visible():
                    await DEFAULT_HUMAN_ENGINE.click_locator(button, page=page, timeout=2500)
                    clicked += 1
                    await human_wait(page, 300)
            except Exception:
                continue
            if clicked >= max_clicks:
                return


async def fetch_tiktok_comments(
        mgr: BrowserManager, identity: Identity, aweme_id: str,
        known_ids: set[str] | None = None, *, max_comments: int = 20,
        max_scrolls: int = 12, include_replies: bool = False,
        block_media: bool = True, author_unique_id: str = "",
        target_url: str = "") -> Tuple[List[dict], str]:
    known_ids = known_ids or set()
    collected: Dict[str, dict] = {}
    page = await mgr.new_page(identity, block_media)

    async def on_response(response):
        if (COMMENT_LIST_API not in response.url and COMMENT_REPLY_API not in response.url) \
                or response.status != 200:
            return
        try:
            payload = await response.json()
        except Exception:
            return
        comments = payload.get("comments") or payload.get("comment_list") or []
        parent = ""
        if COMMENT_REPLY_API in response.url:
            try:
                from urllib.parse import parse_qs, urlsplit
                query = parse_qs(urlsplit(response.url).query)
                parent = str((query.get("comment_id") or query.get("item_id") or [""])[0])
            except Exception:
                parent = ""
        rows = flatten_tiktok_comments(
            comments if isinstance(comments, list) else [],
            include_replies=include_replies and not parent)
        for raw in rows:
            cid = str(raw.get("cid") or raw.get("id") or raw.get("comment_id") or "")
            if cid and cid not in known_ids:
                value = dict(raw)
                if parent:
                    value.setdefault("reply_id", parent)
                collected.setdefault(cid, value)

    page.on("response", on_response)
    try:
        url = str(target_url or "").strip() or video_url(aweme_id, author_unique_id)
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await human_wait(page, 1400)
        failure = await _page_failure(page)
        if failure:
            return [], failure
        for _ in range(max(1, min(30, int(max_scrolls or 1)))):
            if include_replies:
                await _expand_reply_threads(page)
            await human_scroll(page, settle=False)
            await human_wait(page, 750)
            if len(collected) >= max_comments:
                break
        if not include_replies:
            collected = {key: value for key, value in collected.items()
                         if not (value.get("reply_id") or value.get("replyId"))}
        return list(collected.values())[:max_comments], ""
    except Exception as exc:
        return list(collected.values())[:max_comments], f"TikTok 评论抓取失败: {exc}"
    finally:
        try:
            await page.close()
        except Exception:
            pass
