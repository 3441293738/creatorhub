"""用真实浏览器打开用户主页,拦截抖音自己发的 post 接口响应,
直接拿到 aweme_list —— 绕过自算 a_bogus。
对应原项目 engine.ContentChecker + NativeClient 的抓取角色。

优化:屏蔽图片/视频/字体资源(只取数据,省带宽提速)、无新增即提前停止下滑。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit

from .identity import Identity
from .manager import BrowserManager

POST_API = "aweme/v1/web/aweme/post"
PROFILE_API = "aweme/v1/web/user/profile/other"
SELF_PROFILE_API = "aweme/v1/web/user/profile/self"
COMMENT_API = "aweme/v1/web/comment/list"
# 重发时必须去掉的一次性签名/风控参数,让抖音的 fetch 拦截器重新签
_SIGN_PARAMS = ("a_bogus", "X-Bogus", "x-bogus", "msToken", "_signature", "verifyFp")

# 抖音主页不一定由 window 承担滚动。选出页面里滚动范围最大的容器并拉到底，
# 再配合 mouse.wheel 触发 React 的滚动/分页监听。
_SCROLL_PROFILE_JS = """() => {
  const roots = [document.scrollingElement, document.documentElement, document.body];
  const nodes = [...document.querySelectorAll('main,section,div')];
  let best = null;
  let bestRange = 0;
  for (const el of [...roots, ...nodes]) {
    if (!el) continue;
    const range = (el.scrollHeight || 0) - (el.clientHeight || 0);
    if (range > bestRange) { best = el; bestRange = range; }
  }
  window.scrollTo(0, Math.max(document.body.scrollHeight, document.documentElement.scrollHeight));
  if (best) best.scrollTop = best.scrollHeight;
  return { range: bestRange, top: best ? best.scrollTop : window.scrollY };
}"""


def _page_reaches_boundary(items: List[dict], known_ids: Set[str],
                           stop_before: int = 0) -> bool:
    """一整页都已见过/早于监控起点时，才认为翻到了历史边界。

    置顶作品会把旧 ID 混在第一页，不能因为单个旧 ID 就停止。
    """
    rows = [it for it in items if str(it.get("aweme_id") or "")]
    if not rows:
        return False
    if known_ids and all(str(it.get("aweme_id") or "") in known_ids for it in rows):
        return True
    if stop_before:
        times = [int(it.get("create_time") or 0) for it in rows]
        return bool(times) and all(ts and ts < stop_before for ts in times)
    return False


# 作品页没拿到数据时,看页面究竟是什么状态:登录墙?空态?还是 tab 没激活?
_WORKS_DOM_PROBE_JS = """() => {
  const txt = (document.body.innerText || '').replace(/\\s+/g, ' ');
  const tabs = [...document.querySelectorAll('[data-e2e*="tab"],[class*="tab"]')]
    .map(e => (e.textContent || '').trim().slice(0, 8))
    .filter(t => t && t.length <= 8).slice(0, 8);
  return {
    tabs: [...new Set(tabs)],
    items: document.querySelectorAll('[data-e2e="user-post-list"] li, li[data-e2e]').length,
    // 这几种文案能把「空态 / 登录墙 / 风控」区分开
    empty: /暂无作品|还没有发布|没有更多了/.test(txt),
    login_wall: /登录后查看|立即登录|扫码登录/.test(txt),
    risk: /访问频繁|环境异常|验证/.test(txt),
    body_len: txt.length,
  };
}"""


async def fetch_videos(mgr: BrowserManager, identity: Identity, sec_uid: str,
                       known_ids: Set[str], max_scrolls: int = 12,
                       settle_ms: int = 1800, block_media: bool = True,
                       stop_before: int = 0, min_scrolls: int = 2,
                       ) -> Tuple[List[dict], Optional[dict], str]:
    """打开主页并下滑,收集作品。返回 (新作品列表, 作者信息dict, error)。"""
    collected: Dict[str, dict] = {}
    author: Optional[dict] = None
    error = ""
    post_hits = []        # 命中的 aweme/post 响应(判断是「没发」还是「发了解不出」)
    post_pages: List[List[dict]] = []  # 保留每页边界，不能拿混合后的 collected 判断停止
    api_seen = []         # 该页发出的抖音 API(post_hits 为空时,靠它看页面到底在请求什么)
    pagination_stalled = False

    page = await mgr.new_page(identity, block_media)

    async def on_response(resp):
        nonlocal author
        url = resp.url
        if ("douyin.com" in url and ("/aweme/v1/web/" in url or "/web/api/" in url)
                and len(api_seen) < 40):
            api_seen.append(f"{resp.status} {url.split('?')[0].split('douyin.com')[-1]}")
        if POST_API in url:
            try:
                data = await resp.json()
            except Exception as e:
                # 页面跳转会丢弃 body。别静默 pass,否则「200 却没数据」永远查不出原因
                post_hits.append(f"{resp.status} body_read_failed={e!r}")
                return
            lst = data.get("aweme_list")
            post_hits.append(f"{resp.status} status_code={data.get('status_code')} "
                             f"aweme_list={len(lst) if isinstance(lst, list) else lst!r} "
                             f"has_more={data.get('has_more')} max_cursor={data.get('max_cursor')} "
                             f"keys={sorted(data)[:8]}")
            if isinstance(lst, list):
                post_pages.append(lst)
            for it in (lst or []):
                aid = str(it.get("aweme_id") or "")
                if aid:
                    collected[aid] = it
                    if author is None and it.get("author"):
                        author = it["author"]
        elif PROFILE_API in url and author is None:
            try:
                data = await resp.json()
            except Exception:
                return
            if data.get("user"):
                author = data["user"]

    page.on("response", on_response)

    try:
        await page.goto(f"https://www.douyin.com/user/{sec_uid}",
                        wait_until="domcontentloaded", timeout=30000)
        # 同私信/粉丝入口:没 hydrate 完,作品列表的分页请求根本不会发
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        await page.wait_for_timeout(settle_ms)
        stagnant = 0
        min_scrolls = max(1, min(min_scrolls, max_scrolls))
        for scroll_index in range(max_scrolls):
            before = len(collected)
            pages_before = len(post_pages)
            try:
                await page.evaluate(_SCROLL_PROFILE_JS)
            except Exception:
                pass
            await page.mouse.wheel(0, 4000)
            await page.wait_for_timeout(settle_ms)
            fresh_pages = post_pages[pages_before:]
            boundary = any(_page_reaches_boundary(p, known_ids, stop_before)
                           for p in fresh_pages)
            # 至少真实滚动几次，避免首屏里一个置顶旧作品直接截断扫描。
            if scroll_index + 1 >= min_scrolls and boundary:
                break
            if len(collected) == before:               # 本次下滑无新增
                # 一条都没抓到时别提前退:那是「还没开始」,不是「已经到底」。
                # 首屏 XHR 可能比 networkidle 更晚,滚满 max_scrolls 再放弃。
                stagnant += 1
                if (collected and scroll_index + 1 >= min_scrolls
                        and stagnant >= 3):             # 连续三次无响应才判定到底
                    pagination_stalled = bool(post_pages and len(post_pages) == 1)
                    break
            else:
                stagnant = 0
        if not collected:
            error = "未拦截到作品数据(可能未登录/被风控/该用户无公开作品)"
    except Exception as e:
        error = f"打开主页失败: {e!r}"
    finally:
        final_url = page.url
        dom = {}
        if not collected:
            try:                        # 页面到底渲染成什么样了(tab?空态?登录墙?)
                dom = await page.evaluate(_WORKS_DOM_PROBE_JS)
            except Exception as e:
                dom = {"probe_failed": repr(e)}
        try:
            await page.close()
        except Exception:
            pass

    if not collected:
        # 「aweme/post 没发出来」和「发了但 aweme_list 空/读不到」是两回事,原来一律报同一句话
        print(f"[works] 未拿到作品; sec_uid={sec_uid[:24]}… final_url={final_url}; "
              f"post_hits({len(post_hits)})={post_hits[:5]}; dom={dom}")
        print(f"[works] api_seen({len(api_seen)})={api_seen[:30]}")
    elif pagination_stalled:
        print(f"[works] 主页分页未触发; sec_uid={sec_uid[:24]}… "
              f"collected={len(collected)} post_hits={post_hits[:3]}")
    new_items = [it for aid, it in collected.items() if aid not in known_ids]
    return new_items, author, error


# 滚动评论区的可滚动容器(而不是整页),驱动抖音自己的分页请求
_SCROLL_COMMENTS = """
() => {
  const item = document.querySelector('[data-e2e="comment-item"]')
            || document.querySelector('[data-e2e="comment-list"]');
  if (!item) { window.scrollBy(0, 3000); return false; }
  let el = item;
  while (el && el !== document.body) {
    const oy = getComputedStyle(el).overflowY;
    if ((oy === 'auto' || oy === 'scroll') && el.scrollHeight > el.clientHeight + 20) {
      el.scrollTop = el.scrollHeight;
      return true;
    }
    el = el.parentElement;
  }
  window.scrollBy(0, 3000);
  return false;
}
"""


async def fetch_comments(mgr: BrowserManager, identity: Identity, aweme_id: str,
                         known_cids: Set[str], max_scrolls: int = 6,
                         settle_ms: int = 1600, block_media: bool = True
                         ) -> Tuple[List[dict], str]:
    """打开作品详情页,滚动评论容器翻页,拦截评论列表接口收集评论原始 JSON。
    返回 (新评论原始列表, error)。注意:抖音评论默认按热度排序,非严格时间序,
    只能尽量翻页扫到前若干页的新评论。
    """
    collected: Dict[str, dict] = {}
    error = ""
    page = await mgr.new_page(identity, block_media)

    async def on_response(resp):
        if COMMENT_API in resp.url:
            try:
                data = await resp.json()
            except Exception:
                return
            for c in (data.get("comments") or []):
                cid = str(c.get("cid") or "")
                if cid:
                    collected[cid] = c

    page.on("response", on_response)
    try:
        await page.goto(f"https://www.douyin.com/video/{aweme_id}",
                        wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(settle_ms)
        stagnant = 0
        for _ in range(max_scrolls):
            before = len(collected)
            try:
                await page.evaluate(_SCROLL_COMMENTS)
            except Exception:
                pass
            await page.wait_for_timeout(settle_ms)
            if len(collected) == before:        # 本次没翻出新评论
                stagnant += 1
                if stagnant >= 2:               # 连续两次到底,停
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


# ── 抖音发评论(浏览器自动化)──
# 评论输入框 / 发送按钮选择器(抖音改版时改这里。data-e2e 较稳,排前)
_COMMENT_INPUT = [
    '[data-e2e="comment-input"]',
    'div.comment-input-inner [contenteditable="true"]',
    'div[data-e2e="comment-publish"] [contenteditable="true"]',
    '.comment-input [contenteditable="true"]',
    'div[contenteditable="true"][data-line-wrapper]',
    'div[contenteditable="true"]',
]
_COMMENT_SUBMIT = [
    '[data-e2e="comment-publish"]',
    'div.comment-input-area button:has-text("发送")',
    'button:has-text("发送")',
    'span:has-text("发送")',
]
# 抖音发表评论接口(权威成功判据:拦截它的响应看 status_code)
_PUBLISH_API = "aweme/v1/web/comment/publish"

# 找不到输入框时,导出页面真实结构,便于对症补选择器
_DIAG_INPUTS = """
() => {
  const ce = [];
  document.querySelectorAll('[contenteditable]').forEach(el => {
    ce.push(((el.tagName || '') + '.' + (typeof el.className === 'string' ? el.className : ''))
      .slice(0, 70) + ' | ph=' +
      (el.getAttribute('data-placeholder') || el.getAttribute('placeholder')
       || el.getAttribute('aria-label') || '').slice(0, 30));
  });
  const e2e = [];
  document.querySelectorAll('[data-e2e]').forEach(el => {
    const v = el.getAttribute('data-e2e');
    if (v && /comment|input|publish|reply|editor/i.test(v)) e2e.push(v);
  });
  return JSON.stringify({ ce: ce.slice(0, 12), e2e: [...new Set(e2e)].slice(0, 20),
                          url: location.href });
}
"""


async def post_comment_browser(mgr: BrowserManager, identity: Identity, aweme_id: str,
                               content: str, reply_to_text: str = "", headed: bool = True,
                               settle_ms: int = 1800, timeout_ms: int = 12000
                               ) -> Tuple[bool, str]:
    """用账号持久 profile(已含登录态)打开作品页,在评论框输入并发送。
    headed=True:弹真实浏览器窗口(抖音对无头写操作常降级/拦截,有头更稳,且能手动过验证码)。
    成功判据 = 拦截抖音 comment/publish 接口响应的 status_code(0=成功),
    而非"输入框是否清空"(后者会被验证码/频控误判为成功)。
    reply_to_text 非空:尝试定位含该文本的评论、点其「回复」内联输入;失败回退顶层评论。
    返回 (ok, error)。⚠️ 选择器随抖音改版可能失效,集中在 _COMMENT_INPUT/_COMMENT_SUBMIT。"""
    content = (content or "").strip()
    if not content:
        return False, "空文案"
    ctx = None
    if headed:
        ctx = await mgr.open_headed(identity)   # 同 profile 有头窗口(关闭即落盘 Cookie)
        page = await ctx.new_page()
    else:
        page = await mgr.new_page(identity, block_media=False)
    # 拦截发表接口响应(权威判据)
    pub = {"seen": False, "ok": False, "code": None, "msg": ""}

    async def on_response(resp):
        if _PUBLISH_API in resp.url and not pub["seen"]:
            try:
                data = await resp.json()
            except Exception:
                return
            pub["seen"] = True
            pub["code"] = data.get("status_code")
            pub["ok"] = data.get("status_code") == 0
            pub["msg"] = data.get("status_msg") or ""

    page.on("response", on_response)
    try:
        await page.goto(f"https://www.douyin.com/video/{aweme_id}",
                        wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(settle_ms)
        if "passport" in page.url or "/login" in page.url:
            return False, "logged_out:账号未登录,无法发评论"

        # 评论区懒加载:显式等输入框出现(全是 CSS 选择器,可合并等待),并轻滚一下触发渲染
        try:
            await page.evaluate("() => window.scrollBy(0, 600)")
            await page.wait_for_selector(",".join(_COMMENT_INPUT), timeout=8000,
                                         state="attached")
        except Exception:
            pass

        editor = None
        # 回复模式:先在评论区找到目标评论,点它的「回复」打开内联框
        if reply_to_text:
            try:
                await page.evaluate(_SCROLL_COMMENTS)
                await page.wait_for_timeout(1200)
                item = page.locator('[data-e2e="comment-item"]', has_text=reply_to_text[:20]).first
                if await item.count():
                    rbtn = item.get_by_text("回复", exact=False).first
                    await rbtn.click(timeout=4000)
                    await page.wait_for_timeout(800)
                    editor = page.locator('[contenteditable="true"]').last
            except Exception:
                editor = None  # 回退到顶层评论框

        if editor is None:
            for sel in _COMMENT_INPUT:
                loc = page.locator(sel).first
                try:
                    if await loc.count():
                        editor = loc
                        break
                except Exception:
                    continue
        if editor is None:
            diag = ""
            try:
                diag = await page.evaluate(_DIAG_INPUTS)
            except Exception:
                pass
            print(f"[comment_post] 未找到输入框 aweme={aweme_id} diag={diag}")
            return False, ("未找到评论输入框(评论区可能未加载/被关闭/页面改版)。"
                           f"页面诊断: {diag[:300]}")

        await editor.click(timeout=timeout_ms)
        await page.wait_for_timeout(300)
        await page.keyboard.type(content, delay=40)   # 逐字输入,更像真人
        await page.wait_for_timeout(600)

        # 优先点发送按钮;找不到则回车提交
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

        # 等抖音的发表接口回包(权威判据),最多 ~8s
        for _ in range(27):
            if pub["seen"]:
                break
            await page.wait_for_timeout(300)

        if pub["seen"]:
            if pub["ok"]:
                return True, ""
            return False, (f"抖音拒绝评论(status_code={pub['code']}"
                           f"{' ' + pub['msg'] if pub['msg'] else ''})—— 多为验证码/频控/风控,"
                           f"请降低频率或换号稍后再试")

        # 没等到发表接口:多半是点击没真正触发提交,或被前置验证拦下
        try:
            left = (await editor.inner_text())[:50]
        except Exception:
            left = ""
        if content[:8] in left:
            return False, "已输入但未触发发表(可能弹了验证码/发送按钮未激活)"
        return False, "未捕获到抖音发表接口响应,无法确认是否成功(请人工核对该作品评论区)"
    except Exception as e:
        return False, f"发评论异常: {e!r}"
    finally:
        try:
            if ctx is not None:
                await ctx.close()   # 有头:关 context 即落盘 Cookie
            else:
                await page.close()
        except Exception:
            pass


def _extract_user(data) -> Optional[dict]:
    """从 profile 响应里挖出 user 对象(多结构兜底)。"""
    if not isinstance(data, dict):
        return None
    for u in (data.get("user"), data.get("user_info"), data):
        if isinstance(u, dict) and u.get("sec_uid"):
            return u
    return None


async def _refetch_in_page(page, full_url: str) -> Optional[dict]:
    """在 douyin 页面内重发 profile/self。剥掉一次性签名参数后走相对路径,
    抖音自己的 fetch 拦截器会重新补 a_bogus(同 account_hub._fetch_im_user_info)。"""
    try:
        u = urlsplit(full_url)
        qs = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True)
              if k not in _SIGN_PARAMS]
        path = u.path + (("?" + urlencode(qs)) if qs else "")
        return await page.evaluate(
            """async (p) => {
              try {
                const r = await fetch(p, {credentials:'include',
                                          headers:{'accept':'application/json'}});
                return await r.json();
              } catch (e) { return null; }
            }""", path)
    except Exception as e:
        print(f"[self_profile] refetch failed: {e!r}")
        return None


async def fetch_self_profile(mgr: BrowserManager, identity: Identity,
                             timeout_ms: int = 15000, block_media: bool = False
                             ) -> Tuple[dict, str]:
    """打开自己的主页,显式等待并拦截 user/profile/self 拿登录账号真实资料。
    返回 (user dict, error)。error == "logged_out" 表示登录态失效。

    ⚠️ /user/self 常被重定向到 /jingxuan,但 profile/self 仍会发出;拦截时若页面正在
    跳转,Playwright 读 body 会失败,故再补一发页内 refetch(抖音自己的 fetch 拦截器
    会补 a_bogus 签名,同 _fetch_im_user_info 的做法)。
    注:query/user 不是资料接口,它返回的是设备会话记录(user_uid/browser_name),无 sec_uid。"""
    result: dict = {}
    api_seen = []                   # 看到的抖音 API 请求(诊断用)
    hit_apis = []                   # 命中的 profile/self(判断是"没发"还是"读不到")
    shapes = []                     # 命中但挖不出 user 时的响应结构/读取异常(标定用)
    self_urls: List[str] = []       # profile/self 的完整 URL(带 query),供页内 refetch 复用
    error = ""
    page = await mgr.new_page(identity, block_media)

    def is_profile(resp):
        # 只认自己的 profile/self:profile/other 是看别人主页时发的,拦了会绑错号
        return SELF_PROFILE_API in resp.url

    async def on_response(resp):
        url = resp.url
        if ("douyin.com" in url and ("/aweme/v1/web/" in url or "/web/api/" in url)
                and len(api_seen) < 40):
            api_seen.append(f"{resp.status} {url.split('?')[0]}")
        if is_profile(resp) and resp.status == 200:
            path = url.split("?")[0]
            hit_apis.append(path)
            if url not in self_urls:
                self_urls.append(url)
            try:
                data = await resp.json()
            except Exception as e:
                # 页面跳转会丢弃 body。别静默 return,否则日志显示"命中了"却查不出原因
                if len(shapes) < 4:
                    shapes.append(f"{path} body_read_failed={e!r}")
                return
            u = _extract_user(data)
            if u:
                result.update(u)
            elif isinstance(data, dict) and len(shapes) < 4:
                shapes.append(f"{path} keys={sorted(data)[:12]}")

    page.on("response", on_response)
    logged_out = False
    final_url = ""
    has_login_btn = None
    try:
        for url in ("https://www.douyin.com/user/self", "https://www.douyin.com/"):
            await page.goto(url, wait_until="load", timeout=30000)
            try:                    # 主动等待 profile XHR(而不是死等固定时间)
                await page.wait_for_response(
                    lambda r: is_profile(r) and r.status == 200, timeout=timeout_ms)
            except Exception:
                pass
            await page.wait_for_timeout(1500)
            final_url = page.url
            if "passport" in final_url or "/login" in final_url:
                logged_out = True
                break
            if result:
                break
        if not result and self_urls:
            # 拦到了但 body 读不到:页内重发一次(此时页面已静止,不会再丢 body)
            data = await _refetch_in_page(page, self_urls[-1])
            u = _extract_user(data)
            if u:
                result.update(u)
            elif isinstance(data, dict) and len(shapes) < 6:
                shapes.append(f"refetch keys={sorted(data)[:12]}")
        # 是否能看到“登录”按钮(看到=其实没登录进去)
        try:
            has_login_btn = await page.get_by_text("登录", exact=True).first.is_visible(
                timeout=1500)
        except Exception:
            has_login_btn = None
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
            # 区分「接口没发出来」和「发了但取不到 user」——之前一律报后者,误导排查
            error = ("profile/self 命中但取不到 user" if hit_apis else "no_profile_xhr")
        print(f"[self_profile] 未拿到资料; err={error}; final_url={final_url}; "
              f"login_btn_visible={has_login_btn}; hit={hit_apis}; shapes={shapes}; "
              f"api_seen({len(api_seen)})={api_seen[:25]}")
    return result, error


def _dig_comment_list(data) -> list:
    """从创作中心各种可能的响应结构里挖出评论数组(防御式)。"""
    if not isinstance(data, dict):
        return []
    for key in ("comments", "comment_list", "comment_infos", "list", "data"):
        v = data.get(key)
        if isinstance(v, list):
            return v
        if isinstance(v, dict):     # 再下钻一层
            for k2 in ("comments", "comment_list", "list"):
                if isinstance(v.get(k2), list):
                    return v[k2]
    return []


async def fetch_creator_comments(mgr: BrowserManager, identity: Identity,
                                 known_cids: Set[str], page_url: str,
                                 max_scrolls: int = 8, settle_ms: int = 1600,
                                 block_media: bool = True
                                 ) -> Tuple[List[dict], str]:
    """⚠️ 实验性:打开创作中心评论管理页,拦截评论列表接口(按时间序、含刚发的)。
    抖音改版时改 page_url 和下面的拦截判断即可。返回 (新评论原始列表, error)。
    创作中心 Cookie 与 www 同在 .douyin.com,故复用账号同一持久 profile。
    """
    collected: Dict[str, dict] = {}
    error = ""
    page = await mgr.new_page(identity, block_media)

    async def on_response(resp):
        url = resp.url
        # 宽松匹配:创作中心域名下任何含 comment 的接口
        if "creator.douyin.com" in url and "comment" in url and "list" in url:
            try:
                data = await resp.json()
            except Exception:
                return
            for c in _dig_comment_list(data):
                cid = str(c.get("cid") or c.get("comment_id") or "")
                if cid:
                    collected[cid] = c

    page.on("response", on_response)
    try:
        await page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(settle_ms)
        # 若被重定向到登录页,说明创作者登录态失效
        if "/login" in page.url or "passport" in page.url:
            error = "创作者登录态已失效,请重新创作者登录"
        else:
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
            if not collected:
                error = error or "未拦截到创作中心评论(页面/接口可能已改版,见 README)"
    except Exception as e:
        error = f"打开创作中心失败: {e!r}"
    finally:
        try:
            await page.close()
        except Exception:
            pass

    new = [c for cid, c in collected.items() if cid not in known_cids]
    return new, error
