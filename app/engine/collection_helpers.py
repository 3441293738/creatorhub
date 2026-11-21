"""作品采集流水线关键词解析、搜索排序与候选过滤纯函数。"""
from __future__ import annotations

import json
import time

from app.models import KeywordCollectionJob
from app.platforms.douyin.extract import Aweme
from app.platforms.rednote import parse_rednote_brief
from app.platforms.tiktok.client import TikTokNativeClient
from app.platforms.twitter import parse_twitter_user
from app.platforms.xhs import parse_note_brief


def _loads_keywords(raw: str) -> list[str]:
    try:
        values = json.loads(raw or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [str(value).strip() for value in values if str(value).strip()]


def _media_json(aweme: Aweme) -> str:
    rows = []
    for media in aweme.medias:
        entry = {
            "url": media.url,
            "kind": media.kind,
            "ext": media.ext,
            "index": media.index,
        }
        decode_key = str(getattr(media, "decode_key", "") or "")
        if decode_key:
            entry["decode_key"] = decode_key
        audio_url = str(getattr(media, "audio_url", "") or "")
        if audio_url:
            entry["audio_url"] = audio_url
        rows.append(entry)
    return json.dumps(rows, ensure_ascii=False)


def _tiktok_download_session(account, identity, default_user_agent: str) \
        -> tuple[str, dict[str, str]]:
    """Return the exact account UA and TikTok cookies for CDN downloads.

    A visible login normally lives in ``storage_state``.  Keep the creator
    state as an additive fallback so imported/legacy accounts do not lose a
    valid TikTok session merely because it was stored in the alternate field.
    Cookie extraction remains domain-scoped inside ``TikTokNativeClient``.
    """
    user_agent = str(
        getattr(identity, "ua", "")
        or getattr(account, "ua", "")
        or default_user_agent
        or ""
    )
    cookies: dict[str, str] = {}
    for field in ("storage_state", "creator_storage_state"):
        state = getattr(account, field, "") or ""
        if not state:
            continue
        cookies.update(TikTokNativeClient(
            storage_state=state,
            user_agent=user_agent,
        ).cookies)
    return user_agent, cookies


def _content_error_after_download(value: str, error: str) -> str:
    """Replace stale downloader errors while preserving unrelated detail errors."""
    parts = [
        part.strip() for part in str(value or "").split(";")
        if part.strip() and not part.strip().startswith("下载失败(")
    ]
    if error:
        parts.append(str(error).strip())
    return "; ".join(parts)


def _author_id(raw: dict, platform: str) -> str:
    if platform == "douyin":
        author = raw.get("author") or {}
        return str(author.get("sec_uid") or author.get("uid") or "")
    if platform == "kuaishou":
        photo = raw.get("photo") or {}
        author = raw.get("author") or photo.get("author") or {}
        return str(author.get("id") or author.get("userId")
                   or author.get("user_id") or author.get("eid") or "")
    if platform == "shipinhao":
        contact = raw.get("contact") if isinstance(raw.get("contact"), dict) else {}
        author = raw.get("author") or raw.get("finderUser") or contact or {}
        return str(
            author.get("username")
            or author.get("finderUsername")
            or author.get("id")
            or raw.get("username")
            or raw.get("finderUsername")
            or ""
        )
    if platform == "tiktok":
        item = raw.get("item") or raw.get("aweme_info") or raw.get("itemStruct") or raw
        author = item.get("author") or {}
        return str(author.get("secUid") or author.get("sec_uid")
                   or author.get("id") or author.get("uid") or "")
    if platform == "bilibili":
        owner = raw.get("owner") if isinstance(raw.get("owner"), dict) else {}
        return str(owner.get("mid") or raw.get("mid") or "")
    if platform == "rednote":
        return str((parse_rednote_brief(raw) or {}).get("author_id") or "")
    if platform == "instagram":
        user = raw.get("user") if isinstance(raw.get("user"), dict) else (
            raw.get("owner") if isinstance(raw.get("owner"), dict) else {}
        )
        return str(user.get("username") or user.get("pk") or user.get("id") or raw.get("author_unique_id") or "")
    if platform == "twitter":
        core_u = ((raw.get("core") or {}).get("user_results") or {}).get("result") if isinstance(raw.get("core"), dict) else raw.get("user")
        u_info = parse_twitter_user(core_u) if core_u else {}
        return str(u_info.get("unique_id") or u_info.get("uid") or raw.get("author_unique_id") or raw.get("screen_name") or "")
    return str((parse_note_brief(raw) or {}).get("author_id") or "")


_XHS_SEARCH_SORT = {
    "general": "general",
    "latest": "time_descending",
    "most_liked": "popularity_descending",
}


_XHS_NOTE_TYPE = {"all": 0, "video": 1, "images": 2}


_XHS_TIME_SECONDS = {
    "day": 86400,
    "week": 7 * 86400,
    "half_year": 180 * 86400,
}


def _xhs_count(value) -> int:
    text = str(value or "").strip().replace("+", "")
    try:
        if "万" in text:
            return int(float(text.replace("万", "")) * 10_000)
        if "亿" in text:
            return int(float(text.replace("亿", "")) * 100_000_000)
        return int(float(text))
    except (TypeError, ValueError):
        return 0


def _sort_xhs_search_items(items: list[dict], search_sort: str) -> list[dict]:
    """Keep general ranking stable and make explicit XHS sorts deterministic."""
    if search_sort == "latest":
        return sorted(
            items,
            key=lambda item: int((parse_note_brief(item) or {}).get("create_time") or 0),
            reverse=True,
        )
    if search_sort == "most_liked":
        def likes(item: dict) -> int:
            card = item.get("note_card") or item
            interact = card.get("interact_info") or {}
            return _xhs_count(interact.get("liked_count") or interact.get("likedCount"))
        return sorted(items, key=likes, reverse=True)
    return list(items)


def _sort_ks_search_items(items: list[dict], search_sort: str) -> list[dict]:
    """快手搜索排序参数不稳定，按响应字段在本地保持确定性。"""
    if search_sort not in {"latest", "most_liked"}:
        return list(items)

    def sort_value(item: dict) -> int:
        photo = item.get("photo") or item
        if search_sort == "latest":
            value = photo.get("timestamp") or photo.get("createTime") or 0
        else:
            value = photo.get("realLikeCount") or photo.get("likeCount") or 0
        return _xhs_count(value)

    return sorted(items, key=sort_value, reverse=True)


def _channels_item_metric(item: dict, field: str) -> int:
    obj = item.get("object") if isinstance(item.get("object"), dict) else item
    direct_counts = obj.get("countInfo") if isinstance(obj.get("countInfo"), dict) else {}
    mono_counts = (
        (obj.get("monotonicData") or {}).get("countInfo")
        if isinstance(obj.get("monotonicData"), dict)
        else {}
    )
    ext_counts = (
        ((obj.get("objectExtend") or {}).get("monotonicData") or {})
        .get("countInfo") or {}
    )
    candidates = [
        obj.get(field),
        direct_counts.get(field) if isinstance(direct_counts, dict) else 0,
        mono_counts.get(field) if isinstance(mono_counts, dict) else 0,
        ext_counts.get(field) if isinstance(ext_counts, dict) else 0,
    ]
    if field == "likeCount":
        candidates.extend([obj.get("favCount"), obj.get("friendLikeCount")])
    for val in candidates:
        num = _xhs_count(val)
        if num > 0:
            return num
    return 0


def _sort_channels_search_items(
    items: list[dict],
    search_sort: str,
    *,
    prefer_engaged: bool = False,
) -> list[dict]:
    """视频号搜索结果按响应时间戳或互动量保持本地确定性排序。"""
    if search_sort in {"latest", "most_liked"}:
        def sort_value(item: dict) -> int:
            obj = item.get("object") if isinstance(item.get("object"), dict) else item
            if search_sort == "latest":
                return _xhs_count(obj.get("createtime") or obj.get("createTime") or 0)
            return _channels_item_metric(obj, "likeCount")

        return sorted(items, key=sort_value, reverse=True)

    if prefer_engaged:
        def engaged_tier(item: dict) -> tuple[int, int]:
            return (
                1 if _channels_item_metric(item, "commentCount") > 0 else 0,
                1 if _channels_item_metric(item, "likeCount") > 0 else 0,
            )

        return sorted(items, key=engaged_tier, reverse=True)

    return list(items)


def _collection_content_matches(aweme: Aweme, job: KeywordCollectionJob,
                                *, now: int | None = None) -> bool:
    if job.content_type != "all" and aweme.media_type != job.content_type:
        return False
    if job.content_type == "images" and job.platform == "twitter" and not aweme.medias:
        return False
    if aweme.like_count < max(0, int(job.min_likes or 0)):
        return False
    if aweme.comment_count < max(0, int(job.min_comments or 0)):
        return False
    window = _XHS_TIME_SECONDS.get(job.publish_time)
    if window and aweme.create_time:
        if aweme.create_time < int(now or time.time()) - window:
            return False
    return True


def _xhs_content_matches(aweme: Aweme, job: KeywordCollectionJob,
                         *, now: int | None = None) -> bool:
    """保留既有测试/调用名；筛选规则同样适用于已归一化的快手作品。"""
    return _collection_content_matches(aweme, job, now=now)
