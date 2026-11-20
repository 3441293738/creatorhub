"""抖音 / 小红书 / 快手 / 视频号批量采集流水线。"""
from __future__ import annotations

import asyncio
import json
import math
import random
import time
from pathlib import Path

from sqlalchemy import func
from sqlmodel import select

from ..browser import (
    fetch_comments,
    fetch_channels_comments,
    fetch_channels_works,
    fetch_douyin_search,
    fetch_ks_comments,
    fetch_ks_search,
    fetch_ks_videos,
    fetch_videos,
    fetch_xhs_comments,
    fetch_xhs_favorites,
    fetch_xhs_note_detail,
    fetch_xhs_notes,
    fetch_xhs_search,
    fetch_rednote_comments,
    fetch_rednote_favorites,
    fetch_rednote_note_detail,
    fetch_rednote_notes,
    fetch_rednote_search,
)
from ..browser.tiktok_fetcher import fetch_tiktok_comments, fetch_tiktok_search
from ..browser.bili_fetcher import (
    fetch_bili_comments,
    fetch_bili_search,
    fetch_bili_videos,
)
from ..browser.ig_fetcher import (
    fetch_ig_comments,
    fetch_ig_posts,
    fetch_ig_search,
)
from ..db import get_session
from ..models import (
    KeywordCollectionComment,
    KeywordCollectionContent,
    KeywordCollectionJob,
)
from ..platforms.douyin import (
    DouyinClient,
    cookie_from_state as douyin_cookie_from_state,
    parse_aweme,
    parse_comment,
    safe_title,
)
from ..platforms.douyin.extract import Aweme
from ..platforms.kuaishou import (
    flatten_ks_comments,
    parse_ks_comment,
    parse_ks_feed,
)
from ..platforms.tiktok import parse_tiktok_aweme, parse_tiktok_comment
from ..platforms.tiktok.client import TikTokNativeClient
from ..platforms.bilibili import (
    BilibiliClient,
    cookie_map_from_state as bili_cookie_map,
    parse_bili_aweme,
    resolve_bili_mid,
)
from ..platforms.instagram import (
    InstagramClient,
    cookie_map_from_state as ig_cookie_map,
    parse_ig_aweme,
    resolve_ig_user_target,
)
from ..platforms.channels import (
    ChannelsProtocolClient,
    flatten_channels_comments,
    parse_channels_comment,
    parse_channels_feed,
)
from ..platforms.xhs import (
    XhsApiClient,
    cookie_str_from_state,
    flatten_comments as flatten_xhs_comments,
    has_a1,
    parse_comment as parse_xhs_comment,
    parse_note_brief,
    parse_note_detail,
)
from ..platforms.rednote import (
    RednoteApiClient,
    cookie_str_from_state as rednote_cookie_str_from_state,
    flatten_rednote_comments,
    has_a1 as rednote_has_a1,
    parse_rednote_brief,
    parse_rednote_comment,
    parse_rednote_detail,
)
from ..risk import classify_platform_error, RiskCategory
from ..transport_matrix import douyin_client_environment


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


