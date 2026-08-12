"""用真实浏览器打开用户主页,拦截抖音自己发的 post 接口响应,
直接拿到 aweme_list —— 绕过自算 a_bogus。
对应原项目 engine.ContentChecker + NativeClient 的抓取角色。

优化:屏蔽图片/视频/字体资源(只取数据,省带宽提速)、无新增即提前停止下滑。
"""
from __future__ import annotations

import json
import random
import re
import string
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from .identity import Identity
from .manager import BrowserManager
from ..platforms.douyin.extract import danmaku_key

POST_API = "aweme/v1/web/aweme/post"
PROFILE_API = "aweme/v1/web/user/profile/other"
SELF_PROFILE_API = "aweme/v1/web/user/profile/self"
COMMENT_API = "aweme/v1/web/comment/list"
DANMAKU_API = "aweme/v1/web/danmaku"
SEARCH_API_MARKERS = (
    "/aweme/v1/web/general/search/single/",
    "/aweme/v1/web/search/item/",
    "/aweme/v1/web/search/feed/",
)
# 与 login.py 的登录成功判据保持一致。资料接口改版时不能再只靠页面“登录”按钮
# 判断登录态：按钮可能未渲染，或者被 AB 页面隐藏。
_LOGIN_COOKIES = {"sessionid", "sessionid_ss", "sid_tt", "uid_tt", "sid_guard"}
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

# 搜索页的首屏请求现在并不总会由页面自动发出（登录弹窗、AB 页面和首屏
# 服务端渲染都会造成这种情况）。在已经打开的同源页面里主动 fetch，浏览器会
# 自动携带当前账号 Cookie、UA、代理和 Referer；这比另起 HTTP 客户端更不容易
# 出现“账号资料能刷新、搜索接口却是未登录”的上下文分裂。
_DOUYIN_SEARCH_FETCH_JS = """async (params) => {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value !== undefined && value !== null && value !== '') {
      query.set(key, String(value));
    }
  }
  const nav = navigator || {};
  const ua = String(nav.userAgent || '');
  const chrome = ua.match(/(?:Chrome|Chromium)\/(\d+(?:\.\d+){0,3})/);
  const windows = ua.match(/Windows NT ([\d.]+)/);
  const mac = ua.match(/Mac OS X ([\d_]+)/);
  const dynamic = {
    browser_language: nav.language || 'zh-CN',
    browser_platform: nav.platform || 'Win32',
    browser_name: /Chrome|Chromium/.test(ua) ? 'Chrome' : 'Unknown',
    browser_version: chrome ? chrome[1] : '',
    browser_online: nav.onLine === false ? 'false' : 'true',
    engine_name: /Chrome|Chromium/.test(ua) ? 'Blink' : '',
    engine_version: chrome ? chrome[1].split('.')[0] + '.0' : '',
    os_name: windows ? 'Windows' : (mac ? 'Mac OS' : ''),
    os_version: windows ? windows[1] : (mac ? mac[1].replaceAll('_', '.') : ''),
    cpu_core_num: nav.hardwareConcurrency || 8,
    device_memory: nav.deviceMemory || 8,
    screen_width: (window.screen && window.screen.width) || 1920,
    screen_height: (window.screen && window.screen.height) || 1080,
  };
  for (const [key, value] of Object.entries(dynamic)) {
    if (value !== '') query.set(key, String(value));
  }
  try {
    const token = window.localStorage && window.localStorage.getItem('xmst');
    if (token) query.set('msToken', token);
  } catch (_) {}

  const endpoint = '/aweme/v1/web/general/search/single/?' + query.toString();
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(endpoint, {
      method: 'GET',
      credentials: 'include',
      headers: {Accept: 'application/json, text/plain, */*'},
      cache: 'no-store',
      signal: controller.signal,
    });
    const text = await response.text();
    let data = null;
    try { data = JSON.parse(text); } catch (_) {}
    return {
      ok: response.ok,
      status: response.status,
      url: response.url,
      data,
      preview: text.slice(0, 500),
    };
  } catch (error) {
    return {ok: false, status: 0, error: String(error), data: null};
  } finally {
    clearTimeout(timer);
  }
}"""


