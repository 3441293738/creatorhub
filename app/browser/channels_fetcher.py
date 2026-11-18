"""视频号 Web 抓取(创作者助手 channels.weixin.qq.com 浏览器自动化 + 响应拦截)。

视频号创作者助手所有管理数据打在 `cgi-bin/mmfinderassistant-bin/*` POST 接口上，依赖 `_finder_auth` 登录态。
由于助手前端采用 wujie 微前端架构(实际业务 DOM 位于子 iframe / 开放 shadowRoot 内)，
本模块统一跨 `page.frames` 穿透定位与滚动，并兼容拦截 `pc_findergetcommentlist`。
公开作品搜索、第三方创作者作品、加密视频解码及全量二级回复协议分页由
`app.platforms.channels.ChannelsProtocolClient` 负责。
"""
from __future__ import annotations

from typing import Dict, List, Optional, Set, Tuple

from .identity import Identity
from .manager import BrowserManager

BASE = "https://channels.weixin.qq.com"
PLATFORM_URL = BASE + "/platform"
POST_VIDEO_LIST_URL = BASE + "/platform/post/list"
POST_IMAGE_LIST_URL = BASE + "/platform/post/finderNewLifePostList"
COMMENT_MGMT_URL = BASE + "/platform/comment"
POST_LIST_URL = POST_VIDEO_LIST_URL  # 向后兼容旧引用
POST_LIST_URLS = (POST_VIDEO_LIST_URL, POST_IMAGE_LIST_URL)

AUTH_API = "mmfinderassistant-bin/auth/"
POST_LIST_API = "mmfinderassistant-bin/post/post_list"
STAT_API = "mmfinderassistant-bin/statistic/post_list"
COMMENT_API = "mmfinderassistant-bin/comment/"
FINDER_COMMENT_API = "pc_findergetcommentlist"


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
    for key in ("list", "post_list", "object_list", "objectList", "object", "feeds", "posts"):
        v = d.get(key)
        if isinstance(v, list):
            return v
    return []


def _dig_comments(data: dict) -> list:
    d = _dig_data(data)
    for key in (
        "commentInfo",
        "comment_info",
        "comment",
        "comments",
        "commentList",
        "comment_list",
        "list",
        "root_comments",
    ):
        v = d.get(key)
        if isinstance(v, list):
            return v
    return []


def _obj_id(it: dict) -> str:
    return str(_rf(it, "objectId", "exportId", "id", default="") or "")


async def _find_in_frames(page, selectors: List[str]):
    """在主页面与所有 wujie 子 iframe 中查找第一个匹配定位器。"""
    for frame in page.frames:
        for sel in selectors:
            try:
                loc = frame.locator(sel).first
                if await loc.count():
                    return loc, frame
            except Exception:
                continue
    return None, None


async def _click_in_frames(page, selectors: List[str], timeout: int = 3000) -> bool:
    for frame in page.frames:
        for sel in selectors:
            try:
                loc = frame.locator(sel).first
                if await loc.count():
                    await loc.click(timeout=timeout)
                    return True
            except Exception:
                continue
    return False


async def _scroll_all_frames(page, delta_y: int = 3000) -> None:
    try:
        await page.mouse.wheel(0, delta_y)
    except Exception:
        pass
    for frame in page.frames:
        try:
            await frame.evaluate(
                """(dy) => {
                    window.scrollBy(0, dy);
                    const nodes = document.querySelectorAll(
                        '[class*="scroll"], [class*="list"], [class*="comment"], main'
                    );
                    for (const el of nodes) {
                        if (el.scrollHeight > el.clientHeight + 20) {
                            el.scrollTop += dy;
                        }
                    }
                }""",
                delta_y,
            )
        except Exception:
            continue


