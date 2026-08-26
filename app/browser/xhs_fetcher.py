"""小红书抓取:用真实浏览器打开 xiaohongshu.com 页面,拦截它自己发的接口响应,
直接拿到 notes / feed / comments —— 与抖音同一套「浏览器拦截」思路,免签名。
小红书改版时,改下面的接口路径常量与导航 URL 即可。
"""
from __future__ import annotations

import asyncio
import json
import re
import urllib.parse
from typing import Dict, List, Optional, Set, Tuple

from .identity import Identity
from .manager import BrowserManager

USER_POSTED_API = "/api/sns/web/v1/user_posted"
OTHERINFO_API = "/api/sns/web/v1/user/otherinfo"
# 网页端 2026 版已切到 v2；保留 v1 匹配以兼容旧内核/灰度页面。
SEARCH_API = "/api/sns/web/v2/search/notes"
SEARCH_API_LEGACY = "/api/sns/web/v1/search/notes"
FEED_API = "/api/sns/web/v1/feed"
COMMENT_API = "/api/sns/web/v2/comment/page"
# 小红书网页端「当前登录用户」接口(旧的 v1/user/selfinfo 已不再用)
USER_ME_API = "/api/sns/web/v2/user/me"

_BASE = "https://www.xiaohongshu.com"


def _is_search_notes_response(url: str) -> bool:
    return any(path in str(url or "") for path in (
        SEARCH_API, SEARCH_API_LEGACY))


def _profile_url(user_id: str, xsec_token: str = "", xsec_source: str = "") -> str:
    url = f"{_BASE}/user/profile/{user_id}"
    qs = {}
    if xsec_token:
        qs["xsec_token"] = xsec_token
    if xsec_source:
        qs["xsec_source"] = xsec_source
    return url + ("?" + urllib.parse.urlencode(qs) if qs else "")


def _decamel(obj):
    """SSR 的 __INITIAL_STATE__ 键是 camelCase(noteCard/displayTitle),
    统一转成接口同款 snake_case,让下游归一函数两种来源通吃。"""
    if isinstance(obj, dict):
        return {re.sub(r"(?<!^)(?=[A-Z])", "_", k).lower(): _decamel(v)
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decamel(x) for x in obj]
    return obj


# 用户主页首屏笔记是 SSR 直出的(挂在 window.__INITIAL_STATE__.user.notes),
# 笔记数不满一页时根本不会发 user_posted XHR —— 拦截会一无所获,须直读页面状态。
# notes 结构:[发布列表, 收藏, 赞过](Vue ref 序列化时可能包一层 _rawValue),只取发布列表。
_SSR_NOTES_JS = """() => {
    const marker = 'window.__INITIAL_STATE__=';
    let st = window.__INITIAL_STATE__;
    if (!st || !Object.keys(st).length) {
      for (const script of document.scripts) {
        const text = script.textContent || '';
        const at = text.indexOf(marker);
        if (at < 0) continue;
        try {
          st = (new Function('return (' + text.slice(at + marker.length) + ')'))();
        } catch (e) { st = null; }
        break;
      }
    }
    let ns = st && st.user && st.user.notes;
    if (ns && ns._rawValue !== undefined) ns = ns._rawValue;
    if (!Array.isArray(ns)) return '[]';
    const posted = (ns.length && Array.isArray(ns[0])) ? ns[0] : ns;
    return JSON.stringify(posted.filter(x => x && typeof x === 'object'));
}"""


_SSR_NOTE_DETAIL_JS = """(noteId) => {
    const marker = 'window.__INITIAL_STATE__=';
    let st = window.__INITIAL_STATE__;
    if (!st || !Object.keys(st).length) {
      for (const script of document.scripts) {
        const text = script.textContent || '';
        const at = text.indexOf(marker);
        if (at < 0) continue;
        try {
          st = (new Function('return (' + text.slice(at + marker.length) + ')'))();
        } catch (e) { st = null; }
        break;
      }
    }
    let map = st && st.note && st.note.noteDetailMap;
    if (map && map._rawValue !== undefined) map = map._rawValue;
    let entry = map && map[noteId];
    if (entry && entry._rawValue !== undefined) entry = entry._rawValue;
    const note = entry && (entry.note || entry.noteCard || entry);
    return JSON.stringify(note && typeof note === 'object' ? note : {});
}"""


