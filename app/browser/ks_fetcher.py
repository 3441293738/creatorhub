"""快手 Web 抓取(浏览器自动化 + 拦截 GraphQL 响应),对标 douyin/fetcher.py。

快手 PC 站把数据都打在 POST https://www.kuaishou.com/graphql 上,前端拦截响应即可:
  - 打开 /profile/{userId}      -> visionProfile(资料) + visionProfilePhotoList(作品)
  - 打开 /short-video/{photoId} -> visionVideoDetail + visionCommentList(评论)
登录态写在该账号专属持久 profile 里(www 与 cp 同顶域 .kuaishou.com 共享 Cookie)。

⚠️ 全部解析走「多候选键兜底」。快手 GraphQL operationName / 字段随版本变,
   若拿不到数据,优先核对 _dig_* 里的候选键与下面的页面 URL/选择器。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Set, Tuple

from .identity import Identity
from .manager import BrowserManager

GRAPHQL_API = "/graphql"
# 评论现走 REST v2(实测):/rest/v/photo/comment/list 返回 rootCommentsV2
COMMENT_API = "/rest/v/photo/comment/"
# 作品列表现走 REST(实测 [ks_videos] 只见 /rest/v/profile/feed,无 graphql)
PROFILE_FEED_API = "/rest/v/profile/feed"
PROFILE_URL = "https://www.kuaishou.com/profile/{uid}"
PHOTO_URL = "https://www.kuaishou.com/short-video/{pid}"


def _rf(d: dict, *keys, default=""):
    for k in keys:
        v = (d or {}).get(k)
        if v not in (None, "", [], {}):
            return v
    return default


def _dig_rest_feeds(data: dict) -> list:
    """从 /rest/v/profile/feed 响应里挖出作品数组(feeds / list / data.feeds 兜底)。"""
    if not isinstance(data, dict):
        return []
    for key in ("feeds", "list", "photos"):
        v = data.get(key)
        if isinstance(v, list):
            return v
    d = data.get("data") or {}
    if isinstance(d, dict):
        for key in ("feeds", "list", "photos"):
            v = d.get(key)
            if isinstance(v, list):
                return v
    return []


def _dig_feeds(data: dict) -> list:
    """从 GraphQL 响应里挖出作品 feeds 数组(profile / search / 推荐都兜底)。"""
    d = (data or {}).get("data") or {}
    for key in ("visionProfilePhotoList", "visionSearchPhoto", "visionProfilePhotoListV2",
                "feeds", "brilliantTypeData"):
        v = d.get(key)
        if isinstance(v, dict) and isinstance(v.get("feeds"), list):
            return v["feeds"]
        if isinstance(v, list):
            return v
    return []


def _dig_profile(data: dict) -> Optional[dict]:
    """从 visionProfile 响应里挖出 userProfile。"""
    d = (data or {}).get("data") or {}
    vp = d.get("visionProfile") or {}
    up = vp.get("userProfile") if isinstance(vp, dict) else None
    return up if isinstance(up, dict) else None


def _dig_comments(data: dict) -> list:
    """从评论响应里挖出评论数组。兼容两套:
      - REST v2:顶层 rootCommentsV2 / subCommentsV2(现行)
      - 旧 GraphQL:data.visionCommentList.rootComments(兜底)"""
    if not isinstance(data, dict):
        return []
    for key in ("rootCommentsV2", "subCommentsV2", "rootComments"):
        v = data.get(key)
        if isinstance(v, list):
            return v
    d = data.get("data") or {}
    for key in ("visionCommentList", "commentListQuery"):
        v = d.get(key)
        if isinstance(v, dict):
            rc = v.get("rootComments") or v.get("commentList") or v.get("comments")
            if isinstance(rc, list):
                return rc
    return []


async def fetch_ks_videos(mgr: BrowserManager, identity: Identity, user_id: str,
                          known_ids: Set[str], max_scrolls: int = 12,
                          settle_ms: int = 1800, block_media: bool = True,
                          open_url: str = ""
                          ) -> Tuple[List[dict], Optional[dict], str]:
    """打开创作者主页并下滑,拦截 GraphQL 收集作品 feeds。
    返回 (新作品 feed 列表, 作者 userProfile dict, error)。
    open_url 非空时直接打开它(如站内「我」入口解析出的自己主页链接)。"""
    collected: Dict[str, dict] = {}
    author: Optional[dict] = None
    error = ""
    api_seen: list = []          # 诊断:看到的 GraphQL operation + 其它响应路径
    feed_sample: list = []       # 标定:REST feed 首个作品项样本(字段核对用)
    page = await mgr.new_page(identity, block_media)

    async def on_response(resp):
        nonlocal author
        # 快手作品现走 REST /rest/v/profile/feed(实测项已含嵌套 photo,同 graphql 形状);
        # 直接透传给下游 _norm_ks_work(读 feed.photo),老账号仍走 graphql,两路都收
        if PROFILE_FEED_API in resp.url:
            try:
                data = await resp.json()
            except Exception:
                return
            items = _dig_rest_feeds(data)
            for it in items:
                photo = it.get("photo") if isinstance(it, dict) else None
                if not isinstance(photo, dict):
                    continue
                pid = str(_rf(photo, "id", "photoId", "photo_id", default=""))
                if not pid:
                    continue
                collected[pid] = it       # 整项带 photo,交 _norm_ks_work 统一归一
                if not feed_sample:
                    feed_sample.append("photo=" + str(photo)[:1100])   # 核对 photo 字段名
                if author is None and it.get("author"):
                    author = {"profile": it["author"]}
            return
        if GRAPHQL_API in resp.url:
            # GraphQL 都打到 /graphql,operationName 在请求体里 —— 取出来看到底发了哪些查询
            op = ""
            try:
                pd = resp.request.post_data or ""
                m = re.search(r'"operationName"\s*:\s*"([^"]+)"', pd)
                op = m.group(1) if m else ""
            except Exception:
                pass
            if len(api_seen) < 40:
                api_seen.append(f"{resp.status} gql:{op or '?'}")
        elif "kuaishou.com" in resp.url and resp.request.resource_type in ("xhr", "fetch"):
            if len(api_seen) < 40:
                p = resp.url.split("?")[0].split("kuaishou.com")[-1]
                api_seen.append(f"{resp.status} {p}")
        if GRAPHQL_API not in resp.url:
            return
        try:
            data = await resp.json()
        except Exception:
            return
        for feed in _dig_feeds(data):
            photo = (feed.get("photo") or {}) if isinstance(feed, dict) else {}
            pid = str(photo.get("id") or feed.get("id") or "")
            if pid:
                collected[pid] = feed
                if author is None and feed.get("author"):
                    author = {"profile": feed["author"]}
        up = _dig_profile(data)
        if up and author is None:
            author = up

    page.on("response", on_response)
    final_url = ""
    try:
        await page.goto(open_url or PROFILE_URL.format(uid=user_id),
                        wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(settle_ms)
        stagnant = 0
        for _ in range(max_scrolls):
            if known_ids & set(collected.keys()):     # 翻到旧内容了
                break
            before = len(collected)
            await page.mouse.wheel(0, 4000)
            await page.wait_for_timeout(settle_ms)
            if len(collected) == before:
                stagnant += 1
                if stagnant >= 2:
                    break
            else:
                stagnant = 0
        final_url = page.url
        if not collected:
            if "passport" in final_url or "/login" in final_url:
                error = "logged_out:登录态失效,请重新登录"
            else:
                error = "未拦截到作品数据(可能未登录/被风控/该用户无公开作品)"
    except Exception as e:
        error = f"打开主页失败: {e!r}"
    finally:
        try:
            await page.close()
        except Exception:
            pass

    if not collected:
        print(f"[ks_videos] user_id={user_id!r} open_url={open_url!r} "
              f"final_url={final_url} api_seen({len(api_seen)})={api_seen[:40]}")
    # REST feed 命中但可能字段没对齐:打样本便于核对 _rest_item_to_feed
    if feed_sample:
        print(f"[ks_videos] feed_sample={feed_sample[0]}")

    new_items = [feed for pid, feed in collected.items() if pid not in known_ids]
    return new_items, author, error


async def fetch_ks_comments(mgr: BrowserManager, identity: Identity, photo_id: str,
                            known_cids: Set[str], max_scrolls: int = 6,
                            settle_ms: int = 1600, block_media: bool = True
                            ) -> Tuple[List[dict], str]:
    """打开作品详情页,滚动评论区翻页,拦截评论 GraphQL 收集评论原始 JSON。
    返回 (新评论原始列表, error)。"""
    collected: Dict[str, dict] = {}
    error = ""
    page = await mgr.new_page(identity, block_media)

    async def on_response(resp):
        if COMMENT_API not in resp.url and GRAPHQL_API not in resp.url:
            return
        try:
            data = await resp.json()
        except Exception:
            return
        for c in _dig_comments(data):
            cid = str((c or {}).get("comment_id") or (c or {}).get("commentId")
                      or (c or {}).get("id") or "")
            if cid:
                collected[cid] = c

    page.on("response", on_response)
    try:
        await page.goto(PHOTO_URL.format(pid=photo_id),
                        wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(settle_ms)
        stagnant = 0
        for _ in range(max_scrolls):
            before = len(collected)
            try:
                await page.evaluate("() => window.scrollBy(0, document.body.scrollHeight)")
            except Exception:
                pass
            await page.wait_for_timeout(settle_ms)
            if len(collected) == before:
                stagnant += 1
                if stagnant >= 2:
                    break
            else:
                stagnant = 0
    except Exception as e:
        error = f"打开作品页失败: {e!r}"
    finally:
        try:
            await page.close()
        except Exception:
            pass

    if not collected and not error:
        error = "未拦截到评论(可能未登录/评论区未加载/作品无评论)"
    new = [c for cid, c in collected.items() if cid not in known_cids]
    return new, error


_SELF_LINK_RE = re.compile(r"/profile/([0-9a-zA-Z_\-]+)")

# 快手 visionProfile 查询(实测标定,字段与 _dig_profile/parse_self_user 对齐)
_VISION_PROFILE_QUERY = (
    "query visionProfile($userId: String) {\n"
    "  visionProfile(userId: $userId) {\n"
    "    result\n"
    "    hostName\n"
    "    userProfile {\n"
    "      ownerCount { fan photo follow photo_public __typename }\n"
    "      profile { gender user_name user_id headurl user_text __typename }\n"
    "      isFollowing\n"
    "      __typename\n"
    "    }\n"
    "    __typename\n"
    "  }\n"
    "}\n"
)

# 在已登录页面里直接 POST graphql(同源带 Cookie,快手 graphql 无需签名)。
# 传 {op, query, variables},返回解析后的 JSON。
_KS_GQL_JS = """
async (v) => {
  try {
    const r = await fetch("https://www.kuaishou.com/graphql", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "include",
      body: JSON.stringify({
        operationName: v.op,
        variables: v.variables,
        query: v.query,
      }),
    });
    return await r.json();
  } catch (e) { return { __err: String(e) }; }
}
"""

# 登录后取「本人」资料的 REST 接口(实测 hit:含本人 userId)。先 GET,失败再 POST。
_KS_REST_PROFILE_JS = """
async () => {
  const url = "https://www.kuaishou.com/rest/v/profile/get";
  for (const m of ["GET", "POST"]) {
    try {
      const opt = { method: m, credentials: "include" };
      if (m === "POST") { opt.headers = {"Content-Type":"application/json"}; opt.body = "{}"; }
      const r = await fetch(url, opt);
      const j = await r.json();
      if (j && (j.result === 1 || j.userProfile || j.profile || j.user || j.data)) {
        j.__method = m; return j;
      }
      j.__method = m; j.__nohit = true; return j;
    } catch (e) { /* try next method */ }
  }
  return { __err: "profile/get fetch failed" };
}
"""


def _extract_rest_profile(data: dict):
    """从 /rest/v/profile/get 响应里抽出 userProfile 结构(喂给 parse_self_user)。
    快手该接口字段未知,这里多形状兜底;并把扁平计数归并进 ownerCount。"""
    if not isinstance(data, dict):
        return None, []
    # 1) 已是 graphql 形状
    up = data.get("userProfile")
    if isinstance(up, dict) and up.get("profile"):
        return up, list(up.keys())
    # 2) 找像 profile 的容器
    NAME_KEYS = ("user_name", "userName", "name", "nickName", "nickname")
    for key in ("profile", "user", "userInfo", "data", "owner"):
        c = data.get(key)
        if isinstance(c, dict) and any(k in c for k in NAME_KEYS):
            oc = (c.get("ownerCount") or data.get("ownerCount")
                  or data.get("counts") or {})
            # 扁平计数兜底
            flat = {k: c.get(k) or data.get(k) for k in
                    ("fan", "photo", "photo_public", "follow", "fanCount",
                     "photoCount", "follower")}
            merged = {**{k: v for k, v in flat.items() if v is not None}, **oc}
            return {"profile": c, "ownerCount": merged}, list(c.keys())
    # 3) 顶层就是扁平 profile
    if any(k in data for k in NAME_KEYS):
        return ({"profile": data, "ownerCount": data.get("ownerCount") or {}},
                list(data.keys()))
    return None, list(data.keys())


def _harvest_self(node, uid: str, out: list, depth: int = 0):
    """在任意 JSON 里找"含本人数字 userId 的那个对象",抽出 昵称/头像/真实 3x user_id。
    依据:uid(cookie 里的数字 userId)确凿是本人,谁的字段里带它,谁就是自己。"""
    if depth > 8 or out or not uid:
        return
    if isinstance(node, dict):
        # 本对象的标量值里是否出现本人 uid
        hit = any(str(v) == uid for v in node.values()
                  if isinstance(v, (str, int)))
        if hit:
            name = (node.get("user_name") or node.get("userName") or node.get("name")
                    or node.get("nickName") or node.get("nickname") or "")
            head = (node.get("headurl") or node.get("headerUrl") or node.get("avatar")
                    or node.get("headUrl") or "")
            if isinstance(head, list):
                head = (head[0].get("url") if head and isinstance(head[0], dict)
                        else (head[0] if head else "")) or ""
            u3 = (node.get("eid") or node.get("encryptedUserId") or "")
            uid_field = node.get("user_id") or node.get("userId") or node.get("id") or ""
            # user_id 形如 3x... 才是 web profile id;纯数字则不是
            if isinstance(uid_field, str) and uid_field.startswith("3x"):
                u3 = u3 or uid_field
            if name or head or u3:
                out.append({"profile": {"user_name": name, "headurl": head,
                                        "user_id": u3}})
                return
        for v in node.values():
            _harvest_self(v, uid, out, depth + 1)
    elif isinstance(node, list):
        for v in node:
            _harvest_self(v, uid, out, depth + 1)


async def fetch_ks_self_profile(mgr: BrowserManager, identity: Identity,
                                timeout_ms: int = 15000, block_media: bool = False
                                ) -> Tuple[dict, str]:
    """拿登录账号真实资料。难点:数字 userId 不被 visionProfile 接受,/profile 通配会 404,
    首页 DOM 链接多是推荐位(会抓成别人)。可靠做法:登录 cookie 里的数字 userId 确凿是本人,
    拦截首页所有响应,找出"正文里出现该 userId"的那个响应,从中抽出本人昵称/头像/真实 3x id,
    再用 3x id 调 visionProfile 拿完整粉丝/作品数。
    返回 (userProfile dict, error)。error == "logged_out" 表示登录态失效。"""
    result: dict = {}
    error = ""
    page = await mgr.new_page(identity, block_media)
    logged_out = False
    has_login_btn = None
    attempts: List[str] = []

    # 先取 cookie(在导航前就绪,这样响应监听里能用 uid 匹配)
    uid_cookie = ""
    try:
        cks0 = await page.context.cookies()
        names0 = {c["name"] for c in cks0}
        uid_cookie = next((c.get("value", "") for c in cks0 if c["name"] == "userId"), "")
    except Exception:
        names0 = set()
    if "passToken" not in names0 and not uid_cookie:
        logged_out = True

    hits: list = []          # 含本人 uid 的响应里抽出的 profile 候选
    hit_urls: list = []      # 命中响应的 URL(诊断用)

    async def on_response(resp):
        if not uid_cookie or len(hits) >= 3:
            return
        rt = resp.request.resource_type
        if rt not in ("xhr", "fetch", "document"):
            return
        try:
            txt = await resp.text()
        except Exception:
            return
        if uid_cookie not in txt:
            return
        hit_urls.append(resp.url.split("?")[0])
        try:
            _harvest_self(json.loads(txt), uid_cookie, hits)
        except Exception:
            pass

    page.on("response", on_response)

    async def _vp(uid: str):
        try:
            data = await page.evaluate(_KS_GQL_JS, {
                "op": "visionProfile", "query": _VISION_PROFILE_QUERY,
                "variables": {"userId": str(uid)}})
        except Exception as e:
            attempts.append(f"vp({uid}):eval_err({e!r})")
            return None
        up = _dig_profile(data)
        uname = ((up or {}).get("profile") or {}).get("user_name") or ""
        attempts.append(f"vp({uid}):name={uname or '-'}"
                        + (f",errors={data.get('errors')}" if (data or {}).get("errors") else ""))
        return up if (up and up.get("profile")) else None

    try:
        await page.goto("https://www.kuaishou.com", wait_until="domcontentloaded",
                        timeout=30000)
        await page.wait_for_timeout(3000)     # 等头部自身信息接口回来
        try:
            has_login_btn = await page.get_by_text(
                "登录", exact=True).first.is_visible(timeout=1200)
        except Exception:
            has_login_btn = None

        # ① 主路径:直接调本人资料 REST 接口 /rest/v/profile/get
        self3x = ""
        try:
            rp = await page.evaluate(_KS_REST_PROFILE_JS)
        except Exception as e:
            rp = {"__err": repr(e)}
        up, top_keys = _extract_rest_profile(rp or {})
        attempts.append(f"rest:method={(rp or {}).get('__method')},top_keys={top_keys[:12]}"
                        + (f",err={rp.get('__err')}" if (rp or {}).get("__err") else ""))
        if up and up.get("profile"):
            pf = up["profile"]
            result = up
            # 真实 web profile id 是 3x... 形式,可能落在 user_id / eid / kwaiId 任一字段,
            # 直接扫描所有字符串值找 3x 开头的那个
            self3x = next((str(v) for v in pf.values()
                           if isinstance(v, str) and v.startswith("3x")), "")
            attempts.append(f"rest_profile:name={pf.get('user_name') or pf.get('userName') or pf.get('name') or '-'},"
                            f"3x={self3x or '-'},pf_keys={list(pf.keys())[:14]}")

        # ② 用真实 3x id 调 visionProfile 拿权威资料(含正确头像 + 粉丝/作品数,字段对齐)
        if self3x:
            full = await _vp(self3x)
            if full and full.get("profile"):
                result = full      # graphql 字段与 parse_self_user 完全对齐(头像=headurl)

        # ③ 兜底:用"含本人 uid 的响应"里 harvest 出的 3x id / 资料
        if not result and hits:
            best = next((h for h in hits
                         if (h["profile"].get("user_id") or "").startswith("3x")), hits[0])
            h3x = best["profile"].get("user_id") or ""
            attempts.append(f"harvest:name={best['profile'].get('user_name') or '-'},"
                            f"3x={h3x or '-'},urls={hit_urls[:4]}")
            if h3x:
                full = await _vp(h3x)
                if full:
                    result = full
            if not result and (best["profile"].get("user_name")
                               or best["profile"].get("headurl")):
                result = best
        if has_login_btn is True:
            # uid/passToken 可能仍保存在本地但已被服务端撤销；真实页面显示登录时
            # 不接受 REST、GraphQL 或首页缓存中的旧资料。
            result = {}
            logged_out = True
    except Exception as e:
        error = f"{e!r}"
    finally:
        try:
            await page.close()
        except Exception:
            pass

    if not result:
        if logged_out:
            error = "logged_out"
        elif not error:
            error = "no_profile_data"
        print(f"[ks_self_profile] 未拿到资料; err={error}; login_btn={has_login_btn}; uid_cookie={uid_cookie!r}; "
              f"hit_urls={hit_urls[:6]}; attempts={attempts}")
    return result, error


# ── 快手发评论(浏览器自动化)──
# 评论输入框 / 发送按钮选择器(快手改版时改这里)
_COMMENT_INPUT = [
    'textarea[placeholder*="评论"]',
    'div[contenteditable="true"][placeholder*="评论"]',
    '.comment-editor [contenteditable="true"]',
    'textarea.comment-input',
    'div[contenteditable="true"]',
    'textarea',
]
_COMMENT_SUBMIT = [
    'button:has-text("发送")',
    'button:has-text("发布")',
    '.submit-btn',
    'span:has-text("发送")',
]


async def post_ks_comment(mgr: BrowserManager, identity: Identity, photo_id: str,
                          content: str, reply_to_text: str = "", headed: bool = True,
                          settle_ms: int = 1800, timeout_ms: int = 12000
                          ) -> Tuple[bool, str]:
    """用账号持久 profile(已含登录态)打开作品页,在评论框输入并发送。
    headed=True:弹真实窗口(写操作有头更稳,且能手动过验证码)。
    返回 (ok, error)。⚠️ 选择器随快手改版可能失效,集中在 _COMMENT_INPUT/_COMMENT_SUBMIT。"""
    content = (content or "").strip()
    if not content:
        return False, "空文案"
    ctx = None
    if headed:
        ctx = await mgr.open_headed(identity)
        page = await ctx.new_page()
    else:
        page = await mgr.new_page(identity, block_media=False)
    # 拦截评论提交接口。快手评论读取走 REST v2(/rest/v/photo/comment/list),
    # 提交大概率是同族的 /rest/v/photo/comment/add(result==1 表示成功);
    # 同时兜底 graphql 的 visionAddComment mutation。⚠️ add 端点名需真机抓包确认。
    pub = {"seen": False, "ok": False, "msg": ""}

    async def on_response(resp):
        if pub["seen"]:
            return
        url = resp.url
        is_rest_add = "/rest/v/photo/comment/add" in url or "/comment/add" in url
        if not is_rest_add and GRAPHQL_API not in url:
            return
        try:
            data = await resp.json()
        except Exception:
            return
        if is_rest_add:           # REST v2:顶层 result==1 即成功
            pub["seen"] = True
            pub["ok"] = data.get("result") in (1, "1")
            pub["msg"] = data.get("error_msg") or data.get("errorMsg") or ""
            return
        d = (data or {}).get("data") or {}
        for key in ("visionAddComment", "addComment", "commentAdd"):
            if key in d:
                r = d[key] or {}
                pub["seen"] = True
                pub["ok"] = (r.get("result") in (1, "1", None)) and not r.get("errorMsg")
                pub["msg"] = r.get("errorMsg") or ""
                break

    page.on("response", on_response)
    try:
        await page.goto(PHOTO_URL.format(pid=photo_id),
                        wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(settle_ms)
        if "passport" in page.url or "/login" in page.url:
            return False, "logged_out:账号未登录,无法发评论"

        try:
            await page.evaluate("() => window.scrollBy(0, 600)")
            await page.wait_for_selector(",".join(_COMMENT_INPUT), timeout=8000,
                                         state="attached")
        except Exception:
            pass

        editor = None
        for sel in _COMMENT_INPUT:
            loc = page.locator(sel).first
            try:
                if await loc.count():
                    editor = loc
                    break
            except Exception:
                continue
        if editor is None:
            return False, "未找到评论输入框(评论区可能未加载/被关闭/页面改版)"

        await editor.click(timeout=timeout_ms)
        await page.wait_for_timeout(300)
        await page.keyboard.type(content, delay=40)
        await page.wait_for_timeout(600)

        sent = False
        for sel in _COMMENT_SUBMIT:
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
        if not sent:
            return False, "未找到发送按钮且回车提交失败"

        for _ in range(27):       # 等提交接口回包,最多 ~8s
            if pub["seen"]:
                break
            await page.wait_for_timeout(300)

        if pub["seen"]:
            if pub["ok"]:
                return True, ""
            return False, (f"快手拒绝评论({pub['msg'] or '未知原因'})—— 多为验证码/频控/风控,"
                           f"请降低频率或换号稍后再试")
        return False, "未捕获到快手提交接口响应,无法确认是否成功(请人工核对该作品评论区)"
    except Exception as e:
        return False, f"发评论异常: {e!r}"
    finally:
        try:
            if ctx is not None:
                await ctx.close()
            else:
                await page.close()
        except Exception:
            pass
