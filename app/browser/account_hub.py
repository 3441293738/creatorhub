"""本账号管理(作品 / 关注 / 粉丝 / 私信)的浏览器抓取与写操作。

复用既有「浏览器拦截」基建(manager / identity / 各平台 fetcher),把抓到的原始项
归一成 models.AccountWork / FollowEdge / DmConversation / DmMessage 用的扁平 dict。

阶段约定:
  - 作品:三平台接口已知,直接复用 fetch_videos / fetch_xhs_notes / fetch_ks_videos。
  - 关注/粉丝、私信:无公开接口,走「导航到对应页 + 拦截该页所有同域 XHR/GraphQL +
    启发式解析」。真实接口路径/字段需对着登录态抓包标定(日志打 api_seen),
    标定后把常量固化进 _FOLLOW_HINT / _DM_HINT。
"""
from __future__ import annotations

import json
import os
import re
from typing import Dict, List, Optional, Set, Tuple

from .douyin_im_pb import parse_conversations
from .fetcher import fetch_videos
from .ks_fetcher import fetch_ks_videos, fetch_ks_self_profile
from .manager import BrowserManager
from .xhs_fetcher import fetch_xhs_notes
from ..platforms.kuaishou import parse_self_user as parse_ks_self_user


def _num(v) -> int:
    """互动数可能是 int / "1.2万" / "999+" 字符串,尽量转 int。"""
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v or "").strip().replace("+", "").replace(",", "")
    if not s:
        return 0
    try:
        if s.endswith("万"):
            return int(float(s[:-1]) * 10000)
        if s.endswith("亿"):
            return int(float(s[:-1]) * 100000000)
        return int(float(s))
    except (ValueError, TypeError):
        return 0


def _first(d: dict, *keys, default=""):
    for k in keys:
        v = (d or {}).get(k)
        if v not in (None, "", [], {}):
            return v
    return default


# ─────────── 自己主页的站内入口 ───────────
# 小红书/快手直开 /user/profile/{uid} 缺站内参数(小红书没有 xsec_token 时
# user_posted 常拿不到),从站内「我」入口解析真实主页链接最稳,顺带可补缺失的 uid。
_SELF_HOME = {
    "xhs": "https://www.xiaohongshu.com/explore",
    "kuaishou": "https://www.kuaishou.com/",
}
_SELF_LINK_JS = {
    # 侧栏「我」的链接(feed 卡片作者也是 /user/profile/,须按文本挑)
    "xhs": """() => {
        const as = [...document.querySelectorAll('a[href*="/user/profile/"]')];
        const me = as.find(a => (a.textContent || '').trim() === '我');
        const el = me || (as.length === 1 ? as[0] : null);
        return el ? el.href : '';
    }""",
    # 顶栏头像/用户区指向自己主页(正文卡片作者链接太多,只认头部区域)
    "kuaishou": """() => {
        for (const sel of ['header a[href*="/profile/"]',
                           '[class*="header"] a[href*="/profile/"]',
                           '[class*="user-info"] a[href*="/profile/"]']) {
            const a = document.querySelector(sel);
            if (a) return a.href;
        }
        return '';
    }""",
}


async def _self_profile_link(mgr: BrowserManager, identity, platform: str
                             ) -> Tuple[str, str]:
    """打开站内首页,从「我」入口拿自己主页的真实链接(带 xsec_token 等参数)。
    返回 (绝对 URL, uid);拿不到返回 ("", "")。"""
    home = _SELF_HOME.get(platform)
    js = _SELF_LINK_JS.get(platform)
    if not home or not js:
        return "", ""
    href = ""
    page = await mgr.new_page(identity, block_media=True)
    try:
        await page.goto(home, wait_until="domcontentloaded", timeout=30000)
        for _ in range(6):          # 侧栏/顶栏异步渲染,轮询最多 ~6s
            await page.wait_for_timeout(1000)
            try:
                href = await page.evaluate(js) or ""
            except Exception:
                href = ""
            if href:
                break
    except Exception:
        href = ""
    finally:
        try:
            await page.close()
        except Exception:
            pass
    m = re.search(r"/(?:user/profile|profile)/([0-9a-zA-Z_-]+)", href)
    uid = m.group(1) if m else ""
    print(f"[hub-self] platform={platform} href={href!r} uid={uid}")
    return href, uid


# ─────────── 原始作品项 → AccountWork 扁平 dict ───────────
def _norm_douyin_work(it: dict) -> Optional[dict]:
    aid = str(it.get("aweme_id") or "")
    if not aid:
        return None
    stats = it.get("statistics") or {}
    cover = ((it.get("video") or {}).get("cover") or {}).get("url_list") or []
    return {
        "item_id": aid,
        "desc": (it.get("desc") or "").strip(),
        "media_type": "images" if it.get("images") else "video",
        "cover_url": cover[0] if cover else "",
        "create_time": int(it.get("create_time") or 0),
        "like_count": _num(stats.get("digg_count")),
        "comment_count": _num(stats.get("comment_count")),
        "collect_count": _num(stats.get("collect_count")),
        "share_count": _num(stats.get("share_count")),
        "play_count": _num(stats.get("play_count")),
        "status": "",
    }


def _norm_xhs_work(it: dict) -> Optional[dict]:
    card = it.get("note_card") or it
    nid = str(_first(it, "note_id", "id", default="")
              or _first(card, "note_id", "id", default=""))
    if not nid:
        return None
    cover = card.get("cover") or {}
    cov = _first(cover, "url_default", "url_pre", "url", default="")
    if not cov and isinstance(cover.get("info_list"), list) and cover["info_list"]:
        cov = cover["info_list"][0].get("url", "")
    inter = card.get("interact_info") or {}
    return {
        "item_id": nid,
        "desc": _first(card, "display_title", "title", "desc", default=""),
        "media_type": "video" if card.get("type") == "video" else "images",
        "cover_url": cov,
        "create_time": int(_num(card.get("time")) / 1000) if _num(card.get("time")) > 1e12
                       else _num(card.get("time")),
        "like_count": _num(inter.get("liked_count")),
        "comment_count": _num(inter.get("comment_count")),
        "collect_count": _num(inter.get("collected_count")),
        "share_count": _num(inter.get("share_count")),
        "play_count": 0,
        "status": "",
        # 抓评论/打开笔记要用;SSR 项在 item 顶层,拦截项也可能带
        "xsec_token": str(_first(it, "xsec_token", "xsecToken", default="")
                          or _first(card, "xsec_token", "xsecToken", default="")),
    }


def _norm_ks_work(feed: dict) -> Optional[dict]:
    photo = (feed.get("photo") or {}) if isinstance(feed, dict) else {}
    pid = str(_first(photo, "id", default="") or feed.get("id") or "")
    if not pid:
        return None
    ts = _num(photo.get("timestamp"))
    return {
        "item_id": pid,
        "desc": (_first(photo, "caption", "name", default="") or "").strip(),
        "media_type": "images" if photo.get("atlas") or photo.get("imgUrls") else "video",
        "cover_url": _first(photo, "coverUrl", "cover_url", "webpCoverUrl", default=""),
        "create_time": int(ts / 1000) if ts > 1e12 else ts,
        "like_count": _num(_first(photo, "realLikeCount", "likeCount", default=0)),
        "comment_count": _num(photo.get("commentCount")),
        "collect_count": 0,
        "share_count": _num(photo.get("shareCount")),
        "play_count": _num(_first(photo, "viewCount", "playCount", default=0)),
        "status": "",
    }