class KeywordCollector:
    """执行单个持久化采集任务；调度与账号风控由 MonitorEngine 负责。"""

    def __init__(self, cfg, browser, downloader):
        self.cfg = cfg
        self.browser = browser
        self.downloader = downloader

    async def _xhs_gap(self, seconds: float) -> None:
        base = max(0.0, float(seconds or 0.0))
        if not base:
            return
        jitter = min(1.0, max(
            0.0, float(self.cfg.engine.xhs_request_jitter or 0.0)))
        await asyncio.sleep(base * random.uniform(1.0, 1.0 + jitter))

    def _xhs_browser_reads_enabled(self) -> bool:
        return bool(
            self.cfg.engine.xhs_read_mode == "browser"
            and callable(getattr(self.browser, "visible_page", None))
        )

    def _rednote_read_mode(self) -> str:
        mode = str(getattr(self.cfg.engine, "rednote_read_mode", "hybrid") or "hybrid").lower()
        return mode if mode in {"browser", "hybrid", "api"} else "hybrid"

    def _channels_read_mode(self) -> str:
        mode = str(getattr(self.cfg.engine, "channels_read_mode", "auto") or "auto").lower()
        return mode if mode in {"auto", "protocol", "browser"} else "auto"

    def _channels_protocol_client(self) -> ChannelsProtocolClient | None:
        if self._channels_read_mode() not in {"auto", "protocol"}:
            return None
        return ChannelsProtocolClient.from_config(self.cfg)

    def _direct_request_ua(self, identity) -> str:
        resolver = getattr(self.browser, "direct_request_user_agent", None)
        if callable(resolver):
            return resolver(identity)
        return str(getattr(identity, "ua", "") or self.cfg.engine.user_agent)

    def _result(self, job_id: int, *, stopped: bool = False) -> dict:
        contents, comments = self._refresh_counts(job_id)
        final = self._job(job_id)
        return {
            "canceled": False,
            "stopped": stopped,
            "errors": final.error_count if final else 0,
            "contents": contents,
            "comments": comments,
        }

    @staticmethod
    def _job(job_id: int) -> KeywordCollectionJob | None:
        with get_session() as session:
            return session.get(KeywordCollectionJob, job_id)

    @staticmethod
    def _cancel_requested(job_id: int) -> bool:
        job = KeywordCollector._job(job_id)
        return not job or job.cancel_requested or job.status == "canceled"

    @staticmethod
    def _progress(job_id: int, *, keyword: str | None = None,
                  step: str | None = None,
                  processed_keyword_count: int | None = None,
                  current_item_index: int | None = None,
                  current_item_total: int | None = None) -> None:
        with get_session() as session:
            job = session.get(KeywordCollectionJob, job_id)
            if not job or job.cancel_requested or job.status == "canceled":
                return
            if keyword is not None:
                job.current_keyword = keyword
            if step is not None:
                job.current_step = step
            if processed_keyword_count is not None:
                job.processed_keyword_count = max(0, processed_keyword_count)
            if current_item_index is not None:
                job.current_item_index = max(0, current_item_index)
            if current_item_total is not None:
                job.current_item_total = max(0, current_item_total)
            session.add(job)
            session.commit()

    @staticmethod
    def _record_error(job_id: int, message: str) -> None:
        message = str(message or "").strip()
        if not message:
            return
        with get_session() as session:
            job = session.get(KeywordCollectionJob, job_id)
            if not job:
                return
            lines = [line for line in (job.error or "").splitlines() if line]
            if message[:360] in lines:
                return
            job.error_count += 1
            lines.append(message[:360])
            job.error = "\n".join(lines[-8:])[:2400]
            session.add(job)
            session.commit()

    @staticmethod
    def _mark_account_invalid(account_id: int) -> None:
        """搜索页明确出现登录墙时同步账号状态，避免继续把旧 Cookie 当作可用。"""
        from ..models import DouyinAccount
        with get_session() as session:
            account = session.get(DouyinAccount, account_id)
            if account and account.status != "invalid":
                account.status = "invalid"
                session.add(account)
                session.commit()

    @staticmethod
    def _refresh_counts(job_id: int) -> tuple[int, int]:
        with get_session() as session:
            contents = int(session.exec(
                select(func.count(KeywordCollectionContent.id))
                .where(KeywordCollectionContent.job_id == job_id)
            ).one() or 0)
            comments = int(session.exec(
                select(func.count(KeywordCollectionComment.id))
                .where(KeywordCollectionComment.job_id == job_id)
            ).one() or 0)
            job = session.get(KeywordCollectionJob, job_id)
            if job:
                job.content_count = contents
                job.comment_count = comments
                session.add(job)
                session.commit()
        return contents, comments

    async def _discover_douyin(self, account, keyword: str,
                               job: KeywordCollectionJob,
                               client: DouyinClient | None = None,
                               context=None) -> tuple[list[dict], str]:
        mode = str(getattr(self.cfg.engine, "douyin_read_mode", "hybrid") or "hybrid")
        if mode in {"api", "hybrid"} and client is not None:
            items, error = [], ""
            api_compatibility_failure = False
            try:
                print("[dy-search] HTTP search/single API 请求")
                items, error = await client.search_awemes(
                    keyword,
                    max_results=job.max_contents_per_keyword,
                    max_pages=job.max_pages_per_keyword,
                    search_sort=job.search_sort,
                    publish_time=job.publish_time,
                    content_type=job.content_type,
                    min_likes=job.min_likes,
                    min_comments=job.min_comments,
                )
            except Exception as exc:
                message = f"抖音搜索接口异常: {exc!r}"
                category, _ = classify_platform_error(exc)
                if mode == "api" or category in {
                        RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK}:
                    return [], message
                print(f"[dy-search] API 兼容性异常，浏览器回退: {type(exc).__name__}")
                api_compatibility_failure = True
            if items:
                return items, ("抖音搜索接口失败: " + error if error else "")
            if not error and not api_compatibility_failure:
                return [], ""
            if error:
                message = f"抖音搜索接口失败: {error}"
                category, _ = classify_platform_error(message)
                if error == "verification_required" or category in {
                        RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK}:
                    print("[dy-search] API 返回验证/风控信号，进入冷却，不打开浏览器")
                    return [], message
                if mode == "api":
                    return [], message
                print(f"[dy-search] API 兼容性失败，浏览器回退: {error}")
        elif mode == "api":
            return [], "抖音搜索接口异常: API 客户端不可用"
        identity = self.browser.identity_for(account)
        return await fetch_douyin_search(
            self.browser, identity, keyword,
            max_results=job.max_contents_per_keyword,
            max_scrolls=job.max_pages_per_keyword,
            stagnant_limit=job.stagnant_pages,
            search_sort=job.search_sort,
            publish_time=job.publish_time,
            content_type=job.content_type,
            min_likes=job.min_likes,
            min_comments=job.min_comments,
            captcha_wait_seconds=self.cfg.engine.douyin_captcha_wait_seconds,
            block_media=self.cfg.engine.block_media_resources,
            context=context,
        )

    async def _discover_douyin_creator(
            self, account, job: KeywordCollectionJob,
            client: DouyinClient | None) -> tuple[list[dict], str]:
        mode = str(getattr(self.cfg.engine, "douyin_read_mode", "hybrid") or "hybrid")
        if mode in {"api", "hybrid"} and client is not None:
            try:
                values = await client.fetch_all_video_list(
                    job.source_ref,
                    max_pages=job.max_pages_per_keyword,
                    count=min(20, job.max_contents_per_keyword))
                if values or mode == "api":
                    return values[:job.max_contents_per_keyword], ""
            except Exception as exc:
                if mode == "api":
                    return [], f"抖音创作者作品读取失败: {exc}"
        values, _author, error = await fetch_videos(
            self.browser, self.browser.identity_for(account), job.source_ref, set(),
            max_scrolls=job.max_pages_per_keyword,
            block_media=self.cfg.engine.block_media_resources)
        return values[:job.max_contents_per_keyword], error

    async def _discover_ks(self, account, keyword: str,
                           job: KeywordCollectionJob) -> tuple[list[dict], str]:
        return await fetch_ks_search(
            self.browser,
            self.browser.identity_for(account),
            keyword,
            set(),
            max_scrolls=max(1, min(40, int(job.max_pages_per_keyword or 1))),
            stagnant_limit=max(1, min(8, int(job.stagnant_pages or 1))),
            block_media=self.cfg.engine.block_media_resources,
        )

    async def _discover_tiktok(self, account, identity, keyword: str,
                               job: KeywordCollectionJob) -> tuple[list[dict], str]:
        mode = str(getattr(self.cfg.engine, "tiktok_read_mode", "native") or "native")
        if mode in {"auto", "native"}:
            client = TikTokNativeClient(
                storage_state=getattr(account, "storage_state", "") or "",
                user_agent=identity.ua,
                proxy=identity.proxy,
                locale=identity.locale,
                timezone=identity.timezone_id,
                viewport=(identity.viewport_w, identity.viewport_h),
                region="US",
                device_seed=identity.fp_seed or str(identity.account_id or "creatorhub"),
                timeout=max(10, int(self.cfg.engine.request_timeout_seconds)),
            )
            native = await client.search(
                keyword, count=job.max_contents_per_keyword, offset=0)
            if native.items or mode == "native":
                return native.items, native.error
            if native.category == "empty_valid":
                return [], ""
            print(
                f"[tiktok-search] 离线接口兼容失败，页面回退: "
                f"{native.category}; HTTP={native.http_status}; "
                f"bytes={native.body_length}; tt_orcas_res={native.tt_orcas_res!r}"
            )
        if mode == "auto":
            async with self.browser.temporary_headed_context(identity):
                return await fetch_tiktok_search(
                    self.browser, identity, keyword,
                    max_results=job.max_contents_per_keyword,
                    max_scrolls=job.max_pages_per_keyword,
                    stagnant_limit=job.stagnant_pages,
                    block_media=self.cfg.engine.block_media_resources,
                )
        return await fetch_tiktok_search(
            self.browser, identity, keyword,
            max_results=job.max_contents_per_keyword,
            max_scrolls=job.max_pages_per_keyword,
            stagnant_limit=job.stagnant_pages,
            block_media=self.cfg.engine.block_media_resources,
        )

    async def _discover_ks_creator(
            self, account, job: KeywordCollectionJob) -> tuple[list[dict], str]:
        values, _author, error = await fetch_ks_videos(
            self.browser,
            self.browser.identity_for(account),
            job.source_ref,
            set(),
            max_scrolls=max(1, min(40, int(job.max_pages_per_keyword or 1))),
            block_media=self.cfg.engine.block_media_resources,
        )
        return values[:job.max_contents_per_keyword], error

    async def _discover_channels_account(
            self, identity, job: KeywordCollectionJob, *,
            account=None,
            proto_client: ChannelsProtocolClient | None = None) -> tuple[list[dict], str]:
        mode = self._channels_read_mode()
        sec_uid = str(getattr(account, "sec_uid", "") or "").strip()
        has_finder_username = bool(sec_uid and (sec_uid.startswith("v2_") or "@finder" in sec_uid))

        if mode == "protocol":
            if proto_client is None:
                return [], "视频号原生协议采集尚未连接本机微信通道（请在「设置 → 视频号微信通道」开启一键本地代理或配置 CDP）"
            if not has_finder_username:
                return [], "当前视频号账号缺少 finderUsername(v2_...@finder)，无法走原生协议读取作品"
            values, _author, err = await proto_client.fetch_user_feeds(
                sec_uid,
                max_results=job.max_contents_per_keyword,
                max_pages=job.max_pages_per_keyword,
            )
            return values[:job.max_contents_per_keyword], err

        if mode == "auto" and proto_client is not None and has_finder_username:
            values, _author, proto_err = await proto_client.fetch_user_feeds(
                sec_uid,
                max_results=job.max_contents_per_keyword,
                max_pages=job.max_pages_per_keyword,
            )
            if values or not proto_err:
                return values[:job.max_contents_per_keyword], proto_err

        values, _author, error = await fetch_channels_works(
            self.browser,
            identity,
            set(),
            max_scrolls=max(1, min(40, int(job.max_pages_per_keyword or 1))),
            block_media=self.cfg.engine.block_media_resources,
        )
        if values and job.download_media and proto_client is not None:
            enriched: list[dict] = []
            for item in values:
                brief = parse_channels_feed(item, job.video_quality or "highest")
                if brief is not None and not brief.medias and brief.aweme_id:
                    detail_obj, _ = await proto_client.fetch_feed_detail(
                        object_id=brief.aweme_id
                    )
                    if detail_obj:
                        enriched.append(detail_obj)
                        continue
                enriched.append(item)
            values = enriched
        return values, error

    async def _discover_channels_creator(
            self, job: KeywordCollectionJob, *,
            proto_client: ChannelsProtocolClient | None = None) -> tuple[list[dict], str]:
        if self._channels_read_mode() == "browser" or proto_client is None:
            return (
                [],
                "视频号创作者主页采集需要连接本机已登录微信（请在「设置 → 视频号微信通道」开启一键本地代理并在微信内打开一次视频号页面）",
            )
        values, _author, err = await proto_client.fetch_user_feeds(
            job.source_ref,
            max_results=job.max_contents_per_keyword,
            max_pages=job.max_pages_per_keyword,
        )
        return values[:job.max_contents_per_keyword], err

    async def _discover_channels_keyword(
            self, keyword: str, job: KeywordCollectionJob, *,
            proto_client: ChannelsProtocolClient | None = None) -> tuple[list[dict], str]:
        if self._channels_read_mode() == "browser" or proto_client is None:
            return (
                [],
                "视频号关键词搜索需要连接本机已登录微信（请在「设置 → 视频号微信通道」开启一键本地代理并在微信内打开一次视频号页面）",
            )
        pages = max(1, min(40, int(job.max_pages_per_keyword or 1)))
        needs_local_filter = bool(
            job.publish_time != "all"
            or job.min_likes
            or job.min_comments
            or job.search_sort in {"latest", "most_liked"}
        )
        candidate_limit = (
            max(job.max_contents_per_keyword, min(45, pages * 15))
            if needs_local_filter
            else (
                max(job.max_contents_per_keyword, min(15, job.max_contents_per_keyword * 3))
                if job.max_comments_per_content > 0
                else job.max_contents_per_keyword
            )
        )
        prefer_engaged = bool(
            job.max_comments_per_content > 0 or job.search_sort == "most_liked"
        )
        values, err = await proto_client.search_feeds(
            keyword,
            max_results=candidate_limit,
            max_pages=pages,
            prefer_engaged=prefer_engaged,
        )
        return _sort_channels_search_items(
            values,
            job.search_sort,
            prefer_engaged=(job.max_comments_per_content > 0),
        ), err

    async def _discover_xhs(self, client: XhsApiClient, keyword: str,
                            job: KeywordCollectionJob) -> tuple[list[dict], str]:
        found: dict[str, dict] = {}
        pages = max(1, min(40, int(job.max_pages_per_keyword or 1)))
        needs_local_filter = bool(
            job.publish_time != "all" or job.min_likes or job.min_comments)
        candidate_limit = (pages * 20 if needs_local_filter
                           else job.max_contents_per_keyword)
        stagnant = 0
        try:
            for page in range(1, pages + 1):
                raw_items = await client.search_notes(
                    keyword,
                    page=page,
                    page_size=20,
                    sort=_XHS_SEARCH_SORT.get(job.search_sort, "general"),
                    note_type=_XHS_NOTE_TYPE.get(job.content_type, 0),
                )
                before = len(found)
                for raw in raw_items:
                    brief = parse_note_brief(raw)
                    if brief:
                        found.setdefault(brief["note_id"], raw)
                    if len(found) >= candidate_limit:
                        break
                stagnant = stagnant + 1 if len(found) == before else 0
                if len(found) >= candidate_limit:
                    break
                if not raw_items or stagnant >= max(1, int(job.stagnant_pages or 1)):
                    break
                await self._xhs_gap(0.6)
        except Exception as exc:
            return _sort_xhs_search_items(
                list(found.values()), job.search_sort), f"小红书搜索失败: {exc}"
        return _sort_xhs_search_items(list(found.values()), job.search_sort), ""

    async def _discover_xhs_browser(
            self, identity, keyword: str, job: KeywordCollectionJob
            ) -> tuple[list[dict], str]:
        values, error = await fetch_xhs_search(
            self.browser,
            identity,
            keyword,
            set(),
            max_scrolls=max(1, min(40, int(job.max_pages_per_keyword or 1))),
            stagnant_limit=max(1, min(8, int(job.stagnant_pages or 1))),
            search_sort=job.search_sort,
            content_type=job.content_type,
            block_media=self.cfg.engine.block_media_resources,
        )
        return _sort_xhs_search_items(values, job.search_sort), error

    async def _discover_xhs_favorites_browser(
            self, identity, account,
            job: KeywordCollectionJob) -> tuple[list[dict], str]:
        values, error = await fetch_xhs_favorites(
            self.browser, identity, str(getattr(account, "sec_uid", "") or ""),
            board_id=job.source_ref if job.source_type == "favorite_board" else "",
            board_name=job.source_name,
            max_scrolls=max(1, min(40, int(job.max_pages_per_keyword or 1))),
            stagnant_limit=max(1, min(8, int(job.stagnant_pages or 1))),
        )
        return values, error

    async def _discover_xhs_favorites_api(
            self, client: XhsApiClient, job: KeywordCollectionJob) -> tuple[list[dict], str]:
        found: dict[str, dict] = {}
        cursor = ""
        pages = max(1, min(40, int(job.max_pages_per_keyword or 1)))
        try:
            for page in range(pages):
                data = await client.favorite_notes(
                    board_id=(job.source_ref if job.source_type == "favorite_board" else ""),
                    cursor=cursor, page_size=min(50, job.max_contents_per_keyword))
                values = data.get("notes") or data.get("items") or []
                before = len(found)
                for raw in values:
                    brief = parse_note_brief(raw)
                    if brief:
                        raw.setdefault("xsec_source", "pc_user")
                        found.setdefault(brief["note_id"], raw)
                    if len(found) >= job.max_contents_per_keyword:
                        break
                if len(found) >= job.max_contents_per_keyword:
                    break
                next_cursor = str(data.get("cursor") or data.get("next_cursor") or "")
                has_more = bool(data.get("has_more"))
                if len(found) == before or not has_more or not next_cursor or next_cursor == cursor:
                    break
                cursor = next_cursor
                if page + 1 < pages:
                    await self._xhs_gap(0.6)
        except Exception as exc:
            return list(found.values()), f"小红书收藏读取失败: {exc}"
        return list(found.values()), ""

    async def _discover_xhs_creator_browser(
            self, identity, job: KeywordCollectionJob) -> tuple[list[dict], str]:
        values, _author, error = await fetch_xhs_notes(
            self.browser, identity, job.source_ref, set(),
            max_scrolls=max(1, min(40, job.max_pages_per_keyword)),
            block_media=self.cfg.engine.block_media_resources,
            ssr_fallback=True)
        return values[:job.max_contents_per_keyword], error

    async def _discover_xhs_creator_api(
            self, client: XhsApiClient, job: KeywordCollectionJob) -> tuple[list[dict], str]:
        found: dict[str, dict] = {}
        cursor = ""
        try:
            for page in range(max(1, min(40, job.max_pages_per_keyword))):
                data = await client.notes_by_creator(
                    job.source_ref, cursor=cursor,
                    page_size=min(30, job.max_contents_per_keyword),
                    xsec_source="pc_feed")
                values = data.get("notes") or []
                for raw in values:
                    brief = parse_note_brief(raw)
                    if brief:
                        raw.setdefault("xsec_source", "pc_feed")
                        found.setdefault(brief["note_id"], raw)
                    if len(found) >= job.max_contents_per_keyword:
                        break
                next_cursor = str(data.get("cursor") or data.get("next_cursor") or "")
                if (len(found) >= job.max_contents_per_keyword or not data.get("has_more")
                        or not next_cursor or next_cursor == cursor):
                    break
                cursor = next_cursor
                if page + 1 < job.max_pages_per_keyword:
                    await self._xhs_gap(0.6)
        except Exception as exc:
            return list(found.values()), f"小红书创作者作品读取失败: {exc}"
        return list(found.values()), ""

    async def _materialize_xhs(self, client: XhsApiClient, raw: dict) \
            -> tuple[Aweme | None, dict, str]:
        brief = parse_note_brief(raw)
        if not brief:
            return None, {}, "搜索结果缺少 note_id"
        card = {}
        error = ""
        try:
            card = await client.note_detail(
                brief["note_id"], xsec_token=brief.get("xsec_token", ""),
                xsec_source=brief.get("xsec_source") or "pc_search")
        except Exception as exc:
            error = f"详情抓取失败: {exc}"
        # Media absence is a download failure, not a reason to erase the
        # detail's author/title/counts (or the list metadata if detail failed).
        aweme = parse_note_detail(
            card if isinstance(card, dict) else {}, brief, require_media=False)
        return aweme, {"brief": brief, "card": card}, error

    async def _materialize_xhs_browser(self, identity, raw: dict) \
            -> tuple[Aweme | None, dict, str]:
        brief = parse_note_brief(raw)
        if not brief:
            return None, {}, "搜索结果缺少 note_id"
        card, error = await fetch_xhs_note_detail(
            self.browser,
            identity,
            brief["note_id"],
            xsec_token=brief.get("xsec_token", ""),
            xsec_source=brief.get("xsec_source") or "pc_search",
            block_media=self.cfg.engine.block_media_resources,
        )
        aweme = parse_note_detail(
            card if isinstance(card, dict) else {}, brief, require_media=False)
        return aweme, {"brief": brief, "card": card or {}}, error

    async def _discover_rednote_source(
        self,
        identity,
        account,
        keyword: str,
        job: KeywordCollectionJob,
        *,
        rednote_client: RednoteApiClient | None = None,
    ) -> tuple[list[dict], str]:
        mode = self._rednote_read_mode()
        items: list[dict] = []
        err = ""
        if mode in {"api", "hybrid"} and rednote_client is not None:
            if job.source_type in {"favorite_all", "favorite_board"}:
                items, err = await self._discover_xhs_favorites_api(rednote_client, job)
            elif job.source_type == "creator":
                items, err = await self._discover_xhs_creator_api(rednote_client, job)
            else:
                items, err = await self._discover_xhs(rednote_client, keyword, job)
            if err:
                err = err.replace("小红书", "海外小红书")
            if items or mode == "api":
                return items, err
            if err and classify_platform_error(err)[0] in {
                RiskCategory.AUTH,
                RiskCategory.NETWORK,
            }:
                return [], err
        elif mode == "api":
            return [], "海外小红书 API 客户端不可用"

        if job.source_type in {"favorite_all", "favorite_board"}:
            values, b_err = await fetch_rednote_favorites(
                self.browser,
                identity,
                str(getattr(account, "sec_uid", "") or ""),
                board_id=job.source_ref if job.source_type == "favorite_board" else "",
                board_name=job.source_name,
                max_scrolls=max(1, min(40, int(job.max_pages_per_keyword or 1))),
                stagnant_limit=max(1, min(8, int(job.stagnant_pages or 1))),
            )
            return values, b_err
        if job.source_type == "creator":
            values, _author, b_err = await fetch_rednote_notes(
                self.browser,
                identity,
                job.source_ref,
                set(),
                max_scrolls=max(1, min(40, int(job.max_pages_per_keyword or 1))),
                block_media=self.cfg.engine.block_media_resources,
                ssr_fallback=True,
            )
            return values[: job.max_contents_per_keyword], b_err
        values, b_err = await fetch_rednote_search(
            self.browser,
            identity,
            keyword,
            set(),
            max_scrolls=max(1, min(40, int(job.max_pages_per_keyword or 1))),
            stagnant_limit=max(1, min(8, int(job.stagnant_pages or 1))),
            search_sort=job.search_sort,
            content_type=job.content_type,
            block_media=self.cfg.engine.block_media_resources,
        )
        return _sort_xhs_search_items(values, job.search_sort), b_err

    async def _materialize_rednote(
        self,
        identity,
        raw: dict,
        *,
        rednote_client: RednoteApiClient | None = None,
    ) -> tuple[Aweme | None, dict, str]:
        brief = parse_rednote_brief(raw)
        if not brief:
            return None, {}, "搜索结果缺少 note_id"
        mode = self._rednote_read_mode()
        card: dict = {}
        error = ""
        if mode in {"api", "hybrid"} and rednote_client is not None:
            try:
                card = await rednote_client.note_detail(
                    brief["note_id"],
                    xsec_token=brief.get("xsec_token", ""),
                    xsec_source=brief.get("xsec_source") or "pc_search",
                )
            except Exception as exc:
                error = f"详情抓取失败: {exc}"
            if (
                not card
                and mode == "hybrid"
                and (
                    not error
                    or classify_platform_error(error)[0]
                    not in {RiskCategory.AUTH, RiskCategory.NETWORK}
                )
            ):
                b_card, b_err = await fetch_rednote_note_detail(
                    self.browser,
                    identity,
                    brief["note_id"],
                    xsec_token=brief.get("xsec_token", ""),
                    xsec_source=brief.get("xsec_source") or "pc_search",
                    block_media=self.cfg.engine.block_media_resources,
                    keep_context=True,
                )
                if b_card:
                    card, error = b_card, ""
                elif b_err:
                    error = b_err
        elif mode == "api":
            error = "详情抓取失败: 海外小红书 API 客户端不可用"
        else:
            b_card, error = await fetch_rednote_note_detail(
                self.browser,
                identity,
                brief["note_id"],
                xsec_token=brief.get("xsec_token", ""),
                xsec_source=brief.get("xsec_source") or "pc_search",
                block_media=self.cfg.engine.block_media_resources,
            )
            card = b_card if isinstance(b_card, dict) else {}
        aweme = parse_rednote_detail(
            card if isinstance(card, dict) else {}, brief, require_media=False
        )
        return aweme, {"brief": brief, "card": card or {}}, error

    async def _rednote_comments(
        self,
        identity,
        note_id: str,
        xsec_token: str,
        xsec_source: str,
        limit: int,
        include_replies: bool,
        *,
        rednote_client: RednoteApiClient | None = None,
    ) -> tuple[list[dict], str]:
        if limit <= 0:
            return [], ""
        mode = self._rednote_read_mode()
        parsed: list[dict] = []
        error = ""
        if mode in {"api", "hybrid"} and rednote_client is not None:
            try:
                page = await rednote_client.collect_note_comments(
                    note_id,
                    xsec_token=xsec_token,
                    xsec_source=xsec_source or "pc_search",
                    max_comments=limit,
                    max_requests=max(1, min(20, math.ceil(limit / 10) + 1)),
                    include_replies=include_replies,
                    request_interval=max(0.5, self.cfg.engine.xhs_item_gap_seconds),
                )
                parsed = [
                    item
                    for item in (parse_rednote_comment(v) for v in page["comments"])
                    if item
                ]
            except Exception as exc:
                error = f"评论抓取失败: {exc}"
        if (
            mode == "browser"
            or (
                mode == "hybrid"
                and not parsed
                and (
                    rednote_client is None
                    or (
                        error
                        and classify_platform_error(error)[0]
                        not in {RiskCategory.AUTH, RiskCategory.NETWORK}
                    )
                )
            )
        ):
            raw, b_error = await fetch_rednote_comments(
                self.browser,
                identity,
                note_id,
                set(),
                xsec_token=xsec_token,
                xsec_source=xsec_source or "pc_search",
                max_scrolls=max(1, min(20, math.ceil(limit / 10) + 1)),
                block_media=self.cfg.engine.block_media_resources,
                keep_context=True,
                include_replies=include_replies,
            )
            values = flatten_rednote_comments(raw) if include_replies else raw
            b_parsed = [
                item for item in (parse_rednote_comment(v) for v in values) if item
            ]
            if b_parsed or not b_error:
                parsed, error = b_parsed, ""
            else:
                error = b_error
        if not include_replies:
            parsed = [item for item in parsed if not item.get("reply_to")]
        return self._dedupe_comments(parsed, limit), error

    @staticmethod
    def _upsert_content(job: KeywordCollectionJob, keyword: str, aweme: Aweme,
                        author_id: str, xsec_token: str, xsec_source: str,
                        detail_error: str) \
            -> KeywordCollectionContent:
        with get_session() as session:
            row = session.exec(
                select(KeywordCollectionContent)
                .where(KeywordCollectionContent.job_id == job.id)
                .where(KeywordCollectionContent.keyword == keyword)
                .where(KeywordCollectionContent.aweme_id == aweme.aweme_id)
            ).first()
            if row is None:
                row = KeywordCollectionContent(
                    job_id=job.id, platform=job.platform, keyword=keyword,
                    aweme_id=aweme.aweme_id,
                )
            row.desc = aweme.desc
            row.author_name = aweme.author_name
            row.author_id = author_id
            row.source_url = aweme.source_url
            row.media_type = aweme.media_type
            row.create_time = aweme.create_time
            row.cover_url = aweme.cover or ""
            row.like_count = aweme.like_count
            row.comment_count = aweme.comment_count
            row.media_json = _media_json(aweme)
            row.xsec_token = xsec_token
            row.xsec_source = xsec_source
            row.error = detail_error
            if job.download_media and aweme.medias and row.download_status != "done":
                row.download_status = "pending"
            elif (job.download_media and not aweme.medias
                  and job.platform == "shipinhao"):
                row.download_status = "skipped"
                row.error = detail_error or "视频号未提供可直接下载媒体，仅保存作品元数据"
            elif job.download_media and not aweme.medias:
                row.download_status = "failed"
                row.error = detail_error or "未取得媒体地址"
            elif not job.download_media:
                row.download_status = "skipped"
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    async def _download(self, job: KeywordCollectionJob,
                        content: KeywordCollectionContent, aweme: Aweme,
                        proxy: str, *, account=None, identity=None) -> None:
        if not job.download_media or not aweme.medias or content.download_status == "done":
            return
        with get_session() as session:
            row = session.get(KeywordCollectionContent, content.id)
            if row:
                row.download_status = "downloading"
                session.add(row)
                session.commit()
        root = Path(job.download_dir).expanduser() if job.download_dir else \
            Path(self.cfg.engine.media_dir) / "keyword-collections"
        base_dir = root / f"job_{job.id}" / safe_title(content.keyword)
        routed_proxy = proxy if self.cfg.engine.route_download_via_proxy else ""
        download_ua = str(getattr(identity, "ua", "") or "")
        download_cookies = None
        download_referer = ""
        if job.platform == "tiktok" and account is not None:
            download_ua, download_cookies = _tiktok_download_session(
                account, identity, self.cfg.engine.user_agent)
            download_referer = aweme.source_url or "https://www.tiktok.com/"
        elif job.platform == "bilibili":
            if account is not None:
                download_cookies = bili_cookie_map(account.storage_state or account.cookie)
            download_referer = aweme.source_url or "https://www.bilibili.com/"
        elif job.platform == "rednote":
            download_referer = aweme.source_url or "https://www.rednote.com/"
        elif job.platform == "instagram":
            if account is not None:
                download_ua = download_ua or str(getattr(account, "ua", "") or "")
            download_referer = "https://www.instagram.com/"
        ok, local_path, error = await self.downloader.download_aweme(
            aweme, str(base_dir), routed_proxy,
            user_agent=download_ua,
            cookies=download_cookies,
            referer=download_referer,
        )
        with get_session() as session:
            row = session.get(KeywordCollectionContent, content.id)
            if row:
                row.download_status = "done" if ok else "failed"
                row.local_path = local_path
                row.error = _content_error_after_download(row.error, error)
                session.add(row)
                session.commit()
        if not ok:
            self._record_error(
                job.id,
                f"{content.keyword}/{content.aweme_id}: 媒体下载失败: "
                f"{error or '下载器未返回成功状态'}",
            )

    async def _douyin_comments(self, account, client: DouyinClient,
                                aweme: Aweme, limit: int,
                                include_replies: bool,
                                context=None) -> tuple[list[dict], str]:
        if limit <= 0:
            return [], ""
        mode = str(getattr(
            self.cfg.engine, "douyin_read_mode", "hybrid") or "hybrid").lower()
        if mode not in {"api", "hybrid", "browser"}:
            mode = "hybrid"
        error = ""
        raw: list[dict] = []
        if mode in {"api", "hybrid"}:
            try:
                raw = await client.fetch_all_comments(
                    aweme.aweme_id,
                    max_pages=max(1, min(20, math.ceil(limit / 20))),
                    with_replies=include_replies,
                    max_reply_pages=max(1, min(8, math.ceil(limit / 20))),
                )
            except Exception as exc:
                error = f"评论接口失败: {exc}"
        parsed = [item for item in (parse_comment(value) for value in raw) if item]
        if not include_replies:
            parsed = [item for item in parsed if not item.get("reply_to")]

        if mode == "api":
            # API-only 必须保持通道纯净：空 body、验证码、网络和解析失败都直接
            # 返回给任务，绝不偷偷打开页面。作品明确有评论时才把空结果记为异常。
            if not parsed and aweme.comment_count > 0 and not error:
                error = ("评论接口失败: "
                         f"{getattr(client, 'last_error', '') or 'empty_response'}")
            return self._dedupe_comments(parsed, limit), error

        # browser 模式从第一步就走页面；hybrid 仅在直连接口被静默降级时回退。
        if mode == "browser" or (not parsed and aweme.comment_count > 0):
            identity = self.browser.identity_for(account)
            fallback, browser_error = await fetch_comments(
                self.browser, identity, aweme.aweme_id, set(),
                max_scrolls=max(2, min(20, math.ceil(limit / 12))),
                block_media=self.cfg.engine.block_media_resources,
                context=context,
            )
            parsed = [item for item in (parse_comment(value) for value in fallback) if item]
            if not include_replies:
                parsed = [item for item in parsed if not item.get("reply_to")]
            error = "" if parsed else (browser_error or error)
        return self._dedupe_comments(parsed, limit), error

    async def _xhs_comments(self, client: XhsApiClient, note_id: str,
                            xsec_token: str, xsec_source: str, limit: int,
                            include_replies: bool) -> tuple[list[dict], str]:
        if limit <= 0:
            return [], ""
        parsed: list[dict] = []
        error = ""
        try:
            page = await client.collect_note_comments(
                note_id, xsec_token=xsec_token,
                xsec_source=xsec_source or "pc_search",
                max_comments=limit,
                max_requests=max(1, min(20, math.ceil(limit / 10) + 1)),
                include_replies=include_replies,
                request_interval=max(0.5, self.cfg.engine.xhs_item_gap_seconds))
            parsed = [item for item in (
                parse_xhs_comment(value) for value in page["comments"]) if item]
        except Exception as exc:
            error = f"评论抓取失败: {exc}"
        if not include_replies:
            parsed = [item for item in parsed if not item.get("reply_to")]
        return self._dedupe_comments(parsed, limit), error

    async def _xhs_comments_browser(
            self, identity, note_id: str, xsec_token: str, xsec_source: str,
            limit: int,
            include_replies: bool) -> tuple[list[dict], str]:
        if limit <= 0:
            return [], ""
        raw, error = await fetch_xhs_comments(
            self.browser,
            identity,
            note_id,
            set(),
            xsec_token=xsec_token,
            xsec_source=xsec_source or "pc_search",
            max_scrolls=max(1, min(20, math.ceil(limit / 10) + 1)),
            block_media=self.cfg.engine.block_media_resources,
        )
        values = flatten_xhs_comments(raw) if include_replies else raw
        parsed = [item for item in
                  (parse_xhs_comment(value) for value in values) if item]
        if not include_replies:
            parsed = [item for item in parsed if not item.get("reply_to")]
        return self._dedupe_comments(parsed, limit), error

    async def _ks_comments(self, identity, photo_id: str, limit: int,
                           include_replies: bool) -> tuple[list[dict], str]:
        if limit <= 0:
            return [], ""
        raw, error = await fetch_ks_comments(
            self.browser,
            identity,
            photo_id,
            set(),
            max_scrolls=max(1, min(20, math.ceil(limit / 10) + 1)),
            block_media=self.cfg.engine.block_media_resources,
        )
        values = flatten_ks_comments(raw) if include_replies else raw
        parsed = [item for item in
                  (parse_ks_comment(value) for value in values) if item]
        if not include_replies:
            parsed = [item for item in parsed if not item.get("reply_to")]
        return self._dedupe_comments(parsed, limit), error

    @staticmethod
    def _extract_channels_audit_counts(audit: Any) -> dict:
        if not isinstance(audit, dict):
            return {}
        pages = (audit.get("main_pagination") or {}).get("pages") or []
        if isinstance(pages, list) and pages and isinstance(pages[0], dict):
            ci = pages[0].get("count_info")
            if isinstance(ci, dict):
                return ci
        return {}

    @staticmethod
    def _backfill_channels_counts(
        content: KeywordCollectionContent,
        *,
        like_count: int = 0,
        comment_count: int = 0,
    ) -> None:
        new_likes = max(int(content.like_count or 0), max(0, int(like_count or 0)))
        new_comments = max(int(content.comment_count or 0), max(0, int(comment_count or 0)))
        if new_likes == content.like_count and new_comments == content.comment_count:
            return
        with get_session() as session:
            row = session.get(KeywordCollectionContent, content.id)
            if row is not None:
                row.like_count = max(int(row.like_count or 0), new_likes)
                row.comment_count = max(int(row.comment_count or 0), new_comments)
                session.add(row)
                session.commit()
        content.like_count = new_likes
        content.comment_count = new_comments

    async def _channels_comments(self, identity, object_id: str, limit: int,
                                 include_replies: bool, *,
                                 object_nonce_id: str = "",
                                 proto_client: ChannelsProtocolClient | None = None,
                                 source_type: str = "account_works") -> tuple[list[dict], str]:
        if limit <= 0:
            return [], ""
        mode = self._channels_read_mode()
        max_proto_pages = max(2, min(50, math.ceil(limit / 15) + 2))
        counts_cache = self.__dict__.setdefault("_last_channels_counts", {})
        if mode == "protocol":
            if proto_client is None:
                return [], "视频号原生评论采集尚未连接本机微信通道（请在「设置 → 视频号微信通道」开启一键本地代理或配置 CDP）"
            comments, audit, err = await proto_client.collect_comments(
                object_id,
                object_nonce_id,
                limit=limit,
                include_replies=include_replies,
                max_pages=max_proto_pages,
            )
            counts_cache[object_id] = self._extract_channels_audit_counts(audit)
            return self._dedupe_comments(comments, limit), err

        if mode == "auto" and proto_client is not None:
            comments, audit, proto_err = await proto_client.collect_comments(
                object_id,
                object_nonce_id,
                limit=limit,
                include_replies=include_replies,
                max_pages=max_proto_pages,
            )
            counts_cache[object_id] = self._extract_channels_audit_counts(audit)
            if comments or not proto_err or source_type != "account_works":
                return self._dedupe_comments(comments, limit), proto_err

        if source_type != "account_works":
            return (
                [],
                "非本账号视频号作品评论采集需要连接本机已登录微信（请在「设置 → 视频号微信通道」开启一键本地代理并在微信内打开一次视频号页面）",
            )

        raw, error = await fetch_channels_comments(
            self.browser,
            identity,
            object_id,
            set(),
            max_scrolls=max(1, min(20, math.ceil(limit / 10) + 1)),
            block_media=self.cfg.engine.block_media_resources,
        )
        values = flatten_channels_comments(raw) if include_replies else raw
        parsed = [item for item in
                  (parse_channels_comment(value) for value in values) if item]
        if not include_replies:
            parsed = [item for item in parsed if not item.get("reply_to")]
        return self._dedupe_comments(parsed, limit), error

    async def _tiktok_comments(self, identity, aweme_id: str, limit: int,
                               include_replies: bool, *, author_unique_id: str = "",
                               target_url: str = "", account=None) -> tuple[list[dict], str]:
        if limit <= 0:
            return [], ""
        mode = str(getattr(self.cfg.engine, "tiktok_read_mode", "native") or "native")
        if mode == "native" and account is None:
            return [], "TikTok 离线评论读取缺少账号会话"
        if mode in {"auto", "native"} and account is not None:
            client = TikTokNativeClient(
                storage_state=getattr(account, "storage_state", "") or "",
                user_agent=identity.ua,
                proxy=identity.proxy,
                locale=identity.locale,
                timezone=identity.timezone_id,
                viewport=(identity.viewport_w, identity.viewport_h),
                region="US",
                device_seed=identity.fp_seed or str(identity.account_id or "creatorhub"),
                timeout=max(10, int(self.cfg.engine.request_timeout_seconds)),
            )
            native = await client.comments(
                aweme_id, count=limit, include_replies=include_replies,
                author_unique_id=author_unique_id, target_url=target_url)
            if native.items or native.compatible or mode == "native":
                parsed = [
                    item for item in
                    (parse_tiktok_comment(value) for value in native.items)
                    if item
                ]
                if not include_replies:
                    parsed = [item for item in parsed if not item.get("reply_to")]
                return self._dedupe_comments(parsed, limit), native.error
            print(
                f"[tiktok-comments] 离线接口兼容失败，页面回退: "
                f"{native.category}; HTTP={native.http_status}; "
                f"bytes={native.body_length}; tt_orcas_res={native.tt_orcas_res!r}"
            )
        raw, error = await fetch_tiktok_comments(
            self.browser, identity, aweme_id, set(), max_comments=limit,
            max_scrolls=max(1, min(20, math.ceil(limit / 10) + 1)),
            include_replies=include_replies,
            block_media=self.cfg.engine.block_media_resources,
            author_unique_id=author_unique_id, target_url=target_url)
        parsed = [item for item in (parse_tiktok_comment(value) for value in raw) if item]
        if not include_replies:
            parsed = [item for item in parsed if not item.get("reply_to")]
        return self._dedupe_comments(parsed, limit), error

    @staticmethod
    def _dedupe_comments(values: list[dict], limit: int) -> list[dict]:
        found: dict[str, dict] = {}
        for value in values:
            cid = str(value.get("comment_id") or "")
            if cid:
                found.setdefault(cid, value)
            if len(found) >= limit:
                break
        return list(found.values())[:limit]

    @staticmethod
    def _persist_comments(job_id: int, content_id: int, platform: str,
                          aweme_id: str, comments: list[dict]) -> int:
        with get_session() as session:
            known = set(session.exec(
                select(KeywordCollectionComment.comment_id)
                .where(KeywordCollectionComment.job_id == job_id)
                .where(KeywordCollectionComment.content_id == content_id)
            ).all())
            added = 0
            for item in comments:
                cid = str(item.get("comment_id") or "")
                if not cid or cid in known:
                    continue
                known.add(cid)
                session.add(KeywordCollectionComment(
                    job_id=job_id, content_id=content_id, platform=platform,
                    aweme_id=aweme_id, comment_id=cid,
                    text=item.get("text") or "",
                    user_nickname=item.get("user_nickname") or "",
                    user_sec_uid=item.get("user_sec_uid") or "",
                    ip_label=item.get("ip_label") or "",
                    like_count=int(item.get("like_count") or 0),
                    create_time=int(item.get("create_time") or 0),
                    reply_to=item.get("reply_to") or "",
                ))
                added += 1
            content = session.get(KeywordCollectionContent, content_id)
            if content:
                content.collected_comment_count = len(known)
                session.add(content)
            session.commit()
        return added

    async def run(self, job_id: int, account) -> dict:
        job = self._job(job_id)
        if not job:
            raise RuntimeError("采集任务不存在")
        keywords = _loads_keywords(job.keywords)
        if job.source_type in {
                "favorite_all", "favorite_board", "creator", "account_works"}:
            default_name = ("全部收藏" if job.source_type == "favorite_all" else
                            "收藏夹" if job.source_type == "favorite_board" else
                            "本账号作品" if job.source_type == "account_works" else
                            f"创作者 {job.source_ref}")
            keywords = [job.source_name or default_name]
        if not keywords:
            raise RuntimeError("采集任务没有可执行来源")

        identity = (
            self.browser.identity_for(account)
            if account is not None
            else self.browser.anon_identity()
        )
        proxy = (account.proxy or "") if account is not None else ""
        douyin_client = None
        xhs_client = None
        if job.platform == "douyin":
            douyin_client = DouyinClient(
                douyin_cookie_from_state(account.storage_state), identity.ua,
                timeout=self.cfg.engine.request_timeout_seconds, proxy=proxy,
                storage_state=account.storage_state,
                **douyin_client_environment(identity))
        elif job.platform == "xhs" and not self._xhs_browser_reads_enabled():
            cookie = cookie_str_from_state(account.storage_state)
            if not has_a1(cookie):
                raise RuntimeError("小红书登录态缺少 a1，请重新扫码登录")
            xhs_client = XhsApiClient(
                cookie, self._direct_request_ua(identity),
                timeout=self.cfg.engine.request_timeout_seconds, proxy=proxy)

        if job.platform == "douyin":
            mode = str(getattr(
                self.cfg.engine, "douyin_read_mode", "hybrid") or "hybrid").lower()
            if mode == "api":
                # 纯 API：只创建 HTTP session，不启动 Chromium，也不给后续评论
                # 路径传 page context。失败由任务记录，不做隐式浏览器回退。
                self._progress(job_id, step="启动抖音 API 采集")
                async with douyin_client.session_scope():
                    return await self._run_keywords(
                        job_id, job, account, keywords, proxy,
                        douyin_client=douyin_client, context=None)

            # Hybrid 先走 API；只有普通兼容性失败才允许浏览器回退。
            # verification_required/风控/登录/网络信号由调度器冷却，不开浏览器。
            if mode == "hybrid":
                self._progress(job_id, step="启动抖音 API 优先采集")
                async with douyin_client.session_scope():
                    return await self._run_keywords(
                        job_id, job, account, keywords, proxy,
                        douyin_client=douyin_client, context=None)
            self._progress(job_id, step="启动抖音可见采集窗口")
            async with self.browser.temporary_headed_context(identity) as context:
                return await self._run_keywords(
                    job_id, job, account, keywords, proxy,
                    douyin_client=douyin_client, context=context)
        if job.platform == "kuaishou":
            return await self._run_keywords(
                job_id, job, account, keywords, proxy, identity=identity)
        if job.platform == "tiktok":
            mode = str(getattr(self.cfg.engine, "tiktok_read_mode", "native") or "native")
            if mode == "browser":
                self._progress(job_id, step="启动 TikTok 可见采集窗口")
                async with self.browser.temporary_headed_context(identity) as context:
                    return await self._run_keywords(
                        job_id, job, account, keywords, proxy,
                        identity=identity, context=context)
            self._progress(job_id, step="启动 TikTok 离线签名 API 采集")
            return await self._run_keywords(
                job_id, job, account, keywords, proxy,
                identity=identity, context=None)
        if job.platform == "shipinhao":
            channels_client = self._channels_protocol_client()
            try:
                return await self._run_keywords(
                    job_id, job, account, keywords, proxy,
                    identity=identity, channels_client=channels_client)
            finally:
                if channels_client is not None:
                    await channels_client.close()
        if job.platform == "bilibili":
            bili_state = (account.storage_state or account.cookie) if account is not None else ""
            bili_client = BilibiliClient(
                bili_state,
                getattr(identity, "ua", "") or self.cfg.engine.user_agent,
                proxy=proxy,
                timeout=self.cfg.engine.request_timeout_seconds,
            )
            self._progress(job_id, step="启动哔哩哔哩采集任务")
            async with bili_client.session_scope():
                return await self._run_keywords(
                    job_id, job, account, keywords, proxy,
                    identity=identity, bili_client=bili_client)
        if job.platform == "rednote":
            rn_mode = self._rednote_read_mode()
            rn_state = (
                (getattr(account, "storage_state", "") or getattr(account, "creator_storage_state", ""))
                if account is not None
                else ""
            )
            rn_cookie = rednote_cookie_str_from_state(rn_state)
            rednote_client = None
            if rn_mode in {"api", "hybrid"}:
                if rednote_has_a1(rn_cookie):
                    rednote_client = RednoteApiClient(
                        rn_cookie,
                        self._direct_request_ua(identity),
                        timeout=self.cfg.engine.request_timeout_seconds,
                        proxy=proxy,
                        storage_state=rn_state,
                    )
                elif rn_mode == "api":
                    raise RuntimeError("海外小红书登录态缺少 a1，请重新扫码登录或切换为浏览器读取模式")
            self._progress(job_id, step="启动海外小红书采集任务")
            if rednote_client is not None:
                async with rednote_client.session_scope():
                    return await self._run_keywords(
                        job_id, job, account, keywords, proxy,
                        identity=identity, rednote_client=rednote_client)
            return await self._run_keywords(
                job_id, job, account, keywords, proxy,
                identity=identity, rednote_client=None)
        if job.platform == "instagram":
            ig_state = (account.storage_state or account.cookie) if account is not None else ""
            ig_client = InstagramClient(
                ig_state,
                getattr(identity, "ua", "") or self.cfg.engine.user_agent,
                proxy=proxy,
                timeout=self.cfg.engine.request_timeout_seconds,
                account_seed=(getattr(account, "fp_seed", "") or str(getattr(account, "id", "") or "")),
                timezone_id=(getattr(account, "timezone_id", "Asia/Shanghai") if account else "Asia/Shanghai"),
            )
            self._progress(job_id, step="启动 Instagram 采集任务")
            async with ig_client.session_scope():
                return await self._run_keywords(
                    job_id, job, account, keywords, proxy,
                    identity=identity, ig_client=ig_client)
        if xhs_client is not None:
            async with xhs_client.session_scope():
                return await self._run_keywords(
                    job_id, job, account, keywords, proxy,
                    xhs_client=xhs_client, identity=identity)
        return await self._run_keywords(
            job_id, job, account, keywords, proxy,
            xhs_client=xhs_client, identity=identity)

    async def _discover_bilibili_keyword(
        self,
        keyword: str,
        job: KeywordCollectionJob,
        *,
        bili_client: BilibiliClient | None,
        identity=None,
    ) -> tuple[list[dict], str]:
        mode = str(getattr(self.cfg.engine, "bilibili_read_mode", "hybrid") or "hybrid").lower()
        order = (
            "pubdate" if job.search_sort == "latest" else
            "click" if job.search_sort == "most_liked" else
            "totalrank"
        )
        items: list[dict] = []
        err = ""
        if mode in {"api", "hybrid"} and bili_client is not None:
            seen: set[str] = set()
            max_pages = max(1, min(10, int(job.max_pages_per_keyword or 3)))
            for page_no in range(1, max_pages + 1):
                batch, has_more, b_err = await bili_client.search_videos(
                    keyword, page=page_no, page_size=20, order=order
                )
                if b_err:
                    err = b_err
                    break
                for row in batch:
                    bvid = str(row.get("bvid") or "")
                    if bvid and bvid not in seen:
                        seen.add(bvid)
                        items.append(row)
                if len(items) >= max(20, job.max_contents_per_keyword * 2) or not has_more:
                    break
        if (mode == "browser" or (mode == "hybrid" and not items)) and identity is not None:
            b_items, b_err = await fetch_bili_search(
                self.browser, identity, keyword,
                max_scrolls=max(1, min(8, int(job.max_pages_per_keyword or 3))),
            )
            if b_items:
                items, err = b_items, ""
            elif mode == "browser":
                err = b_err
        return items, err

    async def _discover_bilibili_creator(
        self,
        job: KeywordCollectionJob,
        *,
        bili_client: BilibiliClient | None,
        identity=None,
    ) -> tuple[list[dict], str]:
        mode = str(getattr(self.cfg.engine, "bilibili_read_mode", "hybrid") or "hybrid").lower()
        ua = getattr(bili_client, "user_agent", "") or self.cfg.engine.user_agent
        px = getattr(bili_client, "proxy", "") or ""
        mid = await resolve_bili_mid(job.source_ref, ua, proxy=px) or str(job.source_ref or "").strip()
        if not mid:
            return [], "无法解析 B 站 UP 主 UID，请输入空间链接或纯数字 UID"
        items: list[dict] = []
        err = ""
        if mode in {"api", "hybrid"} and bili_client is not None:
            seen: set[str] = set()
            max_pages = max(1, min(10, int(job.max_pages_per_keyword or 3)))
            for pn in range(1, max_pages + 1):
                batch, has_more, b_err = await bili_client.fetch_user_videos(
                    mid, pn=pn, ps=30
                )
                if b_err:
                    err = b_err
                    break
                for row in batch:
                    bvid = str(row.get("bvid") or "")
                    if bvid and bvid not in seen:
                        seen.add(bvid)
                        items.append(row)
                if len(items) >= max(20, job.max_contents_per_keyword * 2) or not has_more:
                    break
        if (mode == "browser" or (mode == "hybrid" and not items)) and identity is not None:
            b_items, _author, b_err = await fetch_bili_videos(
                self.browser, identity, mid, set(),
                max_scrolls=max(1, min(8, int(job.max_pages_per_keyword or 3))),
            )
            if b_items:
                items, err = b_items, ""
            elif mode == "browser":
                err = b_err
        return items, err

    async def _discover_bilibili_account(
        self,
        account,
        job: KeywordCollectionJob,
        *,
        bili_client: BilibiliClient | None,
        identity=None,
    ) -> tuple[list[dict], str]:
        if bili_client is not None:
            works, err = await bili_client.fetch_all_creator_archives(
                max_pages=max(1, min(8, int(job.max_pages_per_keyword or 3)))
            )
            if works:
                return [
                    {
                        "bvid": w["item_id"],
                        "aid": w.get("aid", 0),
                        "title": w.get("desc", ""),
                        "pic": w.get("cover_url", ""),
                        "pubdate": w.get("create_time", 0),
                        "duration": w.get("duration", 0),
                        "author": getattr(account, "nickname", "") or "",
                        "mid": getattr(account, "uid", "") or getattr(account, "sec_uid", "") or "",
                        "stat": {
                            "like": w.get("like_count", 0),
                            "reply": w.get("comment_count", 0),
                            "view": w.get("play_count", 0),
                        },
                    }
                    for w in works
                ], ""
            if err.startswith("logged_out"):
                return [], err
        mid = str(getattr(account, "uid", "") or getattr(account, "sec_uid", "") or "")
        if mid and identity is not None:
            b_items, _author, b_err = await fetch_bili_videos(
                self.browser, identity, mid, set(), max_scrolls=3
            )
            return b_items, b_err
        return [], "未获取到本账号 B 站稿件"

    async def _bilibili_comments(
        self,
        identity,
        work_id: str,
        limit: int,
        include_replies: bool,
        *,
        bili_client: BilibiliClient | None = None,
    ) -> tuple[list[dict], str]:
        if limit <= 0:
            return [], ""
        mode = str(getattr(self.cfg.engine, "bilibili_read_mode", "hybrid") or "hybrid").lower()
        comments: list[dict] = []
        err = ""
        if mode in {"api", "hybrid"} and bili_client is not None:
            comments, err = await bili_client.fetch_comments(
                work_id, max_count=limit, include_replies=include_replies
            )
        if (mode == "browser" or (mode == "hybrid" and not comments and err)) and identity is not None:
            b_comments, b_err = await fetch_bili_comments(
                self.browser, identity, work_id, max_count=limit
            )
            if b_comments:
                comments, err = b_comments, ""
            elif mode == "browser":
                err = b_err
        if not include_replies:
            comments = [c for c in comments if not c.get("reply_to")]
        return comments[:limit], err

    async def _discover_instagram_keyword(
        self,
        keyword: str,
        job: KeywordCollectionJob,
        *,
        ig_client: InstagramClient | None,
        identity=None,
    ) -> tuple[list[dict], str]:
        mode = str(getattr(self.cfg.engine, "instagram_read_mode", "hybrid") or "hybrid").lower()
        items: list[dict] = []
        err = ""
        max_pages = max(1, min(8, int(job.max_pages_per_keyword or 3)))
        if mode in {"api", "hybrid"} and ig_client is not None:
            items, err = await ig_client.search_posts(
                keyword, max_pages=max_pages, sort=job.search_sort or "general"
            )
        if (mode == "browser" or (mode == "hybrid" and not items and not err.startswith(("logged_out:", "verification_required:")))) and identity is not None:
            b_items, b_err = await fetch_ig_search(
                self.browser, identity, keyword, max_scrolls=max_pages
            )
            if b_items:
                items, err = b_items, ""
            elif mode == "browser":
                err = b_err
        return items, err

    async def _discover_instagram_creator(
        self,
        job: KeywordCollectionJob,
        *,
        ig_client: InstagramClient | None,
        identity=None,
    ) -> tuple[list[dict], str]:
        mode = str(getattr(self.cfg.engine, "instagram_read_mode", "hybrid") or "hybrid").lower()
        user_ref = resolve_ig_user_target(job.source_ref) or str(job.source_ref or "").strip()
        if not user_ref:
            return [], "无法解析 Instagram 创作者用户名或链接"
        items: list[dict] = []
        err = ""
        max_pages = max(1, min(10, int(job.max_pages_per_keyword or 3)))
        if mode in {"api", "hybrid"} and ig_client is not None:
            seen: set[str] = set()
            cursor = ""
            for _ in range(max_pages):
                batch, next_cursor, more, f_err = await ig_client.fetch_user_feed(
                    user_ref, max_id=cursor
                )
                if f_err:
                    err = f_err
                    break
                for row in batch:
                    code = str(row.get("code") or row.get("shortcode") or row.get("pk") or "")
                    if code and code not in seen:
                        seen.add(code)
                        items.append(row)
                if len(items) >= max(20, job.max_contents_per_keyword * 2) or not more or not next_cursor or next_cursor == cursor:
                    break
                cursor = next_cursor
        if (mode == "browser" or (mode == "hybrid" and not items and not err.startswith(("logged_out:", "verification_required:")))) and identity is not None:
            b_items, _author, b_err = await fetch_ig_posts(
                self.browser, identity, user_ref, set(), max_scrolls=max_pages
            )
            if b_items:
                items, err = b_items, ""
            elif mode == "browser":
                err = b_err
        return items, err

    async def _discover_instagram_account(
        self,
        account,
        job: KeywordCollectionJob,
        *,
        ig_client: InstagramClient | None,
        identity=None,
    ) -> tuple[list[dict], str]:
        user_ref = str(
            getattr(account, "douyin_id", "")
            or getattr(account, "uid", "")
            or getattr(account, "sec_uid", "")
            or ""
        ).strip()
        if ig_client is not None and not user_ref:
            prof, p_err = await ig_client.fetch_self_profile()
            if p_err.startswith(("logged_out:", "verification_required:")):
                return [], p_err
            user_ref = str(prof.get("uid") or prof.get("unique_id") or ig_client.self_uid)
        if not user_ref:
            return [], "未获取到当前 Instagram 账号标识，请先在账号页刷新资料"
        clone_job = KeywordCollectionJob(
            platform="instagram",
            account_id=getattr(account, "id", 0) or 0,
            source_type="creator",
            source_ref=user_ref,
            max_contents_per_keyword=job.max_contents_per_keyword,
            max_pages_per_keyword=job.max_pages_per_keyword,
        )
        return await self._discover_instagram_creator(
            clone_job, ig_client=ig_client, identity=identity
        )

    async def _instagram_comments(
        self,
        identity,
        work_id: str,
        limit: int,
        include_replies: bool,
        *,
        ig_client: InstagramClient | None = None,
    ) -> tuple[list[dict], str]:
        if limit <= 0:
            return [], ""
        mode = str(getattr(self.cfg.engine, "instagram_read_mode", "hybrid") or "hybrid").lower()
        comments: list[dict] = []
        err = ""
        if mode in {"api", "hybrid"} and ig_client is not None:
            comments, err = await ig_client.fetch_comments(
                work_id, max_count=limit, include_replies=include_replies
            )
        if (mode == "browser" or (mode == "hybrid" and not comments and err and not err.startswith(("logged_out:", "verification_required:")))) and identity is not None:
            b_comments, b_err = await fetch_ig_comments(
                self.browser, identity, work_id, max_count=limit, include_replies=include_replies
            )
            if b_comments:
                comments, err = b_comments, ""
            elif mode == "browser":
                err = b_err
        if not include_replies:
            comments = [c for c in comments if not c.get("reply_to")]
        return comments[:limit], err

    async def _run_keywords(self, job_id: int, job: KeywordCollectionJob,
                            account, keywords: list[str], proxy: str,
                             douyin_client: DouyinClient | None = None,
                             xhs_client: XhsApiClient | None = None,
                             rednote_client: RednoteApiClient | None = None,
                             channels_client: ChannelsProtocolClient | None = None,
                             bili_client: BilibiliClient | None = None,
                             ig_client: InstagramClient | None = None,
                             context=None, identity=None) -> dict:

        browser_reads = self._xhs_browser_reads_enabled()
        self._progress(
            job_id,
            processed_keyword_count=0,
            current_item_index=0,
            current_item_total=0,
        )
        for keyword_index, keyword in enumerate(keywords):
            if self._cancel_requested(job_id):
                return {"canceled": True, "errors": self._job(job_id).error_count}
            if job.platform == "douyin" and keyword_index:
                gap = max(0.0, float(self.cfg.engine.douyin_keyword_gap_seconds))
                if gap:
                    await asyncio.sleep(gap * random.uniform(1.0, 1.35))
            elif job.platform in {"xhs", "rednote"} and keyword_index:
                await self._xhs_gap(
                    self.cfg.engine.xhs_keyword_gap_seconds)
            elif job.platform in {"kuaishou", "shipinhao"} and keyword_index:
                await asyncio.sleep(0.8 * random.uniform(1.0, 1.35))
            elif job.platform == "tiktok" and keyword_index:
                await asyncio.sleep(1.0 * random.uniform(1.0, 1.35))
            elif job.platform == "bilibili" and keyword_index:
                await asyncio.sleep(0.8 * random.uniform(1.0, 1.35))
            elif job.platform == "instagram" and keyword_index:
                await asyncio.sleep(1.2 * random.uniform(1.0, 1.35))
            self._progress(
                job_id,
                keyword=keyword,
                step=(
                    "同步本账号作品"
                    if (job.platform in {"shipinhao", "bilibili", "instagram"} and job.source_type == "account_works")
                    else "读取创作者作品"
                    if (job.platform in {"shipinhao", "bilibili", "instagram"} and job.source_type == "creator")
                    else "搜索作品"
                ),
                processed_keyword_count=keyword_index,
                current_item_index=0,
                current_item_total=0,
            )
            if job.platform == "douyin":
                if job.source_type == "creator":
                    raw_items, search_error = await self._discover_douyin_creator(
                        account, job, douyin_client)
                else:
                    raw_items, search_error = await self._discover_douyin(
                        account, keyword, job, client=douyin_client,
                        context=context)
            elif job.platform == "kuaishou":
                if job.source_type == "creator":
                    raw_items, search_error = await self._discover_ks_creator(
                        account, job)
                else:
                    raw_items, search_error = await self._discover_ks(
                        account, keyword, job)
            elif job.platform == "tiktok":
                raw_items, search_error = await self._discover_tiktok(
                    account, identity, keyword, job)
            elif job.platform == "bilibili":
                if job.source_type == "creator":
                    raw_items, search_error = await self._discover_bilibili_creator(
                        job, bili_client=bili_client, identity=identity)
                elif job.source_type == "account_works":
                    raw_items, search_error = await self._discover_bilibili_account(
                        account, job, bili_client=bili_client, identity=identity)
                else:
                    raw_items, search_error = await self._discover_bilibili_keyword(
                        keyword, job, bili_client=bili_client, identity=identity)
            elif job.platform == "instagram":
                if job.source_type == "creator":
                    raw_items, search_error = await self._discover_instagram_creator(
                        job, ig_client=ig_client, identity=identity)
                elif job.source_type == "account_works":
                    raw_items, search_error = await self._discover_instagram_account(
                        account, job, ig_client=ig_client, identity=identity)
                else:
                    raw_items, search_error = await self._discover_instagram_keyword(
                        keyword, job, ig_client=ig_client, identity=identity)
            elif job.platform == "shipinhao":
                if job.source_type == "creator":
                    raw_items, search_error = await self._discover_channels_creator(
                        job, proto_client=channels_client)
                elif job.source_type == "keyword":
                    raw_items, search_error = await self._discover_channels_keyword(
                        keyword, job, proto_client=channels_client)
                else:
                    raw_items, search_error = await self._discover_channels_account(
                        identity, job, account=account, proto_client=channels_client)
            elif job.platform == "rednote":
                raw_items, search_error = await self._discover_rednote_source(
                    identity, account, keyword, job, rednote_client=rednote_client)
            elif browser_reads:
                if job.source_type in {"favorite_all", "favorite_board"}:
                    raw_items, search_error = await self._discover_xhs_favorites_browser(
                        identity, account, job)
                elif job.source_type == "creator":
                    raw_items, search_error = await self._discover_xhs_creator_browser(
                        identity, job)
                else:
                    raw_items, search_error = await self._discover_xhs_browser(
                        identity, keyword, job)
            else:
                if job.source_type in {"favorite_all", "favorite_board"}:
                    raw_items, search_error = await self._discover_xhs_favorites_api(
                        xhs_client, job)
                elif job.source_type == "creator":
                    raw_items, search_error = await self._discover_xhs_creator_api(
                        xhs_client, job)
                else:
                    raw_items, search_error = await self._discover_xhs(
                        xhs_client, keyword, job)
            if search_error:
                self._record_error(job_id, f"{keyword}: {search_error}")
                if "登录态已失效" in search_error and account is not None:
                    self._mark_account_invalid(account.id)
                category, _ = classify_platform_error(search_error)
                if category in {
                        RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK}:
                    # 风险/登录态/网络异常后停止后续关键词，避免一次任务继续放大信号。
                    break
            if not raw_items:
                self._progress(
                    job_id,
                    processed_keyword_count=keyword_index + 1,
                    current_item_index=0,
                    current_item_total=0,
                )
                continue

            if job.platform == "kuaishou":
                raw_items = _sort_ks_search_items(raw_items, job.search_sort)

            candidates = (raw_items[:job.max_contents_per_keyword]
                          if job.platform == "douyin" else raw_items)
            accepted = 0
            for index, raw in enumerate(candidates, 1):
                if self._cancel_requested(job_id):
                    return {"canceled": True, "errors": self._job(job_id).error_count}
                self._progress(
                    job_id, keyword=keyword,
                    step=f"处理作品 {index}/{len(candidates)}",
                    processed_keyword_count=keyword_index,
                    current_item_index=index,
                    current_item_total=len(candidates),
                )
                if job.platform in {"xhs", "rednote"} and index > 1:
                    await self._xhs_gap(self.cfg.engine.xhs_item_gap_seconds)
                detail_error = ""
                detail_raw = raw
                source = ""
                if job.platform == "douyin":
                    aweme = parse_aweme(raw, job.video_quality or "highest")
                    if aweme is None and raw.get("aweme_id"):
                        author = raw.get("author") or {}
                        aweme = Aweme(
                            aweme_id=str(raw.get("aweme_id")),
                            desc=(raw.get("desc") or "").strip(),
                            create_time=int(raw.get("create_time") or 0),
                            author_name=author.get("nickname") or "",
                            media_type="images" if raw.get("images") else "video",
                        )
                        detail_error = "未取得媒体地址"
                    token = ""
                elif job.platform == "kuaishou":
                    aweme = parse_ks_feed(raw, job.video_quality or "highest")
                    token = ""
                elif job.platform == "shipinhao":
                    aweme = parse_channels_feed(raw, job.video_quality or "highest")
                    if (aweme is not None and channels_client is not None
                            and job.download_media and not aweme.medias
                            and aweme.aweme_id):
                        detail_obj, _ = await channels_client.fetch_feed_detail(
                            object_id=aweme.aweme_id,
                            object_nonce_id=aweme.object_nonce_id,
                        )
                        if detail_obj:
                            detail_aweme = parse_channels_feed(
                                detail_obj, job.video_quality or "highest")
                            if detail_aweme is not None:
                                detail_aweme.like_count = max(
                                    detail_aweme.like_count, aweme.like_count)
                                detail_aweme.comment_count = max(
                                    detail_aweme.comment_count, aweme.comment_count)
                                if not detail_aweme.author_name:
                                    detail_aweme.author_name = aweme.author_name
                                if not detail_aweme.desc:
                                    detail_aweme.desc = aweme.desc
                                aweme = detail_aweme
                                detail_raw = detail_obj
                    if (aweme is not None and not aweme.author_name
                            and job.source_type == "account_works"):
                        aweme.author_name = str(getattr(account, "nickname", "") or "")
                    token = (aweme.object_nonce_id if aweme is not None else "") or ""
                elif job.platform == "tiktok":
                    aweme = parse_tiktok_aweme(raw, job.video_quality or "highest")
                    token = ""
                elif job.platform == "bilibili":
                    aweme = parse_bili_aweme(raw, require_media=False)
                    if (
                        aweme is not None
                        and job.download_media
                        and not aweme.medias
                        and bili_client is not None
                    ):
                        full_aw, play_err = await bili_client.fetch_aweme_by_work(
                            aweme.aweme_id,
                            with_play_url=True,
                            quality=job.video_quality or "highest",
                        )
                        if full_aw is not None:
                            aweme = full_aw
                        if play_err and not aweme.medias:
                            detail_error = play_err
                    token = ""
                elif job.platform == "rednote":
                    aweme, parts, detail_error = await self._materialize_rednote(
                        identity, raw, rednote_client=rednote_client)
                    brief = parts.get("brief") or {}
                    card = parts.get("card") or {}
                    detail_raw = card or raw
                    token = brief.get("xsec_token") or ""
                    source = brief.get("xsec_source") or (
                        "pc_feed" if job.source_type == "creator" else
                        "pc_user" if job.source_type != "keyword" else "pc_search")
                elif job.platform == "instagram":
                    aweme = parse_ig_aweme(raw, require_media=False)
                    if (
                        aweme is not None
                        and (not aweme.medias or (aweme.media_type == "video" and not any(m.kind == "video" for m in aweme.medias)))
                        and ig_client is not None
                    ):
                        full_aw, full_raw, info_err = await ig_client.fetch_media_info(aweme.aweme_id)
                        if full_aw is not None:
                            aweme = full_aw
                            detail_raw = full_raw
                        elif info_err and job.download_media and not aweme.medias:
                            detail_error = info_err
                    token = ""
                elif browser_reads:
                    aweme, parts, detail_error = \
                        await self._materialize_xhs_browser(identity, raw)
                    brief = parts.get("brief") or {}
                    card = parts.get("card") or {}
                    detail_raw = card or raw
                    token = brief.get("xsec_token") or ""
                    source = brief.get("xsec_source") or (
                        "pc_feed" if job.source_type == "creator" else
                        "pc_user" if job.source_type != "keyword" else "pc_search")
                else:
                    aweme, parts, detail_error = await self._materialize_xhs(xhs_client, raw)
                    brief = parts.get("brief") or {}
                    card = parts.get("card") or {}
                    detail_raw = card or raw
                    token = brief.get("xsec_token") or ""
                    source = brief.get("xsec_source") or (
                        "pc_feed" if job.source_type == "creator" else
                        "pc_user" if job.source_type != "keyword" else "pc_search")
                if job.platform in {"xhs", "rednote"} and detail_error:
                    category, _ = classify_platform_error(detail_error)
                    if category in {
                            RiskCategory.RISK, RiskCategory.AUTH,
                            RiskCategory.NETWORK}:
                        note_id = str(
                            (parts.get("brief") or {}).get("note_id") or "")
                        self._record_error(
                            job_id, f"{keyword}/{note_id}: {detail_error}")
                        return self._result(job_id, stopped=True)
                if aweme is None:
                    self._record_error(job_id, f"{keyword}: 无法解析一条搜索结果")
                    continue
                if job.platform in {"xhs", "rednote", "kuaishou", "shipinhao", "tiktok", "bilibili", "instagram"} and not \
                        _collection_content_matches(aweme, job):
                    continue

                current = self._job(job_id)
                author_id = _author_id(detail_raw, job.platform)
                if job.platform in {"xhs", "rednote"} and not author_id:
                    author_id = brief.get("author_id") or ""
                if (job.platform == "shipinhao" and not author_id
                        and job.source_type == "account_works"):
                    author_id = str(getattr(account, "sec_uid", "") or "")
                content = self._upsert_content(
                    current, keyword, aweme, author_id,
                    token, source, detail_error)
                accepted += 1
                if detail_error:
                    self._record_error(
                        job_id, f"{keyword}/{aweme.aweme_id}: {detail_error}")
                self._refresh_counts(job_id)

                if self._cancel_requested(job_id):
                    return {"canceled": True, "errors": self._job(job_id).error_count}
                if job.download_media:
                    self._progress(job_id, keyword=keyword, step=f"下载作品 {index}")
                    await self._download(
                        current, content, aweme, proxy,
                        account=account, identity=identity)

                if self._cancel_requested(job_id):
                    return {"canceled": True, "errors": self._job(job_id).error_count}
                should_collect_comments = (
                    job.max_comments_per_content > 0
                    and (
                        job.platform != "shipinhao"
                        or aweme.comment_count > 0
                        or channels_client is not None
                    )
                )
                if should_collect_comments:
                    self._progress(job_id, keyword=keyword, step=f"抓取评论 {index}")
                    if job.platform == "douyin":
                        comments, comment_error = await self._douyin_comments(
                            account, douyin_client, aweme,
                            job.max_comments_per_content, job.include_replies,
                            context=context)
                    elif job.platform == "kuaishou":
                        comments, comment_error = await self._ks_comments(
                            identity, aweme.aweme_id,
                            job.max_comments_per_content, job.include_replies)
                    elif job.platform == "shipinhao":
                        comments, comment_error = await self._channels_comments(
                            identity, aweme.aweme_id,
                            job.max_comments_per_content, job.include_replies,
                            object_nonce_id=token,
                            proto_client=channels_client,
                            source_type=job.source_type)
                        if (not comments and not comment_error
                                and aweme.comment_count > 0
                                and channels_client is not None):
                            detail_obj, _ = await channels_client.fetch_feed_detail(
                                object_id=aweme.aweme_id,
                                object_nonce_id=token,
                            )
                            detail_nonce = str(
                                (detail_obj or {}).get("objectNonceId")
                                or (detail_obj or {}).get("object_nonce_id")
                                or ""
                            ).strip()
                            if detail_nonce and detail_nonce != token:
                                comments, comment_error = await self._channels_comments(
                                    identity, aweme.aweme_id,
                                    job.max_comments_per_content, job.include_replies,
                                    object_nonce_id=detail_nonce,
                                    proto_client=channels_client,
                                    source_type=job.source_type)
                    elif job.platform == "tiktok":
                        comments, comment_error = await self._tiktok_comments(
                            identity, aweme.aweme_id,
                            job.max_comments_per_content, job.include_replies,
                            author_unique_id=aweme.author_unique_id,
                            target_url=aweme.source_url, account=account)
                    elif job.platform == "bilibili":
                        comments, comment_error = await self._bilibili_comments(
                            identity, aweme.aweme_id,
                            job.max_comments_per_content, job.include_replies,
                            bili_client=bili_client)
                    elif job.platform == "rednote":
                        comments, comment_error = await self._rednote_comments(
                            identity, aweme.aweme_id, token, source,
                            job.max_comments_per_content, job.include_replies,
                            rednote_client=rednote_client)
                    elif job.platform == "instagram":
                        comments, comment_error = await self._instagram_comments(
                            identity, aweme.aweme_id,
                            job.max_comments_per_content, job.include_replies,
                            ig_client=ig_client)
                    elif browser_reads:
                        comments, comment_error = await self._xhs_comments_browser(
                            identity, aweme.aweme_id, token, source,
                            job.max_comments_per_content, job.include_replies)
                    else:
                        comments, comment_error = await self._xhs_comments(
                            xhs_client, aweme.aweme_id, token, source,
                            job.max_comments_per_content, job.include_replies)
                    self._persist_comments(
                        job_id, content.id, job.platform, aweme.aweme_id, comments)
                    if job.platform == "shipinhao":
                        audit_counts = self.__dict__.get("_last_channels_counts", {}).pop(
                            aweme.aweme_id, {})
                        self._backfill_channels_counts(
                            content,
                            like_count=_xhs_count(audit_counts.get("likeCount")),
                            comment_count=max(
                                _xhs_count(audit_counts.get("commentCount")),
                                len(comments),
                            ),
                        )
                    if comment_error:
                        self._record_error(
                            job_id, f"{keyword}/{aweme.aweme_id}: {comment_error}")
                        if job.platform in {"xhs", "rednote", "kuaishou", "shipinhao", "tiktok", "bilibili", "instagram"} and classify_platform_error(
                                comment_error)[0] in {
                                RiskCategory.RISK, RiskCategory.AUTH,
                                RiskCategory.NETWORK}:
                            return self._result(job_id, stopped=True)
                self._refresh_counts(job_id)
                if job.platform not in {"xhs", "rednote"}:
                    await asyncio.sleep(0.4)
                if accepted >= job.max_contents_per_keyword:
                    break

            self._progress(
                job_id,
                processed_keyword_count=keyword_index + 1,
                current_item_index=0,
                current_item_total=0,
            )

        return self._result(job_id)