_SSR_NOTE_COMMENTS_JS = """(noteId) => {
    const marker = 'window.__INITIAL_STATE__=';
    let st = window.__INITIAL_STATE__;
    if (!st || !Object.keys(st).length) {
      for (const script of document.scripts) {
        const text = script.textContent || '';
        const at = text.indexOf(marker);
        if (at < 0) continue;
        try {
          st = (new Function('return (' + text.slice(at + marker.length) + ')'))();
        } catch (e) { st = null; }
        break;
      }
    }
    let map = st && st.note && st.note.noteDetailMap;
    if (map && map._rawValue !== undefined) map = map._rawValue;
    let entry = map && map[noteId];
    if (entry && entry._rawValue !== undefined) entry = entry._rawValue;
    let comments = entry && entry.comments;
    if (comments && comments._rawValue !== undefined) comments = comments._rawValue;
    const list = comments && (comments.list || comments.comments || comments);
    return JSON.stringify(Array.isArray(list) ? list : []);
}"""


_SSR_PROFILE_AUTHOR_JS = """() => {
    const marker = 'window.__INITIAL_STATE__=';
    let st = window.__INITIAL_STATE__;
    if (!st || !Object.keys(st).length) {
      for (const script of document.scripts) {
        const text = script.textContent || '';
        const at = text.indexOf(marker);
        if (at < 0) continue;
        try {
          st = (new Function('return (' + text.slice(at + marker.length) + ')'))();
        } catch (e) { st = null; }
        break;
      }
    }
    const user = st && st.user;
    let profile = user && user.userPageData;
    if (profile && profile._rawValue !== undefined) profile = profile._rawValue;
    return JSON.stringify(profile && typeof profile === 'object' ? profile : {});
}"""


def _note_url(note_id: str, xsec_token: str = "", xsec_source: str = "pc_feed") -> str:
    qs = {}
    if xsec_token:
        qs["xsec_token"] = xsec_token
        qs["xsec_source"] = xsec_source or "pc_feed"
    return f"{_BASE}/explore/{note_id}" + ("?" + urllib.parse.urlencode(qs) if qs else "")


