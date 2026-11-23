"""哔哩哔哩 Web 浏览器模式读写适配器（与项目其他平台浏览器抓取器保持统一返回契约）。

当传输模式设为 `browser` 或直连 WBI 接口遇到 `-352`/`-412` 风控回退时，
在账号专属隔离 Profile 内通过真实页面拦截 XHR / Shadow DOM 完成读写。
"""
from __future__ import annotations

import asyncio
from typing import Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import quote, urlparse

from ..platforms._submit import notify_submit
from ..platforms.bilibili.extract import (
    flatten_bili_comments,
    parse_bili_comment,
    parse_bili_self_user,
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


async def fetch_bili_self_profile(
    mgr: BrowserManager, identity: Identity
) -> Tuple[dict, str]:
    """在账号隔离浏览器上下文内调用同源 `nav` + `stat` 获取当前 B 站登录资料。"""
    page = await mgr.new_page(identity, block_media=True)
    try:
        await page.goto(
            "https://www.bilibili.com/",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        result = await page.evaluate(
            """async () => {
                const getJson = async (u) => {
                    try {
                        const r = await fetch(u, { credentials: 'include' });
                        return await r.json();
                    } catch (_) { return null; }
                };
                const nav = await getJson('https://api.bilibili.com/x/web-interface/nav');
                if (!nav) return { error: 'nav_network_error' };
                if (nav.code === -101 || !nav.data || !nav.data.isLogin) {
                    return { error: 'logged_out' };
                }
                const mid = String(nav.data.mid || '');
                const stat = mid
                    ? await getJson('https://api.bilibili.com/x/relation/stat?vmid=' + encodeURIComponent(mid))
                    : null;
                return {
                    nav: nav.data,
                    stat: (stat && stat.code === 0 && stat.data) ? stat.data : {},
                    error: ''
                };
            }"""
        )
        if not isinstance(result, dict):
            return {}, "browser_eval_failed"
        if result.get("error") == "logged_out":
            return {}, "logged_out:登录态失效，请重新登录"
        if result.get("error"):
            return {}, str(result["error"])
        return parse_bili_self_user(result.get("nav") or {}, result.get("stat") or {}), ""
    except Exception as exc:
        return {}, f"browser:{type(exc).__name__}:{exc}"
    finally:
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass


async def fetch_bili_videos(
    mgr: BrowserManager,
    identity: Identity,
    mid: str,
    known_ids: Optional[Set[str]] = None,
    max_scrolls: int = 6,
) -> Tuple[List[dict], str, str]:
    """打开 `space.bilibili.com/{mid}/video` 拦截空间投稿列表。"""
    uid = str(mid or "").strip()
    if not uid:
        return [], "", "missing_mid"
    known = known_ids or set()
    collected: Dict[str, dict] = {}
    author_name = ""
    page = await mgr.new_page(identity, block_media=True)

    async def on_response(resp):
        nonlocal author_name
        url_s = str(resp.url or "")
        if "/x/space/wbi/arc/search" not in url_s and "/x/space/arc/search" not in url_s:
            return
        try:
            payload = await resp.json()
        except Exception:
            return
        if not isinstance(payload, dict) or payload.get("code") != 0:
            return
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        lst = data.get("list") if isinstance(data.get("list"), dict) else {}
        for item in (lst.get("vlist") or []):
            if not isinstance(item, dict):
                continue
            bvid = str(item.get("bvid") or "")
            if bvid:
                collected.setdefault(bvid, item)
                if not author_name and item.get("author"):
                    author_name = str(item["author"])

    page.on("response", on_response)
    try:
        await page.goto(
            f"https://space.bilibili.com/{uid}/video",
            wait_until="domcontentloaded",
            timeout=35000,
        )
        await human_wait(page, 2500)
        for _ in range(max(1, int(max_scrolls))):
            if known and collected and all(k in known for k in collected):
                break
            await human_scroll(page)
            await human_wait(page, 900)
            next_btn = page.locator(
                ".vui_pagenation--btn-side:has-text('下一页'), .be-pager-next:not(.be-pager-disabled)"
            ).first
            try:
                if await next_btn.count() and await next_btn.is_visible():
                    cls = str(await next_btn.get_attribute("class") or "")
                    if "disabled" not in cls:
                        await DEFAULT_HUMAN_ENGINE.click_locator(next_btn, page=page, timeout=3000)
                        await human_wait(page, 1500)
                    else:
                        break
                else:
                    break
            except Exception:
                break

        if not collected:
            dom_cards = await page.evaluate(
                r"""() => {
                    const out = [];
                    const seen = new Set();
                    for (const a of document.querySelectorAll('a[href*="/video/BV1"]')) {
                        const m = (a.href || '').match(/\/video\/(BV1[0-9A-Za-z]{9})/);
                        if (!m || seen.has(m[1])) continue;
                        seen.add(m[1]);
                        const card = a.closest('.bili-video-card, .small-item, li, div') || a;
                        const img = card.querySelector('img');
                        const title = (a.getAttribute('title') || a.innerText || '').trim();
                        out.push({
                            bvid: m[1],
                            title: title.slice(0, 120),
                            pic: img ? (img.currentSrc || img.src || '') : ''
                        });
                    }
                    return out;
                }"""
            )
            for row in (dom_cards or []):
                if isinstance(row, dict) and row.get("bvid"):
                    collected.setdefault(str(row["bvid"]), row)
        return list(collected.values()), author_name, ("" if collected else "empty")
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


async def fetch_bili_search(
    mgr: BrowserManager,
    identity: Identity,
    keyword: str,
    max_scrolls: int = 5,
) -> Tuple[List[dict], str]:
    """打开 `search.bilibili.com/video` 拦截搜索结果视频列表。"""
    kw = str(keyword or "").strip()
    if not kw:
        return [], "搜索关键词不能为空"
    collected: Dict[str, dict] = {}
    page = await mgr.new_page(identity, block_media=True)

    async def on_response(resp):
        url_s = str(resp.url or "")
        if "/x/web-interface/wbi/search/" not in url_s and "/x/web-interface/search/" not in url_s:
            return
        try:
            payload = await resp.json()
        except Exception:
            return
        if not isinstance(payload, dict) or payload.get("code") != 0:
            return
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        res = data.get("result")
        if isinstance(res, list):
            for entry in res:
                if not isinstance(entry, dict):
                    continue
                if entry.get("result_type") == "video" and isinstance(entry.get("data"), list):
                    for sub in entry["data"]:
                        if isinstance(sub, dict) and sub.get("bvid"):
                            collected.setdefault(str(sub["bvid"]), sub)
                elif entry.get("bvid"):
                    collected.setdefault(str(entry["bvid"]), entry)

    page.on("response", on_response)
    try:
        await page.goto(
            f"https://search.bilibili.com/video?keyword={quote(kw)}",
            wait_until="domcontentloaded",
            timeout=35000,
        )
        await human_wait(page, 2500)
        for _ in range(max(1, int(max_scrolls))):
            await human_scroll(page)
            await human_wait(page, 900)
            next_btn = page.locator(".vui_pagenation--btn-side:has-text('下一页')").first
            try:
                if await next_btn.count() and await next_btn.is_visible():
                    cls = str(await next_btn.get_attribute("class") or "")
                    if "disabled" not in cls:
                        await DEFAULT_HUMAN_ENGINE.click_locator(next_btn, page=page, timeout=3000)
                        await human_wait(page, 1500)
                    else:
                        break
                else:
                    break
            except Exception:
                break
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


async def fetch_bili_comments(
    mgr: BrowserManager,
    identity: Identity,
    work_id: str,
    max_count: int = 50,
) -> Tuple[List[dict], str]:
    """打开视频/动态详情页并滚动评论区，拦截 `/x/v2/reply/wbi/main` 响应。"""
    wid = str(work_id or "").strip()
    if not wid:
        return [], "missing_work_id"
    url = (
        f"https://www.bilibili.com/video/{wid}"
        if wid.upper().startswith("BV1")
        else f"https://www.bilibili.com/opus/{wid}"
    )
    collected: Dict[str, dict] = {}
    page = await mgr.new_page(identity, block_media=True)

    async def on_response(resp):
        url_s = str(resp.url or "")
        if "/x/v2/reply/wbi/main" not in url_s and "/x/v2/reply/main" not in url_s:
            return
        try:
            payload = await resp.json()
        except Exception:
            return
        if not isinstance(payload, dict) or payload.get("code") != 0:
            return
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        for raw in flatten_bili_comments(data.get("replies"), data.get("top_replies")):
            parsed = parse_bili_comment(raw, default_aweme_id=wid)
            if parsed and parsed["comment_id"] not in collected:
                collected[parsed["comment_id"]] = parsed

    page.on("response", on_response)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=35000)
        await human_wait(page, 2200)
        for _ in range(max(2, min(8, (max_count // 15) + 2))):
            if len(collected) >= max_count:
                break
            await human_scroll(page, distance=820)
            await human_wait(page, 900)
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


async def post_bili_comment(
    mgr: BrowserManager,
    identity: Identity,
    work_id: str,
    text: str,
    *,
    on_submit: Optional[Callable[[], object]] = None,
) -> Tuple[bool, str]:
    """在可见浏览器窗口内打开视频/动态详情页发表评论（支持新版 Lit Web Components 穿透）。"""
    body = str(text or "").strip()
    wid = str(work_id or "").strip()
    if not wid or not body:
        return False, "缺少作品 ID 或评论正文"
    url = (
        f"https://www.bilibili.com/video/{wid}"
        if wid.upper().startswith("BV1")
        else f"https://www.bilibili.com/opus/{wid}"
    )
    ctx = await mgr.open_headed(identity)
    page = await ctx.new_page()
    submitted = {"clicked": False}
    reply_resp = {"seen": False, "ok": False, "message": ""}

    async def on_response(resp):
        if reply_resp["seen"] or not submitted["clicked"]:
            return
        if str(getattr(resp.request, "method", "") or "").upper() != "POST":
            return
        if "/x/v2/reply/add" not in urlparse(str(resp.url or "")).path.lower():
            return
        try:
            payload = await resp.json()
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        reply_resp["seen"] = True
        reply_resp["ok"] = payload.get("code") in (0, "0")
        reply_resp["message"] = str(payload.get("message") or f"code={payload.get('code')}")

    page.on("response", on_response)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=35000)
        await human_wait(page, 2500)
        if "passport.bilibili.com" in page.url or "/login" in page.url:
            return False, "logged_out:B 站账号未登录"

        await human_scroll(page, distance=560)
        await human_wait(page, 1400)

        editor = None
        for sel in (
            "bili-comments bili-comment-box bili-comment-rich-textarea #input",
            "bili-comment-rich-textarea div[contenteditable='true']",
            ".reply-box-textarea",
            "textarea[placeholder*='评论']",
            "div[contenteditable='true'][placeholder*='评论']",
        ):
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                editor = loc
                break
        if editor is None:
            return False, "未找到 B 站评论输入框（可能稿件关闭评论或账号未登录）"

        await DEFAULT_HUMAN_ENGINE.type_text(editor, body, page=page, timeout=4000)
        await human_wait(page, 400)

        send_btn = None
        for bsel in (
            "bili-comments bili-comment-box #pub button",
            ".reply-box-send",
            "button:has-text('发布')",
        ):
            loc = page.locator(bsel).first
            if await loc.count() and await loc.is_visible():
                send_btn = loc
                break
        if send_btn is None:
            return False, "未找到 B 站评论「发布」按钮"

        await notify_submit(on_submit)
        submitted["clicked"] = True
        await DEFAULT_HUMAN_ENGINE.click_locator(send_btn, page=page, timeout=4000)

        for _ in range(20):
            if reply_resp["seen"]:
                if reply_resp["ok"]:
                    return True, ""
                return False, f"write_uncertain:评论接口返回({reply_resp['message']})"
            await human_wait(page, 500)
        return False, "write_uncertain:已点击评论发布但未收到 /x/v2/reply/add 确认"
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        prefix = "write_uncertain:" if submitted["clicked"] else ""
        return False, f"{prefix}浏览器评论异常: {exc!r}"
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
