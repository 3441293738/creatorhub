"""多平台关注与粉丝列表浏览器拦截抓取与滚动加载。"""
from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple
import asyncio
import json
import re

import inspect

from app.browser.hub_common import _bili_cookie_str, _console_safe, _harvest_user_lists, _ig_cookie_str, _norm_follow_user, _self_profile_link
from app.browser.ks_fetcher import fetch_ks_self_profile
from app.browser.manager import BrowserManager
from app.platforms.kuaishou import parse_self_user as parse_ks_self_user


# 各平台「关注/粉丝」页:打开 URL + 打开对应列表的点击候选(CSS 选择器 或 text=文案)。
# 关键:关注与粉丝要点不同入口,否则两边抓到同一份(改版时改 open 候选)。
_FOLLOW_NAV = {
    "douyin": {
        # ⚠️ 顶栏有「关注」feed 导航(<a href="/follow?from_nav=1">,带未读角标),会和主页
        # 统计项撞文本 —— 'text=关注' 的 .first 常点中它。dyjs: 锚定确定存在的
        # [data-e2e="user-info-fans"],反查统计容器,只在容器内点,天然避开顶栏。
        "url": "https://www.douyin.com/user/self",
        "open": {
            "following": ['dyjs:关注', '[data-e2e="user-info-follow"]',
                          '[data-e2e="user-following"]'],
            "fan":       ['dyjs:粉丝', '[data-e2e="user-info-fans"]',
                          '[data-e2e="user-fans"]'],
        },
    },
    "xhs": {
        # 在 main/profile 统计区内按可见文案点击，避免顶栏「关注」feed 标签。
        "url": "https://www.xiaohongshu.com/user/profile/{uid}",
        "open": {"following": ['xhs:关注'], "fan": ['xhs:粉丝']},
    },
    "kuaishou": {
        "url": "https://www.kuaishou.com/profile/{uid}",
        "open": {"following": ['ksstat:关注'], "fan": ['ksstat:粉丝']},
    },
}


# 抖音:主页统计区里「关注」没有 data-e2e(只有编译 hash class),而「粉丝/获赞」有。
# 所以拿 user-info-fans 当锚点:向上找到同时含「关注」「粉丝」的最小容器(= 统计区),
# 再在容器内点目标项。顶栏的「关注」导航不在这个容器里,天然被排除。
_DOUYIN_OPEN_STAT_JS = """(label) => {
  const anchor = document.querySelector('[data-e2e="user-info-fans"]');
  if (!anchor) return '';
  // 向上找最小的、同时包含「关注」和「粉丝」的祖先 —— 那就是统计区
  let box = anchor;
  for (let d = 0; d < 6 && box; d++, box = box.parentElement) {
    const t = box.textContent || '';
    if (t.includes('关注') && t.includes('粉丝')) break;
  }
  if (!box) return '';
  // 容器内找目标项:文本形如「关注22」/「22关注」
  const re = new RegExp('^(' + label + '\\\\s*[\\\\d.万亿]*|[\\\\d.万亿]+\\\\s*' + label + ')$');
  const hits = [...box.querySelectorAll('div,span,a')]
    .filter(e => re.test((e.textContent || '').trim()))
    .filter(e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; });
  if (!hits.length) return '';
  // 「关注」这个纯文本标签也会匹配(数字部分可为空),它只是 label,handler 在带数字的
  // 统计项上。优先带数字的最内层;真没有数字(计数为 0 被隐藏)才退回最内层。
  const withNum = hits.filter(e => /\\d/.test(e.textContent || ''));
  const pool = withNum.length ? withNum : hits;
  const el = pool[pool.length - 1];          // 最内层
  el.click();
  return (el.tagName + ':' + (el.textContent || '').trim()).slice(0, 40);
}"""