async def fetch_account_works(mgr: BrowserManager, identity, platform: str, uid: str,
                              max_scrolls: int = 14) -> Tuple[List[dict], str]:
    """抓取登录账号自己发布的作品(复用各平台已有的主页拦截抓取)。
    返回 (归一后的作品 dict 列表, error)。需要账号已知自身 uid/sec_uid。
    入参用 identity/platform/uid 原语(由调用方在 session 活跃时取出),避免 ORM 实例失效。"""
    uid = (uid or "").strip()
    open_url = ""
    if platform == "xhs":
        # 小红书:站内「我」入口拿真实主页链接(带 xsec_token);失败再退回 uid 直开
        open_url, self_uid = await _self_profile_link(mgr, identity, "xhs")
        uid = uid or self_uid
    elif platform == "kuaishou" and (not uid or uid.isdigit()):
        # 快手:header 抓链接不稳(实测 href='');/profile 只认真实 3x id(纯数字 userId 会 404)。
        # 改用可靠法 —— cookie 数字 userId 反查本人 3x id(与「刷新资料」同一套 fetch_ks_self_profile)
        try:
            prof, perr = await fetch_ks_self_profile(mgr, identity)
            if perr == "logged_out":
                return [], "logged_out:登录态失效,请重新登录"
            self3x = str(parse_ks_self_user(prof or {}).get("sec_uid") or "")
            if self3x:
                uid = self3x
        except Exception as e:
            print(f"[hub-self] kuaishou self-resolve failed: {e!r}")
    if not uid and not open_url:
        return [], "missing_uid:账号缺自身 uid,请先点账号「刷新资料」再同步作品"

    known: Set[str] = set()
    try:
        if platform == "xhs":
            items, _author, err = await fetch_xhs_notes(mgr, identity, uid, known,
                                                        xsec_token="",
                                                        max_scrolls=max_scrolls,
                                                        open_url=open_url,
                                                        ssr_fallback=True)
            norm = _norm_xhs_work
        elif platform == "kuaishou":
            items, _author, err = await fetch_ks_videos(mgr, identity, uid, known,
                                                        max_scrolls=max_scrolls,
                                                        open_url=open_url)
            norm = _norm_ks_work
        else:
            items, _author, err = await fetch_videos(mgr, identity, uid, known,
                                                     max_scrolls=max_scrolls)
            norm = _norm_douyin_work
    except Exception as e:
        return [], f"抓取作品异常: {e!r}"

    out = [w for w in (norm(it) for it in (items or [])) if w]
    return out, ("" if out else err)


# ═══════════ 关注 / 粉丝(无公开接口:拦截该账号登录态打开的关注/粉丝页 XHR) ═══════════
_NAME_KEYS = ("nickname", "nick_name", "user_name", "userName", "name", "nick", "nickName")
_ID_KEYS = ("user_id", "userId", "uid", "id", "red_id", "kwaiId")
# 「强用户特征」字段:webpack 模块清单 {id,name} 没有这些,用来把模块/无关对象剔掉
_STRONG_ID_KEYS = ("user_id", "userId", "uid", "sec_uid", "secUid", "red_id", "kwaiId")
_AVATAR_KEYS = ("avatar", "avatar_thumb", "avatar_small", "avatar_larger",
                "avatarUrl", "avatar_url", "headurl",
                "head_url", "headUrl", "image", "images", "icon")


def _looks_like_user(d: dict) -> bool:
    """判断是否一个「用户对象」。光有 name+id 不够(JS 模块清单 {id,name} 会误中),
    必须额外带「强用户特征」:user_id/sec_uid 等强 id,或头像。"""
    if not isinstance(d, dict):
        return False
    if not any(d.get(k) for k in _NAME_KEYS):
        return False
    return bool(any(d.get(k) for k in _STRONG_ID_KEYS) or _avatar_of(d))


def _avatar_of(d: dict) -> str:
    for k in _AVATAR_KEYS:
        v = d.get(k)
        if isinstance(v, str) and v.startswith("http"):
            return v
        if isinstance(v, dict):
            ul = v.get("url_list") or v.get("urlList")
            if isinstance(ul, list) and ul:
                return ul[0]
            for kk in ("url", "uri", "url_default"):
                if isinstance(v.get(kk), str) and v[kk].startswith("http"):
                    return v[kk]
        if isinstance(v, list) and v:
            if isinstance(v[0], dict) and v[0].get("url"):
                return v[0]["url"]
            if isinstance(v[0], str) and v[0].startswith("http"):
                return v[0]
    return ""


def _harvest_user_lists(node, out: List[dict], depth: int = 0):
    """递归找出「元素像用户对象的数组」,把这些用户对象收集起来。"""
    if depth > 8:
        return
    if isinstance(node, list):
        users = [x for x in node if _looks_like_user(x)]
        if len(users) >= 1 and len(users) >= len(node) * 0.5:
            out.extend(users)
        for x in node:
            _harvest_user_lists(x, out, depth + 1)
    elif isinstance(node, dict):
        for v in node.values():
            _harvest_user_lists(v, out, depth + 1)


def _norm_follow_user(d: dict, direction: str) -> Optional[dict]:
    uid = ""
    for k in _ID_KEYS:
        if d.get(k):
            uid = str(d[k]); break
    nickname = ""
    for k in _NAME_KEYS:
        if d.get(k):
            nickname = str(d[k]); break
    if not uid or not nickname:
        return None
    # 关系判定:多平台字段兜底
    rel = d.get("follow_status") if d.get("follow_status") is not None else \
          d.get("followStatus") if d.get("followStatus") is not None else \
          d.get("relation_type")
    is_following = bool(d.get("isFollowing") or d.get("following")
                        or (isinstance(rel, int) and rel in (1, 2)))
    is_mutual = bool(d.get("isFollowed") and d.get("isFollowing")) \
        or (isinstance(rel, int) and rel == 2) \
        or d.get("mutual_relation") is True
    # 粉丝列表里默认 direction=fan;关注列表里默认 is_following=True
    if direction == "following":
        is_following = True
    return {
        "uid": uid,
        "sec_uid": str(d.get("sec_uid") or d.get("secUid") or ""),
        "nickname": nickname,
        "avatar": _avatar_of(d),
        "signature": str(d.get("signature") or d.get("desc")
                         or d.get("user_text") or d.get("userText") or ""),
        "is_following": is_following,
        "is_mutual": is_mutual,
    }


# 各平台「关注/粉丝」页:打开 URL + 打开对应列表的点击候选(CSS 选择器 或 text=文案)。
# 关键:关注与粉丝要点不同入口,否则两边抓到同一份(改版时改 open 候选)。
_FOLLOW_NAV = {
    "douyin": {
        "url": "https://www.douyin.com/user/self",
        "open": {
            "following": ['[data-e2e="user-info-follow"]', '[data-e2e="user-following"]',
                          'text=关注'],
            "fan":       ['[data-e2e="user-info-fans"]', '[data-e2e="user-fans"]',
                          'text=粉丝'],
        },
    },
    "xhs": {
        # ⚠️ xiaohongshu.com 顶栏有「关注」feed 标签,get_by_text 会点错;用 js: 精确点主页统计区
        "url": "https://www.xiaohongshu.com/user/profile/{uid}",
        "open": {"following": ['js:关注'], "fan": ['js:粉丝']},
    },
    "kuaishou": {
        "url": "https://www.kuaishou.com/profile/{uid}",
        "open": {"following": ['text=关注'], "fan": ['text=粉丝']},
    },
}
# 小红书:主页统计里「关注/粉丝」文案会和顶栏导航「关注」撞,用 JS 只点「紧挨数字、且不在
# 顶栏/侧栏」的那个统计项(点它才会弹出关注/粉丝列表抽屉,进而触发列表接口)。
_XHS_OPEN_STAT_JS = """(label) => {
  const bad = 'header,nav,[class*="nav"],[class*="header"],[class*="side"],[class*="channel"],[class*="tab"]';
  const inChrome = (el) => !!el.closest(bad);
  const cands = [...document.querySelectorAll('span,div,a')].filter(el => {
    const t = (el.textContent || '').trim();
    return (t === label || t === label + '数') && !inChrome(el) && el.children.length <= 1;
  });
  // 打分:父级文本里带数字(统计项形如「12 关注」)的优先
  const scored = cands.map(el => {
    const p = el.parentElement, pp = p && p.parentElement;
    const near = ((p && p.textContent) || '') + ' ' + ((pp && pp.textContent) || '');
    return { el, num: /\\d/.test(near) ? 1 : 0 };
  }).sort((a, b) => b.num - a.num);
  const pick = scored.length ? scored[0].el : null;
  if (!pick) return false;
  (pick.closest('[class*="interaction"],[class*="data-info"],[class*="count"],a,div') || pick).click();
  return true;
}"""
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
    # follower/list 接口是活的(浏览器拦截能拿到数据),但直连拿不到:所有参数组合都是
    # HTTP 200 + 空 body,而同一套签名下 following/list 正常。推测是假 msToken 被风控拒
    # (未验证)。所以 fan 方向实际靠浏览器兜底,别再去调直连的参数。
    "douyin":   {"following": ("following/list",), "fan": ("follower/list",)},
    "xhs":      {"following": ("followings", "/follows"), "fan": ("fans", "/followers")},
    "kuaishou": {"following": (), "fan": ()},   # 快手走 graphql visionProfileUserList(见下)
}


