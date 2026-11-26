"""十一平台关键词与创作者批量作品采集主调度器。"""
from __future__ import annotations

import asyncio
import logging
import random
import time

from sqlalchemy import func
from sqlmodel import select

from app.db import get_session
from app.engine.collection_helpers import _author_id, _collection_content_matches, _loads_keywords, _sort_ks_search_items, _xhs_count
from app.models import KeywordCollectionComment, KeywordCollectionContent, KeywordCollectionJob
from app.progress_hub import progress_hub
from app.platforms.bilibili import BilibiliClient, parse_bili_aweme
from app.platforms.channels import ChannelsProtocolClient, parse_channels_feed
from app.platforms.douyin import DouyinClient, cookie_from_state as douyin_cookie_from_state, parse_aweme
from app.platforms.douyin.extract import Aweme
from app.platforms.facebook import FacebookClient, parse_facebook_aweme
from app.platforms.youtube import YouTubeClient, parse_youtube_aweme
from app.platforms.instagram import InstagramClient, parse_ig_aweme
from app.platforms.kuaishou import parse_ks_feed
from app.platforms.rednote import RednoteApiClient, cookie_str_from_state as rednote_cookie_str_from_state, has_a1 as rednote_has_a1
from app.platforms.tiktok import parse_tiktok_aweme
from app.platforms.twitter import TwitterClient, parse_twitter_aweme
from app.platforms.xhs import XhsApiClient, cookie_str_from_state, has_a1
from app.profiles import pick_ephemeral_proxy
from app.risk import RiskCategory, classify_platform_error
from app.transport_matrix import douyin_client_environment
from app.engine.collection_comments import CollectionCommentsMixin
from app.engine.collection_discover import CollectionDiscoverMixin
from app.engine.collection_social import CollectionSocialMixin

log = logging.getLogger("creatorhub.collection")


