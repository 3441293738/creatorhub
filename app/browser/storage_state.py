"""无闪烁 BrowserContext storage_state 快照采集辅助。

Patchright / Playwright 内置的 ``context.storage_state()`` 在上下文曾访问过
多个 origin（包括抖音等站点内嵌的跨域 iframe、登录跳转子域或环境体检页等）
且这些 origin 当前未作为独立主标签页打开时，会强行 ``newPage(..., forStorageState=true)``
新建临时可见标签页、逐个跳转读取 ``localStorage`` 后再关闭，导致有头浏览器窗口
反复弹出空白标签页并抢占焦点。

本模块改用纯 CDP ``context.cookies()`` 获取全上下文 Cookie（永不新建标签页），
并仅从当前已打开的第一方主页面原地读取 ``localStorage``，同时与既有快照中的
第一方 ``origins`` 增量合并，彻底消除全平台有头窗口闪烁问题。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit


_READ_LOCAL_STORAGE_JS = """() => {
  const items = [];
  try {
    const store = window.localStorage;
    if (!store) return items;
    for (let i = 0; i < store.length; i += 1) {
      const key = store.key(i);
      if (key === null) continue;
      const val = store.getItem(key);
      items.push({ name: String(key), value: val === null ? '' : String(val) });
    }
  } catch (_) {}
  return items;
}"""


def _domain_allowed(platform: str, domain_or_host: Any) -> bool:
    pf = str(platform or "").strip().lower()
    if not pf:
        return True
    from app.services.session_keepalive import domain_allowed_for_platform

    return domain_allowed_for_platform(pf, domain_or_host)


def _extract_origins_list(raw: Any) -> list[Mapping[str, Any]]:
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            raw = json.loads(text)
        except (TypeError, ValueError):
            return []
    if isinstance(raw, Mapping):
        origins = raw.get("origins")
        return [o for o in origins if isinstance(o, Mapping)] if isinstance(origins, list) else []
    if isinstance(raw, (list, tuple)):
        return [o for o in raw if isinstance(o, Mapping)]
    return []


def _normalize_origin_url(origin_url: Any) -> tuple[str, str]:
    text = str(origin_url or "").strip()
    if not text:
        return "", ""
    try:
        parsed = urlsplit(text)
    except (TypeError, ValueError):
        return "", ""
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if scheme not in {"http", "https"} or not host or not parsed.netloc:
        return "", ""
    return f"{scheme}://{parsed.netloc}", host


def merge_state_origins(
    base_state_or_origins: Any,
    live_origins: Iterable[Mapping[str, Any]] | None,
    *,
    platform: str = "",
) -> list[dict[str, Any]]:
    """安全合并既有快照与当前活跃页面采集到的第一方 ``origins``。"""
    origins_map: dict[str, dict[str, str]] = {}

    for entry in _extract_origins_list(base_state_or_origins):
        origin, host = _normalize_origin_url(entry.get("origin"))
        if not origin or not _domain_allowed(platform, host):
            continue
        ls_map = origins_map.setdefault(origin, {})
        for kv in entry.get("localStorage") or ():
            if isinstance(kv, Mapping) and kv.get("name"):
                ls_map[str(kv["name"])] = str(kv.get("value") or "")

    for entry in _extract_origins_list(live_origins):
        origin, host = _normalize_origin_url(entry.get("origin"))
        if not origin or not _domain_allowed(platform, host):
            continue
        ls_map = origins_map.setdefault(origin, {})
        for kv in entry.get("localStorage") or ():
            if isinstance(kv, Mapping) and kv.get("name"):
                name = str(kv["name"])
                val = str(kv.get("value") or "")
                if val or name not in ls_map:
                    ls_map[name] = val

    return [
        {
            "origin": origin,
            "localStorage": [{"name": k, "value": v} for k, v in sorted(ls_map.items())],
        }
        for origin, ls_map in sorted(origins_map.items())
        if ls_map
    ]


async def capture_context_storage_state(
    context: Any,
    *,
    platform: str = "",
    existing_state: Any = None,
    timeout: float = 5.0,
    per_page_timeout: float = 1.5,
    filter_cookies_by_platform: bool = False,
) -> dict[str, Any]:
    """无闪烁采集 BrowserContext 的 Cookie 与当前活跃页面 ``localStorage``。

    绝不调用会新建临时标签页的 ``context.storage_state()``（除非传入的轻量级
    桩对象未实现 ``cookies()`` 方法）。
    """
    if context is None:
        return {"cookies": [], "origins": merge_state_origins(existing_state, (), platform=platform)}

    cookies_fn = getattr(context, "cookies", None)
    if not callable(cookies_fn):
        storage_fn = getattr(context, "storage_state", None)
        if not callable(storage_fn):
            return {"cookies": [], "origins": merge_state_origins(existing_state, (), platform=platform)}
        raw_state = (
            await asyncio.wait_for(storage_fn(), timeout=timeout)
            if timeout and timeout > 0
            else await storage_fn()
        )
        if not isinstance(raw_state, Mapping):
            return {"cookies": [], "origins": merge_state_origins(existing_state, (), platform=platform)}
        cookies = [dict(c) for c in (raw_state.get("cookies") or ()) if isinstance(c, Mapping)]
        if filter_cookies_by_platform and platform:
            cookies = [c for c in cookies if _domain_allowed(platform, c.get("domain", ""))]
        origins = merge_state_origins(existing_state, raw_state.get("origins"), platform=platform)
        return {"cookies": cookies, "origins": origins}

    loop = asyncio.get_running_loop()
    deadline = (loop.time() + float(timeout)) if (timeout and timeout > 0) else None

    raw_cookies = (
        await asyncio.wait_for(cookies_fn(), timeout=timeout)
        if timeout and timeout > 0
        else await cookies_fn()
    )
    cookies = [dict(c) for c in (raw_cookies or ()) if isinstance(c, Mapping)]
    if filter_cookies_by_platform and platform:
        cookies = [c for c in cookies if _domain_allowed(platform, c.get("domain", ""))]

    live_origins: list[dict[str, Any]] = []
    seen_origins: set[str] = set()
    pages_attr = getattr(context, "pages", None)
    pages = list(pages_attr) if isinstance(pages_attr, (list, tuple)) else []

    for page in pages:
        if deadline is not None:
            remaining = deadline - loop.time()
            if remaining <= 0.05:
                break
            step_timeout = min(max(0.2, float(per_page_timeout)), remaining)
        else:
            step_timeout = max(0.2, float(per_page_timeout))

        try:
            is_closed = getattr(page, "is_closed", None)
            if callable(is_closed) and is_closed():
                continue
            origin, host = _normalize_origin_url(getattr(page, "url", ""))
            if not origin or origin in seen_origins:
                continue
            if not _domain_allowed(platform, host):
                continue
            eval_fn = getattr(page, "evaluate", None)
            if not callable(eval_fn):
                continue
            items_raw = await asyncio.wait_for(
                eval_fn(_READ_LOCAL_STORAGE_JS),
                timeout=step_timeout,
            )
            seen_origins.add(origin)
            if not isinstance(items_raw, list) or not items_raw:
                continue
            cleaned = [
                {"name": str(item["name"]), "value": str(item.get("value") or "")}
                for item in items_raw
                if isinstance(item, Mapping) and item.get("name")
            ]
            if cleaned:
                cleaned.sort(key=lambda pair: pair["name"])
                live_origins.append({"origin": origin, "localStorage": cleaned})
        except Exception:
            continue

    origins = merge_state_origins(existing_state, live_origins, platform=platform)
    return {"cookies": cookies, "origins": origins}