async def fetch_follows(mgr: BrowserManager, identity, platform: str, uid: str,
                        direction: str, known_uids: Set[str], settle_ms: int = 2000,
                        max_scrolls: int = 40) -> Tuple[List[dict], str]:
    """打开账号自己主页,切到「关注 / 粉丝」并滚动,拦截该页所有同域 XHR/GraphQL,
    启发式抽出用户对象。无公开接口,首版用于标定(日志打 api_seen)。
    返回 (归一后的用户 dict 列表, error)。"""
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
    page = await mgr.new_page(identity, block_media=True)

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
            if any(k in body for k in ("user_name", "userName", "nickname", "headurl",
                                       "fols", "userList", "\"fan\"", "following")):
                ks_samples.append(f"{path} => {body[:700]}")
        found: List[dict] = []
        _harvest_user_lists(data, found)
        if not found:
            return
        precise = _is_follow_api(path.lower(), data)
        sink = collected if precise else broad
        added = 0
        for d in found:
            n = _norm_follow_user(d, direction)
            if n and n["uid"] not in sink:
                sink[n["uid"]] = n
                added += 1
        if added and path not in hit_urls:
            hit_urls.append(("✓" if precise else "?") + path)

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
                if cand.startswith("js:"):
                    # 小红书:JS 精确点主页统计区的「关注/粉丝」(避开顶栏同名标签)
                    clicked = await page.evaluate(_XHS_OPEN_STAT_JS, cand[3:])
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
            if precise_hints:   # 等该方向接口(following/list 或 follower/list)回包来确认
                try:
                    await page.wait_for_response(
                        lambda r: any(h in r.url for h in precise_hints) and r.status == 200,
                        timeout=7000)
                    opened = True
                    break
                except Exception:
                    continue    # 这个入口没触发对的接口,换下一个候选
            else:
                await page.wait_for_timeout(settle_ms)
                opened = True
                break
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
        for _ in range(max_scrolls):
            before = len(collected) + len(broad) + len(scraped)
            try:
                # 找页面里「可滚动且内容最高」的容器(通常就是关注/粉丝弹窗列表)滚到底
                await page.evaluate(
                    "() => { let best=null,bh=0;"
                    " document.querySelectorAll('div,ul,section,main').forEach(el=>{"
                    "  const s=getComputedStyle(el);"
                    "  if((s.overflowY==='auto'||s.overflowY==='scroll')"
                    "     && el.scrollHeight>el.clientHeight+40 && el.scrollHeight>bh){best=el;bh=el.scrollHeight;}});"
                    " if(best){best.scrollTop=best.scrollHeight;} window.scrollBy(0,3000); }")
                await page.mouse.wheel(0, 3000)
            except Exception:
                pass
            await page.wait_for_timeout(settle_ms)
            await _xhs_scrape()
            if (len(collected) + len(broad) + len(scraped)) == before:
                stagnant += 1
                if stagnant >= 4:
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
    # 快手:两方向各自独立导航(粉丝 REST relation / 关注 myFollow graphql),不会互相串,
    # 故精确为空时用 broad 兜底(broad 就是当前方向 harvest 到的用户)。
    if platform == "kuaishou" and not result and broad:
        result = broad
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
            print(f"[follow-ks {direction} {i}] {smp}")
    if not result:
        if platform == "xhs":
            # 实测三轮:点开后从不发关注/粉丝接口,页内也无该方向用户链接 ——
            # 小红书网页端不提供关注/粉丝列表(App 专属),非本项目可解。
            error = error or ("小红书网页端不提供关注/粉丝列表(该列表为 App 专属,"
                              "网页端既无接口也无弹层),无法同步;请在手机 App 查看。")
        else:
            error = error or (f"未拦截到{'关注' if direction=='following' else '粉丝'}列表"
                              "(没等到该方向专属接口,可能入口未点开/接口待标定)")
    return list(result.values()), ("" if result else error)


# ═══════════ 私信(无公开接口:拦截私信页 XHR;发送走 UI 自动化) ═══════════
_DM_NAV = {
    # 抖音私信(IM)接口(/v1/message/、/v1/stranger/)在主站/关注页就会自动加载,
    # 打开 /follow 最稳(实测会触发 get_conversation_list / get_message_by_init)
    "douyin":   "https://www.douyin.com/follow",
    # 小红书:/im 直开会 goto 失败(api_seen=0)。回到首页(能正常加载并触发 message/entry),
    # 靠采样 entry 响应体 + 点私信入口来标定
    "xhs":      "https://www.xiaohongshu.com/",
    "kuaishou": "https://www.kuaishou.com/",
}
# 私信/会话接口的 URL 关键词(抖音实测:/v1/message/、/v1/stranger/get_conversation_list)。
# 注意排除 /aweme/v1/web/im/{resources,active,strategy} 这类「IM 配置/表情」噪声接口。
# 小红书实测 api_seen 有 /api/im/web/message/entry —— 说明其私信走 web REST(非纯 WS),
# 把 /api/im/web/ 纳入命中,便于采样真实会话列表接口(排除 entry/version 这类配置噪声)。
_DM_URL_HINTS = ("/v1/message/", "/v1/stranger/", "get_conversation_list",
                 "get_message_by_init", "get_user_message", "/imapi/",
                 "/im/conversation", "conversation/list",
                 # 抖音 IM 真实端点(protobuf,实测标定):建会话/拉会话列表/发消息
                 "imapi.douyin.com", "/v2/conversation/create",
                 "/v2/conversation/get_info_list", "/v1/message/send",
                 "/api/im/web/chat", "/api/im/web/session", "/api/im/web/conversation",
                 "/api/im/web/msg")

# 「私信面板真的打开了」的信号:只认会话/消息数据接口。
# 不含 im/strategy/config、im/resources/*、im/user/active/* 等 —— 它们随便哪个页面加载都会发,
# 拿来当信号会把「SDK 预加载」误判成「面板已打开」。
_IM_BOOTSTRAP_SIGNALS = ("/v1/message/", "/v1/stranger/", "get_message_by_init",
                         "get_conversation_list", "/imapi/", "/v2/conversation/",
                         "/aweme/v1/web/im/user/info", "/api/im/web/chat",
                         "/api/im/web/session", "/api/im/web/msg")

# 抖音顶栏「消息」是 <div>(无 href),React onClick 绑在祖先上。
# ⚠️ 向上找可点祖先只用来判定「这一项可点」,坐标必须取文本元素自身的中心 ——
#    祖先常是整条导航栏容器,取它的中心会点到旁边的空白或别的 nav item。
_DOUYIN_IM_BOXES_JS = """() => {
  const hits = [...document.querySelectorAll('div,span,a,button,li')]
    .filter(e => (e.textContent || '').trim() === '消息');
  const out = [];
  for (const e of hits) {
    const r = e.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    let clickable = false;
    for (let t = e, d = 0; d < 4 && t; d++, t = t.parentElement) {
      const s = getComputedStyle(t);
      if (t.tagName === 'A' || t.tagName === 'BUTTON'
          || t.getAttribute('role') === 'button' || s.cursor === 'pointer') {
        clickable = true; break;
      }
    }
    const x = r.x + r.width / 2, y = r.y + r.height / 2;
    const top = document.elementFromPoint(x, y);   // 该坐标上真正会收到点击的元素
    const covered = !(top && (top === e || e.contains(top) || top.contains(e)));
    out.push({x, y, tag: e.tagName, clickable, covered});
  }
  return out;
}"""