class KeywordCollector(
    CollectionDiscoverMixin,
    CollectionCommentsMixin,
    CollectionSocialMixin,
):
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

    def _direct_request_ua(self, identity_or_account) -> str:
        if identity_or_account is None:
            return self.cfg.engine.user_agent
        target = identity_or_account
        if not hasattr(target, "key"):
            builder = getattr(self.browser, "identity_for", None)
            if callable(builder):
                try:
                    target = builder(target)
                except Exception:
                    pass
        resolver = getattr(self.browser, "direct_request_user_agent", None)
        if callable(resolver):
            try:
                return resolver(target)
            except Exception:
                pass
        return str(getattr(target, "ua", "") or self.cfg.engine.user_agent)

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

    _JOB_RUNTIME: dict[int, dict] = {}

    @classmethod
    def _emit_hub_progress(cls, job: KeywordCollectionJob) -> None:
        if not job or job.cancel_requested or job.status == "canceled":
            return
        rt = cls._JOB_RUNTIME.get(int(job.id or 0)) or {}
        kw_total = max(1, int(rt.get("total_keywords") or 0) or len(_loads_keywords(job.keywords)) or 1)
        kw_done = min(kw_total, max(0, int(job.processed_keyword_count or 0)))
        it_total = max(0, int(job.current_item_total or 0))
        it_idx = min(it_total, max(0, int(job.current_item_index or 0)))
        dl_total = int(rt.get("dl_total") or 0)
        dl_done = int(rt.get("dl_done") or 0)
        dl_active = int(rt.get("dl_active") or 0)
        has_dl = bool(job.download_media)

        determinate = False
        pct: float | None = None
        if kw_done >= kw_total:
            determinate = True
            pct = 99.5 if dl_active > 0 else 100.0
        elif it_total > 0:
            determinate = True
            if has_dl and dl_total > 0:
                item_part = 0.15 + 0.65 * (it_idx / it_total)
                dl_part = 0.20 * min(1.0, dl_done / max(1, dl_total))
                frac = min(0.99, item_part + dl_part)
            elif has_dl:
                frac = 0.15 + 0.65 * (it_idx / it_total)
            else:
                frac = 0.15 + 0.85 * (it_idx / it_total)
            pct = round(min(99.5, (kw_done + frac) * 100.0 / kw_total), 1)
        elif kw_done > 0:
            determinate = True
            pct = round(min(99.0, (kw_done + 0.05) * 100.0 / kw_total), 1)

        eta_seconds: float | None = None
        started_mono = float(rt.get("started_at") or 0.0)
        if determinate and pct is not None and pct >= 5.0 and started_mono > 0:
            elapsed = max(0.0, time.monotonic() - started_mono)
            if elapsed >= 2.0 and pct < 99.5:
                eta_seconds = round(elapsed * (100.0 - pct) / pct, 1)

        title = str(rt.get("title") or "")
        if not title:
            kws = _loads_keywords(job.keywords) if job.source_type == "keyword" else []
            title = ("、".join(kws[:3]) + (f" 等 {len(kws)} 词" if len(kws) > 3 else "")) if kws else (job.source_name or job.source_type or f"采集任务 #{job.id}")

        detail_parts = []
        if job.current_keyword:
            detail_parts.append(f"当前：{job.current_keyword}")
        detail_parts.append(f"已采 {int(job.content_count or 0)} 作品 / {int(job.comment_count or 0)} 评论")
        if has_dl and dl_total > 0:
            detail_parts.append(f"媒体 {dl_done}/{dl_total}")

        progress_hub.upsert(
            f"collection:{job.id}",
            category="collection",
            title=title[:120],
            platform=str(job.platform or ""),
            account_id=job.account_id,
            source_tab="collections",
            status="running",
            phase="downloading" if (it_total > 0 and it_idx >= it_total and dl_active > 0) else ("processing" if it_total > 0 else "discovering"),
            phase_label=str(job.current_step or "采集中"),
            detail=" · ".join(detail_parts),
            determinate=determinate,
            percent=pct,
            current=it_idx if it_total else kw_done,
            total=it_total if it_total else kw_total,
            unit="items",
            eta_seconds=eta_seconds,
            cancelable=True,
            metrics={
                "content_count": int(job.content_count or 0),
                "comment_count": int(job.comment_count or 0),
                "error_count": int(job.error_count or 0),
                "processed_keyword_count": kw_done,
                "total_keyword_count": kw_total,
                "current_item_index": it_idx,
                "current_item_total": it_total,
                "dl_done": dl_done,
                "dl_total": dl_total,
                "dl_active": dl_active,
            },
            extra={"job_id": job.id, "current_keyword": job.current_keyword or ""},
        )

    @staticmethod
    def _progress(job_id: int, *, keyword: str | None = None,
                  step: str | None = None,
                  processed_keyword_count: int | None = None,
                  current_item_index: int | None = None,
                  current_item_total: int | None = None) -> None:
        job_snap = None
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
            session.refresh(job)
            job_snap = job
        if job_snap is not None:
            KeywordCollector._emit_hub_progress(job_snap)

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
        from app.models import DouyinAccount
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

        if account is not None:
            identity = self.browser.identity_for(account)
            proxy = account.proxy or ""
        else:
            with get_session() as s:
                proxy = pick_ephemeral_proxy(s, self.cfg, platform=job.platform)
            try:
                identity = self.browser.anon_identity(proxy=proxy, platform=job.platform)
            except TypeError:
                identity = self.browser.anon_identity()
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
                cookie,
                self._direct_request_ua(identity),
                timeout=self.cfg.engine.request_timeout_seconds,
                proxy=proxy,
                storage_state=account.storage_state,
            )

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
                self._direct_request_ua(identity if identity is not None else account),
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
                self._direct_request_ua(identity if identity is not None else account),
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
        if job.platform == "twitter":
            tw_state = (account.storage_state or account.cookie) if account is not None else ""
            tw_client = TwitterClient(
                tw_state,
                self._direct_request_ua(identity if identity is not None else account),
                proxy=proxy,
                timeout=self.cfg.engine.request_timeout_seconds,
            )
            self._progress(job_id, step="启动 X (Twitter) 采集任务")
            async with tw_client.session_scope():
                return await self._run_keywords(
                    job_id, job, account, keywords, proxy,
                    identity=identity, tw_client=tw_client)
        if job.platform == "facebook":
            fb_state = (
                (account.storage_state or account.creator_storage_state or account.cookie)
                if account is not None
                else ""
            )
            fb_client = FacebookClient(
                fb_state,
                self._direct_request_ua(identity if identity is not None else account),
                proxy=proxy,
                timeout=self.cfg.engine.request_timeout_seconds,
            )
            fb_mode = str(getattr(self.cfg.engine, "facebook_read_mode", "hybrid") or "hybrid").lower()
            step_label = (
                "启动 Facebook 纯协议采集" if fb_mode == "api"
                else "启动 Facebook 浏览器采集" if fb_mode == "browser"
                else "启动 Facebook 协议优先采集"
            )
            self._progress(job_id, step=step_label)
            async with fb_client.session_scope():
                return await self._run_keywords(
                    job_id, job, account, keywords, proxy,
                    identity=identity, fb_client=fb_client)
        if job.platform == "youtube":
            yt_state = (
                (account.storage_state or account.creator_storage_state or account.cookie)
                if account is not None
                else ""
            )
            yt_client = YouTubeClient(
                yt_state,
                self._direct_request_ua(identity if identity is not None else account),
                proxy=proxy,
                timeout=self.cfg.engine.request_timeout_seconds,
            )
            yt_mode = str(getattr(self.cfg.engine, "youtube_read_mode", "hybrid") or "hybrid").lower()
            step_label = (
                "启动 YouTube 纯协议采集" if yt_mode == "api"
                else "启动 YouTube 浏览器采集" if yt_mode == "browser"
                else "启动 YouTube 协议优先采集"
            )
            self._progress(job_id, step=step_label)
            async with yt_client.session_scope():
                return await self._run_keywords(
                    job_id, job, account, keywords, proxy,
                    identity=identity, yt_client=yt_client)
        if xhs_client is not None:
            async with xhs_client.session_scope():
                return await self._run_keywords(
                    job_id, job, account, keywords, proxy,
                    xhs_client=xhs_client, identity=identity)
        return await self._run_keywords(
            job_id, job, account, keywords, proxy,
            xhs_client=xhs_client, identity=identity)

    async def _run_keywords(self, job_id: int, job: KeywordCollectionJob,
                            account, keywords: list[str], proxy: str,
                             douyin_client: DouyinClient | None = None,
                             xhs_client: XhsApiClient | None = None,
                             rednote_client: RednoteApiClient | None = None,
                             channels_client: ChannelsProtocolClient | None = None,
                             bili_client: BilibiliClient | None = None,
                             ig_client: InstagramClient | None = None,
                             tw_client: TwitterClient | None = None,
                             fb_client: FacebookClient | None = None,
                             yt_client: YouTubeClient | None = None,
                             context=None, identity=None) -> dict:

        browser_reads = self._xhs_browser_reads_enabled()
        title_label = (
            ("、".join(keywords[:3]) + (f" 等 {len(keywords)} 词" if len(keywords) > 3 else ""))
            if job.source_type == "keyword"
            else (job.source_name or keywords[0] or f"采集任务 #{job_id}")
        )
        rt = {
            "started_at": time.monotonic(),
            "total_keywords": max(1, len(keywords)),
            "title": title_label,
            "dl_total": 0,
            "dl_done": 0,
            "dl_active": 0,
        }
        self._JOB_RUNTIME[int(job_id)] = rt
        dl_concurrency = max(1, min(3, int(getattr(self.cfg.engine, "worker_pool_size", 2) or 2)))
        dl_sem = asyncio.Semaphore(dl_concurrency)
        pending_downloads: list[asyncio.Task] = []

        async def _bg_download(cur_job, cur_content, cur_aweme, cur_proxy, cur_acc, cur_ident):
            from app.pacing import download_stagger
            rt["dl_active"] = int(rt.get("dl_active", 0)) + 1
            try:
                await download_stagger(0.18, 0.7)
                async with dl_sem:
                    if self._cancel_requested(job_id):
                        return
                    await self._download(
                        cur_job, cur_content, cur_aweme, cur_proxy,
                        account=cur_acc, identity=cur_ident,
                    )
            finally:
                rt["dl_active"] = max(0, int(rt.get("dl_active", 0)) - 1)
                rt["dl_done"] = int(rt.get("dl_done", 0)) + 1
                snap = self._job(job_id)
                if snap is not None and snap.status == "running":
                    self._emit_hub_progress(snap)

        async def _drain_downloads(*, cancel: bool = False, keyword_label: str = "") -> None:
            alive = [t for t in pending_downloads if not t.done()]
            pending_downloads.clear()
            if not alive:
                return
            if cancel:
                for t in alive:
                    t.cancel()
                await asyncio.gather(*alive, return_exceptions=True)
                return
            self._progress(
                job_id,
                keyword=keyword_label or None,
                step=f"等待后台媒体下载完成 ({rt['dl_done']}/{rt['dl_total']})",
            )
            await asyncio.gather(*alive, return_exceptions=True)

        self._progress(
            job_id,
            processed_keyword_count=0,
            current_item_index=0,
            current_item_total=0,
        )
        try:
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
            elif job.platform in {"instagram", "twitter", "facebook", "youtube"} and keyword_index:
                await asyncio.sleep(1.2 * random.uniform(1.0, 1.35))
            self._progress(
                job_id,
                keyword=keyword,
                step=(
                    "同步本账号作品"
                    if (job.platform in {"shipinhao", "bilibili", "instagram", "twitter", "facebook"} and job.source_type == "account_works")
                    else "读取创作者作品"
                    if (job.platform in {"shipinhao", "bilibili", "instagram", "twitter", "facebook"} and job.source_type == "creator")
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
            elif job.platform == "twitter":
                if job.source_type == "creator":
                    raw_items, search_error = await self._discover_twitter_creator(
                        job, tw_client=tw_client, identity=identity)
                elif job.source_type == "account_works":
                    raw_items, search_error = await self._discover_twitter_account(
                        account, job, tw_client=tw_client, identity=identity)
                else:
                    raw_items, search_error = await self._discover_twitter_keyword(
                        keyword, job, tw_client=tw_client, identity=identity)
            elif job.platform == "facebook":
                if job.source_type == "creator":
                    raw_items, search_error = await self._discover_facebook_creator(
                        job, fb_client=fb_client, identity=identity)
                elif job.source_type == "account_works":
                    raw_items, search_error = await self._discover_facebook_account(
                        account, job, fb_client=fb_client, identity=identity)
                else:
                    raw_items, search_error = await self._discover_facebook_keyword(
                        keyword, job, fb_client=fb_client, identity=identity)
            elif job.platform == "youtube":
                if job.source_type == "creator":
                    raw_items, search_error = await self._discover_youtube_creator(
                        job, yt_client=yt_client, identity=identity)
                elif job.source_type == "account_works":
                    raw_items, search_error = await self._discover_youtube_account(
                        account, job, yt_client=yt_client, identity=identity)
                else:
                    raw_items, search_error = await self._discover_youtube_keyword(
                        keyword, job, yt_client=yt_client, identity=identity)
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
                elif job.platform == "twitter":
                    aweme = parse_twitter_aweme(raw)
                    if (
                        aweme is not None
                        and job.download_media
                        and not aweme.medias
                        and tw_client is not None
                        and not isinstance(raw.get("legacy"), dict)
                    ):
                        full_aw, full_raw, _info_err = await tw_client.fetch_tweet_syndication(
                            aweme.source_url or aweme.aweme_id
                        )
                        if full_aw is not None and full_aw.medias:
                            aweme = full_aw
                            detail_raw = full_raw
                    token = ""
                elif job.platform == "facebook":
                    aweme = parse_facebook_aweme(raw)
                    if (
                        aweme is not None
                        and job.download_media
                        and not aweme.medias
                        and fb_client is not None
                        and aweme.source_url
                    ):
                        full_aw, _info_err = await fb_client.fetch_post_detail(aweme.source_url)
                        if full_aw is not None and full_aw.medias:
                            full_aw.like_count = max(full_aw.like_count, aweme.like_count)
                            full_aw.comment_count = max(full_aw.comment_count, aweme.comment_count)
                            if not full_aw.author_name:
                                full_aw.author_name = aweme.author_name
                            if not full_aw.desc:
                                full_aw.desc = aweme.desc
                            aweme = full_aw
                    token = ""
                elif job.platform == "youtube":
                    aweme = parse_youtube_aweme(raw, quality=job.video_quality or "highest")
                    if aweme is not None:
                        if yt_client is not None and (aweme.like_count == 0 or aweme.comment_count == 0):
                            try:
                                full_aw, yt_det, _yt_err = await yt_client.fetch_video_detail(
                                    aweme.source_url or aweme.aweme_id,
                                    quality=job.video_quality or "highest",
                                )
                                if full_aw is not None:
                                    full_aw.like_count = max(full_aw.like_count, aweme.like_count)
                                    full_aw.comment_count = max(full_aw.comment_count, aweme.comment_count)
                                    if not full_aw.duration and aweme.duration:
                                        full_aw.duration = aweme.duration
                                    if not full_aw.create_time and aweme.create_time:
                                        full_aw.create_time = aweme.create_time
                                    if not full_aw.author_name:
                                        full_aw.author_name = aweme.author_name
                                    if not full_aw.author_unique_id:
                                        full_aw.author_unique_id = aweme.author_unique_id
                                    if not full_aw.avatar:
                                        full_aw.avatar = aweme.avatar
                                    if not full_aw.cover:
                                        full_aw.cover = aweme.cover
                                    if not full_aw.desc:
                                        full_aw.desc = aweme.desc
                                    aweme = full_aw
                                    if isinstance(yt_det, dict) and yt_det:
                                        detail_raw = {**raw, **yt_det}
                            except Exception as exc:
                                log.debug("youtube detail enrich failed(%s): %s", aweme.aweme_id, exc)
                        aweme.quality_label = job.video_quality or aweme.quality_label
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
                        await _drain_downloads(cancel=False, keyword_label=keyword)
                        return self._result(job_id, stopped=True)
                if aweme is None:
                    self._record_error(job_id, f"{keyword}: 无法解析一条搜索结果")
                    continue
                if job.platform in {"xhs", "rednote", "kuaishou", "shipinhao", "tiktok", "bilibili", "instagram", "twitter", "facebook", "youtube"} and not \
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
                if job.download_media and aweme.medias and content.download_status != "done":
                    rt["dl_total"] = int(rt.get("dl_total", 0)) + 1
                    pending_downloads.append(asyncio.create_task(
                        _bg_download(current, content, aweme, proxy, account, identity)
                    ))
                    self._progress(
                        job_id,
                        keyword=keyword,
                        step=f"后台下载媒体 {index}/{len(candidates)}（并行 {rt['dl_done']}/{rt['dl_total']}）",
                    )

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
                    elif job.platform == "twitter":
                        comments, comment_error = await self._twitter_comments(
                            identity, aweme.aweme_id,
                            job.max_comments_per_content, job.include_replies,
                            tw_client=tw_client, target_url=aweme.source_url)
                    elif job.platform == "facebook":
                        comments, comment_error = await self._facebook_comments(
                            identity, aweme.aweme_id,
                            job.max_comments_per_content, job.include_replies,
                            fb_client=fb_client, target_url=aweme.source_url)
                    elif job.platform == "youtube":
                        comments, comment_error = await self._youtube_comments(
                            identity, aweme.aweme_id,
                            job.max_comments_per_content, job.include_replies,
                            yt_client=yt_client, target_url=aweme.source_url)
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
                        if job.platform in {"xhs", "rednote", "kuaishou", "shipinhao", "tiktok", "bilibili", "instagram", "twitter", "facebook", "youtube"} and classify_platform_error(
                                comment_error)[0] in {
                                RiskCategory.RISK, RiskCategory.AUTH,
                                RiskCategory.NETWORK}:
                            await _drain_downloads(cancel=False, keyword_label=keyword)
                            return self._result(job_id, stopped=True)
                self._refresh_counts(job_id)
                if job.platform not in {"xhs", "rednote"}:
                    from app.pacing import jittered_sleep
                    await jittered_sleep(0.55, ratio=0.4, min_delay=0.2)
                if accepted >= job.max_contents_per_keyword:
                    break

            await _drain_downloads(cancel=self._cancel_requested(job_id), keyword_label=keyword)
            self._progress(
                job_id,
                processed_keyword_count=keyword_index + 1,
                current_item_index=0,
                current_item_total=0,
            )

          await _drain_downloads(cancel=self._cancel_requested(job_id))
          return self._result(job_id)
        except asyncio.CancelledError:
            await _drain_downloads(cancel=True)
            raise
        finally:
            await _drain_downloads(cancel=self._cancel_requested(job_id))
            self._JOB_RUNTIME.pop(int(job_id), None)