def douyin_search_request_params(keyword: str, offset: int = 0,
                                 count: int = 15,
                                 search_id: str = "") -> Dict[str, str]:
    """构造抖音网页搜索参数；通用浏览器参数在页面内按实际环境补齐。"""
    webid = "".join(random.choice(string.digits) for _ in range(19))
    return {
        "search_channel": "aweme_video_web",
        "enable_history": "1",
        "keyword": keyword,
        "search_source": "tab_search",
        "query_correct_type": "1",
        "is_filter_search": "0",
        "from_group_id": "7378810571505847586",
        "offset": str(max(0, offset)),
        "count": str(max(1, min(count, 15))),
        "need_filter_settings": "1",
        "list_type": "multi",
        "search_id": search_id,
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "version_code": "190600",
        "version_name": "19.6.0",
        "update_version_code": "170400",
        "pc_client_type": "1",
        "cookie_enabled": "true",
        "platform": "PC",
        "effective_type": "4g",
        "round_trip_time": "50",
        "webid": webid,
    }


def _douyin_search_browser_params(identity: Identity) -> Dict[str, str]:
    """从账号固定画像补齐请求参数，避免 API 请求与浏览器 UA/视口不一致。"""
    ua = identity.ua or ""
    chrome = re.search(r"(?:Chrome|Chromium)/(\d+(?:\.\d+){0,3})", ua)
    windows = re.search(r"Windows NT ([\d.]+)", ua)
    mac = re.search(r"Mac OS X ([\d_]+)", ua)
    version = chrome.group(1) if chrome else ""
    is_mac = bool(mac)
    return {
        "browser_language": identity.locale or "zh-CN",
        "browser_platform": "MacIntel" if is_mac else "Win32",
        "browser_name": "Chrome",
        "browser_version": version,
        "browser_online": "true",
        "engine_name": "Blink",
        "engine_version": (version.split(".")[0] + ".0") if version else "",
        "os_name": "Mac OS" if is_mac else "Windows",
        "os_version": (mac.group(1).replace("_", ".") if mac
                       else (windows.group(1) if windows else "10")),
        "cpu_core_num": "8",
        "device_memory": "8",
        "screen_width": str(identity.viewport_w or 1440),
        "screen_height": str(identity.viewport_h or 900),
    }


async def _request_douyin_search(page, identity: Identity,
                                 params: Dict[str, str], referer: str) -> dict:
    """通过 BrowserContext 的共享 Cookie 请求搜索接口，并设置硬超时。"""
    request_params = dict(params)
    request_params.update({
        key: value for key, value in _douyin_search_browser_params(identity).items()
        if value
    })
    try:
        state = await page.context.storage_state()
        for origin in state.get("origins") or []:
            if "douyin.com" not in str(origin.get("origin") or ""):
                continue
            local = {
                str(item.get("name") or ""): str(item.get("value") or "")
                for item in origin.get("localStorage") or []
            }
            if local.get("xmst"):
                request_params["msToken"] = local["xmst"]
                break
    except Exception:
        pass

    response = None
    try:
        response = await page.context.request.get(
            "https://www.douyin.com/aweme/v1/web/general/search/single/",
            params=request_params,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": referer,
                "User-Agent": identity.ua or "",
            },
            timeout=20000,
            fail_on_status_code=False,
        )
        body = await response.text()
        try:
            payload = json.loads(body)
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = None
        return {
            "ok": response.ok,
            "status": response.status,
            "url": response.url,
            "data": payload,
            "preview": body[:500],
        }
    except Exception as exc:
        return {"ok": False, "status": 0, "error": repr(exc), "data": None}
    finally:
        if response is not None:
            try:
                await response.dispose()
            except Exception:
                pass


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


def extract_search_awemes(payload) -> List[dict]:
    """从抖音不同版本的搜索响应中提取作品对象并保持首次出现顺序。

    搜索接口先后出现过 ``data[].aweme_info``、``aweme_list`` 与混合卡片等
    包装；这里只遍历已知容器键，避免把作者推荐卡误判成作品。
    """
    found: Dict[str, dict] = {}
    container_keys = (
        "data", "items", "list", "aweme_list", "aweme_info",
        "aweme_mix_info", "mix_items", "search_result", "card",
    )

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        aid = str(value.get("aweme_id") or "")
        if aid and (value.get("video") or value.get("images")):
            found.setdefault(aid, value)
            return
        for key in container_keys:
            child = value.get(key)
            if child is not None and child is not value:
                visit(child)

    visit(payload)
    return list(found.values())


