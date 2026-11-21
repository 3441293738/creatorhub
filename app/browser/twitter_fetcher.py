"""X (Twitter) 网页端隔离浏览器读取适配器（GraphQL/REST 响应拦截 + DOM 兜底）。"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Mapping, Optional, Set, Tuple

from ..platforms.twitter.extract import (
    extract_graphql_comments,
    extract_graphql_tweets,
    extract_graphql_users,
    parse_twitter_dm_inbox,
    parse_twitter_dm_messages,
    parse_twitter_follow_users,
    parse_twitter_self_user,
    parse_twitter_user,
)
from ..platforms.twitter.resolve import (
    clean_twitter_username,
    resolve_twitter_tweet_ref,
    resolve_twitter_user_target,
)
from ..platforms.twitter.selectors import (
    ACCOUNT_LOCKED_URL_PARTS,
    ACCOUNT_SUSPENDED_URL_PARTS,
    AUTH_READY_SELECTORS,
    DM_PIN_PROMPT_SELECTORS,
)
from ..platforms.twitter.urls import (
    GQL_FOLLOW_LIST_HINTS,
    GQL_SEARCH_HINTS,
    GQL_TWEET_DETAIL_HINTS,
    GQL_USER_BY_SCREEN_NAME_HINTS,
    GQL_USER_TWEETS_HINTS,
    HOME_URL,
    MESSAGES_URL,
    REST_DM_CONV_HINTS,
    REST_DM_INBOX_HINTS,
    dm_conversation_url,
    followers_url,
    following_url,
    profile_url,
    tweet_search_url,
    tweet_url,
    user_search_url,
)
from .identity import Identity
from .manager import BrowserManager


def check_twitter_url_risk(url: str) -> str:
    """检查当前页面 URL 是否落入未登录、安全挑战或封禁状态。"""
    low = str(url or "").lower()
    if "/i/flow/login" in low or low.rstrip("/").endswith("/login") or "/login?" in low:
        return "logged_out:X (Twitter) 会话未登录或已过期，请重新登录"
    if any(p in low for p in ACCOUNT_LOCKED_URL_PARTS):
        return "verification_required:X 账号触发安全锁定或挑战验证(/account/access)"
    if any(p in low for p in ACCOUNT_SUSPENDED_URL_PARTS):
        return "verification_required:X 账号已被冻结或停用"
    return ""


async def _extract_dom_tweets(page, fallback_screen_name: str = "") -> List[dict]:
    """当 GraphQL 响应因缓存未触发时，从页面 `article[data-testid='tweet']` 提取兜底推文节点。"""
    try:
        rows = await page.evaluate(
            r"""(fallbackUser) => {
                const out = [];
                const seen = new Set();
                const articles = document.querySelectorAll('article[data-testid="tweet"]');
                for (const art of articles) {
                    let statusHref = '';
                    let screenName = fallbackUser || '';
                    let tweetId = '';
                    for (const a of art.querySelectorAll('a[href*="/status/"]')) {
                        const h = a.getAttribute('href') || '';
                        const m = h.match(/^\/([A-Za-z0-9_]{1,15})\/status\/(\d{5,25})/);
                        if (m) {
                            screenName = m[1];
                            tweetId = m[2];
                            statusHref = h;
                            break;
                        }
                    }
                    if (!tweetId || seen.has(tweetId)) continue;
                    seen.add(tweetId);
                    const textEl = art.querySelector('[data-testid="tweetText"]');
                    const timeEl = art.querySelector('time[datetime]');
                    const nameEl = art.querySelector('[data-testid="User-Name"]');
                    let authorName = screenName;
                    if (nameEl && nameEl.innerText) {
                        const firstLine = nameEl.innerText.split('\n')[0].trim();
                        if (firstLine) authorName = firstLine;
                    }
                    const avatarImg = art.querySelector('[data-testid*="UserAvatar"] img');
                    const domMedias = [];
                    let coverUrl = '';
                    for (const v of art.querySelectorAll('video')) {
                        const poster = v.getAttribute('poster') || '';
                        if (poster && !coverUrl) coverUrl = poster;
                        const src = v.currentSrc || v.getAttribute('src') || '';
                        if (src && !src.startsWith('blob:')) {
                            domMedias.push({ url: src, kind: 'video' });
                        }
                    }
                    for (const img of art.querySelectorAll('[data-testid="tweetPhoto"] img, img[src*="pbs.twimg.com/media/"]')) {
                        const src = img.currentSrc || img.getAttribute('src') || '';
                        if (src && src.includes('twimg.com')) {
                            if (!coverUrl) coverUrl = src;
                            domMedias.push({ url: src, kind: 'image' });
                        }
                    }
                    const readMetric = (testid) => {
                        const btn = art.querySelector(`button[data-testid="${testid}"]`);
                        if (!btn) return '0';
                        const aria = btn.getAttribute('aria-label') || '';
                        const m = aria.match(/(\d[\d,.]*)/);
                        if (m) return m[1];
                        return (btn.innerText || '0').trim();
                    };
                    out.push({
                        rest_id: tweetId,
                        screen_name: screenName,
                        author_unique_id: screenName,
                        author_name: authorName,
                        avatar: avatarImg ? (avatarImg.currentSrc || avatarImg.src || '') : '',
                        text: textEl ? textEl.innerText : '',
                        created_at: timeEl ? (timeEl.getAttribute('datetime') || '') : '',
                        favorite_count: readMetric('like') || readMetric('unlike'),
                        reply_count: readMetric('reply'),
                        retweet_count: readMetric('retweet') || readMetric('unretweet'),
                        cover_url: coverUrl,
                        dom_medias: domMedias,
                        source_url: statusHref ? ('https://x.com' + statusHref) : ''
                    });
                }
                return out;
            }""",
            fallback_screen_name or "",
        )
        return [r for r in (rows or []) if isinstance(r, dict) and r.get("rest_id")]
    except Exception:
        return []


async def _resolve_logged_in_handle(page) -> Tuple[str, str, str]:
    """从左侧导航栏提取当前已登录账号 `(screen_name, display_name, avatar)`。"""
    try:
        info = await page.evaluate(
            r"""() => {
                let handle = '';
                let name = '';
                let avatar = '';
                const profLink = document.querySelector('a[data-testid="AppTabBar_Profile_Link"]');
                if (profLink) {
                    const h = (profLink.getAttribute('href') || '').replace(/^\/+|\/+$/g, '');
                    if (h && !h.includes('/')) handle = h;
                }
                const sw = document.querySelector('[data-testid="SideNav_AccountSwitcher_Button"]');
                if (sw) {
                    const lines = (sw.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
                    for (const ln of lines) {
                        if (ln.startsWith('@') && !handle) handle = ln.slice(1);
                        else if (!ln.startsWith('@') && !name) name = ln;
                    }
                    const img = sw.querySelector('img');
                    if (img) avatar = img.currentSrc || img.src || '';
                }
                return { handle, name, avatar };
            }"""
        )
        if isinstance(info, dict):
            return (
                clean_twitter_username(str(info.get("handle") or "")),
                str(info.get("name") or "").strip(),
                str(info.get("avatar") or "").strip(),
            )
    except Exception:
        pass
    return "", "", ""


async def fetch_twitter_self_profile(
    mgr: BrowserManager,
    identity: Identity,
) -> Tuple[dict, str]:
    """在账号隔离浏览器上下文内校验登录态并采集当前 X 账号资料与粉丝/作品计数。"""
    captured_users: List[dict] = []
    page = await mgr.new_page(identity, block_media=True)

    async def on_response(resp):
        u = str(resp.url or "")
        if not any(h in u for h in GQL_USER_BY_SCREEN_NAME_HINTS):
            return
        try:
            payload = await resp.json()
        except Exception:
            return
        if not isinstance(payload, Mapping):
            return
        # Viewer 结构: data.viewer.user_results.result
        data = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
        if isinstance(data.get("viewer"), Mapping):
            p = parse_twitter_self_user(data["viewer"])
            if p.get("uid") or p.get("unique_id"):
                captured_users.append(p)
                return
        p = parse_twitter_self_user(payload)
        if p.get("uid") or p.get("unique_id"):
            captured_users.append(p)

    page.on("response", on_response)
    try:
        await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=40000)
        await page.wait_for_timeout(2200)
        risk_err = check_twitter_url_risk(page.url)
        if risk_err:
            return {}, risk_err

        # 等待侧栏登录元素渲染
        authed_dom = False
        for sel in AUTH_READY_SELECTORS:
            loc = page.locator(sel).first
            try:
                if await loc.count() and await loc.is_visible():
                    authed_dom = True
                    break
            except Exception:
                continue

        if not authed_dom:
            cookies = await page.context.cookies("https://x.com")
            if not any(c.get("name") == "auth_token" and c.get("value") for c in cookies):
                return {}, "logged_out:X 浏览器会话缺少 auth_token，请重新登录"

        handle, dom_name, dom_avatar = await _resolve_logged_in_handle(page)
        if handle:
            # 打开个人主页触发 UserByScreenName 获取精确粉丝数与推文数
            captured_users.clear()
            await page.goto(profile_url(handle), wait_until="domcontentloaded", timeout=35000)
            await page.wait_for_timeout(2000)
            for u in captured_users:
                if str(u.get("unique_id") or "").lower() == handle.lower():
                    return u, ""

        if captured_users:
            return captured_users[-1], ""

        if handle:
            return {
                "uid": "",
                "sec_uid": handle,
                "unique_id": handle,
                "douyin_id": handle,
                "nickname": dom_name or handle,
                "avatar": dom_avatar,
                "follower_count": 0,
                "following_count": 0,
                "aweme_count": 0,
                "total_favorited": 0,
            }, ""

        return {}, "未能从 X 网页端提取当前账号资料"
    except Exception as exc:
        return {}, f"browser:{type(exc).__name__}:{exc}"
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


async def fetch_twitter_user_tweets(
    mgr: BrowserManager,
    identity: Identity,
    user_ref: str,
    known_ids: Optional[Set[str]] = None,
    *,
    max_scrolls: int = 6,
    block_media: bool = True,
) -> Tuple[List[dict], dict, str]:
    """打开创作者主页 `https://x.com/{screen_name}` 拦截 `UserTweets` 与作者资料。"""
    target = resolve_twitter_user_target(user_ref) or str(user_ref or "").strip().lstrip("@")
    if not target:
        return [], {}, "missing_user_target"

    known = known_ids or set()
    collected: Dict[str, dict] = {}
    author_info: Dict[str, Any] = {}
    page = await mgr.new_page(identity, block_media=block_media)

    async def on_response(resp):
        u = str(resp.url or "")
        if not any(h in u for h in (*GQL_USER_TWEETS_HINTS, *GQL_USER_BY_SCREEN_NAME_HINTS)):
            return
        try:
            payload = await resp.json()
        except Exception:
            return
        if any(h in u for h in GQL_USER_BY_SCREEN_NAME_HINTS):
            u_parsed = parse_twitter_user(payload)
            if u_parsed.get("uid") or u_parsed.get("unique_id"):
                author_info.update(u_parsed)
        if any(h in u for h in GQL_USER_TWEETS_HINTS):
            tweets, _cursor = extract_graphql_tweets(payload)
            for t in tweets:
                tid = str(t.get("rest_id") or "")
                if tid:
                    collected.setdefault(tid, t)

    page.on("response", on_response)
    try:
        await page.goto(profile_url(target), wait_until="domcontentloaded", timeout=40000)
        await page.wait_for_timeout(2400)
        risk_err = check_twitter_url_risk(page.url)
        if risk_err:
            return [], author_info, risk_err

        for _ in range(max(1, int(max_scrolls))):
            if known and len(collected) >= 5 and all(tid in known for tid in list(collected.keys())[-5:]):
                break
            await page.evaluate("() => window.scrollBy(0, 1400)")
            await page.wait_for_timeout(1300)

        if not collected:
            dom_rows = await _extract_dom_tweets(page, fallback_screen_name=(target if not target.isdigit() else ""))
            for r in dom_rows:
                collected.setdefault(str(r["rest_id"]), r)

        return list(collected.values()), author_info, ("" if collected else "empty")
    except Exception as exc:
        return list(collected.values()), author_info, f"browser:{type(exc).__name__}:{exc}"
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


async def fetch_twitter_search(
    mgr: BrowserManager,
    identity: Identity,
    keyword: str,
    *,
    latest: bool = False,
    content_type: str = "all",
    max_scrolls: int = 5,
    block_media: bool = True,
) -> Tuple[List[dict], str]:
    """打开 `https://x.com/search?q=...` 拦截 `SearchTimeline` 推文结果。"""
    kw = str(keyword or "").strip()
    if not kw:
        return [], "搜索关键词不能为空"

    target_url = tweet_search_url(kw, latest=latest, content_type=content_type)
    collected: Dict[str, dict] = {}
    search_statuses: List[int] = []
    page = await mgr.new_page(identity, block_media=block_media)

    async def on_response(resp):
        u = str(resp.url or "")
        if not any(h in u for h in GQL_SEARCH_HINTS):
            return
        search_statuses.append(int(resp.status))
        try:
            payload = await resp.json()
        except Exception:
            return
        tweets, _cursor = extract_graphql_tweets(payload)
        for t in tweets:
            tid = str(t.get("rest_id") or "")
            if tid:
                collected.setdefault(tid, t)

    page.on("response", on_response)
    try:
        await page.goto(target_url, wait_until="domcontentloaded", timeout=40000)
        await page.wait_for_timeout(2500)
        risk_err = check_twitter_url_risk(page.url)
        if risk_err:
            return [], risk_err

        # 若首包 SearchTimeline 即返回 404 空包（部分账号/出口被 X 限制主搜索页），无需盲滚浪费时间，直接走同源协议回退
        if not (search_statuses and all(s == 404 for s in search_statuses) and not collected):
            stagnant = 0
            for _ in range(max(1, int(max_scrolls))):
                before = len(collected)
                await page.evaluate("() => window.scrollBy(0, 1500)")
                await page.wait_for_timeout(1400)
                if len(collected) == before:
                    stagnant += 1
                    if stagnant >= 2:
                        break
                else:
                    stagnant = 0

        if not collected:
            for r in await _extract_dom_tweets(page):
                collected.setdefault(str(r["rest_id"]), r)

        if not collected:
            # 复用当前浏览器上下文最新 Cookie 走高可用同源 GraphQL 搜索通道兜底
            try:
                from ..platforms.twitter.client import TwitterClient
                live_cks = await page.context.cookies(["https://x.com", "https://twitter.com"])
                ck_str = "; ".join(
                    f"{c.get('name')}={c.get('value')}"
                    for c in (live_cks or [])
                    if c.get("name") and c.get("value") is not None
                )
                if ck_str:
                    fb_cli = TwitterClient(
                        ck_str,
                        getattr(identity, "ua", "") or "",
                        proxy=getattr(identity, "proxy", "") or "",
                    )
                    async with fb_cli.session_scope():
                        fb_rows, fb_err = await fb_cli.search_posts(
                            kw,
                            max_pages=max(1, min(10, int(max_scrolls))),
                            latest=latest,
                            content_type=content_type,
                        )
                    if fb_rows:
                        return fb_rows, ""
                    if fb_err and fb_err != "empty":
                        return [], fb_err
            except Exception:
                pass

        return list(collected.values()), ("" if collected else "未搜索到匹配推文")
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


async def fetch_twitter_tweet_detail(
    mgr: BrowserManager,
    identity: Identity,
    tweet_or_url: str,
    *,
    block_media: bool = True,
) -> Tuple[Optional[dict], str]:
    """打开单条推文详情页拦截 `TweetDetail` 节点（含完整视频 MP4 变体与高清图直链）。"""
    ref = resolve_twitter_tweet_ref(tweet_or_url)
    tid = ref.tweet_id if ref else str(tweet_or_url or "").strip()
    if not tid:
        return None, "missing_tweet_id"
    url = ref.url if ref else tweet_url(tid)
    found: Dict[str, dict] = {}
    page = await mgr.new_page(identity, block_media=block_media)

    async def on_response(resp):
        u = str(resp.url or "")
        if not any(h in u for h in GQL_TWEET_DETAIL_HINTS):
            return
        try:
            payload = await resp.json()
        except Exception:
            return
        tweets, _ = extract_graphql_tweets(payload)
        for t in tweets:
            rid = str(t.get("rest_id") or "")
            if rid:
                found[rid] = t

    page.on("response", on_response)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=35000)
        await page.wait_for_timeout(2300)
        risk_err = check_twitter_url_risk(page.url)
        if risk_err:
            return None, risk_err
        if tid in found:
            return found[tid], ""
        if found:
            return next(iter(found.values())), ""
        dom_rows = await _extract_dom_tweets(page, fallback_screen_name=(ref.screen_name if ref else ""))
        for r in dom_rows:
            if str(r.get("rest_id")) == tid:
                return r, ""
        return (dom_rows[0], "") if dom_rows else (None, "empty")
    except Exception as exc:
        return None, f"browser:{type(exc).__name__}:{exc}"
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


async def fetch_twitter_comments(
    mgr: BrowserManager,
    identity: Identity,
    tweet_or_url: str,
    max_count: int = 60,
    *,
    include_replies: bool = True,
    block_media: bool = True,
) -> Tuple[List[dict], str]:
    """打开推文详情页拦截 `TweetDetail` 评论与回复流。"""
    ref = resolve_twitter_tweet_ref(tweet_or_url)
    tid = ref.tweet_id if ref else str(tweet_or_url or "").strip()
    if not tid:
        return [], "missing_tweet_id"
    url = ref.url if ref else tweet_url(tid)
    collected: Dict[str, dict] = {}
    page = await mgr.new_page(identity, block_media=block_media)

    async def on_response(resp):
        u = str(resp.url or "")
        if not any(h in u for h in GQL_TWEET_DETAIL_HINTS):
            return
        try:
            payload = await resp.json()
        except Exception:
            return
        comments, _ = extract_graphql_comments(
            payload, target_tweet_id=tid, include_replies=include_replies
        )
        for c in comments:
            cid = str(c.get("comment_id") or "")
            if cid:
                collected.setdefault(cid, c)

    page.on("response", on_response)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=35000)
        await page.wait_for_timeout(2400)
        risk_err = check_twitter_url_risk(page.url)
        if risk_err:
            return [], risk_err

        scroll_steps = max(2, min(10, (max_count // 15) + 2))
        for _ in range(scroll_steps):
            if len(collected) >= max_count:
                break
            await page.evaluate("() => window.scrollBy(0, 1100)")
            await page.wait_for_timeout(1100)

        if not collected:
            dom_rows = await _extract_dom_tweets(page)
            for r in dom_rows:
                rid = str(r.get("rest_id") or "")
                if rid and rid != tid:
                    collected.setdefault(rid, {
                        "comment_id": rid,
                        "text": str(r.get("text") or ""),
                        "user_nickname": str(r.get("author_name") or r.get("screen_name") or rid),
                        "user_sec_uid": str(r.get("screen_name") or ""),
                        "ip_label": "",
                        "like_count": 0,
                        "create_time": 0,
                        "reply_to": "",
                    })

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


async def fetch_twitter_users(
    mgr: BrowserManager,
    identity: Identity,
    keyword: str,
    *,
    max_results: int = 50,
    block_media: bool = True,
) -> Tuple[List[dict], str]:
    """打开用户搜索页 `&f=user` 拦截匹配的用户列表。"""
    kw = str(keyword or "").strip()
    if not kw:
        return [], "请输入搜索关键词"
    collected: Dict[str, dict] = {}
    page = await mgr.new_page(identity, block_media=block_media)

    async def on_response(resp):
        u = str(resp.url or "")
        if not any(h in u for h in GQL_SEARCH_HINTS):
            return
        try:
            payload = await resp.json()
        except Exception:
            return
        users, _ = extract_graphql_users(payload)
        for usr in users:
            key = str(usr.get("uid") or usr.get("unique_id") or "")
            if key:
                collected.setdefault(key, usr)

    page.on("response", on_response)
    try:
        await page.goto(user_search_url(kw), wait_until="domcontentloaded", timeout=35000)
        await page.wait_for_timeout(2400)
        risk_err = check_twitter_url_risk(page.url)
        if risk_err:
            return [], risk_err

        for _ in range(max(1, min(8, (max_results // 15) + 1))):
            if len(collected) >= max_results:
                break
            await page.evaluate("() => window.scrollBy(0, 1200)")
            await page.wait_for_timeout(1100)

        return list(collected.values())[:max_results], ("" if collected else "empty")
    except Exception as exc:
        return list(collected.values())[:max_results], f"browser:{type(exc).__name__}:{exc}"
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


async def fetch_twitter_follows(
    mgr: BrowserManager,
    identity: Identity,
    user_ref: str,
    direction: str,
    *,
    max_pages: int = 12,
) -> Tuple[List[dict], str]:
    """抓取当前账号或指定用户的关注列表 (`following`) / 粉丝列表 (`followers` / `fan`)。"""
    is_following_dir = str(direction or "").lower() == "following"
    collected: Dict[str, dict] = {}
    page = await mgr.new_page(identity, block_media=True)

    async def on_response(resp):
        u = str(resp.url or "")
        if not any(h in u for h in GQL_FOLLOW_LIST_HINTS):
            return
        try:
            payload = await resp.json()
        except Exception:
            return
        rows, _ = parse_twitter_follow_users(payload, default_following=is_following_dir)
        for r in rows:
            key = str(r.get("uid") or r.get("sec_uid") or "")
            if key:
                collected.setdefault(key, r)

    page.on("response", on_response)
    try:
        handle = clean_twitter_username(user_ref)
        if not handle:
            await page.goto(HOME_URL, wait_until="domcontentloaded", timeout=35000)
            await page.wait_for_timeout(1800)
            risk_err = check_twitter_url_risk(page.url)
            if risk_err:
                return [], risk_err
            handle, _, _ = await _resolve_logged_in_handle(page)
        if not handle:
            return [], "无法确认当前 X 账号 @username，请先刷新账号信息"

        target_url = following_url(handle) if is_following_dir else followers_url(handle)
        await page.goto(target_url, wait_until="domcontentloaded", timeout=35000)
        await page.wait_for_timeout(2400)
        risk_err = check_twitter_url_risk(page.url)
        if risk_err:
            return [], risk_err

        stagnant = 0
        for _ in range(max(1, int(max_pages))):
            before = len(collected)
            await page.evaluate("() => window.scrollBy(0, 1300)")
            await page.wait_for_timeout(1200)
            if len(collected) == before:
                stagnant += 1
                if stagnant >= 3:
                    break
            else:
                stagnant = 0

        if not collected:
            # DOM UserCell 兜底
            dom_users = await page.evaluate(
                r"""(defaultFollowing) => {
                    const out = [];
                    const seen = new Set();
                    for (const cell of document.querySelectorAll('[data-testid="UserCell"]')) {
                        const link = cell.querySelector('a[href^="/"]');
                        if (!link) continue;
                        const handle = (link.getAttribute('href') || '').replace(/^\/+|\/+$/g, '');
                        if (!handle || handle.includes('/') || seen.has(handle)) continue;
                        seen.add(handle);
                        const lines = (cell.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
                        const img = cell.querySelector('img');
                        const hasUnfollow = Boolean(cell.querySelector('button[data-testid$="-unfollow"]'));
                        out.push({
                            uid: handle,
                            sec_uid: handle,
                            nickname: lines[0] || handle,
                            avatar: img ? (img.currentSrc || img.src || '') : '',
                            signature: lines.length > 2 ? lines.slice(2).join(' ').slice(0, 120) : '',
                            is_following: hasUnfollow || Boolean(defaultFollowing),
                            is_mutual: false
                        });
                    }
                    return out;
                }""",
                is_following_dir,
            )
            for r in (dom_users or []):
                if isinstance(r, dict) and r.get("sec_uid"):
                    collected.setdefault(str(r["sec_uid"]), r)

        return list(collected.values()), ""
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


async def fetch_twitter_dm_conversations(
    mgr: BrowserManager,
    identity: Identity,
    *,
    self_uid: str = "",
) -> Tuple[List[dict], str]:
    """打开 `https://x.com/messages` 拦截私信会话列表（自动识别端到端加密 PIN 提示不卡死）。"""
    convs: Dict[str, dict] = {}
    page = await mgr.new_page(identity, block_media=True)

    async def on_response(resp):
        u = str(resp.url or "")
        if not any(h in u for h in REST_DM_INBOX_HINTS):
            return
        try:
            payload = await resp.json()
        except Exception:
            return
        for row in parse_twitter_dm_inbox(payload, self_uid=self_uid):
            cid = str(row.get("conv_id") or "")
            if cid:
                convs[cid] = row

    page.on("response", on_response)
    try:
        await page.goto(MESSAGES_URL, wait_until="domcontentloaded", timeout=40000)
        await page.wait_for_timeout(2800)
        risk_err = check_twitter_url_risk(page.url)
        if risk_err:
            return [], risk_err

        # 若账号开启了加密私信四位 PIN 验证，礼貌跳过弹窗或提示用户在打开的浏览器内输入一次 PIN
        for pin_sel in DM_PIN_PROMPT_SELECTORS:
            loc = page.locator(pin_sel).first
            try:
                if await loc.count() and await loc.is_visible():
                    close_btn = page.locator('button[aria-label="Close" i], [data-testid="app-bar-close"]').first
                    if await close_btn.count() and await close_btn.is_visible():
                        await close_btn.click(timeout=1500)
                        await page.wait_for_timeout(1000)
                    elif not convs:
                        return [], "X 加密私信要求输入安全 PIN 码，请点击「打开浏览器」完成一次 PIN 解锁后再同步"
                    break
            except Exception:
                continue

        if not convs:
            dom_convs = await page.evaluate(
                r"""() => {
                    const out = [];
                    const seen = new Set();
                    for (const el of document.querySelectorAll('[data-testid="conversation"]')) {
                        const a = el.querySelector('a[href*="/messages/"]') || el.closest('a[href*="/messages/"]');
                        let cid = '';
                        if (a) {
                            const m = (a.getAttribute('href') || '').match(/\/messages\/([^/?#]+)/);
                            if (m) cid = m[1];
                        }
                        const lines = (el.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
                        const handleLine = lines.find(s => s.startsWith('@')) || '';
                        const handle = handleLine ? handleLine.slice(1) : '';
                        if (!cid) cid = handle || lines[0] || '';
                        if (!cid || seen.has(cid)) continue;
                        seen.add(cid);
                        const img = el.querySelector('img');
                        out.push({
                            conv_id: cid,
                            peer_uid: cid.includes('-') ? cid.split('-')[0] : '',
                            peer_sec_uid: handle || cid,
                            peer_nickname: lines[0] || handle || cid,
                            peer_avatar: img ? (img.currentSrc || img.src || '') : '',
                            last_text: lines.length > 1 ? lines[lines.length - 1] : '',
                            last_time: 0,
                            unread_count: 0
                        });
                    }
                    return out;
                }"""
            )
            for row in (dom_convs or []):
                if isinstance(row, dict) and row.get("conv_id"):
                    convs.setdefault(str(row["conv_id"]), row)

        return list(convs.values()), ""
    except Exception as exc:
        return list(convs.values()), f"browser:{type(exc).__name__}:{exc}"
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


async def fetch_twitter_dm_history(
    mgr: BrowserManager,
    identity: Identity,
    conv_id: str,
    *,
    self_uid: str = "",
    max_msgs: int = 60,
) -> Tuple[List[dict], str]:
    """打开指定私信会话 `https://x.com/messages/{conv_id}` 提取历史消息。"""
    cid = str(conv_id or "").strip()
    if not cid:
        return [], "missing_conv_id"
    msgs: Dict[str, dict] = {}
    page = await mgr.new_page(identity, block_media=True)

    async def on_response(resp):
        u = str(resp.url or "")
        if not any(h in u for h in REST_DM_CONV_HINTS):
            return
        try:
            payload = await resp.json()
        except Exception:
            return
        for m in parse_twitter_dm_messages(payload, conv_id=cid, self_uid=self_uid):
            mid = str(m.get("msg_id") or "")
            if mid:
                msgs[mid] = m

    page.on("response", on_response)
    try:
        await page.goto(dm_conversation_url(cid), wait_until="domcontentloaded", timeout=40000)
        await page.wait_for_timeout(2600)
        risk_err = check_twitter_url_risk(page.url)
        if risk_err:
            return [], risk_err

        if not msgs:
            dom_msgs = await page.evaluate(
                r"""() => {
                    const out = [];
                    const entries = document.querySelectorAll('[data-testid="messageEntry"]');
                    entries.forEach((el, idx) => {
                        const txt = (el.innerText || '').trim();
                        if (!txt) return;
                        out.push({
                            msg_id: 'dom_' + idx + '_' + txt.slice(0, 12),
                            server_msg_id: '',
                            sender_uid: '',
                            direction: 'in',
                            msg_type: 'text',
                            text: txt,
                            create_time: 0
                        });
                    });
                    return out;
                }"""
            )
            for idx, m in enumerate(dom_msgs or []):
                if isinstance(m, dict):
                    msgs[str(m.get("msg_id") or idx)] = m

        ordered = sorted(msgs.values(), key=lambda x: int(x.get("create_time") or 0))
        return ordered[-max_msgs:], ""
    except Exception as exc:
        return list(msgs.values())[-max_msgs:], f"browser:{type(exc).__name__}:{exc}"
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
