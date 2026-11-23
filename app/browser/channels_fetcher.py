"""视频号 Web 抓取(创作者助手 channels.weixin.qq.com 浏览器自动化 + 响应拦截)。

视频号创作者助手所有管理数据打在 `cgi-bin/mmfinderassistant-bin/*` POST 接口上，依赖 `_finder_auth` 登录态。
由于助手前端采用 wujie 微前端架构(实际业务 DOM 位于子 iframe / 开放 shadowRoot 内)，
本模块统一跨 `page.frames` 穿透定位与滚动，并兼容拦截 `pc_findergetcommentlist`。
公开作品搜索、第三方创作者作品、加密视频解码及全量二级回复协议分页由
`app.platforms.channels.ChannelsProtocolClient` 负责。
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from .human_interaction import (
    DEFAULT_HUMAN_ENGINE,
    human_click_first,
    human_fill_first,
    human_scroll,
    human_wait,
)
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
    if not isinstance(it, dict):
        return ""
    for key in ("objectId", "exportId", "id"):
        val = str(it.get(key) or "").strip()
        if val and val != "0":
            return val
    return ""


def _to_nonneg_int(val: Any) -> Optional[int]:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, (int, float)):
        return max(0, int(val))
    s = str(val).strip()
    if not s:
        return None
    try:
        return max(0, int(float(s)))
    except (TypeError, ValueError):
        return None


def _extract_total_count(d: dict) -> Optional[int]:
    if not isinstance(d, dict):
        return None
    for key in ("totalCount", "total_count"):
        if key in d:
            parsed = _to_nonneg_int(d.get(key))
            if parsed is not None:
                return parsed
    return None


def _build_replay_headers(headers: Any) -> Dict[str, str]:
    if not isinstance(headers, dict):
        return {"content-type": "application/json;charset=UTF-8"}
    out: Dict[str, str] = {}
    for raw_k, raw_v in headers.items():
        if not raw_k or raw_v is None:
            continue
        k = str(raw_k).strip().lower()
        if k in ("accept", "content-type") or (
            k.startswith("x-") and not k.startswith(("x-devtools-", "x-playwright-"))
        ):
            out[k] = str(raw_v)
    out.setdefault("content-type", "application/json;charset=UTF-8")
    return out


async def _fetch_post_list_page_in_frame(
    page,
    template: dict,
    current_page: int,
    page_size: int,
) -> Tuple[bool, Optional[dict]]:
    """复用首屏已通过鉴权的 post/post_list 请求模板，在同源上下文按页拉取作品。"""
    base_body = template.get("body")
    if not isinstance(base_body, dict) or not template.get("url"):
        return False, None
    payload = dict(base_body)
    page_key = (
        "currentPage"
        if "currentPage" in payload or "current_page" not in payload
        else "current_page"
    )
    size_key = (
        "pageSize"
        if "pageSize" in payload or "page_size" not in payload
        else "page_size"
    )
    payload[page_key] = int(current_page)
    payload[size_key] = int(page_size)
    if "timestamp" in payload:
        ts_ms = int(time.time() * 1000)
        payload["timestamp"] = (
            str(ts_ms) if isinstance(payload["timestamp"], str) else ts_ms
        )
    req_args = {
        "url": str(template["url"]),
        "headers": template.get("headers") or {"content-type": "application/json;charset=UTF-8"},
        "bodyText": json.dumps(payload, ensure_ascii=False),
    }
    candidate_frames = []
    tpl_frame = template.get("frame")
    if tpl_frame is not None and not getattr(tpl_frame, "is_detached", lambda: False)():
        candidate_frames.append(tpl_frame)
    if page.main_frame not in candidate_frames:
        candidate_frames.append(page.main_frame)
    for frame in candidate_frames:
        try:
            res = await asyncio.wait_for(
                frame.evaluate(
                    """async ({ url, headers, bodyText }) => {
                        const resp = await fetch(url, {
                            method: "POST",
                            headers,
                            body: bodyText,
                            credentials: "include",
                        });
                        const data = await resp.json().catch(() => null);
                        return { ok: resp.ok, status: resp.status, data };
                    }""",
                    req_args,
                ),
                timeout=20.0,
            )
            if isinstance(res, dict) and res.get("ok") and isinstance(res.get("data"), dict):
                err_code = _rf(res["data"], "errCode", "err_code", default=0)
                if err_code in (0, "0", None, ""):
                    return True, res["data"]
            return False, (res.get("data") if isinstance(res, dict) else None)
        except Exception:
            continue
    return False, None


async def _click_next_page_in_frames(page) -> bool:
    """穿透主页面、wujie Shadow DOM 与子 iframe，点击分页器的「下一页」按钮。"""
    for frame in page.frames:
        is_main = frame == page.main_frame
        try:
            hit = await frame.evaluate(
                """({ clickDirectly }) => {
                    const roots = [document];
                    const collectRoots = (root) => {
                        const all = root.querySelectorAll ? root.querySelectorAll('*') : [];
                        for (const el of all) {
                            if (el.shadowRoot) {
                                roots.push(el.shadowRoot);
                                collectRoots(el.shadowRoot);
                            }
                        }
                    };
                    try { collectRoots(document); } catch (e) {}

                    const isVisible = (el) => {
                        if (!el || !el.getBoundingClientRect) return false;
                        const st = window.getComputedStyle(el);
                        if (!st || st.display === 'none' || st.visibility === 'hidden' || Number(st.opacity) === 0) {
                            return false;
                        }
                        const r = el.getBoundingClientRect();
                        return r.width > 2 && r.height > 2;
                    };
                    const isDisabled = (el) => {
                        if (!el) return true;
                        let cur = el;
                        for (let i = 0; i < 3 && cur; i++, cur = cur.parentElement) {
                            const cls = String(cur.className || '');
                            if (
                                cur.disabled ||
                                cur.getAttribute?.('disabled') !== null ||
                                cur.getAttribute?.('aria-disabled') === 'true' ||
                                /disabled/i.test(cls)
                            ) {
                                return true;
                            }
                        }
                        return false;
                    };

                    let target = null;
                    for (const root of roots) {
                        const direct = root.querySelectorAll(
                            '.ant-pagination-next, .rc-pagination-next, .weui-desktop-pagination__nav, [class*="pagination"] a, [class*="pagination"] button, mp-pagination button, mp-pagination a'
                        );
                        for (const el of direct) {
                            if (!isVisible(el) || isDisabled(el)) continue;
                            const txt = (el.innerText || el.textContent || '').trim();
                            const title = (el.getAttribute('title') || el.getAttribute('aria-label') || '').trim();
                            const cls = String(el.className || '');
                            if (
                                txt === '下一页' ||
                                title === '下一页' ||
                                /pagination-next/i.test(cls)
                            ) {
                                target = el;
                                break;
                            }
                        }
                        if (target) break;

                        // MpPagination / weui-desktop-pagination 图标翻页形态: `1 / 24` 右侧的未禁用图标按钮
                        const pagers = root.querySelectorAll(
                            '.weui-desktop-pagination, mp-pagination, [class*="pagination"]'
                        );
                        for (const pg of pagers) {
                            if (!isVisible(pg)) continue;
                            const nodes = [pg, ...pg.querySelectorAll('*')];
                            let indicatorRect = null;
                            for (const n of nodes) {
                                const t = (n.innerText || n.textContent || '').trim();
                                const m = /^(\\d+)\\s*\\/\\s*(\\d+)$/.exec(t);
                                if (m && Number(m[1]) < Number(m[2]) && isVisible(n)) {
                                    indicatorRect = n.getBoundingClientRect();
                                    break;
                                }
                            }
                            if (!indicatorRect) continue;
                            const btns = [...pg.querySelectorAll('button, .weui-desktop-icon-btn, a')]
                                .filter(b => isVisible(b) && !isDisabled(b));
                            const rightBtns = btns
                                .map(b => ({ b, r: b.getBoundingClientRect() }))
                                .filter(x => x.r.left >= indicatorRect.right - 6)
                                .sort((a, b) => a.r.left - b.r.left);
                            if (rightBtns.length) {
                                target = rightBtns[0].b;
                                break;
                            }
                        }
                        if (target) break;
                    }

                    if (!target) return null;
                    try { target.scrollIntoView({ block: 'center', inline: 'nearest' }); } catch (e) {}
                    const r = target.getBoundingClientRect();
                    if (clickDirectly || r.width <= 0 || r.height <= 0) {
                        target.click();
                        return { clicked: true };
                    }
                    return {
                        clicked: false,
                        box: { x: r.x, y: r.y, width: r.width, height: r.height },
                    };
                }""",
                {"clickDirectly": not is_main},
            )
            if not isinstance(hit, dict):
                continue
            if hit.get("clicked"):
                return True
            box = hit.get("box")
            if isinstance(box, dict) and box.get("width") and box.get("height"):
                tx, ty = DEFAULT_HUMAN_ENGINE.sample_box_point(box)
                await DEFAULT_HUMAN_ENGINE.move_mouse(page, tx, ty)
                await page.mouse.click(tx, ty)
                return True
        except Exception:
            continue
    return False


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
                if await loc.count() and await loc.is_visible():
                    await DEFAULT_HUMAN_ENGINE.click_locator(loc, page=page, timeout=timeout)
                    return True
            except Exception:
                continue
    return False


async def _scroll_all_frames(page, delta_y: int = 3000) -> None:
    for frame in page.frames:
        try:
            loc = frame.locator('[class*="scroll"], [class*="list"], [class*="comment"], main').first
            if await loc.count() and await loc.is_visible():
                box = await loc.bounding_box()
                if isinstance(box, dict) and box.get("width") and box.get("height"):
                    tx, ty = DEFAULT_HUMAN_ENGINE.sample_box_point(box)
                    await DEFAULT_HUMAN_ENGINE.move_mouse(page, tx, ty)
                    break
        except Exception:
            continue
    await human_scroll(
        page,
        distance=min(920, max(450, int(delta_y // 4))),
        settle=False,
    )


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
        await human_wait(page, 3500)
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
    max_pages: int = 0,
) -> Tuple[List[dict], Optional[dict], str]:
    """依次打开视频号助手的视频/图文管理页，拦截并分页拉取 post/post_list 全量作品。"""
    collected: Dict[str, dict] = {}
    error = ""
    api_seen: list = []
    sample: list = []
    post_response_count = 0
    post_totals: list = []
    post_errors: list = []
    page_budget = max_pages if max_pages > 0 else max(1, int(max_scrolls or 8))
    current_route: Dict[str, Any] = {
        "ids": set(),
        "total": None,
        "responses": 0,
        "template": None,
    }
    page = await mgr.new_page(identity, block_media)

    def _ingest_payload(
        data: dict, active_route: Dict[str, Any], *, is_post_list: bool
    ) -> Tuple[int, Set[str]]:
        d = _dig_data(data)
        if is_post_list:
            total = _extract_total_count(d)
            if total is not None:
                post_totals.append(total)
                active_route["total"] = total
        err_code = _rf(data, "errCode", "err_code", default=0)
        if err_code not in (0, "0", None, ""):
            post_errors.append(str(err_code))
        posts = _dig_posts(data)
        batch_ids: Set[str] = set()
        for it in posts:
            if not isinstance(it, dict):
                continue
            oid = _obj_id(it)
            if not oid:
                continue
            batch_ids.add(oid)
            active_route["ids"].add(oid)
            if oid in collected:
                collected[oid].update(it)
            else:
                collected[oid] = dict(it)
            if not sample:
                sample.append(str(it)[:1200])
        return len(posts), batch_ids

    async def on_response(resp):
        nonlocal post_response_count
        url = resp.url
        if "mmfinderassistant-bin" in url and len(api_seen) < 40:
            p = url.split("?")[0].split("mmfinderassistant-bin")[-1]
            api_seen.append(f"{resp.status} {p}")
        is_post_list = POST_LIST_API in url
        if not is_post_list and STAT_API not in url:
            return
        active_route = current_route
        post_response_count += 1
        active_route["responses"] = int(active_route.get("responses") or 0) + 1
        try:
            data = await resp.json()
        except Exception as e:
            post_errors.append(f"json:{type(e).__name__}")
            return
        err_code = _rf(data, "errCode", "err_code", default=0)
        if (
            is_post_list
            and active_route.get("template") is None
            and err_code in (0, "0", None, "")
            and resp.request.method.upper() == "POST"
        ):
            try:
                raw_post = resp.request.post_data or ""
                parsed_body = json.loads(raw_post) if raw_post else None
                if isinstance(parsed_body, dict):
                    active_route["template"] = {
                        "url": url,
                        "headers": _build_replay_headers(resp.request.headers),
                        "body": parsed_body,
                        "frame": resp.frame,
                    }
            except Exception:
                pass
        _ingest_payload(data, active_route, is_post_list=is_post_list)

    page.on("response", on_response)
    final_url = ""
    final_urls: list = []
    navigation_errors: list = []
    try:
        for list_url in POST_LIST_URLS:
            current_route = {
                "ids": set(),
                "total": None,
                "responses": 0,
                "template": None,
            }
            route_started = time.monotonic()
            try:
                await page.goto(list_url, wait_until="domcontentloaded", timeout=30000)
                wait_budget_ms = max(int(settle_ms or 1800), 5400)
                waited_ms = 0
                while waited_ms < wait_budget_ms and current_route["responses"] == 0:
                    if "/login" in page.url or "login.html" in page.url:
                        break
                    step_ms = min(600, wait_budget_ms - waited_ms)
                    await human_wait(page, step_ms)
                    waited_ms += step_ms
                if current_route["responses"] > 0:
                    await human_wait(page, min(max(450, settle_ms // 2), 900))

                final_url = page.url
                final_urls.append(final_url)
                if "/login" in final_url or "login.html" in final_url:
                    break

                def _route_complete() -> bool:
                    if "/login" in page.url or "login.html" in page.url:
                        return True
                    rt_total = current_route["total"]
                    if rt_total is not None and len(current_route["ids"]) >= rt_total:
                        return True
                    if (
                        known_ids
                        and len(current_route["ids"]) >= 5
                        and current_route["ids"].issubset(known_ids)
                    ):
                        return True
                    return False

                pages_used = 1
                fetch_failed = False
                tpl = current_route.get("template")
                if (
                    not _route_complete()
                    and page_budget > 1
                    and isinstance(tpl, dict)
                    and isinstance(tpl.get("body"), dict)
                ):
                    body = tpl["body"]
                    page_key = (
                        "currentPage"
                        if "currentPage" in body or "current_page" not in body
                        else "current_page"
                    )
                    size_key = (
                        "pageSize"
                        if "pageSize" in body or "page_size" not in body
                        else "page_size"
                    )
                    orig_size = max(
                        1,
                        _to_nonneg_int(body.get(size_key))
                        or len(current_route["ids"])
                        or 5,
                    )
                    orig_page = max(1, _to_nonneg_int(body.get(page_key)) or 1)
                    active_size = orig_size
                    next_page = orig_page + 1

                    # 视频号助手 UI 首屏默认 pageSize=5；当总作品数更多时，优先从第 1 页尝试
                    # pageSize=20 以减少翻页请求次数，若服务端限制为原条数则无缝按原条数步进。
                    if (
                        orig_page == 1
                        and orig_size < 20
                        and (
                            current_route["total"] is None
                            or current_route["total"] > orig_size
                        )
                    ):
                        await human_wait(page, random.randint(280, 520))
                        ok_probe, probe_data = await _fetch_post_list_page_in_frame(
                            page, tpl, 1, 20
                        )
                        pages_used += 1
                        if ok_probe and isinstance(probe_data, dict):
                            probe_len, probe_ids = _ingest_payload(
                                probe_data, current_route, is_post_list=True
                            )
                            if probe_len > orig_size:
                                active_size = probe_len
                                next_page = 2
                            if known_ids and probe_ids and probe_ids.issubset(known_ids):
                                next_page = page_budget + 1

                    stagnant = 0
                    while (
                        pages_used < page_budget
                        and next_page <= page_budget
                        and not _route_complete()
                        and (time.monotonic() - route_started) < 90.0
                    ):
                        before_ids = len(current_route["ids"])
                        await human_wait(page, random.randint(300, 580))
                        ok_page, page_data = await _fetch_post_list_page_in_frame(
                            page, tpl, next_page, active_size
                        )
                        pages_used += 1
                        if not ok_page or not isinstance(page_data, dict):
                            fetch_failed = True
                            break
                        batch_len, batch_ids = _ingest_payload(
                            page_data, current_route, is_post_list=True
                        )
                        if batch_len == 0:
                            break
                        if known_ids and batch_ids and batch_ids.issubset(known_ids):
                            break
                        if len(current_route["ids"]) == before_ids:
                            stagnant += 1
                            if stagnant >= 2:
                                break
                        else:
                            stagnant = 0
                        if current_route["total"] is None and batch_len < active_size:
                            break
                        next_page += 1

                # 兜底：若未捕获到可复用的 POST 模板或同源分页请求受阻，则穿透 Shadow DOM 点击「下一页」并配合滚动
                if not _route_complete() and (tpl is None or fetch_failed):
                    remaining = max(1, min(page_budget - max(0, pages_used - 1), max(max_scrolls, 30)))
                    stagnant = 0
                    for _ in range(remaining):
                        if (time.monotonic() - route_started) >= 90.0:
                            break
                        before_ids = len(current_route["ids"])
                        clicked = await _click_next_page_in_frames(page)
                        if not clicked:
                            await _scroll_all_frames(page, 4000)
                        await human_wait(page, settle_ms)
                        if _route_complete():
                            break
                        if len(current_route["ids"]) == before_ids:
                            stagnant += 1
                            if stagnant >= 2:
                                break
                        else:
                            stagnant = 0
                final_url = page.url
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
        print(
            f"[channels_works] collected={len(collected)} totals={post_totals} "
            f"sample={sample[0][:400]}"
        )

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
            await human_wait(page, settle_ms)
            if "/login" in page.url or "login.html" in page.url:
                error = "logged_out:视频号登录态已失效,请重新登录"
                break
            if not collected:
                await _click_in_frames(page, _OPEN_COMMENT_DRAWER_SEL, timeout=2500)
                await human_wait(page, settle_ms)
            stagnant = 0
            for _ in range(max_scrolls):
                before = len(collected)
                await _scroll_all_frames(page, 3000)
                await human_wait(page, settle_ms)
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
        await human_wait(page, settle_ms)
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
                            await DEFAULT_HUMAN_ENGINE.click_locator(reply_btn, page=page, timeout=2500)
                            await human_wait(page, 500)
                            break
                except Exception:
                    continue

        editor, _ = await _find_in_frames(page, _COMMENT_INPUT)
        if editor is None:
            return False, "未找到评论回复框(评论管理页选择器需校准)"
        await DEFAULT_HUMAN_ENGINE.type_text(editor, content, page=page, timeout=timeout_ms)
        await human_wait(page, 500)

        sent = False
        for frame in page.frames:
            for sel in _COMMENT_SUBMIT:
                try:
                    btn = frame.locator(sel).first
                    if await btn.count() and await btn.is_enabled():
                        await DEFAULT_HUMAN_ENGINE.click_locator(btn, page=page, timeout=3000)
                        sent = True
                        break
                except Exception:
                    continue
            if sent:
                break
        if not sent:
            return False, "未找到发送按钮(选择器需校准)"
        await human_wait(page, 1500)
        return True, ""
    except Exception as e:
        return False, f"回复评论异常: {e!r}"
    finally:
        try:
            if page is not None and not page.is_closed():
                await page.close()
        except Exception:
            pass
