"""采集作品入库、媒体下载与各短视频/笔记平台评论抓取持久化。"""
from __future__ import annotations

from pathlib import Path
import math

from sqlmodel import select

from app.browser import fetch_channels_comments, fetch_comments, fetch_ks_comments, fetch_xhs_comments
from app.browser.tiktok_fetcher import fetch_tiktok_comments
from app.db import get_session
from app.engine.collection_helpers import _content_error_after_download, _media_json, _tiktok_download_session
from app.engine.monitor_helpers import log
from app.models import KeywordCollectionComment, KeywordCollectionContent, KeywordCollectionJob
from app.platforms.bilibili import cookie_map_from_state as bili_cookie_map
from app.platforms.channels import ChannelsProtocolClient, flatten_channels_comments, parse_channels_comment
from app.platforms.douyin import DouyinClient, parse_comment, safe_title
from app.platforms.douyin.extract import Aweme
from app.platforms.kuaishou import flatten_ks_comments, parse_ks_comment
from app.platforms.tiktok import parse_tiktok_comment
from app.platforms.tiktok.client import TikTokNativeClient
from app.platforms.xhs import XhsApiClient, flatten_comments as flatten_xhs_comments, parse_comment as parse_xhs_comment
from app.services import thumb_cache


class CollectionCommentsMixin:
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
            elif (job.download_media and not aweme.medias
                  and job.platform == "twitter" and not detail_error):
                row.download_status = "skipped"
                row.error = ""
            elif job.download_media and not aweme.medias:
                row.download_status = "failed"
                row.error = detail_error or "未取得媒体地址"
            elif not job.download_media:
                row.download_status = "skipped"
            session.add(row)
            session.commit()
            session.refresh(row)
        if aweme.cover and not (job.download_media and aweme.medias):
            thumb_cache.schedule_thumb_prewarm([aweme.cover], platform=job.platform)
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
        elif job.platform == "twitter":
            if account is not None:
                download_ua = download_ua or str(getattr(account, "ua", "") or "")
            download_referer = "https://x.com/"
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
            if aweme.cover:
                thumb_cache.schedule_thumb_prewarm([aweme.cover], platform=job.platform, proxy=routed_proxy)
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
            log.info(
                "TikTok 评论离线接口兼容失败，回退页面: %s (HTTP=%s, bytes=%s)",
                native.category, native.http_status, native.body_length,
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
