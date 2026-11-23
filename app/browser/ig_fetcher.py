"""Instagram 网页端隔离浏览器读写适配器（与项目其他平台浏览器抓取器保持统一返回契约）。

当 `instagram_read_mode` / `instagram_write_mode` 设为 `browser`，
或在 `hybrid` 模式下遇到直连接口兼容性失败（不含 `write_uncertain:`）时，
通过账号专属持久化 Profile 打开 `www.instagram.com` 拦截同域 API/GraphQL 响应或驱动页面交互。
"""
from __future__ import annotations

import asyncio
from typing import Callable, Dict, List, Mapping, Optional, Set, Tuple
from urllib.parse import quote, urlparse

from ..platforms._submit import notify_submit
from ..platforms.instagram.extract import (
    flatten_ig_comments,
    parse_ig_self_user,
)
from ..platforms.instagram.protocol import WEB_APP_ID
from ..platforms.instagram.resolve import clean_ig_username, resolve_ig_work_ref
from .human_interaction import (
    DEFAULT_HUMAN_ENGINE,
    human_click_first,
    human_fill_first,
    human_scroll,
    human_wait,
)
from .identity import Identity
from .manager import BrowserManager


async def fetch_ig_self_profile(
    mgr: BrowserManager, identity: Identity
) -> Tuple[dict, str]:
    """在账号隔离浏览器上下文内校验登录态并读取当前 Instagram 账号资料。"""
    page = await mgr.new_page(identity, block_media=True)
    try:
        await page.goto(
            "https://www.instagram.com/",
            wait_until="domcontentloaded",
            timeout=35000,
        )
        await human_wait(page, 1500)
        cur = str(page.url or "").lower()
        if "/accounts/login" in cur:
            return {}, "logged_out:Instagram 登录态失效，请重新登录"
        if "/challenge/" in cur or "/checkpoint/" in cur:
            return {}, "verification_required:Instagram 账号需要完成安全验证"

        result = await page.evaluate(
            """async (appId) => {
                const csrfMatch = document.cookie.match(/(?:^|;\\s*)csrftoken=([^;]+)/);
                const csrfToken = csrfMatch ? decodeURIComponent(csrfMatch[1]) : '';
                const getJson = async (u) => {
                    try {
                        const headers = {
                            'X-IG-App-ID': appId,
                            'X-Requested-With': 'XMLHttpRequest'
                        };
                        if (csrfToken) headers['X-CSRFToken'] = csrfToken;
                        const r = await fetch(u, {
                            credentials: 'include',
                            headers
                        });
                        if (r.status === 401) return { __status: 401 };
                        return await r.json();
                    } catch (_) { return null; }
                };
                const edit = await getJson('https://www.instagram.com/api/v1/accounts/edit/web_form_data/');
                if (edit && edit.__status === 401) return { error: 'logged_out' };
                const uname = edit && edit.form_data && edit.form_data.username ? String(edit.form_data.username) : '';
                if (!uname) return { error: 'missing_username' };
                const prof = await getJson('https://www.instagram.com/api/v1/users/web_profile_info/?username=' + encodeURIComponent(uname));
                if (prof && prof.data && prof.data.user) {
                    return { user: prof.data.user, error: '' };
                }
                return { user: { username: uname, full_name: (edit.form_data && edit.form_data.first_name) || uname }, error: '' };
            }""",
            WEB_APP_ID,
        )
        if not isinstance(result, dict):
            return {}, "browser_eval_failed"
        if result.get("error") == "logged_out":
            return {}, "logged_out:Instagram 登录态失效，请重新登录"
        if result.get("error"):
            return {}, str(result["error"])
        return parse_ig_self_user(result.get("user") or {}), ""
    except Exception as exc:
        return {}, f"browser:{type(exc).__name__}:{exc}"
    finally:
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass


def _collect_media_items_from_payload(payload: object, out: Dict[str, dict]) -> str:
    """从 Instagram v1 feed / web_profile_info / GraphQL 响应中递归抽取帖子对象。"""
    author_name = ""
    if not isinstance(payload, Mapping):
        return author_name

    # 1. REST v1 items[]
    items = payload.get("items")
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, Mapping):
                continue
            media = it.get("media") if isinstance(it.get("media"), Mapping) else it
            code = str(media.get("code") or media.get("shortcode") or media.get("pk") or "")
            if code:
                out.setdefault(code, dict(media))
                u = media.get("user") if isinstance(media.get("user"), Mapping) else {}
                if not author_name and (u.get("full_name") or u.get("username")):
                    author_name = str(u.get("full_name") or u.get("username"))

    # 2. web_profile_info -> data.user.edge_owner_to_timeline_media.edges
    data = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
    user = data.get("user") if isinstance(data.get("user"), Mapping) else {}
    if user:
        if not author_name:
            author_name = str(user.get("full_name") or user.get("username") or "")
        for edge_key in ("edge_owner_to_timeline_media", "edge_felix_video_timeline"):
            tl = user.get(edge_key)
            if isinstance(tl, Mapping) and isinstance(tl.get("edges"), list):
                for edge in tl["edges"]:
                    node = edge.get("node") if isinstance(edge, Mapping) else None
                    if isinstance(node, Mapping):
                        code = str(node.get("shortcode") or node.get("code") or node.get("id") or "")
                        if code:
                            enriched = dict(node)
                            enriched.setdefault("owner", {
                                "id": user.get("id"),
                                "username": user.get("username"),
                                "full_name": user.get("full_name"),
                                "profile_pic_url": user.get("profile_pic_url"),
                            })
                            out.setdefault(code, enriched)

    # 3. GraphQL xdt_api__v1__feed__user_timeline_graphql_connection
    conn = data.get("xdt_api__v1__feed__user_timeline_graphql_connection")
    if isinstance(conn, Mapping) and isinstance(conn.get("edges"), list):
        for edge in conn["edges"]:
            node = edge.get("node") if isinstance(edge, Mapping) else None
            if isinstance(node, Mapping):
                code = str(node.get("code") or node.get("shortcode") or node.get("pk") or "")
                if code:
                    out.setdefault(code, dict(node))
    return author_name


async def fetch_ig_posts(
    mgr: BrowserManager,
    identity: Identity,
    username_or_uid: str,
    known_ids: Optional[Set[str]] = None,
    max_scrolls: int = 6,
) -> Tuple[List[dict], str, str]:
    """打开 `https://www.instagram.com/{username}/` 拦截时间线帖子与 Reels。"""
    uname = clean_ig_username(username_or_uid) or str(username_or_uid or "").strip().lstrip("@")
    if not uname:
        return [], "", "missing_username"
    known = known_ids or set()
    collected: Dict[str, dict] = {}
    author_name = ""
    page = await mgr.new_page(identity, block_media=True)

    async def on_response(resp):
        nonlocal author_name
        u = str(resp.url or "")
        if not any(k in u for k in ("/api/v1/users/web_profile_info/", "/api/v1/feed/user/", "/graphql/query")):
            return
        try:
            payload = await resp.json()
        except Exception:
            return
        found_author = _collect_media_items_from_payload(payload, collected)
        if found_author and not author_name:
            author_name = found_author

    page.on("response", on_response)
    try:
        await page.goto(
            f"https://www.instagram.com/{quote(uname)}/",
            wait_until="domcontentloaded",
            timeout=35000,
        )
        await human_wait(page, 2500)
        cur = str(page.url or "").lower()
        if "/accounts/login" in cur:
            return [], "", "logged_out:Instagram 登录态已失效"
        if "/challenge/" in cur or "/checkpoint/" in cur:
            return [], "", "verification_required:Instagram 触发安全验证"

        for _ in range(max(1, int(max_scrolls))):
            if known and collected and all(k in known for k in collected):
                break
            await human_scroll(page)
            await human_wait(page, 950)

        if not collected:
            dom_cards = await page.evaluate(
                r"""(username) => {
                    const out = [];
                    const seen = new Set();
                    for (const a of document.querySelectorAll('a[href*="/p/"], a[href*="/reel/"]')) {
                        const href = a.getAttribute('href') || '';
                        const m = href.match(/\/(p|reel)\/([A-Za-z0-9_-]{5,32})/);
                        if (!m || seen.has(m[2])) continue;
                        seen.add(m[2]);
                        const img = a.querySelector('img');
                        out.push({
                            code: m[2],
                            product_type: m[1] === 'reel' ? 'clips' : 'feed',
                            display_url: img ? (img.currentSrc || img.src || '') : '',
                            accessibility_caption: img ? (img.getAttribute('alt') || '') : '',
                            user: { username: username }
                        });
                    }
                    return out;
                }""",
                uname,
            )
            for row in (dom_cards or []):
                if isinstance(row, dict) and row.get("code"):
                    collected.setdefault(str(row["code"]), row)
        return list(collected.values()), (author_name or uname), ("" if collected else "empty")
    except Exception as exc:
        return list(collected.values()), author_name, f"browser:{type(exc).__name__}:{exc}"
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass


async def fetch_ig_search(
    mgr: BrowserManager,
    identity: Identity,
    keyword: str,
    max_scrolls: int = 5,
) -> Tuple[List[dict], str]:
    """打开 Instagram 标签探索或关键词搜索页，拦截媒体结果。"""
    kw = str(keyword or "").strip()
    if not kw:
        return [], "搜索关键词不能为空"
    clean_tag = kw.lstrip("#").strip().replace(" ", "")
    target_url = (
        f"https://www.instagram.com/explore/tags/{quote(clean_tag)}/"
        if kw.startswith("#") or (" " not in kw and clean_tag)
        else f"https://www.instagram.com/explore/search/keyword/?q={quote(kw)}"
    )
    collected: Dict[str, dict] = {}
    page = await mgr.new_page(identity, block_media=True)

    async def on_response(resp):
        u = str(resp.url or "")
        if not any(k in u for k in ("/api/v1/tags/", "/api/v1/fbsearch/", "/graphql/query")):
            return
        try:
            payload = await resp.json()
        except Exception:
            return
        if not isinstance(payload, Mapping):
            return
        _collect_media_items_from_payload(payload, collected)
        inner = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
        for sec_key in ("sections", "top", "recent", "media_grid"):
            blk = inner.get(sec_key)
            secs = blk.get("sections") if isinstance(blk, Mapping) else blk
            if isinstance(secs, list):
                for sec in secs:
                    if not isinstance(sec, Mapping):
                        continue
                    lc = sec.get("layout_content") if isinstance(sec.get("layout_content"), Mapping) else {}
                    for m in (lc.get("medias") or []):
                        media = m.get("media") if isinstance(m, Mapping) and isinstance(m.get("media"), Mapping) else m
                        if isinstance(media, Mapping):
                            code = str(media.get("code") or media.get("pk") or "")
                            if code:
                                collected.setdefault(code, dict(media))

    page.on("response", on_response)
    try:
        await page.goto(target_url, wait_until="domcontentloaded", timeout=35000)
        await human_wait(page, 2500)
        cur = str(page.url or "").lower()
        if "/accounts/login" in cur:
            return [], "logged_out:Instagram 登录态已失效"
        for _ in range(max(1, int(max_scrolls))):
            await human_scroll(page)
            await human_wait(page, 950)

        if not collected:
            dom_cards = await page.evaluate(
                r"""() => {
                    const out = [];
                    const seen = new Set();
                    for (const a of document.querySelectorAll('a[href*="/p/"], a[href*="/reel/"]')) {
                        const href = a.getAttribute('href') || '';
                        const m = href.match(/\/(p|reel)\/([A-Za-z0-9_-]{5,32})/);
                        if (!m || seen.has(m[2])) continue;
                        seen.add(m[2]);
                        const img = a.querySelector('img');
                        out.push({
                            code: m[2],
                            product_type: m[1] === 'reel' ? 'clips' : 'feed',
                            display_url: img ? (img.currentSrc || img.src || '') : '',
                            accessibility_caption: img ? (img.getAttribute('alt') || '') : ''
                        });
                    }
                    return out;
                }"""
            )
            for row in (dom_cards or []):
                if isinstance(row, dict) and row.get("code"):
                    collected.setdefault(str(row["code"]), row)
        return list(collected.values()), ("" if collected else "empty")
    except Exception as exc:
        return list(collected.values()), f"browser:{type(exc).__name__}:{exc}"
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass


async def fetch_ig_comments(
    mgr: BrowserManager,
    identity: Identity,
    work_id: str,
    max_count: int = 50,
    *,
    include_replies: bool = True,
) -> Tuple[List[dict], str]:
    """打开帖子详情页拦截 `/comments/` 或 GraphQL 评论流。"""
    ref = resolve_ig_work_ref(work_id)
    code = ref.shortcode if ref else str(work_id or "").strip()
    if not code:
        return [], "missing_work_id"
    url = ref.canonical_url if (ref and ref.canonical_url) else f"https://www.instagram.com/p/{quote(code)}/"
    collected: Dict[str, dict] = {}
    page = await mgr.new_page(identity, block_media=True)

    async def on_response(resp):
        u = str(resp.url or "")
        if "/comments/" not in u and "stream_comments" not in u and "/graphql/query" not in u:
            return
        try:
            payload = await resp.json()
        except Exception:
            return
        if not isinstance(payload, Mapping):
            return
        raw_list = list(payload.get("comments") or []) + list(payload.get("preview_comments") or [])
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else {}
        media_gql = data.get("xdt_api__v1__media__shortcode__web_info")
        if isinstance(media_gql, Mapping) and isinstance(media_gql.get("items"), list):
            for it in media_gql["items"]:
                if isinstance(it, Mapping) and isinstance(it.get("preview_comments"), list):
                    raw_list.extend(it["preview_comments"])
        for c in flatten_ig_comments(raw_list, include_replies=include_replies):
            collected.setdefault(c["comment_id"], c)

    page.on("response", on_response)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=35000)
        await human_wait(page, 2200)
        if "/accounts/login" in str(page.url or "").lower():
            return [], "logged_out:Instagram 登录态已失效"
        for _ in range(max(2, min(8, (max_count // 15) + 2))):
            if len(collected) >= max_count:
                break
            await human_scroll(page, distance=720)
            await human_wait(page, 850)
        return list(collected.values())[:max_count], ("" if collected else "empty")
    except Exception as exc:
        return list(collected.values())[:max_count], f"browser:{type(exc).__name__}:{exc}"
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass


async def post_ig_comment(
    mgr: BrowserManager,
    identity: Identity,
    work_id: str,
    text: str,
    *,
    target_url: str = "",
    on_submit: Optional[Callable[[], object]] = None,
) -> Tuple[bool, str]:
    """在可见隔离浏览器窗口内打开帖子详情页发表评论并拦截 `/comment/` 确认。"""
    body = str(text or "").strip()
    ref = resolve_ig_work_ref(target_url or work_id)
    code = ref.shortcode if ref else str(work_id or "").strip()
    if not code or not body:
        return False, "缺少 Instagram 作品 ID 或评论正文"
    url = target_url or (ref.canonical_url if ref else f"https://www.instagram.com/p/{quote(code)}/")

    ctx = await mgr.open_headed(identity)
    page = await ctx.new_page()
    submitted = {"clicked": False}
    comment_resp = {"seen": False, "ok": False, "message": "", "comment_id": ""}

    async def on_response(resp):
        if comment_resp["seen"] or not submitted["clicked"]:
            return
        if str(getattr(resp.request, "method", "") or "").upper() != "POST":
            return
        path = urlparse(str(resp.url or "")).path.lower()
        if "/comments/add/" not in path and not path.endswith("/comment/"):
            return
        try:
            payload = await resp.json()
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        comment_resp["seen"] = True
        status_ok = str(payload.get("status") or "").lower() == "ok"
        c_obj = payload.get("comment") if isinstance(payload.get("comment"), dict) else payload
        cid = str(c_obj.get("pk") or c_obj.get("id") or "")
        comment_resp["ok"] = bool(status_ok or cid)
        comment_resp["comment_id"] = cid
        comment_resp["message"] = str(payload.get("message") or "")

    page.on("response", on_response)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=35000)
        await human_wait(page, 2500)
        cur = str(page.url or "").lower()
        if "/accounts/login" in cur:
            return False, "logged_out:Instagram 账号未登录"
        if "/challenge/" in cur or "/checkpoint/" in cur:
            return False, "verification_required:Instagram 触发安全验证"

        editor = None
        for sel in (
            'textarea[aria-label*="Add a comment" i]',
            'textarea[placeholder*="Add a comment" i]',
            'textarea[aria-label*="添加评论"]',
            'textarea[placeholder*="添加评论"]',
            "form textarea",
        ):
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                editor = loc
                break
        if editor is None:
            return False, "未找到 Instagram 评论输入框（可能帖子已关闭评论或未登录）"

        await DEFAULT_HUMAN_ENGINE.type_text(editor, body, page=page, timeout=4000)
        await human_wait(page, 400)

        post_btn = None
        for bsel in (
            'form div[role="button"]:has-text("Post")',
            'form button:has-text("Post")',
            'form div[role="button"]:has-text("发布")',
            'form button:has-text("发布")',
        ):
            loc = page.locator(bsel).first
            if await loc.count() and await loc.is_visible():
                post_btn = loc
                break
        if post_btn is None:
            return False, "未找到 Instagram 评论「Post / 发布」按钮"

        await notify_submit(on_submit)
        submitted["clicked"] = True
        await DEFAULT_HUMAN_ENGINE.click_locator(post_btn, page=page, timeout=4000)

        for _ in range(20):
            if comment_resp["seen"]:
                if comment_resp["ok"]:
                    return True, ""
                return False, f"评论接口拒绝: {comment_resp['message'] or 'comment_failed'}"
            await human_wait(page, 500)
        return False, "write_uncertain:已点击 Instagram 评论发布但未收到接口回包确认"
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        prefix = "write_uncertain:" if submitted["clicked"] else ""
        return False, f"{prefix}Instagram 浏览器评论异常: {exc!r}"
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass
