"""Douyin APIs that require the official browser-bound SecureSDK."""
from __future__ import annotations

import re
from contextlib import suppress
from typing import Optional

from .human_interaction import human_wait
from .identity import Identity
from .manager import BrowserManager

_SECURE_WRITE_PATHS = {
    "/aweme/v1/web/comment/publish/",
    "/aweme/v1/web/commit/follow/user/",
}
_DOUYIN_WRITE_BASE = "https://www-hj.douyin.com"
_SEARCH_PATH = "/aweme/v1/web/general/search/stream/"


def _derive_douyin_os_params(ua: str) -> dict[str, str]:
    """Derive browser_platform / os_name / os_version / pc_libra_divert dynamically from UA."""
    ua_s = str(ua or "")
    browser_name = "Edge" if "Edg/" in ua_s else "Chrome"
    if "Macintosh" in ua_s or "Mac OS" in ua_s:
        m = re.search(r"Mac OS X (\d+[._]\d+(?:[._]\d+)?)", ua_s)
        os_ver = m.group(1) if m else "10_15_7"
        return {
            "browser_platform": "MacIntel",
            "browser_name": browser_name,
            "os_name": "Mac OS",
            "os_version": os_ver,
            "pc_libra_divert": "Mac",
        }
    if "Linux" in ua_s and "Android" not in ua_s:
        return {
            "browser_platform": "Linux x86_64",
            "browser_name": browser_name,
            "os_name": "Linux",
            "os_version": "x86_64",
            "pc_libra_divert": "Linux",
        }
    m_win = re.search(r"Windows NT (\d+)", ua_s)
    win_ver = m_win.group(1) if m_win else "10"
    return {
        "browser_platform": "Win32",
        "browser_name": browser_name,
        "os_name": "Windows",
        "os_version": win_ver,
        "pc_libra_divert": "Windows",
    }


async def douyin_secure_search_api(
    context,
    keyword: str,
    *,
    max_results: int = 20,
    max_pages: int = 3,
    search_sort: str = "general",
    publish_time: str = "all",
    content_type: str = "all",
    min_likes: int = 0,
    min_comments: int = 0,
    timeout_ms: int = 30000,
    identity: Optional[Identity] = None,
) -> tuple[list[dict], str]:
    """Call the current search/stream API through Douyin's own SecSDK runtime.

    This does not read search-page DOM or click search controls. Chromium only
    supplies the first-party runtime that appends volatile security fields; the
    returned works come exclusively from the JSON streaming API.
    """
    from .fetcher import (
        _douyin_search_item_matches,
        _sort_douyin_search_items,
        decode_douyin_search_payloads,
        extract_search_awemes,
    )

    page = await context.new_page()
    found: dict[str, dict] = {}
    offset = 0
    sort_codes = {"general": "0", "most_liked": "1", "latest": "2"}
    time_codes = {"all": "0", "day": "1", "week": "7", "half_year": "180"}
    try:
        cookies = await context.cookies("https://www.douyin.com/")
        cookie_values = {
            str(cookie.get("name") or ""): str(cookie.get("value") or "")
            for cookie in cookies
            if isinstance(cookie, dict)
        }
        default_w = str(getattr(identity, "viewport_w", 0) or 1536)
        default_h = str(getattr(identity, "viewport_h", 0) or 864)
        screen_width = cookie_values.get("dy_swidth") or default_w
        screen_height = cookie_values.get("dy_sheight") or default_h
        await page.goto(
            "https://www.douyin.com/jingxuan",
            wait_until="domcontentloaded",
            timeout=timeout_ms,
        )
        await human_wait(page, min(6000, max(2500, timeout_ms // 6)))
        ua_str = (
            getattr(identity, "ua", "") or getattr(identity, "user_agent", "")
            if identity is not None
            else ""
        )
        if not ua_str:
            with suppress(Exception):
                ua_str = str(await page.evaluate("() => navigator.userAgent") or "")
        os_params = _derive_douyin_os_params(ua_str)
        browser_lang = (
            str(getattr(identity, "locale", "") or "").strip()
            if identity is not None
            else ""
        ) or "zh-CN"

        for _ in range(max(1, min(10, int(max_pages or 1)))):
            params = {
                "device_platform": "webapp",
                "aid": "6383",
                "channel": "channel_pc_web",
                "pc_client_type": "1",
                "version_code": "190600",
                "version_name": "19.6.0",
                "cookie_enabled": "true",
                "browser_language": browser_lang,
                "browser_platform": os_params["browser_platform"],
                "browser_name": os_params["browser_name"],
                "browser_online": "true",
                "engine_name": "Blink",
                "os_name": os_params["os_name"],
                "os_version": os_params["os_version"],
                "platform": "PC",
                "downlink": "10",
                "effective_type": "4g",
                "round_trip_time": "150",
                "keyword": keyword,
                "search_channel": "aweme_general",
                "search_source": "normal_search",
                "query_correct_type": "1",
                "is_filter_search": "0",
                "offset": str(offset),
                # The official first page currently fixes count at 10 even
                # when callers only consume a smaller result limit.
                "count": "10",
                "list_type": "single",
                "need_filter_settings": "1",
                "enable_history": "1",
                "disable_rs": "0",
                "from_group_id": "",
                "pc_libra_divert": os_params["pc_libra_divert"],
                "pc_search_top_1_params": '{"enable_ai_search_top_1":1}',
                "support_h265": "0",
                "support_dash": "1",
                "update_version_code": "0",
                "screen_width": screen_width,
                "screen_height": screen_height,
            }
            if search_sort != "general":
                params["sort_type"] = sort_codes.get(search_sort, "0")
            if publish_time != "all":
                params["publish_time"] = time_codes.get(publish_time, "0")
            if content_type == "video":
                params["type"] = "video"
            result = await page.evaluate(
                """
                async input => {
                  const query = new URLSearchParams({
                    ...input.params,
                    browser_version: (navigator.userAgent.match(/Chrome\\/(\\d+)/) || [,'131'])[1] + '.0.0.0',
                    engine_version: (navigator.userAgent.match(/Chrome\\/(\\d+)/) || [,'131'])[1] + '.0.0.0',
                    cpu_core_num: String(navigator.hardwareConcurrency || 8),
                    device_memory: String(navigator.deviceMemory || 8),
                  });
                  const response = await fetch(input.path + '?' + query.toString(), {
                    method:'GET', credentials:'include',
                    headers:{Accept:'application/json, text/plain, */*'},
                  });
                  return {status:response.status, text:await response.text()};
                }
                """,
                {"path": _SEARCH_PATH, "params": params},
            )
            if int(result.get("status") or 0) != 200:
                return list(found.values()), f"http_{result.get('status')}"
            payloads = decode_douyin_search_payloads(result.get("text") or "")
            if not payloads:
                return list(found.values()), "invalid_stream_response"
            has_more = False
            next_offset = offset
            for payload in payloads:
                code = payload.get("status_code")
                message = str(payload.get("status_msg") or payload.get("message") or "")
                marker = message.casefold()
                if code not in (None, 0, "0"):
                    if str(code) in {"8", "2483"} or any(
                        word in marker for word in ("未登录", "登录后", "login")
                    ):
                        return list(found.values()), "logged_out"
                    if any(
                        word in marker
                        for word in ("verify", "captcha", "risk", "验证", "风控")
                    ):
                        return list(found.values()), "verification_required"
                    return list(found.values()), f"api_rejected:status_code={code}"
                nil_info = payload.get("search_nil_info") or {}
                nil_marker = " ".join(
                    str(nil_info.get(k) or "")
                    for k in ("search_nil_type", "search_nil_item", "text_type")
                )
                if any(
                    word in nil_marker.casefold()
                    for word in ("verify", "captcha", "risk")
                ):
                    return list(found.values()), "verification_required"
                for item in extract_search_awemes(payload):
                    aid = str(item.get("aweme_id") or "")
                    if aid and _douyin_search_item_matches(
                        item,
                        content_type=content_type,
                        publish_time=publish_time,
                        min_likes=min_likes,
                        min_comments=min_comments,
                    ):
                        found.setdefault(aid, item)
                has_more = has_more or bool(
                    payload.get("has_more") or payload.get("has_more_item")
                )
                next_offset = max(next_offset, int(payload.get("offset") or 0))
            if len(found) >= max_results or not has_more:
                break
            new_offset = next_offset or (offset + 10)
            if new_offset <= offset:
                break
            offset = new_offset
            await human_wait(page, 600)
        return _sort_douyin_search_items(list(found.values()), search_sort)[
            :max_results
        ], ""
    except Exception as exc:
        return list(found.values()), f"secure_search:{type(exc).__name__}"
    finally:
        with suppress(Exception):
            await page.close()


async def douyin_secure_post_api(
    mgr: BrowserManager,
    identity: Identity,
    path: str,
    params: dict,
    form: dict,
    *,
    base_url: str = _DOUYIN_WRITE_BASE,
    timeout_ms: int = 30000,
) -> tuple[Optional[dict], str, bool]:
    """Disabled for stealth: never MITM webpack bundles or expose runtime globals.

    Callers cleanly fall back to genuine browser UI interaction on the target page.
    """
    _ = (mgr, identity, path, params, form, base_url, timeout_ms)
    return None, "secure_api_disabled_for_stealth", False