def douyin_search_empty_error(page_title: str, page_text: str,
                              final_url: str, api_seen: List[str]) -> str:
    """把空搜索结果区分为验证、掉登录、接口变化和真正的空响应。"""
    diagnostic = f"{page_title}\n{page_text}\n{final_url}".casefold()
    # 登录弹窗本身包含“验证码登录”字样，必须先识别“扫码登录”等登录墙信号，
    # 否则会被下面的安全验证码分支误报成风控验证。
    if any(token in diagnostic for token in (
            "扫码登录", "登录后即可", "登录后查看", "立即登录", "/login")):
        return "抖音登录态已失效；请重新扫码登录后再续跑"
    if any(token in diagnostic for token in (
            "验证码", "安全验证", "访问频繁", "环境异常",
            "captcha", "verify")):
        return ("抖音触发验证码/安全验证；请在账号页点击“打开浏览器”，"
                "完成验证并关窗保存登录态后再续跑")
    if api_seen:
        return "抖音搜索接口有响应，但没有可解析的作品（关键词可能无结果或接口结构已变化）"
    return "抖音搜索页没有发出作品搜索接口（页面可能未完整加载或平台页面已变化）"


def douyin_search_exception_error(exc: Exception) -> str:
    """把用户主动关窗与真正的页面异常分开，避免向界面泄漏超长堆栈文本。"""
    detail = repr(exc)
    if "TargetClosedError" in detail or "has been closed" in detail:
        return "抖音采集窗口被关闭；请续跑任务，并在任务结束前保持窗口打开"
    return f"打开抖音搜索页失败: {detail}"


def _douyin_search_needs_verification(payload) -> bool:
    if not isinstance(payload, dict):
        return False
    nil_info = payload.get("search_nil_info") or {}
    marker = " ".join(str(nil_info.get(key) or "") for key in (
        "search_nil_type", "search_nil_item", "text_type"))
    return "verify" in marker.casefold()


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