def _peer_uid_from_conv_id(conv_id: str, self_uid: str = "") -> str:
    """抖音单聊 conversation_id 格式为 `0:{type}:{uidA}:{uidB}`。
    实测两个 uid 的顺序不固定(见到过 `0:1:{peer}:{me}`,对端在前),所以
    只能靠「不是本账号 uid 的那个」判定对端 —— 必须已知 self_uid,否则返回空不猜。"""
    if not conv_id or ":" not in conv_id or not self_uid:
        return ""
    segs = [s for s in conv_id.split(":") if s.isdigit()]
    uids = [s for s in segs if len(s) >= 6]   # uid 远长于 type/idx 段
    if self_uid not in uids:
        return ""
    return next((u for u in uids if u != self_uid), "")


def _dm_last_text(content) -> str:
    """抖音消息 content 是一段 JSON 字符串,文本在 .text(见 douyin_recv_msg.py)。
    兼容:已是 dict、纯文本、或 {text}/{content} 的 JSON 串。"""
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "")
    if isinstance(content, str):
        s = content.strip()
        if s.startswith("{"):
            try:
                obj = json.loads(s)
                if isinstance(obj, dict):
                    return str(obj.get("text") or obj.get("content") or "")
            except Exception:
                pass
        return content
    return ""


def _norm_conv(d: dict) -> Optional[dict]:
    """从会话对象兜底抽 {conv_id, peer_uid, peer_nickname, peer_avatar, last_text, last_time}。"""
    if not isinstance(d, dict):
        return None
    # ── imapi/protobuf 分支:抖音 v2/conversation/get_info_list 解出的会话只有
    #    conversation_id / conversation_short_id / conversation_type / ticket,
    #    没有内嵌 user 对象。此时从 conv_id 拆 peer_uid,昵称/头像留空待二次水合。
    _conv_id = str(d.get("conversation_id") or d.get("conversationId") or "")
    if _conv_id and int(d.get("conversation_type") or 0) == 1 \
            and not (d.get("user") or d.get("fromUser") or d.get("peer")):
        peer = _peer_uid_from_conv_id(_conv_id)
        if peer:
            return {
                "conv_id": _conv_id,
                "peer_uid": peer,
                "peer_sec_uid": "",
                "peer_nickname": "",          # protobuf 会话不带昵称,需二次水合
                "peer_avatar": "",
                "last_text": _dm_last_text(d.get("content") or d.get("last_message")),
                "last_time": int(d.get("last_time") or 0),
                "unread_count": int(d.get("unread") or d.get("badge_count") or 0),
                "conv_short_id": str(d.get("conversation_short_id") or ""),
                "ticket": str(d.get("ticket") or ""),
            }
    peer = d.get("user") or d.get("fromUser") or d.get("toUser") or d.get("peer") or d
    if not isinstance(peer, dict):
        return None
    # peer 必须像用户(有头像或强 id),否则是 JS 模块/无关对象({id,name})
    if not (_avatar_of(peer) or any(peer.get(k) for k in _STRONG_ID_KEYS)):
        return None
    # 且整条记录要有「会话特征」:会话 id 或最近消息字段(纯用户对象不算会话)
    has_conv_marker = any(d.get(k) for k in (
        "conversation_id", "conversationId", "conv_id", "last_message",
        "lastMessage", "last_msg", "unread", "unreadCount"))
    nickname = ""
    for k in _NAME_KEYS:
        if peer.get(k):
            nickname = str(peer[k]); break
    conv_id = str(d.get("conversation_id") or d.get("conversationId")
                  or d.get("conv_id") or d.get("id") or "")
    peer_uid = ""
    for k in _ID_KEYS:
        if peer.get(k):
            peer_uid = str(peer[k]); break
    if not nickname or not (conv_id or peer_uid) or not has_conv_marker:
        return None
    last = d.get("last_message") or d.get("lastMessage") or {}
    # 抖音 last_message.content 可能是 JSON 串({text}),用统一解析
    last_text = _dm_last_text(last.get("content") or last.get("text") or "") \
        if isinstance(last, dict) else _dm_last_text(last)
    return {
        "conv_id": conv_id or peer_uid,
        "peer_uid": peer_uid,
        "peer_sec_uid": str(peer.get("sec_uid") or ""),
        "peer_nickname": nickname,
        "peer_avatar": _avatar_of(peer),
        "last_text": last_text,
        "last_time": int(d.get("last_time") or d.get("updateTime") or 0),
        "unread_count": int(d.get("unread") or d.get("unreadCount") or 0),
    }


def _harvest_users(data, into: Dict[str, dict]) -> None:
    """从任意 JSON 里递归捞「用户对象」,建 uid -> {nickname, avatar, sec_uid} 映射。
    抖音 /aweme/v1/web/im/user/info/ 返回的用户资料用来给 protobuf 会话水合昵称/头像。"""
    stack = [data]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if _looks_like_user(cur):
                uid = ""
                for k in _ID_KEYS:
                    if cur.get(k):
                        uid = str(cur[k]); break
                if uid:
                    nickname = ""
                    for k in _NAME_KEYS:
                        if cur.get(k):
                            nickname = str(cur[k]); break
                    prev = into.get(uid, {})
                    into[uid] = {
                        "nickname": nickname or prev.get("nickname", ""),
                        "avatar": _avatar_of(cur) or prev.get("avatar", ""),
                        "sec_uid": str(cur.get("sec_uid") or cur.get("secUid")
                                       or prev.get("sec_uid", "")),
                    }
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


_DM_PANEL_PROBE_JS = """() => {
  const txt = document.body.innerText || '';
  const cls = (e) => (e.className && e.className.toString) ? e.className.toString() : '';
  // 抖音的 class 是编译后的 hash(ASay5JTn),靠 class 正则永远匹配不到 —— 认 data-e2e
  const imish = [...document.querySelectorAll('div,section,aside')]
    .filter(e => /im[-_]|message|conversation|chat|私信/i.test(cls(e))
              || /im|message|conversation/i.test(e.getAttribute('data-e2e') || ''))
    .filter(e => { const r = e.getBoundingClientRect(); return r.width > 200 && r.height > 200; });
  return {
    url: location.pathname,
    iframes: document.querySelectorAll('iframe').length,
    // 大块的 im/会话 容器出现 = 面板真的渲染了
    panels: imish.length,
    panel_cls: imish.slice(0, 3).map(e => cls(e).slice(0, 40)),
    // 面板里才会出现的文案
    has_dm_text: /私信|发消息|暂无消息|陌生人消息/.test(txt),
    dialogs: document.querySelectorAll('[role="dialog"],[class*="modal"],[class*="mask"]').length,
  };
}"""


async def _dm_panel_probe(page) -> dict:
    """点击前后各拍一次:面板到底渲染了没。比反复猜「点没点中」有用。"""
    try:
        return await page.evaluate(_DM_PANEL_PROBE_JS)
    except Exception as e:
        return {"probe_failed": repr(e)}


async def _douyin_cookie_str(mgr: BrowserManager, identity) -> str:
    """从账号常驻 context 取 douyin.com 的 cookie 串(给 WS/直连用)。"""
    ctx = await mgr.context_for(identity)
    cks = await ctx.cookies("https://www.douyin.com")
    return "; ".join(f"{c['name']}={c['value']}" for c in cks)


