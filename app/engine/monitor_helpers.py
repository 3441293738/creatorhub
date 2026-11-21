"""监控引擎共享常量、异常定义与平台候选作品筛选纯函数。"""
from __future__ import annotations

import json
import logging
import time

from app.models import MonitorTarget
from app.platforms.channels import parse_channels_feed
from app.platforms.douyin import parse_aweme
from app.platforms.douyin.extract import Aweme
from app.platforms.kuaishou import parse_ks_feed
from app.platforms.tiktok import TikTokNativeClient, parse_tiktok_aweme


MAX_AUTO_RETRY = 3


_WRITE_SUBMIT_PREFIX = "write_submitted:"


log = logging.getLogger("creatorhub.engine")


class _AccountCheckBusy(RuntimeError):
    """An account became busy while a health check waited for its network slot."""


# 账号时区 -> 期望出口国家(ISO2)。仅列常见,匹配不到则跳过地区校验。
_TZ_COUNTRY = {
    "Asia/Shanghai": "CN", "Asia/Chongqing": "CN", "Asia/Urumqi": "CN",
    "Asia/Hong_Kong": "HK", "Asia/Macau": "MO", "Asia/Taipei": "TW",
}


def _loads(s: str) -> dict:
    try:
        return json.loads(s or "{}")
    except Exception:
        return {}


def _loads_list(s: str) -> list:
    try:
        v = json.loads(s or "[]")
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _monitor_terms(raw: str) -> list[str]:
    """Load case-insensitive monitor terms while tolerating legacy CSV values."""
    values = _loads_list(raw)
    if not values and raw and not str(raw).lstrip().startswith("["):
        values = str(raw).replace("，", ",").split(",")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        term = " ".join(str(value or "").strip().split())
        key = term.casefold()
        if not term or key in seen:
            continue
        seen.add(key)
        result.append(term)
    return result


def _monitor_strategy(target: MonitorTarget, *, default_scrolls: int,
                      default_items: int = 0) -> dict:
    """Return a clamped strategy so legacy rows keep platform defaults."""
    configured_scrolls = int(getattr(target, "max_scrolls", 0) or 0)
    configured_items = int(getattr(target, "max_items_per_scan", 0) or 0)
    return {
        "max_scrolls": max(1, min(30, configured_scrolls or default_scrolls)),
        "max_items": max(0, min(100, configured_items or default_items)),
        "media_type": str(getattr(target, "record_media_filter", "all") or "all"),
        "min_likes": max(0, int(getattr(target, "min_like_count", 0) or 0)),
        "min_comments": max(0, int(getattr(target, "min_comment_count", 0) or 0)),
        "recent_days": max(0, int(getattr(target, "recent_days", 0) or 0)),
        "includes": _monitor_terms(getattr(target, "include_keywords", "[]") or "[]"),
        "excludes": _monitor_terms(getattr(target, "exclude_keywords", "[]") or "[]"),
    }


def _monitor_content_matches(aw: Aweme, strategy: dict,
                             *, now_ts: int | None = None) -> bool:
    """Apply target-level record filters after a platform item is normalized."""
    media_type = strategy.get("media_type") or "all"
    if media_type != "all" and aw.media_type != media_type:
        return False
    if int(aw.like_count or 0) < int(strategy.get("min_likes") or 0):
        return False
    if int(aw.comment_count or 0) < int(strategy.get("min_comments") or 0):
        return False
    recent_days = int(strategy.get("recent_days") or 0)
    if recent_days and aw.create_time:
        cutoff = (int(time.time()) if now_ts is None else int(now_ts)) - recent_days * 86400
        if int(aw.create_time) < cutoff:
            return False
    folded = str(aw.desc or "").casefold()
    includes = strategy.get("includes") or []
    excludes = strategy.get("excludes") or []
    if includes and not any(str(term).casefold() in folded for term in includes):
        return False
    if excludes and any(str(term).casefold() in folded for term in excludes):
        return False
    return True


def _danmaku_matches(item: dict, settings: dict) -> bool:
    text = str(item.get("text") or "")
    folded = text.casefold()
    point = max(0, int(item.get("video_time_ms") or 0))
    start_ms = settings.get("time_start_ms", 0)
    end_ms = settings.get("time_end_ms", 0)
    if start_ms and point < start_ms:
        return False
    if end_ms and point > end_ms:
        return False
    includes = settings.get("include_keywords") or []
    excludes = settings.get("exclude_keywords") or []
    if includes and not any(str(k).casefold() in folded for k in includes):
        return False
    if excludes and any(str(k).casefold() in folded for k in excludes):
        return False
    min_len = settings.get("min_text_length", 0)
    max_len = settings.get("max_text_length", 0)
    if min_len and len(text) < min_len:
        return False
    if max_len and len(text) > max_len:
        return False
    if int(item.get("like_count") or 0) < settings.get("min_like_count", 0):
        return False
    return True