async def fetch_douyin_search(mgr: BrowserManager, identity: Identity, keyword: str,
                               max_results: int = 20, max_scrolls: int = 12,
                               settle_ms: int = 1800,
                               block_media: bool = False,
                               context=None) -> Tuple[List[dict], str]:
    """打开抖音视频搜索页，拦截站内搜索响应并返回作品原始对象。"""
    collected: Dict[str, dict] = {}
    api_seen: List[str] = []
    error = ""
    # 关键词搜索在抖音无头上下文中容易直接落到“验证码中间页”。批量任务可传入
    # 同账号的临时有头 context；普通调用仍沿用后台常驻 context。
    if context is not None:
        page = next((candidate for candidate in context.pages
                     if candidate.url == "about:blank"), None)
        page = page or await context.new_page()
    else:
        page = await mgr.new_page(identity, block_media)

    def collect_payload(payload) -> int:
        before = len(collected)
        for raw in extract_search_awemes(payload):
            aid = str(raw.get("aweme_id") or "")
            if aid:
                collected.setdefault(aid, raw)
        return len(collected) - before

    async def on_response(resp):
        url = resp.url
        # 保留已知接口，同时接受路径中含 search 的新版本网页接口，避免平台只改
        # 路径就被误报成“没有发出搜索接口”。
        is_search_api = any(marker in url for marker in SEARCH_API_MARKERS)
        is_search_api = is_search_api or (
            "/aweme/v1/web/" in url and "search" in urlsplit(url).path.casefold())
        if not is_search_api:
            return
        if len(api_seen) < 40:
            api_seen.append(f"{resp.status} {url.split('?')[0]}")
        try:
            payload = await resp.json()
        except Exception:
            return
        collect_payload(payload)

    page.on("response", on_response)
    final_url = ""
    page_title = ""
    page_text = ""
    try:
        # general 页可作为 general/search/single 的同源 Referer；具体只取视频由
        # search_channel=aweme_video_web 控制。
        target = f"https://www.douyin.com/search/{quote(keyword, safe='')}?type=general"
        await page.goto(target, wait_until="domcontentloaded", timeout=30000)
        try:
            await page.wait_for_load_state("networkidle", timeout=12000)
        except Exception:
            pass
        await page.wait_for_timeout(settle_ms)

        # 页面不主动发首屏 XHR 时，直接在当前登录浏览器里请求同一个搜索接口。
        # 每页最多 15 条，数量较大时沿用 extra.logid 继续分页。
        direct_error = ""
        search_id = ""
        for offset in range(0, max(1, max_results), 15):
            if len(collected) >= max_results:
                break
            params = douyin_search_request_params(
                keyword, offset=offset,
                # 网页端接口按固定 15 条返回；传 5 等较小值会被当成异常请求并
                # 返回空 data，因此本地再按 max_results 截断。
                count=15,
                search_id=search_id)
            try:
                result = await _request_douyin_search(
                    page, identity, params, target)
            except Exception as exc:
                direct_error = f"页面内搜索请求失败: {exc!r}"
                break
            result = result if isinstance(result, dict) else {}
            status = int(result.get("status") or 0)
            if len(api_seen) < 40:
                api_seen.append(
                    f"direct {status} /aweme/v1/web/general/search/single/")
            payload = result.get("data")
            if not isinstance(payload, dict):
                preview = str(result.get("preview") or result.get("error") or "")
                direct_error = (
                    f"抖音搜索接口返回异常（HTTP {status or '未知'}）"
                    + (f": {preview[:160]}" if preview else ""))
                break

            # 接口用 search_nil_info=verify_check 表示页面已弹出滑块。保持采集
            # 窗口而不是立即判失败，让用户在同一窗口完成一次验证；验证完成后
            # 复用相同账号上下文自动续跑。
            if _douyin_search_needs_verification(payload):
                try:
                    await page.bring_to_front()
                except Exception:
                    pass
                for _ in range(24):
                    await page.wait_for_timeout(2500)
                    if collected:
                        break
                    result = await _request_douyin_search(
                        page, identity, params, target)
                    payload = (result.get("data")
                               if isinstance(result, dict) else None)
                    if (isinstance(payload, dict)
                            and not _douyin_search_needs_verification(payload)):
                        break
                if collected:
                    break
                if (not isinstance(payload, dict)
                        or _douyin_search_needs_verification(payload)):
                    direct_error = (
                        "抖音要求完成滑块验证；请续跑任务后，在弹出的采集窗口中"
                        "完成滑块，验证通过后任务会自动继续")
                    break
            added = collect_payload(payload)
            search_id = str((payload.get("extra") or {}).get("logid") or "")
            if added == 0 or not payload.get("has_more"):
                break
            await page.wait_for_timeout(600)

        stagnant = 0
        for _ in range(max(1, min(max_scrolls, 40))):
            if len(collected) >= max_results:
                break
            before = len(collected)
            try:
                await page.evaluate(_SCROLL_PROFILE_JS)
            except Exception:
                pass
            await page.mouse.wheel(0, 4200)
            await page.wait_for_timeout(settle_ms)
            if len(collected) == before:
                stagnant += 1
                if collected and stagnant >= 3:
                    break
            else:
                stagnant = 0
        final_url = page.url
        if not collected:
            try:
                page_title = (await page.title()).strip()
            except Exception:
                pass
            try:
                page_text = (await page.locator("body").inner_text())[:1200]
            except Exception:
                pass
            error = direct_error or douyin_search_empty_error(
                page_title, page_text, final_url, api_seen)
    except Exception as exc:
        error = douyin_search_exception_error(exc)
    finally:
        try:
            final_url = final_url or page.url
            await page.close()
        except Exception:
            pass

    if not collected:
        print(f"[dy_search] keyword={keyword!r} final_url={final_url!r} "
              f"title={page_title!r} api_seen({len(api_seen)})={api_seen[:10]}")
    return list(collected.values())[:max_results], error


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
                         settle_ms: int = 1600, block_media: bool = True,
                         context=None,
                         ) -> Tuple[List[dict], str]:
    """打开作品详情页,滚动评论容器翻页,拦截评论列表接口收集评论原始 JSON。
    返回 (新评论原始列表, error)。注意:抖音评论默认按热度排序,非严格时间序,
    只能尽量翻页扫到前若干页的新评论。
    """
    collected: Dict[str, dict] = {}
    error = ""
    page = (await context.new_page() if context is not None
            else await mgr.new_page(identity, block_media))

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


def _dig_danmaku_list(data, depth: int = 0) -> list:
    """从播放页/创作中心响应中递归提取弹幕数组。"""
    if depth > 5:
        return []
    if isinstance(data, list):
        rows = [x for x in data if isinstance(x, dict)]
        if rows and any(any(k in x for k in (
                 "danmaku_id", "barrage_id", "bullet_id", "content",
                 "text", "danmaku_text", "time_point", "video_time", "offset_time"))
                for x in rows):
            return rows
        for value in data:
            found = _dig_danmaku_list(value, depth + 1)
            if found:
                return found
        return []
    if not isinstance(data, dict):
        return []
    for key in ("danmaku_list", "barrage_list", "bullet_list", "danmakus",
                "barrages", "items", "list", "data"):
        value = data.get(key)
        found = _dig_danmaku_list(value, depth + 1)
        if found:
            return found
    for value in data.values():
        if isinstance(value, (dict, list)):
            found = _dig_danmaku_list(value, depth + 1)
            if found:
                return found
    return []


_PROBE_DANMAKU_JS = """async (options) => {
  const video = document.querySelector('video');
  if (!video) {
    window.scrollBy(0, 800);
    return { ok: false, duration: 0 };
  }
  const cfg = (options && typeof options === 'object') ? options : {};
  try { await video.play(); } catch (_) {}
  await new Promise(resolve => setTimeout(resolve, 180));
  const durationHint = Number(cfg.duration || 0);
  const duration = Number.isFinite(video.duration) && video.duration > 0
    ? video.duration : durationHint;
  const start = Math.max(0, Number(cfg.start_ms || 0) / 1000);
  const requestedEnd = Number(cfg.end_ms || 0) / 1000;
  const end = Math.max(start, Math.min(duration || requestedEnd || start, requestedEnd > 0 ? requestedEnd : (duration || start)));
  const step = Math.max(0.25, Number(cfg.step_seconds || 1));
  const maxPoints = Math.max(1, Number(cfg.max_points || 120));
  const span = Math.max(0, end - start);
  const actualStep = span > 0 ? Math.max(step, span / Math.max(1, maxPoints - 1)) : step;
  const points = [];
  if (span <= 0) {
    points.push(start);
  } else {
    for (let point = start; point <= end + 0.01 && points.length < maxPoints; point += actualStep) {
      points.push(Math.min(end, point));
    }
    if (points[points.length - 1] < end - 0.01 && points.length < maxPoints) points.push(end);
  }
  try { await video.play(); } catch (_) {}
  for (const point of points) {
    try {
      if (duration > 0) video.currentTime = Math.max(0, Math.min(duration - .05, point));
      video.dispatchEvent(new Event('timeupdate'));
    } catch (_) {}
    await new Promise(resolve => setTimeout(resolve, 500));
  }
  try { video.pause(); } catch (_) {}
  return { ok: true, duration, points: points.length, start, end };
}"""


def _is_danmaku_url(url: str, creator: bool = False) -> bool:
    low = (url or "").lower()
    if creator and "creator.douyin.com" not in low:
        return False
    return (DANMAKU_API in low or "/danmaku/" in low
            or "danmaku/get" in low or "barrage" in low)


def _danmaku_position_ms(row: dict) -> int:
    for key in ("video_time_ms", "position_ms", "time_ms", "offset_time",
                "offsetTime", "video_offset"):
        value = row.get(key)
        if value not in (None, ""):
            try:
                return max(0, int(float(value)))
            except (TypeError, ValueError):
                pass
    for key in ("time_point", "video_time", "timepoint", "position"):
        value = row.get(key)
        if value not in (None, ""):
            try:
                return max(0, int(float(value) * 1000))
            except (TypeError, ValueError):
                pass
    return 0