async def _fetch_im_user_info(page, sec_ids: list) -> Dict[str, dict]:
    """在 douyin 页面内批量 POST /aweme/v1/web/im/user/info/(body sec_user_ids=[...]),
    抖音自己的 fetch 拦截器会补 a_bogus 签名。返回 sec_uid -> {nickname, avatar, sec_uid}。"""
    ids = list(dict.fromkeys([s for s in sec_ids if s]))   # 去重
    if not ids:
        return {}
    try:
        data = await page.evaluate(
            """async (ids) => {
              const out = [];
              for (let i=0; i<ids.length; i+=20) {
                const chunk = ids.slice(i, i+20);
                const body = 'sec_user_ids=' + encodeURIComponent(JSON.stringify(chunk));
                try {
                  const r = await fetch('/aweme/v1/web/im/user/info/', {
                    method:'POST', credentials:'include',
                    headers:{'content-type':'application/x-www-form-urlencoded'}, body});
                  const j = await r.json();
                  if (j && Array.isArray(j.data)) out.push(...j.data);
                } catch (e) {}
              }
              return out;
            }""", ids)
    except Exception as e:
        print(f"[dm-userinfo] in-page fetch failed: {e!r}")
        return {}
    prof: Dict[str, dict] = {}
    for u in (data or []):
        if not isinstance(u, dict):
            continue
        sec = str(u.get("sec_uid") or u.get("secUid") or "")
        if not sec:
            continue
        nick = str(u.get("nickname") or u.get("alias_nickname") or "")
        avatar = _avatar_of(u)
        prof[sec] = {"nickname": nick, "avatar": avatar, "sec_uid": sec}
    print(f"[dm-userinfo] batch fetched {len(prof)}/{len(ids)} profiles")
    return prof