async def _click_xhs_profile_stat(mgr: BrowserManager, page, label: str) -> bool:
    """在主页内容区按语义定位统计项，通过 Patchright 可见点击打开列表。"""
    scored = []
    # 仅从正文/主页区域搜索，避免命中侧栏的「关注」信息流入口。
    for selector in ("main", '[class*="profile"]'):
        try:
            root = page.locator(selector).first
            if not await root.count() or not await root.is_visible():
                continue
            labels = root.get_by_text(label, exact=True)
            for index in range(min(await labels.count(), 12)):
                item = labels.nth(index)
                if not await item.is_visible():
                    continue
                parent = item.locator("..")
                nearby = (await parent.inner_text()).strip()
                # 统计项通常是「12 关注」；纯「关注」更可能是导航或装饰文本。
                score = 2 if re.search(r"\d", nearby) else 1
                target = parent if score == 2 else item
                scored.append((score, -index, target))
        except Exception:
            continue
        if scored:
            break
    if not scored:
        return False
    target = max(scored, key=lambda row: (row[0], row[1]))[2]
    await mgr.xhs_interaction.click_visible(target)
    return True


async def _click_ks_profile_stat(page, label: str) -> bool:
    """Click the owner profile counter, never the sidebar follow-feed link."""
    counters = page.locator(".profile-top .count span.isOwner")
    for index in range(min(await counters.count(), 8)):
        item = counters.nth(index)
        try:
            if not await item.is_visible():
                continue
            text = re.sub(r"\s+", "", await item.inner_text())
            if label not in text:
                continue
            await item.click(timeout=4000)
            return True
        except Exception:
            continue
    return False


# 小红书关注/粉丝列表:数据渲染在弹层 DOM 里、不发独立 XHR(实测 api_seen 无列表接口),
# 故点开后从弹层 DOM 抽用户。弹层里用户行可能是 /user/profile 锚点,也可能是 div(带 onclick)。
# 返回 {modal, users, dbg}。dbg 在抽不到时打进日志,据真实结构写精确选择器。
_XHS_SCRAPE_DRAWER_JS = """(selfUid) => {
  const vis = el => { const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 140 && r.height > 140 && s.display !== 'none' && s.visibility !== 'hidden'; };
  // 候选浮层:对话框类容器,按「含 /user/profile 链接数」再「img 数」打分选最像的
  let cands = [...document.querySelectorAll(
    '[role="dialog"],[class*="modal"],[class*="dialog"],[class*="drawer"],'
    + '[class*="popover"],[class*="user-list"]')].filter(vis);
  const score = el => ({ el,
    links: el.querySelectorAll('a[href*="/user/profile/"]').length,
    imgs: el.querySelectorAll('img').length });
  const scored = cands.map(score).sort((a, b) => (b.links - a.links) || (b.imgs - a.imgs));
  const best = scored[0] || null;
  const root = best && best.el ? best.el : document.body;

  const seen = new Set(), users = [];
  const pushRow = (uid, name, avatar) => {
    if (!uid || uid === selfUid || seen.has(uid)) return;
    users.push({ uid, nickname: (name || '').trim().slice(0, 40), avatar: avatar || '' });
    seen.add(uid);
  };
  // 1) 锚点式用户行
  root.querySelectorAll('a[href*="/user/profile/"]').forEach(a => {
    const m = a.href.match(/\\/user\\/profile\\/([0-9a-zA-Z]+)/); if (!m) return;
    const img = a.querySelector('img') || (a.parentElement && a.parentElement.querySelector('img'));
    let name = (a.textContent || '').trim() || (img && img.alt) || '';
    if (!name && a.parentElement) name = a.parentElement.textContent.trim();
    pushRow(m[1], name, img ? img.src : '');
  });
  // 2) 退化:div 行(有头像 img + 昵称文本,通过 data-* 或 onclick 里的 userid 兜底找不到时略过)
  if (!users.length && best) {
    root.querySelectorAll('img').forEach(img => {
      const row = img.closest('li,div');
      if (!row) return;
      const a = row.querySelector('a[href*="/user/profile/"]');
      const m = a && a.href.match(/\\/user\\/profile\\/([0-9a-zA-Z]+)/);
      const txt = (row.innerText || '').replace(/\\s+/g, ' ').trim();
      if (m) pushRow(m[1], txt, img.src);
    });
  }
  const pageLinks = document.querySelectorAll('a[href*="/user/profile/"]').length;
  const dbg = {
    pageLinks,
    cls: best && best.el ? (best.el.className || '').toString().slice(0, 90) : '',
    links: best ? best.links : 0, imgs: best ? best.imgs : 0,
    text: best && best.el ? (best.el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 180) : ''
  };
  return JSON.stringify({ modal: !!(best && (best.links > 0 || best.imgs >= 3)), users, dbg });
}"""


