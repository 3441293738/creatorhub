"""视频号 Web 抓取(浏览器自动化 + 拦截 mmfinderassistant-bin 响应),对标 ks_fetcher.py。

视频号创作者助手(channels.weixin.qq.com)所有数据都打在
`cgi-bin/mmfinderassistant-bin/*` 这组 POST 接口上,依赖 `_finder_auth` 登录态。
用账号持久 profile 打开助手页,拦截它自身发出的响应即可拿到本账号数据(免签名):
  - 打开 /platform            -> auth/get_auth_info / auth/auth_data(本账号资料)
  - 打开 /platform/post/list  -> post/post_list(本账号作品 + 统计)
  - 打开作品评论管理           -> comment/*(评论)

⚠️ 视频号只有「本账号」数据,没有对外公开作品接口 —— 不支持监控别人。
⚠️ 接口路径 / 响应字段需用真实账号校准:所有函数拿不到数据时会打印 [channels_*]
   api_seen 诊断(看到的 mmfinderassistant 接口清单 + 样本),照它把下面的 *_API
   常量和 _dig_* 候选键固化即可(与项目 README 的「接口标定」流程一致)。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from .identity import Identity
from .manager import BrowserManager

BASE = "https://channels.weixin.qq.com"
PLATFORM_URL = BASE + "/platform"
# 视频和图文在助手里是两个独立路由，且都会请求 post/post_list，只是
# userpageType 分别为 Video(11) / Photo(10)。只打开 /post/list 会漏掉
# 账号的全部图文作品。
POST_VIDEO_LIST_URL = BASE + "/platform/post/list"
POST_IMAGE_LIST_URL = BASE + "/platform/post/finderNewLifePostList"
POST_LIST_URL = POST_VIDEO_LIST_URL  # 向后兼容旧引用
POST_LIST_URLS = (POST_VIDEO_LIST_URL, POST_IMAGE_LIST_URL)

# 拦截用的接口 URL 片段(子串匹配;micro/content、micro/statistic 前缀也一并覆盖)
AUTH_API = "mmfinderassistant-bin/auth/"          # get_auth_info / auth_data
POST_LIST_API = "mmfinderassistant-bin/post/post_list"
STAT_API = "mmfinderassistant-bin/statistic/post_list"
COMMENT_API = "mmfinderassistant-bin/comment/"


def _rf(d: dict, *keys, default=""):
    for k in keys:
        v = (d or {}).get(k)
        if v not in (None, "", [], {}):
            return v
    return default


def _dig_data(data: dict) -> dict:
    """mmfinderassistant 统一包一层 {errCode, data:{...}};取出 data。"""
    if not isinstance(data, dict):
        return {}
    d = data.get("data")
    return d if isinstance(d, dict) else data


def _dig_posts(data: dict) -> list:
    """从 post_list 响应里挖出作品数组(list / post_list / object_list 兜底)。"""
    d = _dig_data(data)
    for key in ("list", "post_list", "object_list", "objectList", "feeds", "posts"):
        v = d.get(key)
        if isinstance(v, list):
            return v
    return []


def _dig_comments(data: dict) -> list:
    d = _dig_data(data)
    for key in ("comment", "comments", "commentList", "list", "root_comments"):
        v = d.get(key)
        if isinstance(v, list):
            return v
    return []


def _obj_id(it: dict) -> str:
    return str(_rf(it, "objectId", "exportId", "id", default="") or "")


async def fetch_channels_self_profile(mgr: BrowserManager, identity: Identity,
                                      block_media: bool = False
                                      ) -> Tuple[dict, str]:
    """拿登录账号(视频号)的资料。打开助手首页,拦截 auth/get_auth_info(或 auth_data)。
    返回 (finder info dict, error);error == "logged_out" 表示登录态失效。"""
    result: dict = {}
    error = ""
    api_seen: list = []
    page = await mgr.new_page(identity, block_media)

    async def on_response(resp):
        nonlocal result
        url = resp.url
        if "mmfinderassistant-bin" in url and len(api_seen) < 40:
            p = url.split("?")[0].split("mmfinderassistant-bin")[-1]
            api_seen.append(f"{resp.status} {p}")
        if AUTH_API not in url:
            return
        try:
            data = await resp.json()
        except Exception:
            return
        d = _dig_data(data)
        # finder 信息可能在 data.finderUser / data.finder / data 本身
        finder = _rf(d, "finderUser", "finder", "finderInfo", default=None) or d
        if isinstance(finder, dict) and (finder.get("nickname") or finder.get("nickName")):
            result = d       # 交 parse_self_user 归一(它会再兜底找 finderUser)

    page.on("response", on_response)
    final_url = ""
    try:
        await page.goto(PLATFORM_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3500)
        final_url = page.url
        if "/login" in final_url or "login.html" in final_url:
            error = "logged_out"
    except Exception as e:
        error = f"{e!r}"
    finally:
        try:
            await page.close()
        except Exception:
            pass

    if not result and not error:
        error = "no_profile_data"
    if not result:
        print(f"[channels_self_profile] err={error} final_url={final_url} "
              f"api_seen({len(api_seen)})={api_seen[:40]}")
    return result, error


async def fetch_channels_works(mgr: BrowserManager, identity: Identity,
                               known_ids: Set[str], max_scrolls: int = 8,
                               settle_ms: int = 1800, block_media: bool = True
                               ) -> Tuple[List[dict], Optional[dict], str]:
    """依次打开视频号助手的视频/图文管理页，拦截 post/post_list 收集作品。
    返回 (新作品列表, author(视频号无独立 author,返回 None), error)。"""
    collected: Dict[str, dict] = {}
    error = ""
    api_seen: list = []
    sample: list = []
    post_response_count = 0
    post_totals: list = []
    post_errors: list = []
    page = await mgr.new_page(identity, block_media)

    async def on_response(resp):
        nonlocal post_response_count
        url = resp.url
        if "mmfinderassistant-bin" in url and len(api_seen) < 40:
            p = url.split("?")[0].split("mmfinderassistant-bin")[-1]
            api_seen.append(f"{resp.status} {p}")
        if POST_LIST_API not in url and STAT_API not in url:
            return
        post_response_count += 1
        try:
            data = await resp.json()
        except Exception as e:
            post_errors.append(f"json:{type(e).__name__}")
            return
        d = _dig_data(data)
        total = _rf(d, "totalCount", "total_count", default=None)
        if total is not None:
            post_totals.append(total)
        err_code = _rf(data, "errCode", "err_code", default=0)
        if err_code not in (0, "0", None, ""):
            post_errors.append(str(err_code))
        posts = _dig_posts(data)
        for it in posts:
            if not isinstance(it, dict):
                continue
            oid = _obj_id(it)
            if not oid:
                continue
            # 统计接口的项可能只带增量字段:与已收集项浅合并
            if oid in collected:
                collected[oid].update(it)
            else:
                collected[oid] = it
            if not sample:
                sample.append(str(it)[:1200])

    page.on("response", on_response)
    final_url = ""
    final_urls: list = []
    navigation_errors: list = []
    try:
        # 视频和图文是两套列表。即使第一套已经遇到 known_ids，也必须继续打开
        # 第二套，否则“只有图文”的账号会被误判为没有作品。
        for list_url in POST_LIST_URLS:
            try:
                await page.goto(list_url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(settle_ms)
                stagnant = 0
                for _ in range(max_scrolls):
                    before = len(collected)
                    try:
                        await page.mouse.wheel(0, 4000)
                    except Exception:
                        pass
                    await page.wait_for_timeout(settle_ms)
                    if len(collected) == before:
                        stagnant += 1
                        if stagnant >= 2:
                            break
                    else:
                        stagnant = 0
                final_url = page.url
                final_urls.append(final_url)
            except Exception as e:
                navigation_errors.append(f"{list_url}: {e!r}")
                continue

        if not collected:
            if any("/login" in u or "login.html" in u for u in final_urls):
                error = "logged_out:登录态失效,请重新登录"
            elif (post_response_count and post_totals
                  and all(str(n) in ("0", "0.0") for n in post_totals)):
                error = "当前账号没有已发布的视频或图文作品"
            elif post_response_count:
                error = "作品接口已响应但未解析到作品数据(见 channels_works 日志)"
            elif navigation_errors:
                error = "打开作品页失败: " + "; ".join(navigation_errors)
            else:
                error = "未拦截到作品数据(视频号只支持本账号;或未登录/页面改版)"
    finally:
        try:
            await page.close()
        except Exception:
            pass

    if not collected:
        print(f"[channels_works] final_urls={final_urls or [final_url]} "
              f"post_responses={post_response_count} totals={post_totals} "
              f"post_errors={post_errors} "
              f"api_seen({len(api_seen)})={api_seen[:40]}")
    elif sample:
        print(f"[channels_works] sample={sample[0]}")

    new_items = [it for oid, it in collected.items() if oid not in known_ids]
    return new_items, None, error


async def fetch_channels_comments(mgr: BrowserManager, identity: Identity,
                                  object_id: str, known_cids: Set[str],
                                  max_scrolls: int = 6, settle_ms: int = 1600,
                                  block_media: bool = True
                                  ) -> Tuple[List[dict], str]:
    """打开某条本账号作品的评论管理,拦截 comment 接口收集评论。
    返回 (新评论原始列表, error)。⚠️ 评论管理页 URL 需校准(下面用 query 传 objectId 兜底)。"""
    collected: Dict[str, dict] = {}
    error = ""
    api_seen: list = []
    page = await mgr.new_page(identity, block_media)

    async def on_response(resp):
        url = resp.url
        if "mmfinderassistant-bin" in url and len(api_seen) < 40:
            p = url.split("?")[0].split("mmfinderassistant-bin")[-1]
            api_seen.append(f"{resp.status} {p}")
        if COMMENT_API not in url:
            return
        try:
            data = await resp.json()
        except Exception:
            return
        for c in _dig_comments(data):
            cid = str(_rf(c, "commentId", "comment_id", "id", default="") or "")
            if cid:
                collected[cid] = c

    page.on("response", on_response)
    try:
        # 评论管理页路径未定,先落作品管理页(其内可展开评论),URL 需真机校准
        await page.goto(f"{POST_LIST_URL}?objectId={object_id}",
                        wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(settle_ms)
        stagnant = 0
        for _ in range(max_scrolls):
            before = len(collected)
            try:
                await page.mouse.wheel(0, 3000)
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
        error = f"打开评论页失败: {e!r}"
    finally:
        try:
            await page.close()
        except Exception:
            pass

    if not collected and not error:
        error = "未拦截到评论(评论管理页 URL 需校准,见 api_seen 日志)"
        print(f"[channels_comments] object_id={object_id} "
              f"api_seen({len(api_seen)})={api_seen[:40]}")
    new = [c for cid, c in collected.items() if cid not in known_cids]
    return new, error


# 视频号评论回复输入框/发送按钮选择器(评论管理页,需校准)
_COMMENT_INPUT = [
    'textarea[placeholder*="回复"]',
    'div[contenteditable="true"][placeholder*="评论"]',
    '.comment-reply-input textarea',
    'textarea',
]
_COMMENT_SUBMIT = [
    'button:has-text("发送")',
    'button:has-text("回复")',
    '.weui-desktop-btn_primary',
]


async def post_channels_comment(mgr: BrowserManager, identity: Identity, object_id: str,
                                content: str, reply_to_text: str = "", headed: bool = True,
                                settle_ms: int = 1800, timeout_ms: int = 12000
                                ) -> Tuple[bool, str]:
    """在视频号评论管理页回复本账号作品的评论。
    ⚠️ 视频号「主动去别人作品下评论」在助手端不可行 —— 只支持回复自己作品的评论(auto_reply)。
    选择器需校准,集中在 _COMMENT_INPUT / _COMMENT_SUBMIT。返回 (ok, error)。"""
    content = (content or "").strip()
    if not content:
        return False, "空文案"
    ctx = None
    if headed:
        ctx = await mgr.open_headed(identity)
        page = await ctx.new_page()
    else:
        page = await mgr.new_page(identity, block_media=False)
    try:
        await page.goto(f"{POST_LIST_URL}?objectId={object_id}",
                        wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(settle_ms)
        if "/login" in page.url or "login.html" in page.url:
            return False, "logged_out:视频号未登录,无法回复评论"

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
            return False, "未找到评论回复框(评论管理页选择器需校准)"
        await editor.click(timeout=timeout_ms)
        await page.keyboard.type(content, delay=40)
        await page.wait_for_timeout(500)
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
            return False, "未找到发送按钮(选择器需校准)"
        await page.wait_for_timeout(1500)
        return True, ""
    except Exception as e:
        return False, f"回复评论异常: {e!r}"
    finally:
        try:
            if ctx is not None:
                await ctx.close()
            else:
                await page.close()
        except Exception:
            pass