def _select_douyin_awemes(items: list, quality: str, first_scan: bool,
                          monitor_since: int, initial_backfill_count: int) -> list[Aweme]:
    """按发布时间稳定排序，并应用“订阅后新增 + 可选首次回填”策略。"""
    parsed = []
    seen = set()
    for item in items:
        aw = parse_aweme(item, quality)
        if not aw or aw.aweme_id in seen:
            continue
        seen.add(aw.aweme_id)
        parsed.append(aw)
    parsed.sort(key=lambda aw: (aw.create_time, aw.aweme_id), reverse=True)

    if not first_scan:
        # create_time 缺失时宁可保留，避免平台字段小改后静默漏掉真正的新作品。
        return [aw for aw in parsed
                if not aw.create_time or aw.create_time >= monitor_since]
    if initial_backfill_count < 0:
        return parsed

    current = [aw for aw in parsed
               if aw.create_time and aw.create_time >= monitor_since]
    historical = [aw for aw in parsed
                  if aw.create_time and aw.create_time < monitor_since]
    return current + historical[:max(0, initial_backfill_count)]


def _select_tiktok_awemes(items: list, quality: str, first_scan: bool,
                          monitor_since: int, initial_backfill_count: int) -> list[Aweme]:
    parsed = []
    seen = set()
    for item in items:
        aw = parse_tiktok_aweme(item, quality)
        if not aw or aw.aweme_id in seen:
            continue
        seen.add(aw.aweme_id)
        parsed.append(aw)
    parsed.sort(key=lambda aw: (aw.create_time, aw.aweme_id), reverse=True)
    if not first_scan:
        return [aw for aw in parsed
                if not aw.create_time or aw.create_time >= monitor_since]
    if initial_backfill_count < 0:
        return parsed
    current = [aw for aw in parsed
               if aw.create_time and aw.create_time >= monitor_since]
    historical = [aw for aw in parsed
                  if aw.create_time and aw.create_time < monitor_since]
    return current + historical[:max(0, initial_backfill_count)]


def _select_ks_awemes(items: list, quality: str, first_scan: bool,
                      monitor_since: int, initial_backfill_count: int) -> list[Aweme]:
    """快手作品按发布时间稳定排序，应用「订阅后新增 + 可选首次回填」策略。
    与抖音 _select_douyin_awemes 同语义,只是改用 parse_ks_feed 归一。"""
    parsed = []
    seen = set()
    for item in items:
        aw = parse_ks_feed(item, quality)
        if not aw or aw.aweme_id in seen:
            continue
        seen.add(aw.aweme_id)
        parsed.append(aw)
    parsed.sort(key=lambda aw: (aw.create_time, aw.aweme_id), reverse=True)
    if not first_scan:
        # create_time 缺失时宁可保留，避免平台字段小改后静默漏掉真正的新作品。
        return [aw for aw in parsed
                if not aw.create_time or aw.create_time >= monitor_since]
    if initial_backfill_count < 0:
        return parsed
    current = [aw for aw in parsed
               if aw.create_time and aw.create_time >= monitor_since]
    historical = [aw for aw in parsed
                  if aw.create_time and aw.create_time < monitor_since]
    return current + historical[:max(0, initial_backfill_count)]


def _select_channels_awemes(items: list, quality: str, first_scan: bool,
                            monitor_since: int, initial_backfill_count: int) -> list[Aweme]:
    """视频号作品按发布时间稳定排序，应用「订阅后新增 + 可选首次回填」策略。"""
    parsed = []
    seen = set()
    for item in items:
        aw = parse_channels_feed(item, quality)
        if not aw or aw.aweme_id in seen:
            continue
        seen.add(aw.aweme_id)
        parsed.append(aw)
    parsed.sort(key=lambda aw: (aw.create_time, aw.aweme_id), reverse=True)
    if not first_scan:
        return [aw for aw in parsed
                if not aw.create_time or aw.create_time >= monitor_since]
    if initial_backfill_count < 0:
        return parsed
    current = [aw for aw in parsed
               if aw.create_time and aw.create_time >= monitor_since]
    historical = [aw for aw in parsed
                  if aw.create_time and aw.create_time < monitor_since]
    return current + historical[:max(0, initial_backfill_count)]


def _tiktok_download_session(account, identity, default_user_agent: str) \
        -> tuple[str, dict[str, str]]:
    user_agent = str(
        getattr(identity, "ua", "") or getattr(account, "ua", "")
        or default_user_agent or "")
    cookies: dict[str, str] = {}
    for field in ("storage_state", "creator_storage_state"):
        state = getattr(account, field, "") or ""
        if state:
            cookies.update(TikTokNativeClient(
                storage_state=state, user_agent=user_agent).cookies)
    return user_agent, cookies


def _douyin_scan_since(monitor_since: int, known_create_times: list[int]) -> int:
    """给旧版残缺首扫留出自愈窗口，但不回退到整个账号历史。"""
    latest_known = max((ts or 0 for ts in known_create_times), default=0)
    return min(monitor_since, latest_known) if latest_known else monitor_since


def _round_robin_by_account(rows: list[tuple[int, int | None]]) \
        -> list[tuple[int, int | None]]:
    """Interleave due rows so one account cannot monopolize a scheduler burst."""
    buckets: dict[object, list[tuple[int, int | None]]] = {}
    for row_id, account_id in rows:
        key: object = account_id if account_id is not None else f"anon:{row_id}"
        buckets.setdefault(key, []).append((row_id, account_id))
    ordered: list[tuple[int, int | None]] = []
    while buckets:
        for key in list(buckets):
            ordered.append(buckets[key].pop(0))
            if not buckets[key]:
                del buckets[key]
    return ordered
