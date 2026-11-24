"""Facebook 隔离浏览器抓取器（GraphQL 响应拦截 + DOM 双层提取）。

覆盖场景：
- 账号登录态、风控检查点与限制弹窗检测 (`check_facebook_page_health`)
- Cookie 同意 / 隐私授权 / PIN 提示弹窗自动清理 (`dismiss_facebook_overlays`)
- 公共主页操作身份切换 (`switch_facebook_page_identity`, 通过 `.facebook.com` `i_user` Cookie)
- 本账号资料、已管理公共主页与已加入群组同步
- 个人主页 / 公共主页 / 群组时间线帖子采集 (`fetch_facebook_user_posts`)
- 关键词搜索帖子 / 视频 / 群组 / 主页 / 用户 (`fetch_facebook_search`, `fetch_facebook_entities_search`)
- 单帖 / 视频 / Reels 详情与评论区展开抓取 (`fetch_facebook_post_detail`, `fetch_facebook_comments`)
- 好友 / 关注 / 粉丝列表抓取 (`fetch_facebook_follows`)
- Messenger 私信会话收件箱与历史消息抓取 (`fetch_facebook_dm_conversations`, `fetch_facebook_dm_history`)
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from app.browser.human_interaction import DEFAULT_HUMAN_ENGINE, human_scroll, human_wait
from app.browser.identity import Identity
from app.browser.manager import BrowserManager
from app.platforms.facebook.extract import (
    extract_graphql_comments,
    extract_graphql_groups,
    extract_graphql_pages,
    extract_graphql_posts,
    extract_pages_from_html,
    extract_target_post_from_sjs,
    parse_facebook_aweme,
    parse_facebook_comment,
    parse_facebook_dm_conversations,
    parse_facebook_dm_messages,
    parse_facebook_user,
    parse_ndjson_payloads,
)
from app.platforms.facebook.gql_registry import record_observed_gql_request
from app.platforms.facebook.protocol import build_page_switch_cookie, parse_members_count
from app.platforms.facebook.resolve import group_post_url_from_photo, normalize_facebook_url, resolve_facebook_post_ref
from app.platforms.facebook.selectors import (
    ACCOUNT_BLOCKED_DOM_SELECTORS,
    ACCOUNT_DISABLED_URL_PARTS,
    ACCOUNT_LOCKED_URL_PARTS,
    AUTH_READY_SELECTORS,
    COMMENT_OPEN_SELECTORS,
    COOKIE_ACCEPT_SELECTORS,
    LOGIN_PROMPT_SELECTORS,
    RESTRICTED_DIALOG_SELECTORS,
)
from app.platforms.facebook.urls import (
    FACEBOOK_BASE,
    FRIENDS_ME_URL,
    GQL_ENDPOINT_HINTS,
    GROUPS_JOINS_URL,
    GROUPS_SYNC_URL,
    MESSAGES_URL,
    PAGES_SYNC_URL,
    PROFILE_ME_URL,
    dm_thread_url,
    followers_url,
    following_url,
    friends_url,
    group_find_url,
    page_find_url,
    people_search_url,
    post_search_url,
    profile_url,
)

_log = logging.getLogger("creatorhub.browser.facebook")


def _attach_gql_request_observer(page):
    """被动监听浏览器发出的 Facebook `POST /api/graphql/` 请求，学习实时 `doc_id` 与静态变量模板。

    仅记录公开 Relay 查询名、数字 `doc_id` 和剥离隐私后的布尔/枚举开关，绝不持久化任何账号凭据或正文。
    """
    def _on_request(req):
        try:
            if str(getattr(req, "method", "") or "").upper() != "POST":
                return
            url = str(getattr(req, "url", "") or "")
            if not any(h in url for h in GQL_ENDPOINT_HINTS):
                return
            post_data = req.post_data
            if not post_data:
                return
            headers = getattr(req, "headers", None) or {}
            fname_hdr = str(headers.get("x-fb-friendly-name") or "")
            record_observed_gql_request(post_data, friendly_name_header=fname_hdr)
        except Exception:
            pass

    page.on("request", _on_request)
    return _on_request


def _detach_gql_request_observer(page, listener) -> None:
    if listener is None:
        return
    try:
        page.remove_listener("request", listener)
    except Exception:
        pass


async def dismiss_facebook_overlays(page) -> None:
    """自动清理 Facebook Cookie 同意框、隐私确认框与 Messenger 6 位 PIN 码引导弹窗。

    仅关闭非破坏性提示，绝不自动点击验证码或安全检查点挑战控件。
    """
    try:
        for sel in COOKIE_ACCEPT_SELECTORS:
            loc = page.locator(sel).first
            try:
                if await loc.count() and await loc.is_visible():
                    await DEFAULT_HUMAN_ENGINE.click_locator(loc, page=page, timeout=2500)
                    await human_wait(page, 800)
                    break
            except Exception:
                continue
    except Exception:
        pass

    # 若位于隐私同意页 (privacy/consent)，按非破坏性按钮放行
    try:
        cur_url = str(page.url or "").lower()
        if "privacy/consent" in cur_url:
            btn = page.locator('div[id*="mount"] div[role="dialog"] div[role="button"]').first
            if await btn.count() and await btn.is_visible():
                await DEFAULT_HUMAN_ENGINE.click_locator(btn, page=page, timeout=2500)
                await human_wait(page, 1200)
    except Exception:
        pass

    # 清理 Messenger 端到端加密 6 位 PIN 引导弹窗（选择稍后/关闭，不输入伪造 PIN）
    try:
        pin_dlg = page.locator('div[role="dialog"]:has(input[maxlength="6"])').first
        if await pin_dlg.count() and await pin_dlg.is_visible():
            close_cands = (
                'div[role="dialog"] div[role="button"][aria-label="Close" i]',
                'div[role="dialog"] div[role="button"][aria-label="关闭"]',
                'div[role="dialog"] div[role="button"][aria-label="關閉"]',
                'div[role="dialog"] div[role="button"]:has-text("Not now")',
                'div[role="dialog"] div[role="button"]:has-text("暂不")',
                'div[role="dialog"] div[role="button"]:has-text("稍後")',
            )
            for sel in close_cands:
                c = page.locator(sel).first
                if await c.count() and await c.is_visible():
                    await DEFAULT_HUMAN_ENGINE.click_locator(c, page=page, timeout=2500)
                    await human_wait(page, 800)
                    break
    except Exception:
        pass


async def check_facebook_page_health(page) -> str:
    """探测当前页面是否处于未登录、安全检查点锁定或账号功能受限状态。"""
    try:
        cur_url = str(page.url or "").lower()
    except Exception:
        cur_url = ""

    if any(part in cur_url for part in ACCOUNT_DISABLED_URL_PARTS):
        return f"verification_required:Facebook 账号已被停用或封禁({page.url})"
    if any(part in cur_url for part in ACCOUNT_LOCKED_URL_PARTS):
        return f"verification_required:Facebook 账号触发安全检查或二次验证({page.url})"
    if "/login" in cur_url and "checkpoint" not in cur_url:
        return "logged_out:Facebook 登录态已失效，请重新登录"

    # 检查账号功能受限弹窗 (`help/contact/`)
    for sel in RESTRICTED_DIALOG_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                txt = ""
                try:
                    txt = (await page.locator('div[role="dialog"]').first.inner_text() or "").strip()[:160]
                except Exception:
                    pass
                return f"rate_limit:Facebook 功能受限({txt or 'help/contact'})"
        except Exception:
            continue

    # 检查登录框是否占屏（且不存在已登录导航栏）
    has_nav = False
    for sel in AUTH_READY_SELECTORS:
        try:
            loc = page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                has_nav = True
                break
        except Exception:
            continue

    if not has_nav:
        for sel in LOGIN_PROMPT_SELECTORS:
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    return "logged_out:Facebook 登录态已失效，请重新登录"
            except Exception:
                continue
        for sel in ACCOUNT_BLOCKED_DOM_SELECTORS:
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    return "verification_required:Facebook 账号可能被锁定或需安全验证"
            except Exception:
                continue

    return ""


async def switch_facebook_page_identity(ctx, page, page_id: str = "") -> Tuple[bool, str]:
    """切换 Facebook 操作身份（设置或清除 `.facebook.com` 的 `i_user` Cookie 并刷新）。

    - `page_id` 非空：写入 `i_user=<page_id>` 切换至对应公共主页（Profile+ Page）；
    - `page_id` 为空：删除 `i_user` 恢复为个人主账号身份。
    """
    pid = str(page_id or "").strip()
    try:
        # 先取出并过滤掉旧的 i_user，再重新写入或直接清除
        existing = await ctx.cookies([FACEBOOK_BASE, "https://web.facebook.com"])
        other_cookies = [c for c in existing if c.get("name") != "i_user"]
        try:
            await ctx.clear_cookies(name="i_user")
        except TypeError:
            # 兼容不支持按名称清理的旧版本上下文
            await ctx.clear_cookies()
            if other_cookies:
                await ctx.add_cookies(other_cookies)
        if pid:
            await ctx.add_cookies([build_page_switch_cookie(pid)])
        if page is not None and not page.is_closed():
            cur = str(page.url or "")
            if cur.startswith("http"):
                await page.reload(wait_until="domcontentloaded", timeout=35000)
                await human_wait(page, 1800)
        return True, ""
    except Exception as exc:
        return False, f"切换 Facebook 主页身份失败: {exc!r}"


# ── 页面内嵌 SSR `<script type="application/json">` 提取与翻页辅助 ──
async def _extract_inline_sjs_payloads(page, markers: Tuple[str, ...]) -> List[Any]:
    """读取 Facebook Comet 首屏内联 `<script type="application/json" data-sjs>` JSON 块。

    Facebook 搜索页、主页时间线与帖子页的首批数据直接由服务端注入在 `script[type="application/json"]` 中，
    并不会触发 `/api/graphql` XHR，因此必须与网络拦截互补读取。
    """
    try:
        raw_scripts = await page.evaluate(
            """(markers) => {
                const out = [];
                for (const sc of document.querySelectorAll('script[type="application/json"]')) {
                    const txt = sc.textContent || '';
                    if (txt.length < 120) continue;
                    if (markers.some(m => txt.includes(m))) {
                        out.push(txt);
                    }
                }
                return out;
            }""",
            list(markers),
        )
    except Exception:
        return []
    objs: List[Any] = []
    for raw in (raw_scripts or []):
        try:
            objs.append(json.loads(raw))
        except Exception:
            continue
    return objs


def _post_richness_score(aw: Any) -> int:
    return (
        (10 if aw.medias else 0)
        + (4 if aw.desc else 0)
        + (3 if aw.author_name else 0)
        + (3 if aw.create_time else 0)
        + (2 if aw.like_count else 0)
        + (2 if aw.comment_count else 0)
        + (2 if aw.source_url and "/photo" not in aw.source_url else 0)
    )


def _merge_post_candidate(
    items_by_id: Dict[str, Dict[str, Any]],
    scores_by_id: Dict[str, int],
    raw_node: Dict[str, Any],
) -> None:
    aw = parse_facebook_aweme(raw_node)
    if aw is None or not aw.aweme_id:
        return
    score = _post_richness_score(aw)
    if aw.aweme_id not in items_by_id or score > scores_by_id.get(aw.aweme_id, -1):
        items_by_id[aw.aweme_id] = raw_node
        scores_by_id[aw.aweme_id] = score


async def _scroll_feed_down(page, distance: int = 1400) -> None:
    """优先执行拟人滚轮滚动；若滚动容器未响应滚轮（如焦点偏移），补充平滑 window.scrollBy 确保触发翻页。"""
    before_y = 0
    try:
        before_y = int(await page.evaluate("() => Math.round(window.scrollY || document.documentElement.scrollTop || 0)"))
    except Exception:
        pass
    await human_scroll(page, distance=distance)
    try:
        after_y = int(await page.evaluate("() => Math.round(window.scrollY || document.documentElement.scrollTop || 0)"))
        if after_y <= before_y + 40:
            await page.evaluate("(dy) => window.scrollBy({ top: dy, behavior: 'smooth' })", distance)
    except Exception:
        pass


# ── 页面内 DOM 帖子/用户/群组/评论提取脚本（作为 GraphQL 拦截的可靠双保险） ──
_DOM_EXTRACT_POSTS_JS = """() => {
    const out = [];
    const seen = new Set();
    const rawContainers = [
        ...document.querySelectorAll('div[role="feed"] > div, div[aria-posinset], div[role="article"], div[data-pagelet^="FeedUnit_"]')
    ];
    // 过滤嵌套在主帖内部的评论 article，保留顶层帖子卡片
    const articles = rawContainers.filter(el => {
        const aria = (el.getAttribute('aria-label') || '').toLowerCase();
        if (aria.includes('comment') || aria.includes('评论') || aria.includes('留言') || aria.includes('reply') || aria.includes('回复')) {
            return false;
        }
        return true;
    });

    const PRIMARY_LINK_RE = /\\/(posts|permalink|videos|reel|reels|watch|permalink\\.php|story\\.php|share\\/p|share\\/v)/i;
    const FALLBACK_LINK_RE = /(\\/photo\\/|\\/photo\\?|multi_permalinks=|story_fbid=|pfbid[0-9A-Za-z]+)/i;

    for (const art of articles) {
        const links = [...art.querySelectorAll('a[href]')];
        let postUrl = '';
        let fallbackUrl = '';
        for (const a of links) {
            const h = a.href || '';
            if (!h || h.includes('comment_id=') || h.includes('/hashtag/') || h.includes('/search/')) continue;
            const clean = h.split('?__cft__')[0].split('&__cft__')[0].split('&__tn__')[0];
            if (PRIMARY_LINK_RE.test(clean)) {
                postUrl = clean;
                break;
            }
            if (!fallbackUrl && FALLBACK_LINK_RE.test(clean)) {
                fallbackUrl = clean;
            }
        }
        if (!postUrl && fallbackUrl) {
            // 将群组图片灯箱链接 (/photo/?fbid=...&set=gm.<pid>&idorvanity=<gid>) 升级为群组帖子链接
            const mSet = fallbackUrl.match(/[?&]set=(?:gm|pcb)\\.(\\d{5,25})/);
            const mGid = fallbackUrl.match(/[?&]idorvanity=([A-Za-z0-9._-]+)/);
            if (mSet && mGid) {
                postUrl = `https://www.facebook.com/groups/${mGid[1]}/posts/${mSet[1]}/`;
            } else {
                postUrl = fallbackUrl;
            }
        }
        if (!postUrl || seen.has(postUrl)) continue;
        seen.add(postUrl);

        // 提取作者与头像
        let authorName = '';
        let authorUrl = '';
        const hEls = [...art.querySelectorAll('h2 a[href], h3 a[href], h4 a[href], strong a[href]')];
        for (const hEl of hEls) {
            const txt = (hEl.innerText || '').trim();
            if (txt && txt.length <= 100) {
                authorName = txt;
                authorUrl = hEl.href || '';
                break;
            }
        }
        let avatar = '';
        const avImg = art.querySelector('image, img[height="40"], img[width="40"]');
        if (avImg) {
            avatar = avImg.getAttribute('xlink:href') || avImg.getAttribute('href') || avImg.src || '';
        }

        // 提取正文文本
        const msgNodes = [...art.querySelectorAll('div[data-ad-rendering-role="story_message"], div[data-ad-preview="message"], div[dir="auto"]')];
        const parts = [];
        for (const n of msgNodes) {
            if (n.closest('div[role="button"]') || n.closest('form')) continue;
            const t = (n.innerText || '').trim();
            if (t && t.length > 2 && !parts.includes(t)) {
                parts.push(t);
            }
            if (parts.length >= 4) break;
        }
        const desc = parts.join('\\n').slice(0, 2000);

        // 粗略提取卡片底部的点赞与评论数
        let likeCountText = '';
        let commentCountText = '';
        for (const sp of art.querySelectorAll('span[aria-label], div[role="button"] span, span')) {
            const aria = (sp.getAttribute('aria-label') || '').trim();
            const txt = (sp.innerText || '').trim();
            if (!likeCountText && /^(Like|Love|Care|Haha|Wow|Sad|Angry|赞|讚|心情)[:：\\s]*([\\d.,]+\\s*[KkMm万萬]?)/i.test(aria)) {
                likeCountText = aria;
            }
            if (!commentCountText && /([\\d.,]+\\s*[KkMm万萬]?)\\s*(comments?|条评论|則留言|评论|留言)/i.test(txt)) {
                commentCountText = txt;
            }
        }

        // 提取图片/视频标识
        const images = [];
        for (const img of art.querySelectorAll('img[src]')) {
            const src = img.src || '';
            const r = img.getBoundingClientRect();
            if (src.startsWith('http') && !src.includes('emoji') && !src.includes('rsrc.php') && r.width >= 120 && r.height >= 80) {
                if (!images.includes(src)) images.push(src);
            }
        }
        const videoEl = art.querySelector('video');
        const hasVideo = !!videoEl;
        const videoSrc = (videoEl && videoEl.src && videoEl.src.startsWith('http')) ? videoEl.src : '';
        const cover = (videoEl && videoEl.poster) || images[0] || '';

        if (!desc && !images.length && !hasVideo) continue;
        out.push({
            source_url: postUrl,
            author_name: authorName,
            author_url: authorUrl,
            avatar,
            desc,
            like_count: likeCountText,
            comment_count: commentCountText,
            media_type: hasVideo ? 'video' : 'images',
            video_url: videoSrc,
            images,
            cover,
        });
    }
    return out;
}"""


_DOM_EXTRACT_SELF_PROFILE_JS = """() => {
    const out = { uid: '', unique_id: '', nickname: '', avatar: '', signature: '', follower_count: 0, following_count: 0 };
    try {
        const html = document.documentElement.innerHTML || '';
        const mUid = html.match(/"USER_ID"\\s*:\\s*"(\\d{4,25})"/);
        if (mUid && mUid[1] !== '0') out.uid = mUid[1];
        const mVan = html.match(/"vanity"\\s*:\\s*"([A-Za-z0-9._-]{2,80})"/);
        if (mVan) out.unique_id = mVan[1];
    } catch (_) {}

    const h1 = document.querySelector('div[role="main"] h1');
    if (h1) out.nickname = (h1.innerText || '').trim().split('\\n')[0].trim();
    if (!out.nickname) {
        const title = (document.title || '').replace(/\\s*[|\\-–]\\s*Facebook\\s*$/i, '').trim();
        if (title && !/facebook|log in|登录/i.test(title)) out.nickname = title;
    }

    const svgImg = document.querySelector('div[role="main"] svg image[xlink\\:href], div[role="main"] svg image[href]');
    if (svgImg) {
        out.avatar = svgImg.getAttribute('xlink:href') || svgImg.getAttribute('href') || '';
    }
    if (!out.avatar) {
        const img = document.querySelector('div[role="main"] img[src*="scontent"]');
        if (img) out.avatar = img.src || '';
    }

    // 读取主页好友/粉丝/关注文本计数
    const links = [...document.querySelectorAll('div[role="main"] a[href]')];
    for (const a of links) {
        const h = (a.href || '').toLowerCase();
        const t = (a.innerText || '').trim();
        if (!t) continue;
        if (h.includes('/followers') || /follower|粉丝|追蹤者/i.test(t)) {
            out.follower_text = t;
        } else if (h.includes('/following') || /following|正在关注|追蹤中/i.test(t)) {
            out.following_text = t;
        } else if (h.includes('/friends') || /friend|好友|朋友/i.test(t)) {
            out.friends_text = t;
        }
    }
    return out;
}"""


async def fetch_facebook_self_profile(mgr: BrowserManager, identity: Identity) -> Tuple[Dict[str, Any], str]:
    """在账号隔离浏览器中打开 `/profile.php`，读取本账号资料、统计与公共主页列表。"""
    ctx = await mgr.context_for(identity)
    page = await ctx.new_page()
    try:
        await page.goto(PROFILE_ME_URL, wait_until="domcontentloaded", timeout=40000)
        await human_wait(page, 2200)
        await dismiss_facebook_overlays(page)

        health_err = await check_facebook_page_health(page)
        if health_err:
            return {}, health_err

        raw = await page.evaluate(_DOM_EXTRACT_SELF_PROFILE_JS)
        cookies = await ctx.cookies([FACEBOOK_BASE, "https://web.facebook.com"])
        c_user = next((c["value"] for c in cookies if c.get("name") == "c_user" and c.get("value")), "")
        if c_user and not raw.get("uid"):
            raw["uid"] = c_user
        # 从最终跳转 URL 中补充 vanity
        cur_url = str(page.url or "")
        if not raw.get("unique_id"):
            m_v = re.search(r"facebook\.com/([A-Za-z0-9._-]+)(?:[/?#]|$)", cur_url)
            if m_v and m_v.group(1).lower() not in {"profile.php", "home.php", "login"}:
                raw["unique_id"] = m_v.group(1)

        friends_cnt = parse_members_count(raw["friends_text"]) if raw.get("friends_text") else 0
        raw["friends_count"] = friends_cnt
        if raw.get("follower_text"):
            raw["follower_count"] = parse_members_count(raw["follower_text"])
            raw["follower_count_is_friends_fallback"] = False
        elif friends_cnt > 0:
            raw["follower_count"] = friends_cnt
            raw["follower_count_is_friends_fallback"] = True
        if raw.get("following_text"):
            raw["following_count"] = parse_members_count(raw["following_text"])
            raw["following_count_is_friends_fallback"] = False
        elif friends_cnt > 0:
            raw["following_count"] = friends_cnt
            raw["following_count_is_friends_fallback"] = True

        prof = parse_facebook_user(raw)
        if not prof.get("uid") and not prof.get("nickname"):
            return {}, "logged_out:未能读取 Facebook 当前登录账号资料"
        return prof, ""
    except Exception as exc:
        return {}, f"抓取 Facebook 资料失败: {exc!r}"
    finally:
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass


async def fetch_facebook_pages(mgr: BrowserManager, identity: Identity) -> Tuple[List[Dict[str, Any]], str]:
    """同步当前账号管理的 Facebook 公共主页列表。"""
    ctx = await mgr.context_for(identity)
    page = await ctx.new_page()
    captured: Dict[str, Dict[str, Any]] = {}

    async def on_response(resp):
        try:
            if not any(h in str(resp.url or "") for h in GQL_ENDPOINT_HINTS) or resp.status >= 400:
                return
            txt = await resp.text()
            for obj in parse_ndjson_payloads(txt):
                for p in extract_graphql_pages(obj):
                    key = p.get("page_id") or p.get("link")
                    if key and key not in captured:
                        captured[key] = p
        except Exception:
            pass

    page.on("response", on_response)
    try:
        await page.goto(PAGES_SYNC_URL, wait_until="domcontentloaded", timeout=40000)
        await human_wait(page, 2500)
        await dismiss_facebook_overlays(page)
        health_err = await check_facebook_page_health(page)
        if health_err:
            return [], health_err

        html_text = await page.content()
        for p in extract_pages_from_html(html_text):
            key = p.get("page_id") or p.get("link")
            if key and key not in captured:
                captured[key] = p

        # DOM 兜底读取主页列表链接
        dom_pages = await page.evaluate(
            """() => {
                const out = [];
                const seen = new Set();
                for (const a of document.querySelectorAll('div[role="main"] a[href*="facebook.com/"]')) {
                    const href = (a.href || '').split('&__')[0];
                    const name = (a.innerText || '').trim().split('\\n')[0].trim();
                    if (!name || name.length > 80 || seen.has(href)) continue;
                    if (/\\/(pages|groups|events|help|settings|login|watch|marketplace)/i.test(href)) continue;
                    seen.add(href);
                    const mId = href.match(/[?&]id=(\\d{5,25})/);
                    out.push({ page_id: mId ? mId[1] : '', name, link: href, is_profile_plus: true, members_text: '', members_count: 0 });
                }
                return out.slice(0, 60);
            }"""
        )
        for p in (dom_pages or []):
            key = p.get("page_id") or p.get("link")
            if key and key not in captured:
                captured[key] = p
        return list(captured.values()), ""
    except Exception as exc:
        return [], f"同步 Facebook 公共主页失败: {exc!r}"
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


async def fetch_facebook_groups(
    mgr: BrowserManager,
    identity: Identity,
    *,
    max_scrolls: int = 8,
) -> Tuple[List[Dict[str, Any]], str]:
    """同步当前账号已加入的 Facebook 群组列表。"""
    ctx = await mgr.context_for(identity)
    page = await ctx.new_page()
    captured: Dict[str, Dict[str, Any]] = {}

    async def on_response(resp):
        try:
            if not any(h in str(resp.url or "") for h in GQL_ENDPOINT_HINTS) or resp.status >= 400:
                return
            txt = await resp.text()
            for obj in parse_ndjson_payloads(txt):
                for g in extract_graphql_groups(obj):
                    key = g.get("group_id") or g.get("link")
                    if key and key not in captured:
                        captured[key] = g
        except Exception:
            pass

    page.on("response", on_response)
    try:
        await page.goto(GROUPS_JOINS_URL, wait_until="domcontentloaded", timeout=40000)
        await human_wait(page, 2500)
        await dismiss_facebook_overlays(page)
        health_err = await check_facebook_page_health(page)
        if health_err:
            return [], health_err

        prev_total = 0
        stagnant = 0
        for _ in range(max(1, min(int(max_scrolls), 20))):
            dom_groups = await page.evaluate(
                """() => {
                    const out = [];
                    for (const a of document.querySelectorAll('a[href*="/groups/"]')) {
                        const href = (a.href || '').split('?')[0].replace(/\\/+$/, '');
                        const m = href.match(/\\/groups\\/([A-Za-z0-9._-]+)$/);
                        if (!m) continue;
                        const gid = m[1];
                        if (['feed', 'discover', 'joins', 'create', 'notifications'].includes(gid.toLowerCase())) continue;
                        const name = (a.innerText || '').trim().split('\\n')[0].trim();
                        if (!name || name.length > 120) continue;
                        out.push({ group_id: gid, name, link: href, members_text: '', members_count: 0, is_admin: false });
                    }
                    return out;
                }"""
            )
            for g in (dom_groups or []):
                key = g.get("group_id") or g.get("link")
                if key and key not in captured:
                    captured[key] = g
            if len(captured) <= prev_total:
                stagnant += 1
                if stagnant >= 2 and len(captured) > 0:
                    break
            else:
                stagnant = 0
                prev_total = len(captured)
            await _scroll_feed_down(page, distance=1200)
            await human_wait(page, 1600)

        return list(captured.values()), ""
    except Exception as exc:
        return [], f"同步 Facebook 群组失败: {exc!r}"
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


async def fetch_facebook_user_posts(
    mgr: BrowserManager,
    identity: Identity,
    user_ref: str,
    known_ids: Optional[Set[str]] = None,
    *,
    max_scrolls: int = 12,
    max_items: int = 0,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any], str]:
    """抓取 Facebook 个人主页 / 公共主页 / 群组时间线帖子（GraphQL 拦截 + DOM 双层提取）。"""
    known = set(known_ids or ())
    target_url = profile_url(user_ref)
    ctx = await mgr.context_for(identity)
    page = await ctx.new_page()

    items_by_id: Dict[str, Dict[str, Any]] = {}
    scores_by_id: Dict[str, int] = {}
    author_info: Dict[str, Any] = {}

    async def on_response(resp):
        try:
            if not any(h in str(resp.url or "") for h in GQL_ENDPOINT_HINTS) or resp.status >= 400:
                return
            txt = await resp.text()
            for obj in parse_ndjson_payloads(txt):
                for st in extract_graphql_posts(obj):
                    _merge_post_candidate(items_by_id, scores_by_id, st)
        except Exception:
            pass

    page.on("response", on_response)
    req_observer = _attach_gql_request_observer(page)
    try:
        await page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
        await human_wait(page, 2500)
        await dismiss_facebook_overlays(page)
        health_err = await check_facebook_page_health(page)
        if health_err:
            return [], {}, health_err

        for obj in await _extract_inline_sjs_payloads(page, ("comet_sections", "permalink_url", '"Story"')):
            for st in extract_graphql_posts(obj):
                _merge_post_candidate(items_by_id, scores_by_id, st)

        try:
            raw_prof = await page.evaluate(_DOM_EXTRACT_SELF_PROFILE_JS)
            if isinstance(raw_prof, dict):
                if not raw_prof.get("unique_id"):
                    raw_prof["unique_id"] = user_ref
                author_info = parse_facebook_user(raw_prof)
        except Exception:
            pass

        prev_total = 0
        stagnant = 0
        limit_scrolls = max(1, min(int(max_scrolls or 12), 40))
        for _ in range(limit_scrolls):
            try:
                dom_rows = await page.evaluate(_DOM_EXTRACT_POSTS_JS)
                for r in (dom_rows or []):
                    _merge_post_candidate(items_by_id, scores_by_id, r)
            except Exception:
                pass

            if known and any(k in items_by_id for k in known):
                break
            if max_items and len(items_by_id) >= max_items:
                break
            if len(items_by_id) <= prev_total:
                stagnant += 1
                if stagnant >= 3:
                    break
            else:
                stagnant = 0
                prev_total = len(items_by_id)
            await _scroll_feed_down(page, distance=1500)
            await human_wait(page, 2200)

        out = list(items_by_id.values())
        if max_items:
            out = out[:max_items]
        return out, author_info, ""
    except Exception as exc:
        return list(items_by_id.values()), author_info, f"抓取 Facebook 主页帖子失败: {exc!r}"
    finally:
        _detach_gql_request_observer(page, req_observer)
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass


async def fetch_facebook_search(
    mgr: BrowserManager,
    identity: Identity,
    keyword: str,
    *,
    max_scrolls: int = 8,
    max_items: int = 0,
    content_type: str = "all",
    recent: bool = False,
) -> Tuple[List[Dict[str, Any]], str]:
    """关键词搜索 Facebook 帖子 / 视频（GraphQL 拦截 + DOM 提取）。"""
    kw = str(keyword or "").strip()
    if not kw:
        return [], "关键词不能为空"
    search_url = post_search_url(kw, content_type=content_type, recent=recent)
    ctx = await mgr.context_for(identity)
    page = await ctx.new_page()
    items_by_id: Dict[str, Dict[str, Any]] = {}
    scores_by_id: Dict[str, int] = {}

    async def on_response(resp):
        try:
            if not any(h in str(resp.url or "") for h in GQL_ENDPOINT_HINTS) or resp.status >= 400:
                return
            txt = await resp.text()
            for obj in parse_ndjson_payloads(txt):
                for st in extract_graphql_posts(obj):
                    _merge_post_candidate(items_by_id, scores_by_id, st)
        except Exception:
            pass

    page.on("response", on_response)
    req_observer = _attach_gql_request_observer(page)
    try:
        await page.goto(search_url, wait_until="domcontentloaded", timeout=45000)
        await human_wait(page, 2800)
        await dismiss_facebook_overlays(page)
        health_err = await check_facebook_page_health(page)
        if health_err:
            return [], health_err

        for obj in await _extract_inline_sjs_payloads(page, ("serpResponse", "comet_sections", "permalink_url", '"Story"')):
            for st in extract_graphql_posts(obj):
                _merge_post_candidate(items_by_id, scores_by_id, st)

        prev_total = 0
        stagnant = 0
        for _ in range(max(1, min(int(max_scrolls or 8), 35))):
            try:
                dom_rows = await page.evaluate(_DOM_EXTRACT_POSTS_JS)
                for r in (dom_rows or []):
                    _merge_post_candidate(items_by_id, scores_by_id, r)
            except Exception:
                pass
            if max_items and len(items_by_id) >= max_items:
                break
            if len(items_by_id) <= prev_total:
                stagnant += 1
                if stagnant >= 3:
                    break
            else:
                stagnant = 0
                prev_total = len(items_by_id)
            await _scroll_feed_down(page, distance=1500)
            await human_wait(page, 2200)

        out = list(items_by_id.values())
        if max_items:
            out = out[:max_items]
        return out, ""
    except Exception as exc:
        return list(items_by_id.values()), f"Facebook 关键词搜索失败: {exc!r}"
    finally:
        _detach_gql_request_observer(page, req_observer)
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass


async def fetch_facebook_entities_search(
    mgr: BrowserManager,
    identity: Identity,
    keyword: str,
    *,
    scope: str = "groups",
    public_only: bool = False,
    max_scrolls: int = 6,
    limit: int = 30,
) -> Tuple[List[Dict[str, Any]], str]:
    """搜索 Facebook 群组 / 公共主页 / 用户。"""
    kw = str(keyword or "").strip()
    if not kw:
        return [], "关键词不能为空"
    sc = str(scope or "groups").strip().lower()
    if sc == "pages":
        url = page_find_url(kw)
    elif sc == "people":
        url = people_search_url(kw)
    else:
        url = group_find_url(kw, public_only=public_only)

    ctx = await mgr.context_for(identity)
    page = await ctx.new_page()
    captured: Dict[str, Dict[str, Any]] = {}

    async def on_response(resp):
        try:
            if not any(h in str(resp.url or "") for h in GQL_ENDPOINT_HINTS) or resp.status >= 400:
                return
            txt = await resp.text()
            for obj in parse_ndjson_payloads(txt):
                if sc == "groups":
                    for g in extract_graphql_groups(obj):
                        key = g.get("group_id") or g.get("link")
                        if key and key not in captured:
                            captured[key] = g
                elif sc == "pages":
                    for p in extract_graphql_pages(obj):
                        key = p.get("page_id") or p.get("link")
                        if key and key not in captured:
                            captured[key] = p
        except Exception:
            pass

    page.on("response", on_response)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await human_wait(page, 2600)
        await dismiss_facebook_overlays(page)
        health_err = await check_facebook_page_health(page)
        if health_err:
            return [], health_err

        prev_total = 0
        stagnant = 0
        for _ in range(max(1, min(int(max_scrolls or 6), 20))):
            dom_rows = await page.evaluate(
                """(scope) => {
                    const out = [];
                    const cards = [...document.querySelectorAll('div[role="article"], div[role="feed"] > div')];
                    for (const card of cards) {
                        const a = card.querySelector('a[role="presentation"][href], a[href*="facebook.com/"]');
                        if (!a) continue;
                        const href = (a.href || '').split('?__')[0].replace(/\\/+$/, '');
                        const name = (a.innerText || '').trim().split('\\n')[0].trim();
                        if (!name || name.length > 120) continue;
                        const fullText = (card.innerText || '').replace(/\\s+/g, ' ').trim();
                        if (scope === 'groups' && !href.includes('/groups/')) continue;
                        if (scope !== 'groups' && href.includes('/groups/')) continue;
                        const img = card.querySelector('image, img[src*="scontent"]');
                        const avatar = img ? (img.getAttribute('xlink:href') || img.getAttribute('href') || img.src || '') : '';
                        const mG = href.match(/\\/groups\\/([A-Za-z0-9._-]+)/);
                        const mId = href.match(/[?&]id=(\\d{5,25})/);
                        out.push({
                            id: mG ? mG[1] : (mId ? mId[1] : href),
                            name,
                            link: href,
                            avatar,
                            summary: fullText.slice(0, 200),
                        });
                    }
                    return out;
                }""",
                sc,
            )
            for row in (dom_rows or []):
                key = str(row.get("id") or row.get("link") or "").strip()
                if key and key not in captured:
                    summary = str(row.get("summary") or "")
                    entry: Dict[str, Any] = {
                        "name": row.get("name", ""),
                        "link": row.get("link", ""),
                        "avatar": row.get("avatar", ""),
                        "summary": summary,
                        "members_count": parse_members_count(summary),
                    }
                    if sc == "groups":
                        entry["group_id"] = key
                    elif sc == "pages":
                        entry["page_id"] = key
                    else:
                        entry["uid"] = key
                    captured[key] = entry
            if limit and len(captured) >= limit:
                break
            if len(captured) <= prev_total:
                stagnant += 1
                if stagnant >= 2 and len(captured) > 0:
                    break
            else:
                stagnant = 0
                prev_total = len(captured)
            await _scroll_feed_down(page, distance=1300)
            await human_wait(page, 1800)

        out = list(captured.values())
        return (out[:limit] if limit else out), ""
    except Exception as exc:
        return list(captured.values()), f"Facebook {sc} 搜索失败: {exc!r}"
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


async def fetch_facebook_post_detail(
    mgr: BrowserManager,
    identity: Identity,
    post_url_raw: str,
) -> Tuple[Optional[Dict[str, Any]], str]:
    """在隔离浏览器打开单条 Facebook 帖子 / 视频 / Reels 链接并提取完整结构。"""
    target_url = normalize_facebook_url(post_url_raw)
    ctx = await mgr.context_for(identity)
    page = await ctx.new_page()
    captured_posts: List[Dict[str, Any]] = []

    async def on_response(resp):
        try:
            if not any(h in str(resp.url or "") for h in GQL_ENDPOINT_HINTS) or resp.status >= 400:
                return
            txt = await resp.text()
            for obj in parse_ndjson_payloads(txt):
                captured_posts.extend(extract_graphql_posts(obj))
        except Exception:
            pass

    page.on("response", on_response)
    req_observer = _attach_gql_request_observer(page)
    try:
        await page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
        await human_wait(page, 2800)
        await dismiss_facebook_overlays(page)
        health_err = await check_facebook_page_health(page)
        if health_err:
            return None, health_err

        ref = resolve_facebook_post_ref(target_url, allow_numeric=False)
        all_sjs = await _extract_inline_sjs_payloads(page, (ref.post_id,) if (ref and ref.post_id) else ("comet_sections", "permalink_url", '"Story"', "creation_story"))
        merged_sjs = extract_target_post_from_sjs(
            all_sjs,
            target_url=str(page.url or target_url),
            target_id=(ref.post_id if ref else ""),
        )
        if merged_sjs is not None:
            aw_m = parse_facebook_aweme(merged_sjs)
            if aw_m is not None and (aw_m.medias or aw_m.desc):
                return merged_sjs, ""

        for obj in all_sjs:
            captured_posts.extend(extract_graphql_posts(obj))

        if captured_posts:
            # 优先匹配目标 post_id，其次选首个含媒体的帖子
            if ref and ref.post_id:
                for p in captured_posts:
                    aw = parse_facebook_aweme(p)
                    if aw and ref.post_id in (aw.aweme_id, aw.source_url):
                        return p, ""
            return captured_posts[0], ""

        dom_rows = await page.evaluate(_DOM_EXTRACT_POSTS_JS)
        if dom_rows:
            return dom_rows[0], ""
        return None, "未能从页面提取到 Facebook 帖子详情"
    except Exception as exc:
        return None, f"抓取 Facebook 帖子详情失败: {exc!r}"
    finally:
        _detach_gql_request_observer(page, req_observer)
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass


async def fetch_facebook_comments(
    mgr: BrowserManager,
    identity: Identity,
    post_url_raw: str,
    *,
    max_scrolls: int = 6,
) -> Tuple[List[Dict[str, Any]], str]:
    """抓取 Facebook 帖子 / 视频 / Reels 下的评论列表（GraphQL 拦截 + DOM 双层提取）。"""
    target_url = group_post_url_from_photo(post_url_raw) or normalize_facebook_url(post_url_raw)
    ctx = await mgr.context_for(identity)
    page = await ctx.new_page()
    comments_by_id: Dict[str, Dict[str, Any]] = {}

    async def on_response(resp):
        try:
            if not any(h in str(resp.url or "") for h in GQL_ENDPOINT_HINTS) or resp.status >= 400:
                return
            txt = await resp.text()
            for obj in parse_ndjson_payloads(txt):
                for c in extract_graphql_comments(obj):
                    cid = c.get("comment_id")
                    if cid and cid not in comments_by_id:
                        comments_by_id[cid] = c
        except Exception:
            pass

    page.on("response", on_response)
    req_observer = _attach_gql_request_observer(page)
    try:
        await page.goto(target_url, wait_until="domcontentloaded", timeout=45000)
        await human_wait(page, 2600)
        await dismiss_facebook_overlays(page)
        health_err = await check_facebook_page_health(page)
        if health_err:
            return [], health_err

        for obj in await _extract_inline_sjs_payloads(page, ('"Comment"', "comment_rendering_instance", "preferred_body")):
            for c in extract_graphql_comments(obj):
                cid = c.get("comment_id")
                if cid and cid not in comments_by_id:
                    comments_by_id[cid] = c

        # 若为 Reels / 视频页，先点开评论侧栏（兼容新版 aria-label="Comment, 368 comments"）
        cur_low = str(page.url or "").lower()
        if any(p in cur_low for p in ("/reel/", "/reels/", "/videos/", "/watch")):
            for sel in COMMENT_OPEN_SELECTORS:
                btn = page.locator(sel).first
                try:
                    if await btn.count() and await btn.is_visible():
                        await DEFAULT_HUMAN_ENGINE.click_locator(btn, page=page, timeout=3500)
                        await human_wait(page, 2400)
                        break
                except Exception:
                    continue

        # 尝试点击「查看更多评论」/ 滚动加载评论区（优先在弹窗或右侧评论抽屉内操作）
        prev_total = 0
        stagnant = 0
        for _ in range(max(1, min(int(max_scrolls or 6), 25))):
            dlg_loc = page.locator('div[role="dialog"]')
            search_root = dlg_loc.last if await dlg_loc.count() else page
            # 点击「View more comments / 查看更多评论」
            for more_txt in ("View more comments", "View more replies", "查看更多评论", "檢視更多留言", "Ver más comentarios"):
                more_btn = search_root.locator(f'div[role="button"]:has-text("{more_txt}")').first
                try:
                    if await more_btn.count() and await more_btn.is_visible():
                        await DEFAULT_HUMAN_ENGINE.click_locator(more_btn, page=page, timeout=2500)
                        await human_wait(page, 1800)
                        break
                except Exception:
                    continue

            # DOM 评论节点兜底提取
            dom_comments = await page.evaluate(
                """() => {
                    const out = [];
                    const root = document.querySelector('div[role="dialog"]') || document;
                    const arts = [...root.querySelectorAll('div[role="article"]')];
                    for (const art of arts) {
                        const aria = (art.getAttribute('aria-label') || '').toLowerCase();
                        // 过滤主帖容器，保留评论项（带 comment_id 链接或评论 aria-label）
                        const clink = art.querySelector('a[href*="comment_id="]');
                        if (!clink && !aria.includes('comment') && !aria.includes('评论') && !aria.includes('留言')) continue;
                        let cid = '';
                        if (clink) {
                            const m = (clink.href || '').match(/comment_id=([^&]+)/);
                            if (m) cid = decodeURIComponent(m[1]);
                        }
                        const authorA = art.querySelector('a[role="link"][href]:not([href*="comment_id="])');
                        const authorName = authorA ? (authorA.innerText || '').trim().split('\\n')[0] : '';
                        const authorHref = authorA ? (authorA.href || '') : '';
                        const textNodes = [...art.querySelectorAll('div[dir="auto"]')];
                        const body = textNodes.map(n => (n.innerText || '').trim()).filter(Boolean).join(' ').trim();
                        if (!body) continue;
                        out.push({
                            comment_id: cid,
                            text: body,
                            user_nickname: authorName,
                            user_sec_uid: authorHref,
                            like_count: 0,
                            create_time: 0,
                            reply_to: aria.includes('reply') || aria.includes('回复') ? 'reply' : '',
                        });
                    }
                    return out;
                }"""
            )
            for dc in (dom_comments or []):
                parsed = parse_facebook_comment(dc)
                if parsed and parsed["comment_id"] not in comments_by_id:
                    comments_by_id[parsed["comment_id"]] = parsed

            if len(comments_by_id) <= prev_total:
                stagnant += 1
                if stagnant >= 2:
                    break
            else:
                stagnant = 0
                prev_total = len(comments_by_id)
            # 优先滚动弹窗内或 Reels 右侧独立评论抽屉容器，再兜底滚动主窗口
            try:
                scrolled_inner = await page.evaluate(
                    """() => {
                        const dlgs = [...document.querySelectorAll('div[role="dialog"]')];
                        const root = dlgs.length ? dlgs[dlgs.length - 1] : document;
                        let moved = 0;
                        for (const el of root.querySelectorAll('div')) {
                            const st = getComputedStyle(el);
                            if ((st.overflowY === 'auto' || st.overflowY === 'scroll') && el.scrollHeight > el.clientHeight + 60 && el.clientWidth >= 220) {
                                const prev = el.scrollTop;
                                el.scrollTop = Math.min(el.scrollHeight, el.scrollTop + 900);
                                if (el.scrollTop > prev + 10) moved += 1;
                            }
                        }
                        return moved;
                    }"""
                )
            except Exception:
                scrolled_inner = 0
            if not scrolled_inner:
                await _scroll_feed_down(page, distance=900)
            await human_wait(page, 1600)

        return list(comments_by_id.values()), ""
    except Exception as exc:
        return list(comments_by_id.values()), f"抓取 Facebook 评论失败: {exc!r}"
    finally:
        _detach_gql_request_observer(page, req_observer)
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass


async def fetch_facebook_follows(
    mgr: BrowserManager,
    identity: Identity,
    *,
    direction: str = "following",
    user_ref: str = "",
    max_scrolls: int = 10,
) -> Tuple[List[Dict[str, Any]], str]:
    """抓取 Facebook 好友 / 关注 / 粉丝列表。"""
    d = str(direction or "following").strip().lower()
    if d == "fan":
        nav_url = followers_url(user_ref)
    elif d == "friends":
        nav_url = friends_url(user_ref)
    else:
        nav_url = following_url(user_ref) if user_ref else FRIENDS_ME_URL

    ctx = await mgr.context_for(identity)
    page = await ctx.new_page()
    users: Dict[str, Dict[str, Any]] = {}

    try:
        await page.goto(nav_url, wait_until="domcontentloaded", timeout=45000)
        await human_wait(page, 2500)
        await dismiss_facebook_overlays(page)
        health_err = await check_facebook_page_health(page)
        if health_err:
            return [], health_err

        cookies = await ctx.cookies([FACEBOOK_BASE, "https://web.facebook.com"])
        self_c_user = next((str(c.get("value") or "").strip() for c in cookies if c.get("name") == "c_user" and c.get("value")), "")
        self_ids = {s.lower() for s in (self_c_user, str(user_ref or "").strip()) if s}
        _NON_USER_SLUGS = {
            "stories", "story", "friends", "followers", "following", "groups",
            "pages", "photos", "videos", "posts", "reel", "reels", "watch",
            "events", "help", "settings", "notifications", "messages",
            "marketplace", "gaming", "bookmarks", "memories", "saved", "feeds",
            "fundraisers", "ads", "ad_center", "business", "privacy", "policies",
            "login", "recover", "checkpoint", "composer", "dialog", "sharer",
            "hashtag", "search", "about", "home.php", "profile.php", "l.php",
        }

        prev_total = 0
        stagnant = 0
        for _ in range(max(1, min(int(max_scrolls or 10), 25))):
            rows = await page.evaluate(
                """() => {
                    const out = [];
                    const main = document.querySelector('div[role="main"]') || document.body;
                    for (const a of main.querySelectorAll('a[href*="facebook.com/"]')) {
                        const rawHref = (a.href || '').split('?__')[0].split('&__')[0];
                        // 排除主页头部/子标签/功能按钮链接 (?sk=..., /stories/, /friends 等)
                        if (/[?&]sk=/i.test(rawHref)) continue;
                        if (/\\/(stories|friends|followers|following|groups|pages|photos|videos|posts|reel|reels|watch|events|help|settings|notifications|messages|marketplace|gaming|bookmarks|memories|saved|feeds|hashtag|search|about|privacy|policies|login|recover|checkpoint|sharer|dialog|composer)(?:[/?#]|$)/i.test(rawHref)) continue;
                        const name = (a.innerText || '').trim().split('\\n')[0].trim();
                        if (!name || name.length > 60) continue;
                        if (/^\\d+(?:[.,]\\d+)?\\s*[kmb万]?\\s*(?:friends?|followers?|following|mutual\\s+friends?|位?好友|位?粉丝|位?关注|位共同好友)$/i.test(name)) continue;
                        if (/^(?:add to story|edit profile|see all|view all|添加快拍|编辑主页|查看全部)$/i.test(name)) continue;
                        let uid = '';
                        const mId = rawHref.match(/profile\\.php\\?id=(\\d{4,25})(?:&|$)/);
                        if (mId) uid = mId[1];
                        else {
                            const mU = rawHref.match(/facebook\\.com\\/([A-Za-z0-9._-]+)(?:[/?#]|$)/);
                            if (mU) uid = mU[1];
                        }
                        if (!uid) continue;
                        // 向上遍历最多 5 层卡片容器寻找用户头像（Comet 两栏关系列表图片与姓名常为兄弟列）
                        let avatar = '';
                        let box = a.parentElement;
                        for (let depth = 0; depth < 5 && box && box !== main; depth++) {
                            const imgs = box.querySelectorAll('img[src*="scontent"], svg image[xlink\\\\:href], svg image[href]');
                            if (imgs.length === 1) {
                                const img = imgs[0];
                                avatar = img.getAttribute('xlink:href') || img.getAttribute('href') || img.src || '';
                                break;
                            }
                            if (imgs.length > 1) break;
                            box = box.parentElement;
                        }
                        out.push({ uid, sec_uid: uid, nickname: name, avatar, signature: '' });
                    }
                    return out;
                }"""
            )
            for r in (rows or []):
                uid = str(r.get("uid") or "").strip()
                if not uid or uid.lower() in self_ids or uid.lower() in _NON_USER_SLUGS:
                    continue
                if uid not in users:
                    users[uid] = {
                        "uid": uid,
                        "sec_uid": str(r.get("sec_uid") or uid),
                        "nickname": str(r.get("nickname") or uid),
                        "avatar": str(r.get("avatar") or ""),
                        "signature": "",
                        "is_following": d != "fan",
                        "is_mutual": d == "friends",
                    }
                elif not users[uid].get("avatar") and r.get("avatar"):
                    users[uid]["avatar"] = str(r.get("avatar") or "")
            if len(users) <= prev_total:
                stagnant += 1
                if stagnant >= 2 and len(users) > 0:
                    break
            else:
                stagnant = 0
                prev_total = len(users)
            await _scroll_feed_down(page, distance=1200)
            await human_wait(page, 1600)

        return list(users.values()), ""
    except Exception as exc:
        return list(users.values()), f"抓取 Facebook 关系列表失败: {exc!r}"
    finally:
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass


async def fetch_facebook_dm_conversations(
    mgr: BrowserManager,
    identity: Identity,
    *,
    max_scrolls: int = 6,
) -> Tuple[List[Dict[str, Any]], str]:
    """同步 Facebook Messenger 收件箱会话列表（导航 `/messages/t/`）。"""
    ctx = await mgr.context_for(identity)
    page = await ctx.new_page()
    try:
        await page.goto(MESSAGES_URL, wait_until="domcontentloaded", timeout=45000)
        await human_wait(page, 3000)
        await dismiss_facebook_overlays(page)
        health_err = await check_facebook_page_health(page)
        if health_err:
            return [], health_err

        raw_items: List[Dict[str, Any]] = []
        seen_ids: Set[str] = set()
        for _ in range(max(1, min(int(max_scrolls or 6), 15))):
            rows = await page.evaluate(
                """() => {
                    const out = [];
                    const links = [...document.querySelectorAll('a[href*="/messages/t/"], a[href*="/messages/e2ee/t/"]')];
                    for (const a of links) {
                        const href = a.href || '';
                        const m = href.match(/\\/messages\\/(?:e2ee\\/)?t\\/([^/?#]+)/);
                        if (!m) continue;
                        const tid = m[1];
                        const lines = (a.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
                        const name = lines[0] || tid;
                        const lastText = lines.slice(1).join(' ').slice(0, 200);
                        const img = a.querySelector('img[src], image');
                        const avatar = img ? (img.getAttribute('xlink:href') || img.getAttribute('href') || img.src || '') : '';
                        const unread = !!a.querySelector('[aria-label*="unread" i], [aria-label*="未读"], [aria-label*="未讀"]');
                        out.push({
                            conv_id: tid,
                            peer_uid: tid,
                            peer_sec_uid: tid,
                            peer_nickname: name,
                            peer_avatar: avatar,
                            last_text: lastText,
                            last_time: 0,
                            unread_count: unread ? 1 : 0,
                        });
                    }
                    return out;
                }"""
            )
            before = len(seen_ids)
            for r in (rows or []):
                cid = str(r.get("conv_id") or "").strip()
                if cid and cid not in seen_ids:
                    seen_ids.add(cid)
                    raw_items.append(r)
            if len(seen_ids) == before and len(seen_ids) > 0:
                break
            await human_wait(page, 1000)

        return parse_facebook_dm_conversations(raw_items), ""
    except Exception as exc:
        return [], f"同步 Facebook Messenger 会话失败: {exc!r}"
    finally:
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass


async def fetch_facebook_dm_history(
    mgr: BrowserManager,
    identity: Identity,
    conv_id: str,
    *,
    max_scrolls: int = 5,
) -> Tuple[List[Dict[str, Any]], str]:
    """同步指定 Messenger 会话 (`/messages/t/<conv_id>`) 的消息历史。"""
    cid = str(conv_id or "").strip()
    if not cid:
        return [], "缺少 Messenger 会话 ID"
    ctx = await mgr.context_for(identity)
    page = await ctx.new_page()
    try:
        await page.goto(dm_thread_url(cid), wait_until="domcontentloaded", timeout=45000)
        await human_wait(page, 3000)
        await dismiss_facebook_overlays(page)
        health_err = await check_facebook_page_health(page)
        if health_err:
            return [], health_err

        rows = await page.evaluate(
            """() => {
                const out = [];
                const main = document.querySelector('div[role="main"]') || document.body;
                const rows = [...main.querySelectorAll('div[role="row"], div[data-scope="messages_table"] div[dir="auto"]')];
                const seen = new Set();
                for (const el of rows) {
                    if (el.closest('form') || el.closest('[role="textbox"]')) continue;
                    const textEl = el.getAttribute('role') === 'row' ? el.querySelector('div[dir="auto"]') : el;
                    if (!textEl) continue;
                    const text = (textEl.innerText || '').trim();
                    if (!text || seen.has(text + ':' + out.length)) continue;
                    const rect = textEl.getBoundingClientRect();
                    const mainRect = main.getBoundingClientRect();
                    const mid = mainRect.left + mainRect.width * 0.55;
                    const direction = rect.left > mid ? 'out' : 'in';
                    seen.add(text + ':' + out.length);
                    out.push({
                        text,
                        direction,
                        sender_uid: '',
                        sender_name: '',
                        created_at: 0,
                    });
                }
                return out.slice(-80);
            }"""
        )
        return parse_facebook_dm_messages(rows or [], conv_id=cid), ""
    except Exception as exc:
        return [], f"同步 Facebook Messenger 历史消息失败: {exc!r}"
    finally:
        try:
            if not page.is_closed():
                await page.close()
        except Exception:
            pass