async def fetch_dm_conversations(mgr: BrowserManager, identity, platform: str,
                                 settle_ms: int = 2600, max_scrolls: int = 8
                                 ) -> Tuple[List[dict], str]:
    """打开私信页,拦截会话列表 XHR,启发式抽会话。无公开接口,首版用于标定。"""
    url = _DM_NAV.get(platform, _DM_NAV["douyin"])
    host = {"douyin": "douyin.com", "xhs": "xiaohongshu.com",
            "kuaishou": "kuaishou.com"}.get(platform, "douyin.com")
    collected: Dict[str, dict] = {}
    hit_urls: list = []
    api_seen: list = []
    dm_samples: list = []        # 命中私信接口的响应体样本(标定关键:看真实字段)
    dm_seen_paths: Set[str] = set()   # 同一接口只采一份样本,别被重复请求占满
    im_visible: Optional[bool] = None  # 小红书 message/entry 的 visible(false=网页端私信未开放)
    ws_urls: list = []           # 标定:抖音私信可能走 frontier-im WebSocket,XHR 拦截抓不到
    ws_frames: list = []         # 抓少量 WS 帧(仅 frontier-im),看会话/消息是不是走 WS 推
    im_hit = [False]             # IM 是否真的 bootstrap(点入口后据此确认,而非只看「点了」)
    dm_init_raw = [b""]          # 抖音:get_message_by_init 的 protobuf 大包(会话全在这)
    im_profiles: Dict[str, dict] = {}  # uid -> {nickname, avatar, sec_uid},来自 im/user/info JSON
    error = ""
    page = await mgr.new_page(identity, block_media=True)

    # ── WebSocket 探针(标定关键):若会话列表走 frontier-im WS,则 page.on("response")
    #    永远抓不到 —— 这一行日志能直接判定抖音私信的传输形态。
    def on_websocket(ws):
        ws_urls.append(ws.url)

        def _cap(payload, direction):
            # bytelink 是直播心跳,会把配额刷满(上轮 10 帧里 7 帧是它),挤掉 frontier-im
            noisy = "bytelink" in ws.url
            if len(ws_frames) >= 20 or (noisy and sum(
                    1 for f in ws_frames if "bytelink" in f) >= 3):
                return
            try:
                n = len(payload) if payload is not None else 0
            except Exception:
                n = -1
            ws_frames.append(f"{direction}[{n}] {ws.url.split('?')[0].split('//')[-1][:34]}")
        ws.on("framereceived", lambda p: _cap(p, "recv"))
        ws.on("framesent", lambda p: _cap(p, "sent"))

    page.on("websocket", on_websocket)

    async def on_response(resp):
        nonlocal im_visible
        u = resp.url
        if host not in u or resp.request.resource_type not in ("xhr", "fetch"):
            return
        path = u.split("?")[0].split(host)[-1]
        low = path.lower()
        if len(api_seen) < 100:
            api_seen.append(f"{resp.status} {path}")
        # IM 真正起来的信号:必须是「会话/消息数据」接口。
        # ⚠️ 别用宽泛的 "/aweme/v1/web/im/" —— im/strategy/config、im/resources/emoticon/trending、
        # im/user/active/status 在任意页面加载时都会发,拿它当信号会误判成「面板已打开」。
        if any(s in low for s in _IM_BOOTSTRAP_SIGNALS):
            im_hit[0] = True
        # 抖音会话大包:get_message_by_init(protobuf)。留最大的一份(全量那次)。
        if platform == "douyin" and "get_message_by_init" in low:
            try:
                b = await resp.body()      # body() 内部已等 body 下完,无需 finished()
            except Exception as e:
                b = b""
                print(f"[dm-init] body 读取失败: {e!r}")
            # 一次同步会收到多份(增量包),只在换成更大的那份时打日志
            if len(b) > len(dm_init_raw[0]):
                dm_init_raw[0] = b
                print(f"[dm-init] get_message_by_init kept={len(b)} bytes")
        # 私信接口可能是 protobuf:先取文本,JSON 解析失败也留个样本(标注 non-json)
        raw_text = ""
        try:
            raw_text = await resp.text()
        except Exception:
            pass
        try:
            data = await resp.json()
        except Exception:
            data = None
        # 抖音:im/user/info(JSON)带用户资料,建 uid->昵称/头像映射给 protobuf 会话水合
        if platform == "douyin" and "/aweme/v1/web/im/user/info/" in low \
                and data is not None:
            _harvest_users(data, im_profiles)
        # 小红书 message/entry 的 visible 决定网页端私信是否开放(false=未开放,仅 App)
        if "/api/im/web/message/entry" in low and isinstance(data, dict):
            v = (data.get("data") or {}).get("visible")
            if isinstance(v, bool):
                im_visible = v
        # 命中私信接口留样本。小红书标定期:采样所有 /api/im/web/*(含 message/entry ——
        # 上轮它是唯一发出的 im 接口,其响应体可能就含会话/入口线索),仅排除表情/版本噪声。
        _im_calib = ("/api/im/web/" in low
                     and not any(n in low for n in ("version", "redmoji")))
        if (any(h in low for h in _DM_URL_HINTS) or _im_calib) \
                and low not in dm_seen_paths and len(dm_samples) < 10:
            dm_seen_paths.add(low)
            if data is not None:
                dm_samples.append(low + " => " + str(data)[:1400])
            else:
                # protobuf 二进制:resp.text() 对二进制会抛异常(记成 len=0 是假象),
                # 必须用 resp.body() 拿 bytes;base64 前 600 字节留样,离线解 protobuf 结构。
                import base64 as _b64
                try:
                    body = await resp.body()
                except Exception:
                    body = b""
                dm_samples.append(
                    f"{low} => [PROTOBUF,len={len(body)}] b64:"
                    + _b64.b64encode(body[:600]).decode())
        if data is None:
            return
        # 会话列表:找含 conversation/last_message 字段的数组
        before = len(collected)
        stack = [data]
        while stack:
            cur = stack.pop()
            if isinstance(cur, list):
                for x in cur:
                    c = _norm_conv(x) if isinstance(x, dict) else None
                    if c:
                        collected[c["conv_id"]] = c
                    if isinstance(x, (list, dict)):
                        stack.append(x)
            elif isinstance(cur, dict):
                stack.extend(cur.values())
        if len(collected) > before and path not in hit_urls:
            hit_urls.append(path)

    # IM 会话/消息接口(SDK 初始化几秒后才发,且需打开私信抽屉才会拉全)
    _DM_WAIT = ("/v1/message/", "/v1/stranger/", "get_conversation_list",
                "conversation", "/imapi/", "/api/im/web/chat", "/api/im/web/session",
                "/api/im/web/msg")
    # 私信入口候选(图标/抽屉,改版时改这里)
    _DM_ENTRY = ['[data-e2e="im-entry"]', '[data-e2e="message-entry"]',
                 'a[href*="message"]', 'a[href*="/im"]', '[class*="im-entry"]',
                 '[class*="message"]', 'text=私信', 'text=消息']

    page.on("response", on_response)
    # 私信入口可能 window.open 到新标签页(pcim_saas 是独立微应用)。不监听的话:
    # 原 page 的 DOM/URL/XHR 全都不动 —— 看起来就像「点击没生效」,其实是在看错的页。
    popups: List = []

    def on_popup(p):
        if p is page:                     # context 是账号常驻的,别把主页面/别处开的页收进来
            return
        popups.append(p)
        p.on("response", on_response)     # 新页的 XHR 也要拦
        print(f"[dm] douyin popup opened: {p.url!r}")

    page.context.on("page", on_popup)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if "passport" in page.url or "/login" in page.url:
            return [], "logged_out:登录态失效,请重新登录"
        # 上一轮实测:页面没 hydrate 完就点(只有 6 个 XHR、frontier WS 都没连),
        # handler 还没绑上,clickable=True 只是 CSS cursor 的假象。先等网络静默。
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            await page.wait_for_timeout(3000)
        # 标定:枚举页面上所有像「私信/消息」的可点元素(真实 DOM),用来校准入口选择器,
        # 而不是继续盲猜。抖音私信入口是顶栏信封图标(多为 SVG + aria-label,无文字)。
        if platform == "douyin":
            try:
                probe = await page.evaluate(
                    "() => [...document.querySelectorAll('a,button,div,span,li,i')]"
                    ".map(e => ({t:e.tagName, txt:(e.textContent||'').trim().slice(0,10),"
                    " href:e.getAttribute&&e.getAttribute('href'), aria:e.getAttribute&&e.getAttribute('aria-label'),"
                    " cls:(e.className&&e.className.toString?e.className.toString():'').slice(0,40),"
                    " de:e.getAttribute&&e.getAttribute('data-e2e')}))"
                    ".filter(o => /私信|消息|message|\\/im|im-|conversation/i.test("
                    "  [o.txt,o.href,o.aria,o.cls,o.de].join(' ')))"
                    ".slice(0,25)")
                print(f"[dm-probe] douyin entry candidates({len(probe)}): {probe}")
            except Exception as e:
                print(f"[dm-probe] douyin probe failed: {e!r}")
        # 抖音:「消息」是 <div>(无 href),React onClick 绑在祖先上,合成 element.click()
        # 不触发。改用真人式坐标点击:定位可点祖先→hover→page.mouse.click,外层容器优先,
        # 每次确认 IM 是否真的 bootstrap(im_hit);再加 JS 点祖先链兜底。
        if platform == "douyin":
            via = ""
            boxes = []
            # 实测:hydrate 完成后抖音会给入口打 data-e2e="im-entry"。优先用它,
            # 比坐标点击稳(坐标只是没有 data-e2e 时的兜底)。
            try:
                entry = page.locator('[data-e2e="im-entry"]').first
                if await entry.is_visible(timeout=2000):
                    await entry.click(timeout=3000)
                    for _ in range(10):
                        await page.wait_for_timeout(500)
                        if im_hit[0]:
                            break
                    if im_hit[0]:
                        via = 'data-e2e=im-entry'
            except Exception:
                pass
            try:
                boxes = await page.evaluate(_DOUYIN_IM_BOXES_JS)
            except Exception:
                boxes = []
            print(f"[dm-probe] douyin 消息 boxes({len(boxes or [])}): {boxes}")
            # 嵌套的「消息」DIV 常落在同一坐标,点 3 次和点 1 次等价 —— 去重,省 10s
            seen_pt: Set[tuple] = set()
            ordered = []
            for b in sorted(boxes or [], key=lambda b: (not b["clickable"], b["covered"])):
                pt = (int(b["x"]), int(b["y"]))
                if pt not in seen_pt:
                    seen_pt.add(pt)
                    ordered.append(b)
            before_dom = await _dm_panel_probe(page)
            for b in ordered:             # via 非空时 im_hit 必为真,这里就会跳过
                if im_hit[0]:
                    break
                try:
                    await page.mouse.move(b["x"], b["y"])
                    await page.wait_for_timeout(150)
                    await page.mouse.click(b["x"], b["y"])
                    for _ in range(7):
                        await page.wait_for_timeout(500)
                        if im_hit[0]:
                            break
                    if im_hit[0]:
                        via = f"mouse@{int(b['x'])},{int(b['y'])}({b['tag']})"
                except Exception:
                    continue
            # 点击到底做了什么?没有这个对比,只能反复猜「点没点中」
            after_dom = await _dm_panel_probe(page)
            print(f"[dm-probe] douyin dom before={before_dom}")
            print(f"[dm-probe] douyin dom after ={after_dom}")
            if popups:
                # 私信开在了新标签页:后续等待/滚动/取资料都必须对着它做
                page = popups[-1]
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=15000)
                except Exception:
                    pass
                via = via or f"popup:{page.url.split('?')[0]}"
                print(f"[dm] douyin switched to popup url={page.url} "
                      f"dom={await _dm_panel_probe(page)}")
            if not im_hit[0]:
                try:
                    await page.evaluate(
                        # 取最内层(.pop())——外层 wrapper 上通常没绑 handler
                        "() => { const hs=[...document.querySelectorAll('div,span,a,button,li')]"
                        ".filter(e => (e.textContent||'').trim()==='消息'); let t=hs.pop();"
                        " for(let d=0;d<5&&t;d++,t=t.parentElement){ try{t.click();}catch(e){} } }")
                    for _ in range(8):
                        await page.wait_for_timeout(500)
                        if im_hit[0]:
                            break
                    if im_hit[0]:
                        via = "js-clickchain"
                except Exception:
                    pass
            douyin_open_via = via
        # 打开私信抽屉/面板(站内图标里,点开才会拉会话列表);小红书私信走 /api/im/web REST。
        # 关键:入口是 flaky 的 —— 「消息」有多个同文本 DIV,.first 常点中非导航的文本节点。
        # 所以不信任单次点击,逐个可见候选点,每点后确认 IM 是否真的 bootstrap(im_hit),命中才停。
        if platform in ("kuaishou", "xhs"):
            clicked_by = ""
            for cand in _DM_ENTRY:
                if im_hit[0]:
                    break
                try:
                    loc = (page.get_by_text(cand[5:], exact=False)
                           if cand.startswith("text=") else page.locator(cand))
                    n = min(await loc.count(), 5)
                except Exception:
                    continue
                for i in range(n):
                    if im_hit[0]:
                        break
                    try:
                        el = loc.nth(i)
                        if not await el.is_visible():
                            continue
                        await el.click(timeout=3500)
                        # 确认:等 IM 流量出现(最多 ~3.5s),没起来就试下一个候选
                        for _ in range(7):
                            await page.wait_for_timeout(500)
                            if im_hit[0]:
                                break
                        if im_hit[0]:
                            clicked_by = f"{cand}#{i}"
                            break
                    except Exception:
                        continue
            print(f"[dm] {platform} entry clicked_by="
                  f"{clicked_by or 'NONE(IM未起来)'} im_hit={im_hit[0]}")
        # 小红书是 SPA:/im 直接 goto 会失败,但站内点「消息」链接是前端路由。
        # 用 JS 点指向 /im 的锚点做 in-app 跳转(比 Playwright locator 更稳),再等会话接口。
        if platform == "xhs":
            try:
                jumped = await page.evaluate(
                    "() => { const a = document.querySelector('a[href*=\"/im\"]')"
                    " || [...document.querySelectorAll('a,div,li')].find(e => /消息|私信/.test(e.textContent||'') && (e.textContent||'').trim().length<=4);"
                    " if (a) { a.click(); return (a.getAttribute&&a.getAttribute('href'))||'clicked'; } return ''; }")
                print(f"[dm] xhs im-entry jump={jumped!r}")
                await page.wait_for_timeout(2500)
            except Exception as e:
                print(f"[dm] xhs im-entry jump failed: {e!r}")
        # IM SDK 异步初始化,显式等会话/消息接口回包(最多 ~22s),而不是死等固定时间
        try:
            await page.wait_for_response(
                lambda r: any(h in r.url for h in _DM_WAIT) and r.status == 200,
                timeout=22000)
        except Exception:
            pass
        if platform == "douyin":
            # 打印放在等待之后:IM SDK 是慢启动的,点击那一刻 im_hit 必然还是 False
            print(f"[dm] douyin im open im_hit={im_hit[0]} "
                  f"via={douyin_open_via or 'FAIL'} boxes={len(boxes or [])} "
                  f"url={page.url}")
        await page.wait_for_timeout(settle_ms)
        for _ in range(max_scrolls):
            before = len(collected)
            try:
                await page.mouse.wheel(0, 2500)
                await page.evaluate(
                    "() => { let b=null,h=0; document.querySelectorAll('div,ul,section').forEach(e=>{"
                    " const s=getComputedStyle(e); if((s.overflowY==='auto'||s.overflowY==='scroll')"
                    " && e.scrollHeight>e.clientHeight+40 && e.scrollHeight>h){b=e;h=e.scrollHeight;}});"
                    " if(b)b.scrollTop=b.scrollHeight; }")
            except Exception:
                pass
            await page.wait_for_timeout(settle_ms)
            if len(collected) == before:
                break
        # wait_for_response 只等到响应头就返回,大包的 body 可能还在下载 —— 不等的话
        # 解析时 dm_init_raw 还是空的,看起来就像「包没来」。
        if platform == "douyin" and im_hit[0] and not dm_init_raw[0]:
            for _ in range(20):           # 最多 ~10s
                await page.wait_for_timeout(500)
                if dm_init_raw[0]:
                    break
            print(f"[dm-init] 等待 body 落地: len={len(dm_init_raw[0])}")
        # 标定用:把会话大包落盘,protobuf 字段号可以离线试,不用每次都跑浏览器
        _dump = os.environ.get("CREATORHUB_DM_DUMP")
        if _dump and dm_init_raw[0]:
            try:
                with open(_dump, "wb") as f:
                    f.write(dm_init_raw[0])
                print(f"[dm-init] 已落盘 {_dump} ({len(dm_init_raw[0])} bytes)")
            except Exception as e:
                print(f"[dm-init] 落盘失败: {e!r}")
        # 抖音:会话在 get_message_by_init 的 protobuf 大包里。解会话 → 页面内批量
        # POST im/user/info(按 sec_uid,抖音自己签名)补昵称/头像 → 水合。
        if platform == "douyin" and dm_init_raw[0]:
            try:
                convs_parsed = parse_conversations(dm_init_raw[0])
                # 页面内批量拉资料(sec_uid),补全 im/user/info 自然加载没覆盖到的
                sec_ids = [c["peer_sec_uid"] for c in convs_parsed
                           if c.get("peer_sec_uid")
                           and not im_profiles.get(c["peer_uid"])]
                sec_profiles = await _fetch_im_user_info(page, sec_ids)
                for c in convs_parsed:
                    prof = im_profiles.get(c["peer_uid"]) \
                        or sec_profiles.get(c["peer_sec_uid"]) or {}
                    last_meta = json.dumps({
                        "last_sender_uid": c["last_sender_uid"],
                        "self_uid": c["self_uid"],
                        "last_msg_type": c["last_msg_type"],
                    }, ensure_ascii=False)
                    collected[c["conv_id"]] = {
                        "conv_id": c["conv_id"],
                        "peer_uid": c["peer_uid"],
                        "peer_sec_uid": c["peer_sec_uid"] or prof.get("sec_uid", ""),
                        "peer_nickname": prof.get("nickname", ""),
                        "peer_avatar": prof.get("avatar", ""),
                        "last_text": c["last_text"],
                        "last_time": c.get("last_time") or 0,
                        "unread_count": 0,
                        "conv_short_id": c["conv_short_id"],
                        "ticket": c["ticket"],
                        "raw_json": last_meta,
                    }
                _named = sum(1 for v in collected.values() if v["peer_nickname"])
                print(f"[dm-pb] douyin parsed={len(collected)} "
                      f"im_profiles={len(im_profiles)} sec_profiles={len(sec_profiles)} "
                      f"named={_named}")
                # named 远小于 profiles 总数 = 键对不上,而不是「没拉到资料」。
                # 分别统计三条水合路径的命中数,定位是哪条断了。
                _with_sec = sum(1 for c in convs_parsed if c.get("peer_sec_uid"))
                _by_uid = sum(1 for c in convs_parsed if im_profiles.get(c["peer_uid"]))
                _by_sec = sum(1 for c in convs_parsed
                              if sec_profiles.get(c.get("peer_sec_uid") or "\0"))
                print(f"[dm-pb] convs={len(convs_parsed)} with_sec={_with_sec} "
                      f"hydrated_by_uid={_by_uid} hydrated_by_sec={_by_sec}")
                print(f"[dm-pb] im_profiles keys sample={list(im_profiles)[:3]} "
                      f"peer_uid sample={[c['peer_uid'] for c in convs_parsed[:3]]}")
            except Exception as e:
                print(f"[dm-pb] douyin protobuf parse failed: {e!r}")

        final_url = page.url
    except Exception as e:
        error = f"打开私信页失败: {e!r}"
        final_url = ""
    finally:
        try:
            page.context.remove_listener("page", on_popup)
        except Exception:
            pass
        for p in ([page] + popups):      # popup 也要关,否则残留标签页拖慢下个账号
            try:
                if not p.is_closed():
                    await p.close()
            except Exception:
                pass
    # 标定期:无论成败都打日志(私信接口完全未知,这行最关键)
    print(f"[dm] convs platform={platform} got={len(collected)} im_visible={im_visible} "
          f"hit_urls={hit_urls[:8]} final_url={final_url} api_seen({len(api_seen)})={api_seen[:60]}")
    print(f"[dm-ws] ws_urls({len(ws_urls)})={ws_urls[:10]} frames={ws_frames}")
    for i, smp in enumerate(dm_samples):
        print(f"[dm-sample {i}] {smp}")
    if not collected:
        # 小红书:message/entry 返回 visible=false —— 该账号网页端私信未开放(仅 App),非本项目可解
        if platform == "xhs" and im_visible is False:
            error = ("小红书网页端未开放私信(entry visible=false),仅手机 App 可用;"
                     "无法在网页端同步或收发。")
        else:
            error = error or "未拦截到会话(私信接口未知,需标定/或登录态失效)"
    return list(collected.values()), ("" if collected else error)