async def fetch_channels_self_profile(
    mgr: BrowserManager, identity: Identity, block_media: bool = False
) -> Tuple[dict, str]:
    """拿登录账号(视频号)的资料。打开助手首页,拦截 auth/get_auth_info(或 auth_data)。"""
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
        finder = _rf(d, "finderUser", "finder", "finderInfo", default=None) or d
        if isinstance(finder, dict) and (finder.get("nickname") or finder.get("nickName")):
            result = d

    page.on("response", on_response)
    final_url = ""
    has_login_btn = None
    try:
        await page.goto(PLATFORM_URL, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3500)
        final_url = page.url
        if "/login" in final_url or "login.html" in final_url:
            error = "logged_out"
        try:
            has_login_btn = await page.get_by_text("登录", exact=True).first.is_visible(timeout=1200)
        except Exception:
            has_login_btn = None
        if has_login_btn is True:
            result = {}
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
        print(
            f"[channels_self_profile] err={error} final_url={final_url} login_btn={has_login_btn} "
            f"api_seen({len(api_seen)})={api_seen[:40]}"
        )
    return result, error


async def fetch_channels_works(
    mgr: BrowserManager,
    identity: Identity,
    known_ids: Set[str],
    max_scrolls: int = 8,
    settle_ms: int = 1800,
    block_media: bool = True,
) -> Tuple[List[dict], Optional[dict], str]:
    """依次打开视频号助手的视频/图文管理页，拦截 post/post_list 收集作品。"""
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
        for list_url in POST_LIST_URLS:
            try:
                await page.goto(list_url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(settle_ms)
                stagnant = 0
                for _ in range(max_scrolls):
                    before = len(collected)
                    await _scroll_all_frames(page, 4000)
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
            elif (
                post_response_count
                and post_totals
                and all(str(n) in ("0", "0.0") for n in post_totals)
            ):
                error = "当前账号没有已发布的视频或图文作品"
            elif post_response_count:
                error = "作品接口已响应但未解析到作品数据(见 channels_works 日志)"
            elif navigation_errors:
                error = "打开作品页失败: " + "; ".join(navigation_errors)
            else:
                error = "未拦截到作品数据(请确认登录态有效；跨账号公开采集请启用微信原生 CDP 协议)"
    finally:
        try:
            await page.close()
        except Exception:
            pass

    if not collected:
        print(
            f"[channels_works] final_urls={final_urls or [final_url]} "
            f"post_responses={post_response_count} totals={post_totals} "
            f"post_errors={post_errors} "
            f"api_seen({len(api_seen)})={api_seen[:40]}"
        )
    elif sample:
        print(f"[channels_works] sample={sample[0]}")

    new_items = [it for oid, it in collected.items() if oid not in known_ids]
    return new_items, None, error


_OPEN_COMMENT_DRAWER_SEL = [
    'text=评论管理',
    'a:has-text("评论管理")',
    'li:has-text("评论管理")',
    'button:has-text("评论")',
    '[class*="comment-count"]',
    '[class*="comment-btn"]',
]


async def fetch_channels_comments(
    mgr: BrowserManager,
    identity: Identity,
    object_id: str,
    known_cids: Set[str],
    max_scrolls: int = 6,
    settle_ms: int = 1600,
    block_media: bool = True,
) -> Tuple[List[dict], str]:
    """打开创作者助手评论管理/作品页并穿透 wujie iframe 拦截评论接口响应。"""
    collected: Dict[str, dict] = {}
    error = ""
    api_seen: list = []
    comment_resp_seen = 0
    page = await mgr.new_page(identity, block_media)

    async def on_response(resp):
        nonlocal comment_resp_seen
        url = resp.url
        if "mmfinderassistant-bin" in url and len(api_seen) < 40:
            p = url.split("?")[0].split("mmfinderassistant-bin")[-1]
            api_seen.append(f"{resp.status} {p}")
        if COMMENT_API not in url and FINDER_COMMENT_API not in url:
            return
        comment_resp_seen += 1
        try:
            data = await resp.json()
        except Exception:
            return
        for c in _dig_comments(data):
            if not isinstance(c, dict):
                continue
            row_oid = str(_rf(c, "objectId", "exportId", default="") or "")
            if row_oid and object_id and row_oid != str(object_id):
                continue
            cid = str(_rf(c, "commentId", "comment_id", "id", default="") or "")
            if cid:
                collected[cid] = c

    page.on("response", on_response)
    try:
        candidate_urls = (
            f"{COMMENT_MGMT_URL}?objectId={object_id}",
            f"{POST_LIST_URL}?objectId={object_id}",
        )
        for nav_url in candidate_urls:
            await page.goto(nav_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(settle_ms)
            if "/login" in page.url or "login.html" in page.url:
                error = "logged_out:视频号登录态已失效,请重新登录"
                break
            if not collected:
                await _click_in_frames(page, _OPEN_COMMENT_DRAWER_SEL, timeout=2500)
                await page.wait_for_timeout(settle_ms)
            stagnant = 0
            for _ in range(max_scrolls):
                before = len(collected)
                await _scroll_all_frames(page, 3000)
                await page.wait_for_timeout(settle_ms)
                if len(collected) == before:
                    stagnant += 1
                    if stagnant >= 2:
                        break
                else:
                    stagnant = 0
            if collected:
                break
    except Exception as e:
        error = f"打开评论页失败: {e!r}"
    finally:
        try:
            await page.close()
        except Exception:
            pass

    if not collected and not error and comment_resp_seen == 0:
        error = "未拦截到评论响应(若需采集公开作品评论及全量二级回复，请连接桌面微信 Finder CDP 协议通道)"
        print(f"[channels_comments] object_id={object_id} api_seen({len(api_seen)})={api_seen[:40]}")
    new = [c for cid, c in collected.items() if cid not in known_cids]
    return new, error


_COMMENT_INPUT = [
    'textarea[placeholder*="回复"]',
    'div[contenteditable="true"][placeholder*="回复"]',
    'div[contenteditable="true"][placeholder*="评论"]',
    '.comment-reply-input textarea',
    'textarea',
]
_COMMENT_SUBMIT = [
    'button:has-text("发送")',
    'button:has-text("回复")',
    '.weui-desktop-btn_primary',
]


async def post_channels_comment(
    mgr: BrowserManager,
    identity: Identity,
    object_id: str,
    content: str,
    reply_to_text: str = "",
    headed: bool = True,
    settle_ms: int = 1800,
    timeout_ms: int = 12000,
) -> Tuple[bool, str]:
    """在视频号创作者助手评论管理页跨 wujie iframe 回复本账号作品评论。"""
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
        await page.goto(
            f"{COMMENT_MGMT_URL}?objectId={object_id}",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await page.wait_for_timeout(settle_ms)
        if "/login" in page.url or "login.html" in page.url:
            return False, "logged_out:视频号未登录,无法回复评论"

        if reply_to_text:
            snippet = reply_to_text.strip()[:24]
            for frame in page.frames:
                try:
                    row = frame.get_by_text(snippet, exact=False).first
                    if await row.count():
                        reply_btn = row.locator('xpath=ancestor::*[self::li or self::div][1]//button[contains(.,"回复")]').first
                        if await reply_btn.count():
                            await reply_btn.click(timeout=2500)
                            await page.wait_for_timeout(500)
                            break
                except Exception:
                    continue

        editor, _ = await _find_in_frames(page, _COMMENT_INPUT)
        if editor is None:
            return False, "未找到评论回复框(评论管理页选择器需校准)"
        await editor.click(timeout=timeout_ms)
        await page.keyboard.type(content, delay=40)
        await page.wait_for_timeout(500)

        sent = False
        for frame in page.frames:
            for sel in _COMMENT_SUBMIT:
                try:
                    btn = frame.locator(sel).first
                    if await btn.count() and await btn.is_enabled():
                        await btn.click(timeout=3000)
                        sent = True
                        break
                except Exception:
                    continue
            if sent:
                break
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