async def fetch_danmaku(mgr: BrowserManager, identity: Identity, aweme_id: str,
                        known_ids: Set[str], duration: int = 0,
                        max_rounds: int = 4, settle_ms: int = 1800,
                        block_media: bool = False, start_ms: int = 0,
                        end_ms: int = 0, step_seconds: float = 1.0,
                        max_points: int = 120, max_items: int = 0
                        ) -> Tuple[List[dict], str]:
    """打开公开视频页，拦截播放器弹幕接口并按视频时间点收集弹幕。"""
    collected: Dict[str, dict] = {}
    error = ""
    page = await mgr.new_page(identity, block_media)

    async def on_response(resp):
        if not _is_danmaku_url(resp.url):
            return
        try:
            data = await resp.json()
        except Exception:
            return
        for row in _dig_danmaku_list(data):
            key = danmaku_key(row)
            if key:
                collected[key] = row

    page.on("response", on_response)
    try:
        await page.goto(f"https://www.douyin.com/video/{aweme_id}",
                        wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(settle_ms)
        stagnant = 0
        attempts = max(1, min(max_rounds, 2 if step_seconds > 0 else 8))
        for _ in range(attempts):
            before = len(collected)
            try:
                await page.evaluate(_PROBE_DANMAKU_JS, {
                    "duration": duration, "start_ms": max(0, start_ms),
                    "end_ms": max(0, end_ms),
                    "step_seconds": max(0.25, float(step_seconds or 1)),
                    "max_points": max(1, int(max_points or 120)),
                })
            except Exception:
                pass
            await page.wait_for_timeout(settle_ms)
            if len(collected) == before:
                stagnant += 1
                if stagnant >= 2:
                    break
            else:
                stagnant = 0
            if step_seconds > 0 and collected:
                break
    except Exception as e:
        error = f"打开作品页失败: {e!r}"
    finally:
        try:
            await page.close()
        except Exception:
            pass

    if not collected and not error:
        error = "未拦截到视频弹幕(可能未开启弹幕/页面未加载/接口已改版)"
    new = [row for key, row in collected.items() if key not in known_ids]
    new.sort(key=lambda row: (_danmaku_position_ms(row), danmaku_key(row)))
    if max_items > 0:
        new = new[:max_items]
    return new, error


async def fetch_creator_danmaku(mgr: BrowserManager, identity: Identity,
                                known_ids: Set[str], page_url: str,
                                aweme_id: str = "", max_scrolls: int = 8,
                                settle_ms: int = 1600,
                                block_media: bool = True, max_items: int = 0
                                ) -> Tuple[List[dict], str]:
    """打开创作中心弹幕管理页，拦截弹幕列表接口。

    创作中心页面/接口属于实验性网页能力，页面地址和字段变化集中在此处适配。
    aweme_id 非空时只保留目标作品；为空时返回账号范围内的弹幕。
    """
    collected: Dict[str, dict] = {}
    error = ""
    page = await mgr.new_page(identity, block_media)

    async def on_response(resp):
        if not _is_danmaku_url(resp.url, creator=True):
            return
        try:
            data = await resp.json()
        except Exception:
            return
        for row in _dig_danmaku_list(data):
            if max_items > 0 and len(collected) >= max_items:
                break
            row_aweme = str(row.get("aweme_id") or row.get("item_id")
                            or row.get("group_id") or row.get("object_id") or "")
            if aweme_id and row_aweme and row_aweme != str(aweme_id):
                continue
            key = danmaku_key(row)
            if key:
                collected[key] = row

    page.on("response", on_response)
    try:
        await page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(settle_ms)
        if "/login" in page.url or "passport" in page.url:
            error = "创作者登录态已失效,请重新创作者登录"
        else:
            stagnant = 0
            for _ in range(max(1, min(max_scrolls, 20))):
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
                error = error or "未拦截到创作中心弹幕(页面/接口可能已改版)"
    except Exception as e:
        error = f"打开创作中心弹幕页失败: {e!r}"
    finally:
        try:
            await page.close()
        except Exception:
            pass

    new = [row for key, row in collected.items() if key not in known_ids]
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
                               settle_ms: int = 1800, timeout_ms: int = 12000,
                               require_reply: bool = False
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
    if require_reply and not (reply_to_text or "").strip():
        return False, "缺少目标评论原文，已跳过回复"
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
            if editor is None and require_reply:
                return False, "未找到目标评论回复区，未发送成顶层评论"

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
    nested = data.get("data")
    for u in (data.get("user"), data.get("user_info"),
              nested.get("user") if isinstance(nested, dict) else None,
              nested.get("user_info") if isinstance(nested, dict) else None,
              data):
        if isinstance(u, dict) and u.get("sec_uid"):
            return u
    return None


def _extract_post_author(data, expected_sec_uid: str = "") -> Optional[dict]:
    """从本人作品接口里取作者。

    /user/profile/self 偶尔会因页面缓存而不发，但同一页面通常仍会请求
    /aweme/post。只有作者 sec_uid 与本地登录用户一致时才接纳，避免把
    精选页或其它主页的作者误绑到当前账号。
    """
    if not isinstance(data, dict):
        return None
    for item in data.get("aweme_list") or []:
        if not isinstance(item, dict):
            continue
        user = item.get("author")
        if not isinstance(user, dict) or not user.get("sec_uid"):
            continue
        if expected_sec_uid and str(user.get("sec_uid")) != expected_sec_uid:
            continue
        return user
    return None


def _user_from_web_storage(value) -> Optional[dict]:
    """把网页 localStorage 的 user_info 归一成抖音 user 形状。

    2026 版网页会稳定写入：
      {uid: <sec_uid>, nickname: ..., avatarUrl: ...}
    即使 profile/self 被缓存而未发，这份登录用户身份仍可用于打开显式主页。
    数字 uid 是内部账号号，不当作 sec_uid，防止构造错误主页。
    """
    if not isinstance(value, dict):
        return None
    sec_uid = str(value.get("sec_uid") or value.get("secUid") or "")
    if not sec_uid:
        uid = str(value.get("uid") or "")
        if len(uid) >= 24 and not uid.isdigit():
            sec_uid = uid
    if not sec_uid:
        return None
    user = {
        "sec_uid": sec_uid,
        "nickname": value.get("nickname") or value.get("name") or "",
    }
    avatar = value.get("avatarUrl") or value.get("avatar_url") or value.get("avatar")
    if isinstance(avatar, str) and avatar:
        user["avatar_thumb"] = {"url_list": [avatar]}
    return user


_READ_SELF_STORAGE_JS = """() => {
  const out = [];
  const keys = ['user_info', 'userInfo', 'user_info_passport'];
  for (const store of [window.localStorage, window.sessionStorage]) {
    for (const key of keys) {
      try {
        const raw = store.getItem(key);
        if (!raw) continue;
        let value = JSON.parse(raw);
        if (typeof value === 'string') value = JSON.parse(value);
        if (value && typeof value === 'object') out.push(value);
      } catch (_) {}
    }
  }
  return out;
}"""


async def _read_self_from_web_storage(page) -> Optional[dict]:
    try:
        values = await page.evaluate(_READ_SELF_STORAGE_JS)
    except Exception:
        return None
    merged: dict = {}
    for value in values or []:
        user = _user_from_web_storage(value)
        if not user:
            continue
        if merged.get("sec_uid") and user["sec_uid"] != merged["sec_uid"]:
            continue
        for key, item in user.items():
            if item not in (None, "", [], {}):
                merged[key] = item
    return merged or None


def _fill_missing_user_fields(target: dict, source: Optional[dict]) -> None:
    """用弱来源补空字段，不覆盖 profile/self 已返回的权威字段。"""
    for key, value in (source or {}).items():
        if key not in target or target[key] in (None, "", [], {}):
            target[key] = value


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


def _self_profile_session_is_invalid(has_login_btn, has_login_cookie,
                                     has_result: bool,
                                     profile_user_seen: bool) -> bool:
    """根据页面和 Cookie 证据判断本人登录态是否已经失效。

    登录 Cookie 即使仍在有效期内，也可能已被服务端撤销；页面明确显示可见的
    “登录”入口时，应优先判定为退出状态，避免把 localStorage 或缓存接口中的
    旧资料误当成一次成功的登录校验。
    """
    if has_login_btn is True:
        return True
    return bool(has_result and not profile_user_seen and has_login_cookie is False)


async def fetch_self_profile(mgr: BrowserManager, identity: Identity,
                             timeout_ms: int = 15000, block_media: bool = False
                             ) -> Tuple[dict, str]:
    """打开自己的主页,拦截 user/profile/self 拿登录账号真实资料。
    返回 (user dict, error)。error == "logged_out" 表示登录态失效。

    新版网页可能把 /user/self 重定向到 /jingxuan，且因 user_self_cache AB
    不再发 profile/self。此时从 localStorage.user_info 取得本人 sec_uid，
    再打开 /user/<sec_uid> 触发资料请求；仍未触发时，用同页 aweme/post 中
    sec_uid 完全匹配的 author 兜底。无作品账号至少可返回昵称、头像和 sec_uid。

    拦截时若页面正在跳转，Playwright 读 body 会失败，故再补一发页内 refetch
    （抖音自己的 fetch 拦截器会补 a_bogus 签名）。
    注:query/user 不是资料接口,它返回的是设备会话记录(user_uid/browser_name),无 sec_uid。"""
    result: dict = {}
    api_seen = []                   # 看到的抖音 API 请求(诊断用)
    hit_apis = []                   # 命中的 profile/self(判断是"没发"还是"读不到")
    shapes = []                     # 命中但挖不出 user 时的响应结构/读取异常(标定用)
    self_urls: List[str] = []       # profile/self 的完整 URL(带 query),供页内 refetch 复用
    post_users: List[dict] = []     # profile/self 不发时，用本人作品 author 兜底
    storage_uid = ""
    profile_user_seen = False
    error = ""
    page = await mgr.new_page(identity, block_media)

    def is_profile(resp):
        # 只认自己的 profile/self:profile/other 是看别人主页时发的,拦了会绑错号
        return SELF_PROFILE_API in resp.url

    async def on_response(resp):
        nonlocal profile_user_seen
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
                profile_user_seen = True
            elif isinstance(data, dict) and len(shapes) < 4:
                shapes.append(f"{path} keys={sorted(data)[:12]}")
        elif POST_API in url and resp.status == 200:
            try:
                data = await resp.json()
            except Exception:
                return
            # 此时可能还没读到 localStorage，先暂存；稍后按本人 sec_uid 严格筛选。
            u = _extract_post_author(data)
            if u:
                post_users.append(u)

    page.on("response", on_response)
    logged_out = False
    final_url = ""
    has_login_btn = None
    has_login_cookie = None
    try:
        # 先走本人路由；它即使重定向，登录页脚本通常也已经写好 user_info。
        for url in ("https://www.douyin.com/user/self", "https://www.douyin.com/"):
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # 等 hydrate/XHR；wait_for_response 若在 goto 后调用会漏掉已经返回的响应，
            # 因此统一由上面的 response handler 收集。
            await page.wait_for_timeout(min(max(timeout_ms // 4, 1800), 4000))
            final_url = page.url
            if "passport" in final_url or "/login" in final_url:
                logged_out = True
                break

            storage_user = await _read_self_from_web_storage(page)
            if storage_user:
                storage_uid = str(storage_user.get("sec_uid") or "")
                _fill_missing_user_fields(result, storage_user)
                # aweme/post 可能比 localStorage 更早返回；现在才能安全确认它是本人。
                post_user = next(
                    (u for u in post_users
                     if str(u.get("sec_uid") or "") == storage_uid),
                    None,
                )
                if post_user:
                    result.update(post_user)
            if result:
                break

        # profile/self 被缓存/路由改写时，显式本人 sec_uid 路由会重新触发它。
        # 已经命中过权威资料接口则无需多跳一次。
        if storage_uid and not profile_user_seen:
            await page.goto(f"https://www.douyin.com/user/{storage_uid}",
                            wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(min(max(timeout_ms // 3, 2200), 5000))
            final_url = page.url
            post_user = next(
                (u for u in reversed(post_users)
                 if str(u.get("sec_uid") or "") == storage_uid),
                None,
            )
            if post_user:
                result.update(post_user)

        if not profile_user_seen and self_urls:
            # 拦到了但 body 读不到:页内重发一次(此时页面已静止,不会再丢 body)
            data = await _refetch_in_page(page, self_urls[-1])
            u = _extract_user(data)
            if u:
                result.update(u)
                profile_user_seen = True
            elif isinstance(data, dict) and len(shapes) < 6:
                shapes.append(f"refetch keys={sorted(data)[:12]}")
        # 是否能看到“登录”按钮(看到=其实没登录进去)
        try:
            has_login_btn = await page.get_by_text("登录", exact=True).first.is_visible(
                timeout=1500)
        except Exception:
            has_login_btn = None
        try:
            cookies = await page.context.cookies("https://www.douyin.com/")
            has_login_cookie = any(c.get("name") in _LOGIN_COOKIES for c in cookies)
        except Exception:
            has_login_cookie = None

        # 页面上的可见登录入口是最直接的退出证据。Cookie 可能尚未过期但已被
        # 服务端撤销；localStorage/profile 缓存也可能继续返回旧账号资料。
        if _self_profile_session_is_invalid(
                has_login_btn, has_login_cookie, bool(result), profile_user_seen):
            result.clear()
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
        elif has_login_cookie is False:
            error = "logged_out"
        elif not error:
            # 区分「接口没发出来」和「发了但取不到 user」——之前一律报后者,误导排查
            error = ("profile/self 命中但取不到 user" if hit_apis else "no_profile_xhr")
        print(f"[self_profile] 未拿到资料; err={error}; final_url={final_url}; "
              f"login_btn_visible={has_login_btn}; login_cookie={has_login_cookie}; "
              f"storage_uid={bool(storage_uid)}; hit={hit_apis}; shapes={shapes}; "
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
