"""账号中心浏览器抓取通用数值提取、Cookie 读取与用户结构归一化辅助。"""
from __future__ import annotations

from typing import List, Optional, Tuple
from urllib.parse import urljoin
import re
import sys

from app.browser.manager import BrowserManager


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
    "rednote": "https://www.rednote.com/zh",
    "kuaishou": "https://www.kuaishou.com/",
}


_SELF_LINK_JS = {
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
    if not home or (platform not in ("xhs", "rednote") and not js):
        return "", ""
    href = ""
    if platform in ("xhs", "rednote"):
        self_labels = {"我"} if platform == "xhs" else {"我", "Me", "Profile"}
        try:
            try:
                lease = mgr.visible_page(identity, foreground=False)
            except TypeError:  # compatibility with compact manager stubs
                lease = mgr.visible_page(identity)
            async with lease as page:
                await page.goto(
                    home, wait_until="domcontentloaded", timeout=30000)
                for _ in range(6):
                    anchors = page.locator('a[href*="/user/profile/"]')
                    count = min(await anchors.count(), 24)
                    sole = ""
                    for index in range(count):
                        anchor = anchors.nth(index)
                        if not await anchor.is_visible():
                            continue
                        candidate = urljoin(
                            home, await anchor.get_attribute("href") or "")
                        if count == 1:
                            sole = candidate
                        text = (await anchor.inner_text()).strip()
                        if text in self_labels:
                            href = candidate
                            break
                    href = href or sole
                    if href:
                        break
                    await mgr.xhs_interaction.pause(0.25, 0.55)
        except Exception:
            href = ""
        m = re.search(r"/user/profile/([0-9a-zA-Z_-]+)", href)
        uid = m.group(1) if m else ""
        print(f"[hub-self] platform={platform} href={href!r} uid={uid}")
        return href, uid

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


def _console_safe(value) -> str:
    """Return diagnostic text that is printable on Windows GBK consoles."""
    text = str(value)
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="backslashreplace").decode(encoding)


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
    sec_uid = str(d.get("sec_uid") or d.get("secUid") or "")
    if not uid or uid == "0":
        uid = sec_uid
    nickname = ""
    for k in _NAME_KEYS:
        if d.get(k):
            nickname = str(d[k]); break
    if not nickname and sec_uid:
        nickname = str(d.get("unique_id") or d.get("short_id") or "已重置用户")
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


async def _douyin_cookie_str(mgr: BrowserManager, identity) -> str:
    """从账号常驻 context 取 douyin.com 的 cookie 串(给 WS/直连用)。"""
    ctx = await mgr.context_for(identity)
    cks = await ctx.cookies("https://www.douyin.com")
    return "; ".join(f"{c['name']}={c['value']}" for c in cks)


async def _rednote_cookie_str(mgr: BrowserManager, identity) -> str:
    """从账号专属 context 或 storage_state 提取严格归属 rednote.com 的 Cookie 串。"""
    from app.platforms.rednote import cookie_str_from_state
    from app.platforms.rednote.domains import is_rednote_host

    try:
        ctx = await mgr.context_for(identity)
        cks = await ctx.cookies([
            "https://www.rednote.com",
            "https://webapi.rednote.com",
            "https://creator.rednote.com",
        ])
        if cks:
            merged = {
                c["name"]: c["value"]
                for c in cks
                if c.get("name")
                and c.get("value")
                and is_rednote_host(str(c.get("domain") or "").lstrip("."))
            }
            if merged:
                return "; ".join(f"{k}={v}" for k, v in merged.items())
    except Exception:
        pass
    for raw in (
        getattr(identity, "creator_storage_state", "") or "",
        getattr(identity, "storage_state", "") or "",
    ):
        s = cookie_str_from_state(raw)
        if s:
            return s
    return ""


async def _bili_cookie_str(mgr: BrowserManager, identity) -> str:
    """从账号专属 context 或 storage_state 提取 bilibili.com 完整 Cookie 串。"""
    from app.platforms.bilibili import cookie_str_from_state
    try:
        ctx = await mgr.context_for(identity)
        cks = await ctx.cookies([
            "https://www.bilibili.com",
            "https://member.bilibili.com",
            "https://message.bilibili.com",
        ])
        if cks:
            merged = {c["name"]: c["value"] for c in cks if c.get("name")}
            if merged.get("SESSDATA"):
                return "; ".join(f"{k}={v}" for k, v in merged.items())
    except Exception:
        pass
    return cookie_str_from_state(getattr(identity, "storage_state", "") or "")


async def _ig_cookie_str(mgr: BrowserManager, identity) -> str:
    """从账号专属 context 或 storage_state 提取 instagram.com 完整 Cookie 串。"""
    from app.platforms.instagram import cookie_str_from_state
    try:
        ctx = await mgr.context_for(identity)
        cks = await ctx.cookies(["https://www.instagram.com", "https://i.instagram.com"])
        if cks:
            merged = {c["name"]: c["value"] for c in cks if c.get("name")}
            if merged.get("sessionid"):
                return "; ".join(f"{k}={v}" for k, v in merged.items())
    except Exception:
        pass
    return cookie_str_from_state(getattr(identity, "storage_state", "") or "")


async def _twitter_cookie_str(mgr: BrowserManager, identity) -> str:
    """从账号专属 context 或 storage_state 提取 x.com 完整 Cookie 串。"""
    from app.platforms.twitter import cookie_str_from_state
    try:
        ctx = await mgr.context_for(identity)
        cks = await ctx.cookies(["https://x.com", "https://twitter.com"])
        if cks:
            merged = {c["name"]: c["value"] for c in cks if c.get("name")}
            if merged.get("auth_token"):
                return "; ".join(f"{k}={v}" for k, v in merged.items())
    except Exception:
        pass
    return cookie_str_from_state(getattr(identity, "storage_state", "") or "")