class _ResponseInbox:
    """Capture matching responses before navigation/click actions begin.

    Patchright's Python ``Page`` exposes ``expect_response`` but not
    ``wait_for_response``.  The previous calls to the latter therefore returned
    immediately through broad exception handling and could close the temporary
    page before an async response callback parsed its body.  A synchronous event
    listener feeding a queue works for navigation, clicks and scrolling alike.
    """

    def __init__(self, page, predicate):
        self._queue: asyncio.Queue = asyncio.Queue()
        self._predicate = predicate

        def capture(response):
            try:
                matched = self._predicate(response)
            except Exception:
                matched = False
            if matched:
                self._queue.put_nowait(response)

        self._capture = capture
        page.on("response", capture)

    async def wait(self, timeout_ms: int, handler=None, ready=None):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0, timeout_ms) / 1000
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                response = await asyncio.wait_for(
                    self._queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            if handler is not None:
                try:
                    await handler(response)
                except Exception:
                    pass
            if ready is None or ready():
                return response


async def _scroll_collection(mgr: BrowserManager, page, collection: dict,
                             max_steps: int, stop_ids: Set[str] | None = None) -> None:
    stagnant = 0
    for _ in range(max(0, int(max_steps))):
        if stop_ids and stop_ids & set(collection):
            return
        before = len(collection)
        await mgr.xhs_interaction.scroll_step(page)
        if len(collection) == before:
            stagnant += 1
            if stagnant >= 2:
                return
        else:
            stagnant = 0


async def _xhs_reading_pause(
        mgr: BrowserManager, *, content_length: int = 0) -> None:
    pause = getattr(getattr(mgr, "xhs_interaction", None),
                    "reading_pause", None)
    if callable(pause):
        await pause(content_length=content_length)


def _visible_page_scope(
        mgr: BrowserManager, identity: Identity, keep_context: bool):
    kwargs = {"foreground": False}
    if keep_context:
        kwargs["keep_context"] = True
    try:
        return mgr.visible_page(identity, **kwargs)
    except TypeError:  # compatibility with compact manager stubs in integrations
        if keep_context:
            return mgr.visible_page(identity, keep_context=True)
        return mgr.visible_page(identity)


async def _xhs_page_failure(page) -> str:
    """Return an explicit auth/risk signal only when the page proves it.

    A missing intercepted response can also mean an empty result set or a page
    revision.  It must not be treated as logged out merely because that is one
    possible explanation.
    """
    current_url = str(getattr(page, "url", "") or "").lower()
    if "/website-login/captcha" in current_url or "error_code=300012" in current_url:
        return "captcha:小红书要求安全验证"
    if "passport" in current_url or "/login" in current_url:
        return "logged_out:小红书登录态已失效"
    try:
        login_visible = await page.get_by_text(
            "登录", exact=True).first.is_visible(timeout=1200)
    except Exception:
        login_visible = False
    if login_visible:
        return "logged_out:小红书登录态已失效"
    # The risk page is occasionally rendered without keeping the captcha URL
    # (or the URL changes after hydration).  Read only short, explicit page
    # messages so an empty/changed result page is not misclassified as risk.
    body_text = ""
    try:
        body_text = await page.locator("body").inner_text(timeout=1200)
    except Exception:
        try:
            body_text = await page.evaluate(
                "() => (document.body && document.body.innerText || '').slice(0, 2000)")
        except Exception:
            body_text = ""
    normalized = " ".join(str(body_text or "").split())
    if any(marker in normalized for marker in (
            "请求太频繁", "请求频繁", "操作频繁", "访问频繁",
            "一分钟后再试", "安全验证", "扫码验证身份")):
        return f"captcha:小红书平台频控/安全验证：{normalized[:120]}"
    return ""


async def fetch_xhs_notes(mgr: BrowserManager, identity: Identity, user_id: str,
                          known_ids: Set[str], xsec_token: str = "", xsec_source: str = "",
                          max_scrolls: int = 8, settle_ms: int = 1800,
                          block_media: bool = True, open_url: str = "",
                          ssr_fallback: bool = False,
                          keep_context: bool = False,
                          ) -> Tuple[List[dict], Optional[dict], str]:
    """打开创作者主页并下滑,拦截 user_posted 收集笔记精简卡片。
    返回 (笔记原始项列表, 作者信息dict, error)。
    open_url 非空时直接打开它(如站内「我」入口解析出的自带 xsec_token 链接)。
    ssr_fallback=True 时额外直读 __INITIAL_STATE__ 里 SSR 直出的首屏笔记
    (本账号同步用:首屏不发 user_posted,纯拦截抓不到)。"""
    collected: Dict[str, dict] = {}
    author: Optional[dict] = None
    error = ""
    api_seen = []

    async def on_response(resp):
        nonlocal author
        url = resp.url
        if "xiaohongshu.com" in url and "/api/sns/web/" in url and len(api_seen) < 40:
            api_seen.append(f"{resp.status} {url.split('?')[0].split('xiaohongshu.com')[-1]}")
        try:
            if USER_POSTED_API in url:
                data = (await resp.json()).get("data") or {}
                for it in (data.get("notes") or []):
                    nid = str(it.get("note_id") or it.get("id") or "")
                    if nid:
                        collected[nid] = it
            elif OTHERINFO_API in url and author is None:
                data = (await resp.json()).get("data") or {}
                if data:
                    author = data
        except Exception:
            pass

    final_url = ""
    page_failure = ""
    try:
        async with _visible_page_scope(mgr, identity, keep_context) as page:
            page.on("response", on_response)
            responses = _ResponseInbox(
                page,
                lambda r: USER_POSTED_API in r.url and r.status == 200)
            await page.goto(
                open_url or _profile_url(user_id, xsec_token, xsec_source),
                wait_until="domcontentloaded", timeout=30000)
            await responses.wait(
                max(1500, min(5000, settle_ms)), on_response)
            if not collected:
                page_failure = await _xhs_page_failure(page)
            if not page_failure:
                await _xhs_reading_pause(
                    mgr, content_length=min(1200, 120 + len(collected) * 90))
                await _scroll_collection(
                    mgr, page, collected, max_scrolls, known_ids)
            # SSR 兜底/补全:首屏笔记直出在页面状态里,和拦截结果合并(不覆盖)
            if ssr_fallback:
                try:
                    ssr_items = json.loads(
                        await page.evaluate(_SSR_NOTES_JS) or "[]")
                    added = 0
                    for it in ssr_items:
                        it = _decamel(it)
                        nid = str(
                            it.get("note_id") or it.get("id")
                            or (it.get("note_card") or {}).get("note_id") or "")
                        if nid and nid not in collected:
                            collected[nid] = it
                            added += 1
                    print(
                        f"[xhs_notes] ssr_notes={len(ssr_items)} merged={added}")
                except Exception as e:
                    print(f"[xhs_notes] ssr_fallback failed: {e!r}")
            # otherinfo 也可能不再发 XHR，作者资料和首屏笔记一样从 SSR 读取。
            if author is None:
                try:
                    ssr_author = _decamel(json.loads(
                        await page.evaluate(_SSR_PROFILE_AUTHOR_JS) or "{}"))
                    if ssr_author:
                        ssr_author.setdefault("user_id", user_id)
                        author = ssr_author
                except Exception:
                    pass
            final_url = page.url
            if not collected:
                page_failure = await _xhs_page_failure(page)
        if not collected and not error:
            error = page_failure or "未拦截到笔记（创作者可能暂无公开笔记，或页面接口已调整）"
        if not collected:
            saw = any("user_posted" in a for a in api_seen)
            print(f"[xhs_notes] user_id={user_id}; saw_posted_api={saw}; "
                  f"final_url={final_url}; api_seen({len(api_seen)})={api_seen[:30]}")
    except Exception as e:
        error = f"打开创作者主页失败: {e!r}"

    new_items = [it for nid, it in collected.items() if nid not in known_ids]
    return new_items, author, error


async def fetch_xhs_search(mgr: BrowserManager, identity: Identity, keyword: str,
                           known_ids: Set[str], max_scrolls: int = 6, settle_ms: int = 1800,
                           block_media: bool = True,
                           keep_context: bool = False,
                           ) -> Tuple[List[dict], str]:
    """打开搜索结果页并下滑,拦截 search/notes 收集笔记。返回 (笔记原始项列表, error)。"""
    collected: Dict[str, dict] = {}
    api_seen = []
    error = ""

    async def on_response(resp):
        url = resp.url
        if "xiaohongshu.com" in url and "/api/sns/web/" in url and len(api_seen) < 40:
            api_seen.append(f"{resp.status} {url.split('?')[0].split('xiaohongshu.com')[-1]}")
        if _is_search_notes_response(url):
            try:
                data = (await resp.json()).get("data") or {}
            except Exception:
                return
            for it in (data.get("items") or []):
                if it.get("model_type") not in (None, "note"):
                    continue
                nid = str(it.get("id") or (it.get("note_card") or {}).get("note_id") or "")
                if nid:
                    collected[nid] = it

    final_url = ""
    typed = False
    direct_fallback = False
    page_failure = ""
    try:
        async with _visible_page_scope(mgr, identity, keep_context) as page:
            page.on("response", on_response)
            responses = _ResponseInbox(
                page,
                lambda r: (_is_search_notes_response(r.url)
                           and r.status == 200))

            # 首选正常搜索入口和逐字输入。
            await page.goto(
                f"{_BASE}/explore",
                wait_until="domcontentloaded", timeout=30000)
            for sel in (
                    '#search-input', 'input[placeholder*="搜索"]',
                    '.search-input input', 'input.search-input'):
                try:
                    box = page.locator(sel).first
                    await box.wait_for(state="visible", timeout=2500)
                    await mgr.xhs_interaction.type_short(box, keyword)
                    typed = True
                    break
                except Exception:
                    continue
            # 搜索响应可能在 Enter/导航完成前就返回，因此用同步事件回调先放入
            # 队列，再精确解析命中的响应。Python Patchright 没有
            # page.wait_for_response，不能依赖该方法等待。
            response = None
            if typed:
                await box.press("Enter")
                response = await responses.wait(4000, on_response)
            if response is None:
                # 页面尚未完成 hydration 时 Enter 偶尔不生效；直接打开正常搜索
                # 结果 URL。该页面仍由真实浏览器加载并自行发出带签名的 v2 请求。
                direct_fallback = typed
                q = urllib.parse.urlencode({
                    "keyword": keyword,
                    "source": "web_explore_feed",
                    "type": "51",
                })
                await page.goto(
                    f"{_BASE}/search_result?{q}",
                    wait_until="domcontentloaded", timeout=30000)
                await responses.wait(12000, on_response)
            if not collected:
                page_failure = await _xhs_page_failure(page)
            if not page_failure:
                await _xhs_reading_pause(
                    mgr, content_length=min(
                        1000, len(keyword) * 35 + len(collected) * 70))
                await _scroll_collection(
                    mgr, page, collected, max_scrolls)
            final_url = page.url
            if not collected:
                page_failure = await _xhs_page_failure(page)
        if not collected and not error:
            error = page_failure or "未拦截到搜索结果（关键词可能无结果，或页面接口已调整）"
        if not collected:
            saw = any("search/notes" in a for a in api_seen)
            print(f"[xhs_search] kw={keyword!r}; typed={typed}; direct_fallback={direct_fallback}; "
                  f"saw_search_api={saw}; "
                  f"final_url={final_url}; api_seen({len(api_seen)})={api_seen[:30]}")
    except Exception as e:
        error = f"打开搜索页失败: {e!r}"

    new_items = [it for nid, it in collected.items() if nid not in known_ids]
    return new_items, error


async def fetch_xhs_note_detail(mgr: BrowserManager, identity: Identity, note_id: str,
                                xsec_token: str = "", xsec_source: str = "pc_feed",
                                settle_ms: int = 1800, block_media: bool = True,
                                keep_context: bool = False,
                                ) -> Tuple[Optional[dict], str]:
    """打开笔记详情页,拦截 feed 接口拿到完整 note_card(含媒体直链)。
    返回 (note_card dict, error)。"""
    result: dict = {}
    error = ""
    page_failure = ""

    async def on_response(resp):
        if FEED_API in resp.url:
            try:
                data = (await resp.json()).get("data") or {}
            except Exception:
                return
            for it in (data.get("items") or []):
                card = it.get("note_card") or {}
                if str(card.get("note_id") or it.get("id") or "") == note_id or not result:
                    result.update(card)

    try:
        async with _visible_page_scope(mgr, identity, keep_context) as page:
            page.on("response", on_response)
            responses = _ResponseInbox(
                page, lambda r: FEED_API in r.url and r.status == 200)
            await page.goto(
                _note_url(note_id, xsec_token, xsec_source),
                wait_until="domcontentloaded", timeout=30000)
            # 详情首屏目前由 SSR 直出，常常不会再请求 v1/feed。优先读取页面
            # 内联状态；老页面没有 SSR 时再等待 feed 响应。
            try:
                ssr_card = _decamel(json.loads(
                    await page.evaluate(_SSR_NOTE_DETAIL_JS, note_id) or "{}"))
                if ssr_card:
                    result.update(ssr_card)
            except Exception:
                pass
            if not result:
                page_failure = await _xhs_page_failure(page)
            if not result and not page_failure:
                await responses.wait(8000, on_response)
            if result:
                content_length = sum(len(str(result.get(name) or "")) for name in (
                    "title", "display_title", "desc", "description"))
                await _xhs_reading_pause(
                    mgr, content_length=max(120, content_length))
            if not result:
                page_failure = page_failure or await _xhs_page_failure(page)
        if not result:
            error = (page_failure
                     or "未拦截到笔记详情(xsec_token 可能已过期或笔记不可见)")
    except Exception as e:
        error = f"打开笔记详情失败: {e!r}"
    return (result or None), error


async def fetch_xhs_comments(mgr: BrowserManager, identity: Identity, note_id: str,
                             known_cids: Set[str], xsec_token: str = "",
                             xsec_source: str = "pc_feed", max_scrolls: int = 6,
                             settle_ms: int = 1600, block_media: bool = True
                             ) -> Tuple[List[dict], str]:
    """打开笔记页,下滑评论区,拦截 comment/page 收集评论。返回 (新评论原始列表, error)。"""
    collected: Dict[str, dict] = {}
    error = ""
    page_failure = ""

    async def on_response(resp):
        if COMMENT_API in resp.url:
            try:
                data = (await resp.json()).get("data") or {}
            except Exception:
                return
            for c in (data.get("comments") or []):
                cid = str(c.get("id") or "")
                if cid:
                    collected[cid] = c

    try:
        async with _visible_page_scope(mgr, identity, False) as page:
            page.on("response", on_response)
            responses = _ResponseInbox(
                page, lambda r: COMMENT_API in r.url and r.status == 200)
            await page.goto(
                _note_url(note_id, xsec_token, xsec_source),
                wait_until="domcontentloaded", timeout=30000)
            # 首屏评论也可能只存在 SSR 状态中，不再单独发 comment/page。
            try:
                ssr_comments = _decamel(json.loads(
                    await page.evaluate(
                        _SSR_NOTE_COMMENTS_JS, note_id) or "[]"))
                for comment in ssr_comments:
                    cid = str(comment.get("id") or comment.get("comment_id") or "")
                    if cid:
                        collected[cid] = comment
            except Exception:
                pass
            if not collected:
                page_failure = await _xhs_page_failure(page)
            if not collected and not page_failure:
                await responses.wait(
                    max(1500, min(4000, settle_ms)), on_response)
                if not collected:
                    page_failure = await _xhs_page_failure(page)
            if not page_failure:
                await _scroll_collection(
                    mgr, page, collected, max_scrolls, known_cids)
            if not collected:
                page_failure = await _xhs_page_failure(page)
        if not collected and not error:
            error = page_failure or "未拦截到评论（笔记可能暂无评论，或 xsec_token 已过期）"
    except Exception as e:
        error = f"打开笔记页失败: {e!r}"

    new = [c for cid, c in collected.items() if cid not in known_cids]
    return new, error


_XHS_STATE_USER = """
() => {
  try {
    const marker = 'window.__INITIAL_STATE__=';
    let s = window.__INITIAL_STATE__;
    if (!s || !Object.keys(s).length) {
      for (const script of document.scripts) {
        const text = script.textContent || '';
        const at = text.indexOf(marker);
        if (at < 0) continue;
        try {
          s = (new Function('return (' + text.slice(at + marker.length) + ')'))();
        } catch (e) { s = null; }
        break;
      }
    }
    s = s || {};
    const u = s.user || {};
    // 不同页面结构兜底:登录用户资料可能在 userInfo / loginUser / userPageData
    return u.userInfo || u.loginUser || u.userPageData || u.info || null;
  } catch (e) { return null; }
}
"""


async def fetch_creator_published(mgr: BrowserManager, identity: Identity,
                                  settle_ms: int = 2500, block_media: bool = True
                                  ) -> Tuple[List[dict], str]:
    """打开创作平台「笔记管理」页,拦截它自己发的笔记列表接口,拿到已发布笔记。
    返回 (notes, error)。创作平台接口改版时,这里靠"含 note 列表"的启发式自适应。"""
    collected: Dict[str, dict] = {}
    api_seen: list = []
    error = ""

    async def on_response(resp):
        url = resp.url
        if "creator.xiaohongshu.com" not in url or "/api/" not in url:
            return
        try:
            data = await resp.json()
        except Exception:
            return
        d = data.get("data") if isinstance(data, dict) else None
        if not isinstance(d, dict):
            return
        for key in ("notes", "note_infos", "noteList", "list", "items", "noteInfos"):
            arr = d.get(key)
            if isinstance(arr, list) and arr and isinstance(arr[0], dict):
                it = arr[0]
                if any(k in it for k in ("noteId", "note_id", "id")):
                    for x in arr:
                        nid = str(x.get("noteId") or x.get("note_id") or x.get("id") or "")
                        if nid:
                            collected[nid] = x
                    api_seen.append(f"{url.split('?')[0].split('xiaohongshu.com')[-1]} key={key} n={len(arr)}")
                    break

    try:
        async with _visible_page_scope(mgr, identity, False) as page:
            page.on("response", on_response)
            responses = _ResponseInbox(
                page,
                lambda r: (
                    "creator.xiaohongshu.com" in r.url
                    and "/api/" in r.url and r.status == 200))
            for url in (
                    "https://creator.xiaohongshu.com/new/note-manager",
                    "https://creator.xiaohongshu.com/publish/publish?source=official"):
                await page.goto(
                    url, wait_until="domcontentloaded", timeout=40000)
                if "login" in page.url or "passport" in page.url:
                    error = "logged_out:创作平台未登录"
                    break
                await responses.wait(
                    10000, on_response, ready=lambda: bool(collected))
                if collected:
                    break
                await _scroll_collection(mgr, page, collected, 4)
                if collected:
                    break
            final_url = page.url
        if not collected:
            print(
                f"[xhs_creator_published] collected=0 "
                f"final_url={final_url} api_seen={api_seen[:8]}")
    except Exception as e:
        error = f"打开创作平台失败: {e!r}"
    return list(collected.values()), error


async def fetch_xhs_self_profile(mgr: BrowserManager, identity: Identity,
                                 timeout_ms: int = 15000, block_media: bool = False
                                 ) -> Tuple[dict, str]:
    """打开主站首页,拦截 v2/user/me 拿当前登录账号资料。

    ``/user/profile/me`` 已不再稳定触发当前用户接口：页面可能只请求配置和
    未读数，随后一直等到超时。主站首页仍会在初始化导航时请求 ``user/me``，
    因此必须在导航前预先挂好响应等待器，避免漏掉 DOMContentLoaded 之前的响应。
    返回 (user dict, error)。error == "logged_out" 表示登录态失效。"""
    me_data: dict = {}
    other_data: dict = {}
    api_seen = []                 # 看到的小红书 API 请求(诊断用)
    user_me_seen = False
    error = ""

    async def on_response(resp):
        nonlocal user_me_seen
        url = resp.url
        if "xiaohongshu.com" in url and "/api/sns/web/" in url and len(api_seen) < 40:
            api_seen.append(f"{resp.status} {url.split('?')[0].split('xiaohongshu.com')[-1]}")
        try:
            if USER_ME_API in url:
                user_me_seen = True
                d = (await resp.json()).get("data") or {}
                if d:
                    me_data.update(d)
            elif OTHERINFO_API in url:
                d = (await resp.json()).get("data") or {}
                if d:
                    other_data.update(d)
        except Exception:
            pass

    logged_out = False
    final_url = ""
    state_user = None
    has_login_btn = None
    page_failure = ""
    try:
        async with _visible_page_scope(mgr, identity, False) as page:
            page.on("response", on_response)
            # user/me 常在 DOMContentLoaded 前返回，导航前先挂同步事件队列。
            responses = _ResponseInbox(
                page, lambda r: USER_ME_API in r.url and r.status == 200)
            await page.goto(
                f"{_BASE}/",
                wait_until="domcontentloaded", timeout=30000)
            await responses.wait(timeout_ms, on_response)
            final_url = page.url
            if "passport" in final_url or "/login" in final_url:
                logged_out = True
            if me_data.get("guest") is True:
                logged_out = True
            try:
                has_login_btn = await page.get_by_text(
                    "登录", exact=True).first.is_visible(timeout=1200)
            except Exception:
                has_login_btn = None
            authenticated_me = bool(
                me_data.get("guest") is not True
                and (me_data.get("user_id") or me_data.get("red_id"))
            )
            # 首页首帧偶尔短暂显示“登录”；有效 user/me 比瞬态 UI 更权威。
            if has_login_btn is True and not authenticated_me:
                logged_out = True
            if not me_data and not other_data:    # 兜底:读 __INITIAL_STATE__
                try:
                    state_user = await page.evaluate(_XHS_STATE_USER)
                except Exception:
                    state_user = None
            if not authenticated_me and not state_user:
                page_failure = await _xhs_page_failure(page)
    except Exception as e:
        error = f"{e!r}"

    # 合并:me 提供身份(user_id/red_id),otherinfo 提供昵称/头像/粉丝
    result: dict = {}
    if other_data:
        result.update(other_data)
    if me_data:
        for k in ("user_id", "red_id", "nickname", "images", "guest"):
            if me_data.get(k) not in (None, ""):
                result[k] = me_data[k]
    if not result and state_user:
        result.update(state_user)

    has_user = bool(result.get("user_id") or result.get("nickname")
                    or result.get("red_id")
                    or (result.get("basic_info") or {}).get("nickname"))
    if logged_out or not has_user:
        if page_failure:
            error = page_failure
        elif logged_out:
            error = "logged_out"
        elif not error:
            error = ("no_user_me_xhr" if not user_me_seen
                     else "user/me 无有效用户字段")
        print(f"[xhs_self_profile] 未拿到资料; err={error}; final_url={final_url}; login_btn={has_login_btn}; "
              f"guest={me_data.get('guest')}; me={'有' if me_data else '无'}; "
              f"other={'有' if other_data else '无'}; state_user={'有' if state_user else '无'}; "
              f"api_seen({len(api_seen)})={api_seen[:25]}")
        return ({} if logged_out else result), error
    return result, error