# 各平台 关注/粉丝 列表接口的「方向专属」URL 关键词 —— 必须互斥,否则两边混到一起。
# 抖音:following/list vs follower/list(都含 follow,但 following 不含于 follower,反之亦然)。
# 抖音主页「关注/粉丝」统计项:hydrate 后才有真实标记。打出来校准选择器,
# 顺便看它是不是 <a>(能直接 goto)还是纯 div(必须点)。
_DOUYIN_STAT_PROBE_JS = """() => {
  const out = [];
  for (const e of document.querySelectorAll('a,div,span,button')) {
    const t = (e.textContent || '').trim();
    if (!/^(关注|粉丝|获赞)\\s*\\d*$/.test(t) && !/^\\d[\\d.万亿]*\\s*(关注|粉丝)$/.test(t)) continue;
    const r = e.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    out.push({tag: e.tagName, txt: t.slice(0, 12),
              de: e.getAttribute('data-e2e'),
              href: e.getAttribute('href'),
              cls: (e.className && e.className.toString ? e.className.toString() : '').slice(0, 32)});
  }
  return out.slice(0, 12);
}"""


_FOLLOW_PRECISE = {
    # 浏览器回退仍按方向精确匹配接口。直连 follower/list 使用当前 a_bogus
    # 和首屏时间游标；平台签名升级导致直连失效时，hybrid 仍可回退到这里。
    "douyin":   {"following": ("following/list",), "fan": ("follower/list",)},
    "xhs":      {"following": ("followings", "/follows"), "fan": ("fans", "/followers")},
    "kuaishou": {
        "following": ("/rest/v/relation/fol",),
        "fan": ("/rest/v/relation/fol",),
    },
}


_SCROLL_FOLLOW_LIST_JS = """() => {
  const isScrollable = (el) => {
    const s = getComputedStyle(el);
    return (s.overflowY === 'auto' || s.overflowY === 'scroll' || s.overflowY === 'overlay')
      && el.scrollHeight > el.clientHeight + 12;
  };
  const visibleRect = (el) => {
    const r = el.getBoundingClientRect();
    return (r.width > 120 && r.height > 120 && r.bottom > 0 && r.right > 0
            && r.top < window.innerHeight && r.left < window.innerWidth) ? r : null;
  };
  const modalSelectors = [
    '[data-e2e="user-fans-container"]',
    '[data-e2e="user-following-container"]',
    '[data-e2e*="fans-container"]',
    '[data-e2e*="follow-container"]',
    '[role="dialog"]',
    '[aria-modal="true"]',
    '.semi-modal-content',
    '[class*="modal"]',
    '[class*="Dialog"]',
    '[class*="drawer"]',
  ];
  const modals = [];
  const seenModal = new Set();
  for (const sel of modalSelectors) {
    for (const m of document.querySelectorAll(sel)) {
      if (seenModal.has(m)) continue;
      seenModal.add(m);
      const r = visibleRect(m);
      if (!r) continue;
      if (r.width >= window.innerWidth * 0.98 && r.height >= window.innerHeight * 0.98
          && m.getAttribute('role') !== 'dialog' && m.getAttribute('aria-modal') !== 'true') {
        continue;
      }
      const links = m.querySelectorAll('a[href*="/user/"], a[href*="/profile/"]').length;
      const imgs = m.querySelectorAll('img').length;
      if (links > 0 || imgs >= 2 || m.getAttribute('role') === 'dialog') {
        modals.push({ el: m, rect: r, score: links * 10 + imgs });
      }
    }
  }
  modals.sort((a, b) => b.score - a.score);
  if (modals.length) {
    const root = modals[0].el;
    let best = null, bh = 0;
    const candidates = [root, ...root.querySelectorAll('div,ul,section')];
    for (const el of candidates) {
      const r = el.getBoundingClientRect();
      if (r.width < 100 || r.height < 80) continue;
      if (isScrollable(el) && el.scrollHeight > bh) {
        best = el;
        bh = el.scrollHeight;
      }
    }
    const target = best || root;
    const tr = target.getBoundingClientRect();
    if (best) {
      best.scrollTop = best.scrollHeight;
      best.dispatchEvent(new Event('scroll', { bubbles: true }));
      const items = best.querySelectorAll('a[href*="/user/"], li, [class*="item"], [class*="Item"]');
      if (items.length) {
        try { items[items.length - 1].scrollIntoView({ block: 'end' }); } catch (_) {}
      }
    }
    return {
      inModal: true,
      scrolled: !!best,
      x: Math.max(10, Math.min(window.innerWidth - 10, Math.round(tr.left + tr.width / 2))),
      y: Math.max(10, Math.min(window.innerHeight - 10, Math.round(tr.top + tr.height / 2))),
    };
  }
  let best = null, bh = 0;
  document.querySelectorAll('div,ul,section,main').forEach(el => {
    if (isScrollable(el) && el.scrollHeight > el.clientHeight + 40 && el.scrollHeight > bh) {
      best = el;
      bh = el.scrollHeight;
    }
  });
  if (best) {
    best.scrollTop = best.scrollHeight;
    best.dispatchEvent(new Event('scroll', { bubbles: true }));
  }
  window.scrollBy(0, 3000);
  return {
    inModal: false,
    scrolled: !!best,
    x: Math.round(window.innerWidth / 2),
    y: Math.round(window.innerHeight / 2),
  };
}"""