async def fetch_dm_history(mgr: BrowserManager, identity, platform: str,
                           conv_id: str, conv_short_id: str, conv_type: int = 1,
                           cursor: int = 0, count: int = 50,
                           debug: bool = False) -> Tuple[dict, str]:
    """无头抓单个会话的历史消息:imapi/v1/message/get_by_conversation(cmd 301)。
    该接口纯 cookie 鉴权、无 a_bogus/签名,用账号常驻 context 的 request 直接 POST。
    返回 (parse_messages 结果, error)。debug=True 时额外打印原始响应 b64(标定用)。"""
    import base64 as _b64
    from .douyin_im_pb import build_history_request, parse_messages, GET_BY_CONV_URL
    if platform != "douyin":
        return {}, "仅抖音已支持"
    if not conv_id or not conv_short_id:
        return {}, "缺 conv_id / conv_short_id"
    try:
        ctx = await mgr.context_for(identity)      # 常驻无头 context(带 cookie)
        req = build_history_request(conv_id, int(conv_type or 1),
                                    int(conv_short_id), int(cursor or 0), count)
        resp = await ctx.request.post(
            GET_BY_CONV_URL, data=req,
            headers={"content-type": "application/x-protobuf",
                     "referer": "https://www.douyin.com/"})
        body = await resp.body()
        parsed = parse_messages(body)
        if debug:
            print(f"[dm-hist] conv={conv_id} status={resp.status} "
                  f"resp_len={len(body)} msgs={len(parsed.get('messages', []))} "
                  f"next_cursor={parsed.get('next_cursor')}")
            print(f"[dm-hist-raw] b64:{_b64.b64encode(body[:1200]).decode()}")
            # 非文本消息(分享视频=8/图片=27/语音=17...)dump 原始 content JSON,标定字段用
            for _m in parsed.get("messages", []):
                if _m.get("msg_type") not in (7, 0):
                    print(f"[dm-hist-content] type={_m.get('msg_type')} "
                          f"text={_m.get('text')!r} content={_m.get('content')}")
        if resp.status != 200:
            return parsed, f"imapi 返回 {resp.status}"
        return parsed, ""
    except Exception as e:
        return {}, f"抓历史失败: {e!r}"