async def fetch_follows(mgr: BrowserManager, identity, platform: str, uid: str,
                        direction: str, known_uids: Set[str], settle_ms: int = 2000,
                        max_scrolls: int = 40, *, _xhs_visible: bool = False,
                        progress=None) -> Tuple[List[dict], str]:
    """打开账号自己主页,切到「关注 / 粉丝」并滚动,拦截该页所有同域 XHR/GraphQL,
    启发式抽出用户对象。无公开接口,首版用于标定(日志打 api_seen)。
    返回 (归一后的用户 dict 列表, error)。"""
    if platform == "rednote":
        return [], "unavailable:海外小红书网页端未开放关注/粉丝列表"
    if platform == "twitter":
        from app.browser.twitter_fetcher import fetch_twitter_follows
        return await fetch_twitter_follows(
            mgr, identity, uid, direction, max_pages=max(4, min(20, max_scrolls))
        )
    if platform == "instagram":
        from app.platforms.instagram import InstagramClient
        cookie_s = await _ig_cookie_str(mgr, identity)
        if not cookie_s:
            return [], "logged_out:缺少 Instagram 登录 Cookie"
        client = InstagramClient(
            cookie_s,
            getattr(identity, "ua", "") or getattr(identity, "user_agent", ""),
            proxy=getattr(identity, "proxy", ""),
            account_seed=getattr(identity, "fp_seed", "") or getattr(identity, "profile_dir", ""),
            timezone_id=getattr(identity, "timezone_id", "Asia/Shanghai"),
        )
        async with client.session_scope():
            target_uid = (uid or client.self_uid or "").strip()
            if not target_uid:
                prof, perr = await client.fetch_self_profile()
                if perr:
                    return [], perr
                target_uid = str(prof.get("uid") or prof.get("unique_id") or "")
            if not target_uid:
                return [], "missing_uid:账号缺自身 uid,请先点账号「刷新资料」"
            cap = max(50, min(400, max_scrolls * 10))
            if direction == "fan":
                return await client.fetch_followers(target_uid, max_count=cap)
            return await client.fetch_following(target_uid, max_count=cap)
    if platform == "bilibili":
        from app.platforms.bilibili import BilibiliClient
        cookie_s = await _bili_cookie_str(mgr, identity)
        client = BilibiliClient(
            cookie_s,
            getattr(identity, "user_agent", ""),
            proxy=getattr(identity, "proxy", ""),
        )
        kind = "followers" if direction == "fan" else "following"
        out_users: List[dict] = []
        seen_ids: Set[str] = set()
        async with client.session_scope():
            target_mid = (uid or client.self_mid or "").strip()
            if not target_mid:
                prof, perr = await client.fetch_self_profile()
                if perr:
                    return [], perr
                target_mid = str(prof.get("uid") or prof.get("sec_uid") or "")
            if not target_mid:
                return [], "missing_uid:账号缺自身 uid,请先点账号「刷新资料」"
            max_pages = max(1, min(10, (max_scrolls // 4) or 3))
            for pn in range(1, max_pages + 1):
                batch, has_more, err = await client.fetch_follows(
                    target_mid, kind=kind, pn=pn, ps=50
                )
                if err:
                    return out_users, ("" if out_users else err)
                all_known = bool(batch) and bool(known_uids)
                for u in batch:
                    uid_item = str(u.get("uid") or u.get("user_id") or "")
                    if not uid_item:
                        continue
                    if uid_item not in known_uids:
                        all_known = False
                    if uid_item not in seen_ids:
                        seen_ids.add(uid_item)
                        out_users.append(u)
                if not has_more or all_known:
                    break
        return out_users, ""
    if platform == "xhs" and not _xhs_visible:
        async with mgr.visible_action(identity):
            return await fetch_follows(
                mgr, identity, platform, uid, direction, known_uids,
                settle_ms, max_scrolls, _xhs_visible=True, progress=progress)
    nav = _FOLLOW_NAV.get(platform, _FOLLOW_NAV["douyin"])
    uid = (uid or "").strip()
    self_url = ""
    if platform == "xhs":
        # 小红书直开 /user/profile/{uid} 缺 xsec_token 常被拦;从站内「我」入口拿真实链接
        self_url, self_uid = await _self_profile_link(mgr, identity, "xhs")
        uid = uid or self_uid
    elif platform == "kuaishou" and (not uid or uid.isdigit()):
        # 快手 /profile 只认真实 3x id(header 抓链接不稳、纯数字会 404):cookie userId 反查
        try:
            prof, perr = await fetch_ks_self_profile(mgr, identity)
            if perr == "logged_out":
                return [], "logged_out:登录态失效,请重新登录"
            self3x = str(parse_ks_self_user(prof or {}).get("sec_uid") or "")
            if self3x:
                uid = self3x
        except Exception as e:
            print(f"[follow] kuaishou self-resolve failed: {e!r}")
    if "{uid}" in nav["url"] and not uid and not self_url:
        return [], "missing_uid:账号缺自身 uid,请先点账号「刷新资料」"
    url = self_url or (nav["url"].format(uid=uid) if "{uid}" in nav["url"] else nav["url"])

    collected: Dict[str, dict] = {}     # 命中「关注/粉丝接口」的精确结果(优先)
    broad: Dict[str, dict] = {}          # 全页兜底(可能混入推荐位,仅当精确为空时启用)
    scraped: Dict[str, dict] = {}        # 小红书:从弹层 DOM 抽出的用户(XHR 拿不到时兜底)
    modal_seen = False                    # 小红书:是否检测到关注/粉丝弹层
    xhs_dbg: dict = {}                    # 小红书:抽不到时的弹层结构快照(标定用)
    ks_samples: list = []                # 快手:带用户特征的响应体样本(标定用)
    hit_urls: list = []                  # 真正吐出用户列表的接口(标定关键)
    api_seen: list = []
    error = ""
    precise_response_seen = False
    precise_empty_confirmed = False
    precise_pages = 0
    douyin_has_more: Optional[bool] = None
    douyin_last_batch_empty = False
    douyin_reported_total = 0
    precise_response_event = asyncio.Event()
    page = await mgr.new_page(identity, block_media=platform != "xhs")
    if platform == "xhs":
        try:
            await page.bring_to_front()
        except Exception:
            pass

    host = {"douyin": "douyin.com", "xhs": "xiaohongshu.com",
            "kuaishou": "kuaishou.com"}.get(platform, "douyin.com")

    precise_hints = _FOLLOW_PRECISE.get(platform, {}).get(direction, ())

    def _is_follow_api(path: str, data) -> bool:
        """是否「当前方向」的关注/粉丝接口 —— 方向专属,避免关注/粉丝抓到同一份。"""
        if precise_hints and any(h in path for h in precise_hints):
            return True
        # 快手:粉丝走 REST /rest/v/relation/、关注走 /myFollow 页 graphql visionProfileUserList。
        # 两方向各自独立导航(不会串),命中任一即视为当前方向精确结果。
        if platform == "kuaishou":
            if "/rest/v/relation/" in path:
                return True
            if isinstance(data, dict):
                d = data.get("data") or {}
                if isinstance(d, dict) and any(
                        k in d for k in ("visionProfileUserList", "visionFollowUserList",
                                         "fols", "userList")):
                    return True
        return False

    async def on_response(resp):
        nonlocal precise_response_seen, precise_empty_confirmed, precise_pages
        nonlocal douyin_has_more, douyin_last_batch_empty, douyin_reported_total
        u = resp.url
        if host not in u or resp.request.resource_type not in ("xhr", "fetch"):
            return
        path = u.split("?")[0].split(host)[-1]
        if len(api_seen) < 80:
            api_seen.append(f"{resp.status} {path}")
        try:
            data = await resp.json()
        except Exception:
            return
        # 快手标定:采样带用户特征的响应体(粉丝 REST relation / 关注 myFollow graphql),
        # 据此把解析对准(实测粉丝只抽出 1 个、关注抽出 0 个,需看真实结构)
        if platform == "kuaishou" and len(ks_samples) < 6:
            body = str(data)
            post_data = str(getattr(resp.request, "post_data", "") or "")
            operation = ""
            if post_data:
                try:
                    operation = str(json.loads(post_data).get("operationName") or "")
                except Exception:
                    operation = post_data[:120]
            data_keys = list((data.get("data") or {}).keys()) \
                if isinstance(data, dict) and isinstance(data.get("data"), dict) else []
            if path == "/graphql" or any(k in body for k in (
                    "user_name", "userName", "nickname", "headurl",
                    "headerUrl", "fols", "userList", "\"fan\"", "following")):
                ks_samples.append(
                    f"{path} op={operation} keys={data_keys} => {body[:700]}")
        precise = _is_follow_api(path.lower(), data)
        if precise and isinstance(data, dict) and data.get("status_code") in (None, 0, "0"):
            precise_response_seen = True
            precise_pages += 1
            precise_response_event.set()
            # 抖音列表位于顶层 followers/followings；只有看到了明确的空数组，
            # 才能确认账号确实为 0，而不是解析器没对上新结构。
            expected_key = "followers" if direction == "fan" else "followings"
            if platform == "douyin" and isinstance(data.get(expected_key), list):
                batch_rows = data[expected_key]
                precise_empty_confirmed = len(batch_rows) == 0 and len(collected) == 0
                douyin_last_batch_empty = len(batch_rows) == 0
                if data.get("total"):
                    try:
                        douyin_reported_total = max(
                            douyin_reported_total, int(data.get("total") or 0))
                    except (TypeError, ValueError):
                        pass
                if "has_more" in data:
                    douyin_has_more = bool(data.get("has_more"))
            elif platform == "kuaishou" and isinstance(data.get("fols"), list):
                precise_empty_confirmed = len(data["fols"]) == 0
        found: List[dict] = []
        _harvest_user_lists(data, found)
        if not found:
            if precise and progress is not None:
                try:
                    cb = progress({"pages": precise_pages, "fetched": len(collected)})
                    if inspect.isawaitable(cb):
                        await cb
                except Exception:
                    pass
            return
        sink = collected if precise else broad
        added = 0
        for d in found:
            n = _norm_follow_user(d, direction)
            if n and n["uid"] not in sink:
                sink[n["uid"]] = n
                added += 1
        if added and path not in hit_urls:
            hit_urls.append(("exact:" if precise else "candidate:") + path)
        if precise and progress is not None:
            try:
                cb = progress({"pages": precise_pages, "fetched": len(collected)})
                if inspect.isawaitable(cb):
                    await cb
            except Exception:
                pass

    page.on("response", on_response)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if "passport" in page.url or "/login" in page.url:
            return [], "logged_out:登录态失效,请重新登录"
        # 和私信入口同一个坑:没 hydrate 完就点,React handler 还没绑,
        # 元素「看起来可点」但点了没反应。先等网络静默。
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        if platform == "xhs":
            await mgr.xhs_interaction.pause(0.25, 0.55)
        else:
            await page.wait_for_timeout(settle_ms)
        if platform == "douyin":
            # 标定:hydrate 后把关注/粉丝入口的真实标记打出来,别再盲猜选择器
            try:
                print(f"[follow-probe] douyin stat entries: "
                      f"{await page.evaluate(_DOUYIN_STAT_PROBE_JS)}")
            except Exception as e:
                print(f"[follow-probe] douyin probe failed: {e!r}")
        # 打开「当前方向」的列表:依次试候选入口,点完等该方向专属接口回包来确认开对了。
        openers = nav.get("open", {}).get(direction, [])
        opened = False
        for cand in openers:
            try:
                if cand.startswith("dyjs:"):
                    # 抖音:锚定 user-info-fans 反查统计区,只在区内点(避开顶栏「关注」导航)
                    clicked = await page.evaluate(_DOUYIN_OPEN_STAT_JS, cand[5:])
                    print(f"[follow] douyin stat click {cand[5:]} → {clicked!r}")
                    if not clicked:
                        continue
                elif cand.startswith("xhs:"):
                    clicked = await _click_xhs_profile_stat(
                        mgr, page, cand[4:])
                    if not clicked:
                        continue
                elif cand.startswith("ksstat:"):
                    clicked = await _click_ks_profile_stat(page, cand[7:])
                    if not clicked:
                        continue
                elif cand.startswith("text="):
                    el = page.get_by_text(cand[5:], exact=False).first
                    if not await el.count():
                        continue
                    await el.click(timeout=4000)
                else:
                    el = page.locator(cand).first
                    if not await el.count():
                        continue
                    await el.click(timeout=4000)
            except Exception:
                continue
            if platform == "xhs":
                await mgr.xhs_interaction.pause(0.25, 0.55)
                opened = True
                break
            if precise_hints:   # 等监听器确认该方向接口；不会漏掉点击后瞬间返回的响应
                try:
                    await asyncio.wait_for(precise_response_event.wait(), timeout=7)
                    opened = True
                    break
                except Exception:
                    continue    # 这个入口没触发对的接口,换下一个候选
            else:
                await page.wait_for_timeout(settle_ms)
                opened = True
                break
        if platform == "xhs":
            await mgr.xhs_interaction.pause(0.25, 0.55)
        else:
            await page.wait_for_timeout(settle_ms)
        # 小红书:边滚边从弹层 DOM 抽用户(数据不发独立 XHR);其余平台仍靠 XHR 拦截
        async def _xhs_scrape():
            nonlocal modal_seen, xhs_dbg
            if platform != "xhs":
                return
            try:
                import json as _json
                res = _json.loads(await page.evaluate(_XHS_SCRAPE_DRAWER_JS, uid) or "{}")
            except Exception:
                return
            if res.get("modal"):
                modal_seen = True
            if res.get("dbg"):
                xhs_dbg = res["dbg"]
            for u in res.get("users") or []:
                uu = str(u.get("uid") or "")
                nn = str(u.get("nickname") or "")
                if uu and uu not in scraped:
                    scraped[uu] = {
                        "uid": uu, "sec_uid": "", "nickname": nn,
                        "avatar": str(u.get("avatar") or ""), "signature": "",
                        "is_following": direction == "following", "is_mutual": False,
                    }

        stagnant = 0
        stagnant_limit = 6 if platform == "douyin" else 4
        scroll_limit = (max(max_scrolls, 500)
                        if platform == "douyin" and max_scrolls == 40
                        else max_scrolls)
        for _ in range(scroll_limit):
            if platform == "douyin" and douyin_has_more is False:
                break
            before = len(collected) + len(broad) + len(scraped)
            precise_response_event.clear()
            try:
                if platform == "xhs":
                    drawer = page.locator(
                        '[role="dialog"],[class*="modal"],'
                        '[class*="drawer"],[class*="user-list"]').first
                    if await drawer.count() and await drawer.is_visible():
                        await drawer.hover()
                    await mgr.xhs_interaction.scroll_step(page)
                else:
                    # 优先定位关注/粉丝弹窗容器并在弹窗中心触发滚轮，避免主页作品区高度更大而滚错底层页面。
                    pos = await page.evaluate(_SCROLL_FOLLOW_LIST_JS)
                    if isinstance(pos, dict) and pos.get("x") and pos.get("y"):
                        await page.mouse.move(int(pos["x"]), int(pos["y"]))
                    await page.mouse.wheel(0, 2600)
            except Exception:
                pass
            if platform == "douyin":
                try:
                    await asyncio.wait_for(
                        precise_response_event.wait(),
                        timeout=max(1.5, settle_ms / 1000.0))
                    await page.wait_for_timeout(180)
                except Exception:
                    pass
            elif platform != "xhs":
                await page.wait_for_timeout(settle_ms)
            await _xhs_scrape()
            if (len(collected) + len(broad) + len(scraped)) == before:
                stagnant += 1
                if stagnant >= stagnant_limit:
                    break
            else:
                stagnant = 0
        final_url = page.url
    except Exception as e:
        error = f"打开关注/粉丝页失败: {e!r}"
        final_url = ""
    finally:
        try:
            await page.close()
        except Exception:
            pass

    # 只用「方向专属精确接口」的结果,确保关注≠粉丝;broad 仅用于诊断日志,不并入结果。
    # 小红书:XHR 拿不到列表,用弹层 DOM 抽出的 scraped 兜底。
    result = collected
    if platform == "xhs" and not result and scraped:
        result = scraped
    # 标定期:无论成败都打日志,便于把真实接口固化进 _FOLLOW_PRECISE / _norm_follow_user
    scraped_n = len(scraped) if platform == "xhs" else 0
    print(f"[follow] platform={platform} dir={direction} uid={uid} "
          f"precise={len(collected)} broad={len(broad)} scraped={scraped_n} "
          f"modal={modal_seen if platform == 'xhs' else '-'} hit_urls={hit_urls[:8]} "
          f"final_url={final_url} api_seen({len(api_seen)})={api_seen[:50]}")
    # 小红书抽不到时,把弹层真实结构打出来(据此写精确选择器/解析)
    if platform == "xhs" and not result:
        print(f"[follow-dom] dir={direction} dbg={xhs_dbg}")
    # 快手:打样本(粉丝只抽出 1 个 / 关注抽出 0 个时,据真实结构对准解析)
    if platform == "kuaishou":
        for i, smp in enumerate(ks_samples):
            print(_console_safe(f"[follow-ks {direction} {i}] {smp}"))
    if not result:
        if precise_empty_confirmed:
            error = "empty"
        elif platform == "xhs":
            # 实测三轮:点开后从不发关注/粉丝接口,页内也无该方向用户链接 ——
            # 小红书网页端不提供关注/粉丝列表(App 专属),非本项目可解。
            error = error or ("小红书网页端不提供关注/粉丝列表(该列表为 App 专属,"
                              "网页端既无接口也无弹层),无法同步;请在手机 App 查看。")
        else:
            error = error or (f"未拦截到{'关注' if direction=='following' else '粉丝'}列表"
                              "(没等到该方向专属接口,可能入口未点开/接口待标定)")
        return [], error
    if (platform == "douyin" and douyin_has_more is True
            and not douyin_last_batch_empty):
        reached_total = (douyin_reported_total > 0
                         and len(result) >= douyin_reported_total)
        reached_fan_cap = (direction == "fan" and len(result) >= 4500)
        if not reached_total and not reached_fan_cap:
            return (
                list(result.values()),
                f"browser_pagination_incomplete:got={len(result)},pages={precise_pages}",
            )
    return list(result.values()), ""