# ═══════════ 写操作(无公开接口:登录态浏览器 UI 自动化) ═══════════
_FOLLOW_BTN = [
    'button:has-text("关注")', 'div[role=button]:has-text("关注")',
    'span:has-text("关注")', '.follow-button', '[data-e2e="user-info-follow"]',
]
_UNFOLLOW_BTN = [
    'button:has-text("已关注")', 'button:has-text("相互关注")',
    'div[role=button]:has-text("已关注")', 'span:has-text("已关注")',
    'span:has-text("取消关注")',
]
_PROFILE_URL = {
    "douyin": "https://www.douyin.com/user/{sec}",
    "xhs": "https://www.xiaohongshu.com/user/profile/{uid}",
    "kuaishou": "https://www.kuaishou.com/profile/{uid}",
}


async def _open_target_profile(mgr, identity, platform, target_uid, target_sec_uid):
    sec = target_sec_uid or target_uid
    url = _PROFILE_URL.get(platform, _PROFILE_URL["douyin"])
    url = url.format(sec=sec, uid=target_uid or sec)
    ctx = await mgr.open_headed(identity)
    page = await ctx.new_page()
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    await page.wait_for_timeout(1800)
    return ctx, page


async def do_follow(mgr: BrowserManager, identity, platform: str, target_uid: str = "",
                    target_sec_uid: str = "", unfollow: bool = False
                    ) -> Tuple[bool, str]:
    """打开目标主页,点「关注 / 已关注」按钮(UI 自动化,有头窗口更稳)。"""
    ctx = None
    try:
        ctx, page = await _open_target_profile(mgr, identity, platform,
                                               target_uid, target_sec_uid)
        if "passport" in page.url or "/login" in page.url:
            return False, "logged_out:账号未登录"
        sels = _UNFOLLOW_BTN if unfollow else _FOLLOW_BTN
        for sel in sels:
            try:
                btn = page.locator(sel).first
                if await btn.count() and await btn.is_visible():
                    await btn.click(timeout=4000)
                    await page.wait_for_timeout(1500)
                    if unfollow:   # 取关常有二次确认
                        for c in ("确定", "取消关注", "不再关注"):
                            try:
                                cc = page.get_by_text(c, exact=False).last
                                if await cc.count() and await cc.is_visible():
                                    await cc.click(timeout=2500)
                                    break
                            except Exception:
                                continue
                    return True, ""
            except Exception:
                continue
        return False, ("未找到「" + ("已关注" if unfollow else "关注") +
                       "」按钮(可能已是目标状态/页面改版)")
    except Exception as e:
        return False, f"{'取关' if unfollow else '关注'}异常: {e!r}"
    finally:
        try:
            if ctx is not None:
                await ctx.close()
        except Exception:
            pass


_DM_INPUT = [
    'textarea[placeholder*="发送"]', 'div[contenteditable="true"][placeholder*="发送"]',
    'textarea[placeholder*="私信"]', 'div[contenteditable="true"]',
    'textarea', 'input[type="text"]',
]
_DM_SEND = ['button:has-text("发送")', 'span:has-text("发送")', '.send-btn']
_DM_ENTRY_URL = {
    "douyin": "https://www.douyin.com/user/{sec}",
    "xhs": "https://www.xiaohongshu.com/user/profile/{uid}",
    "kuaishou": "https://www.kuaishou.com/profile/{uid}",
}


_SEND_URL = "https://imapi.douyin.com/v1/message/send"


async def send_dm_api(mgr: BrowserManager, identity, conv_id: str,
                      conv_short_id: str, ticket: str, text: str,
                      conv_type: int = 1) -> Tuple[bool, str]:
    """抖音无头发私信:imapi/v1/message/send(cmd 100),cookie POST(先按零签名试,
    与读/ mark_read 一致)。需已有会话的 conv_id + short_id + ticket(同步会话列表时已存库)。"""
    import time
    import uuid as _uuid
    from .douyin_im_pb import build_send_request, parse_send_response
    text = (text or "").strip()
    if not text:
        return False, "空内容"
    if not (conv_id and conv_short_id and ticket):
        return False, "缺 conv_id/short_id/ticket(先同步会话列表)"
    try:
        ctx = await mgr.context_for(identity)
        cmid = str(_uuid.uuid4())
        stime = int(time.time() * 1000)
        req = build_send_request(conv_id, int(conv_type or 1), int(conv_short_id),
                                 ticket, text, cmid, stime)
        resp = await ctx.request.post(
            _SEND_URL, data=req,
            headers={"content-type": "application/x-protobuf",
                     "referer": "https://www.douyin.com/"})
        body = await resp.body()
        r = parse_send_response(body)
        print(f"[dm-send] conv={conv_id} status={resp.status} "
              f"ok={r['ok']} msg={r['msg']!r} code={r['error_code']} resp_len={len(body)}")
        if resp.status == 200 and r["ok"]:
            return True, ""
        return False, f"发送被拒 status={resp.status} msg={r['msg']} code={r['error_code']}"
    except Exception as e:
        return False, f"发送失败: {e!r}"


async def send_dm(mgr: BrowserManager, identity, platform: str, target_uid: str = "",
                  target_sec_uid: str = "", text: str = "") -> Tuple[bool, str]:
    """给目标发私信(UI 自动化):打开对方主页 → 点「私信」→ 输入 → 发送。
    ⚠️ 各平台私信入口/选择器差异大,首版尽力而为,失败有诊断。"""
    text = (text or "").strip()
    if not text:
        return False, "空内容"
    sec = target_sec_uid or target_uid
    url = _DM_ENTRY_URL.get(platform, _DM_ENTRY_URL["douyin"]).format(
        sec=sec, uid=target_uid or sec)
    ctx = None
    try:
        ctx = await mgr.open_headed(identity)
        page = await ctx.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(1800)
        if "passport" in page.url or "/login" in page.url:
            return False, "logged_out:账号未登录"
        # 点开「私信」入口
        opened = False
        for label in ("私信", "发消息", "发私信"):
            try:
                el = page.get_by_text(label, exact=False).first
                if await el.count():
                    await el.click(timeout=4000)
                    opened = True
                    await page.wait_for_timeout(1500)
                    break
            except Exception:
                continue
        editor = None
        for sel in _DM_INPUT:
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    editor = loc
                    break
            except Exception:
                continue
        if editor is None:
            return False, ("未找到私信输入框(私信入口可能需手动打开/页面改版)。"
                           f"opened_entry={opened}")
        await editor.click(timeout=8000)
        await page.keyboard.type(text, delay=35)
        await page.wait_for_timeout(500)
        sent = False
        for sel in _DM_SEND:
            try:
                btn = page.locator(sel).first
                if await btn.count() and await btn.is_enabled():
                    await btn.click(timeout=3000)
                    sent = True
                    break
            except Exception:
                continue
        if not sent:
            try:
                await page.keyboard.press("Enter")
                sent = True
            except Exception:
                pass
        await page.wait_for_timeout(1200)
        return (sent, "" if sent else "未找到发送方式")
    except Exception as e:
        return False, f"发私信异常: {e!r}"
    finally:
        try:
            if ctx is not None:
                await ctx.close()
        except Exception:
            pass
