"""哔哩哔哩、Instagram、X (Twitter)、Facebook 与 YouTube 关键词采集发现与评论拉取。"""
from __future__ import annotations

from app.browser.bili_fetcher import fetch_bili_comments, fetch_bili_search
from app.browser.facebook_fetcher import fetch_facebook_comments, fetch_facebook_search
from app.browser.ig_fetcher import fetch_ig_comments, fetch_ig_search
from app.browser.twitter_fetcher import fetch_twitter_comments, fetch_twitter_search
from app.browser.youtube_fetcher import fetch_youtube_comments, fetch_youtube_search
from app.models import KeywordCollectionJob
from app.platforms.bilibili import BilibiliClient
from app.platforms.facebook import (
    FacebookClient,
    get_slot_operation,
    parse_facebook_aweme,
    post_url as fb_post_url,
)
from app.platforms.instagram import InstagramClient
from app.platforms.twitter import TwitterClient
from app.platforms.youtube import YouTubeClient, video_url as yt_video_url
from app.risk import RiskCategory, classify_platform_error


class CollectionSocialMixin:
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
        err_cat = classify_platform_error(err)[0] if err else RiskCategory.BUSINESS
        hard_block = err_cat in {RiskCategory.AUTH, RiskCategory.RISK}
        if (mode == "browser" or (mode == "hybrid" and not items and not hard_block)) and identity is not None:
            b_items, b_err = await fetch_ig_search(
                self.browser, identity, keyword, max_scrolls=max_pages
            )
            if b_items:
                items, err = b_items, ""
            elif mode == "browser":
                err = b_err
        return items, err

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
        err_cat = classify_platform_error(err)[0] if err else RiskCategory.BUSINESS
        hard_block = err_cat in {RiskCategory.AUTH, RiskCategory.RISK}
        if (mode == "browser" or (mode == "hybrid" and not comments and err and not hard_block)) and identity is not None:
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

    async def _discover_twitter_keyword(
        self,
        keyword: str,
        job: KeywordCollectionJob,
        *,
        tw_client: TwitterClient | None = None,
        identity=None,
    ) -> tuple[list[dict], str]:
        mode = str(getattr(self.cfg.engine, "twitter_read_mode", "hybrid") or "hybrid").lower()
        max_pages = max(1, min(10, int(job.max_pages_per_keyword or 3)))
        items: list[dict] = []
        err = ""
        if mode in {"api", "hybrid"} and tw_client is not None:
            items, err = await tw_client.search_posts(
                keyword,
                max_pages=max_pages,
                latest=(job.search_sort == "latest"),
                content_type=job.content_type or "all",
            )
            if items:
                return items, ""
            if err.startswith(("logged_out:", "verification_required:", "rate_limit:")):
                return [], err
        if (mode == "browser" or (mode == "hybrid" and not items)) and identity is not None:
            b_items, b_err = await fetch_twitter_search(
                self.browser,
                identity,
                keyword,
                latest=(job.search_sort == "latest"),
                content_type=job.content_type or "all",
                max_scrolls=max_pages,
                block_media=self.cfg.engine.block_media_resources,
            )
            if b_items:
                return b_items, ""
            if mode == "browser" or not err:
                err = b_err
        if err == "empty":
            err = ""
        return items, err

    async def _twitter_comments(
        self,
        identity,
        work_id: str,
        limit: int,
        include_replies: bool,
        *,
        tw_client: TwitterClient | None = None,
        target_url: str = "",
    ) -> tuple[list[dict], str]:
        if limit <= 0:
            return [], ""
        mode = str(getattr(self.cfg.engine, "twitter_read_mode", "hybrid") or "hybrid").lower()
        comments: list[dict] = []
        err = ""
        if mode in {"api", "hybrid"} and tw_client is not None:
            comments, err = await tw_client.fetch_comments(
                target_url or work_id,
                max_count=limit,
                include_replies=include_replies,
            )
        if (
            mode == "browser"
            or (mode == "hybrid" and not comments and err and not err.startswith(("logged_out:", "verification_required:", "rate_limit:")))
        ) and identity is not None:
            b_comments, b_err = await fetch_twitter_comments(
                self.browser,
                identity,
                target_url or work_id,
                max_count=limit,
                include_replies=include_replies,
                block_media=self.cfg.engine.block_media_resources,
            )
            if b_comments:
                comments, err = b_comments, ""
            elif mode == "browser":
                err = b_err
        if err == "empty":
            err = ""
        if not include_replies:
            comments = [c for c in comments if not c.get("reply_to")]
        return comments[:limit], err

    @staticmethod
    def _merge_facebook_posts(primary: list[dict], extra: list[dict]) -> list[dict]:
        """合并 API 与浏览器抓取到的 Facebook 帖子，按 `aweme_id` 去重并保留字段更完整的一条。"""
        by_id: dict[str, dict] = {}
        scores: dict[str, int] = {}
        for row in (*primary, *extra):
            aw = parse_facebook_aweme(row)
            if aw is None or not aw.aweme_id:
                continue
            sc = (
                (10 if aw.medias else 0)
                + (4 if aw.desc else 0)
                + (3 if aw.author_name else 0)
                + (3 if aw.create_time else 0)
                + (2 if aw.like_count else 0)
                + (2 if aw.comment_count else 0)
            )
            if aw.aweme_id not in by_id or sc > scores.get(aw.aweme_id, -1):
                by_id[aw.aweme_id] = row
                scores[aw.aweme_id] = sc
        return list(by_id.values())

    async def _discover_facebook_keyword(
        self,
        keyword: str,
        job: KeywordCollectionJob,
        *,
        fb_client: FacebookClient | None = None,
        identity=None,
    ) -> tuple[list[dict], str]:
        mode = str(getattr(self.cfg.engine, "facebook_read_mode", "hybrid") or "hybrid").lower()
        max_pages = max(1, min(12, int(job.max_pages_per_keyword or 4)))
        target_items = max(10, int(job.max_contents_per_keyword or 20) * 2)
        items: list[dict] = []
        err = ""
        if mode in {"api", "hybrid"} and fb_client is not None and fb_client.has_login_cookies:
            items, err = await fb_client.search_posts(
                keyword,
                max_pages=max_pages,
                max_items=target_items,
                content_type=job.content_type or "all",
                recent=(job.search_sort == "latest"),
            )
            if err.startswith(("logged_out:", "verification_required:", "rate_limit:")):
                return items, err
        # 冷启动尚未学到 search_posts 分页 doc_id 且首屏 SSR 条数不足时，hybrid 回退浏览器一次同时学习实时 doc_id
        need_fallback = (
            mode == "browser"
            or (
                mode == "hybrid"
                and (
                    not items
                    or (
                        max_pages > 1
                        and len(items) < min(target_items, 8)
                        and get_slot_operation("search_posts") is None
                    )
                )
            )
        )
        if need_fallback:
            if identity is None:
                if items:
                    return items, ""
                return [], err or "缺少 Facebook 账号浏览器隔离身份"
            b_items, b_err = await fetch_facebook_search(
                self.browser,
                identity,
                keyword,
                max_scrolls=max_pages,
                max_items=target_items,
                content_type=job.content_type or "all",
                recent=(job.search_sort == "latest"),
            )
            if b_items:
                items = self._merge_facebook_posts(b_items, items) if items else b_items
                err = ""
            elif mode == "browser" or not items:
                err = b_err or err
        if err in {"empty", "api_empty_search"}:
            err = "" if items else err
        return items, err

    async def _facebook_comments(
        self,
        identity,
        work_id: str,
        limit: int,
        include_replies: bool,
        *,
        fb_client: FacebookClient | None = None,
        target_url: str = "",
    ) -> tuple[list[dict], str]:
        if limit <= 0:
            return [], ""
        mode = str(getattr(self.cfg.engine, "facebook_read_mode", "hybrid") or "hybrid").lower()
        url = target_url or (work_id if str(work_id).startswith("http") else fb_post_url(work_id))
        max_pages = max(2, min(12, limit // 10 or 4))
        comments: list[dict] = []
        err = ""
        if mode in {"api", "hybrid"} and fb_client is not None and fb_client.has_login_cookies:
            comments, err = await fb_client.fetch_post_comments(
                url,
                max_pages=max_pages,
                max_count=limit,
                include_replies=include_replies,
            )
            if err.startswith(("logged_out:", "verification_required:", "rate_limit:")):
                if not include_replies:
                    comments = [c for c in comments if not c.get("reply_to")]
                return comments[:limit], err
        is_reel_or_video = any(p in url.lower() for p in ("/reel/", "/reels/", "/videos/", "/watch"))
        need_fallback = (
            mode == "browser"
            or (
                mode == "hybrid"
                and (
                    (not comments and bool(err))
                    or (
                        len(comments) < min(limit, 12)
                        and (
                            get_slot_operation("post_comments") is None
                            or (is_reel_or_video and get_slot_operation("reel_comments") is None)
                        )
                    )
                )
            )
        )
        if need_fallback and identity is not None:
            b_comments, b_err = await fetch_facebook_comments(
                self.browser,
                identity,
                url,
                max_scrolls=max(3, min(12, limit // 10 or 4)),
            )
            if b_comments:
                by_cid: dict[str, dict] = {}
                for c in (*b_comments, *comments):
                    cid = str(c.get("comment_id") or "")
                    if cid and cid not in by_cid:
                        by_cid[cid] = c
                comments = list(by_cid.values())
                err = ""
            elif mode == "browser":
                err = b_err
        if err in {"empty", "api_empty_comments"}:
            err = ""
        if not include_replies:
            comments = [c for c in comments if not c.get("reply_to")]
        return comments[:limit], err

    async def _discover_youtube_keyword(
        self,
        keyword: str,
        job: KeywordCollectionJob,
        *,
        yt_client: YouTubeClient | None = None,
        identity=None,
    ) -> tuple[list[dict], str]:
        mode = str(getattr(self.cfg.engine, "youtube_read_mode", "hybrid") or "hybrid").lower()
        max_pages = max(1, min(12, int(job.max_pages_per_keyword or 4)))
        target_items = max(10, int(job.max_contents_per_keyword or 20) * 2)
        upload_date = {"day": "today", "week": "week", "half_year": "year"}.get(str(job.publish_time or ""), "")
        sort_by = {"latest": "date", "most_liked": "views"}.get(str(job.search_sort or ""), "")
        items: list[dict] = []
        err = ""
        if mode in {"api", "hybrid"} and yt_client is not None:
            items, err = await yt_client.search_videos(
                keyword,
                max_items=target_items,
                max_pages=max_pages,
                upload_date=upload_date,
                sort_by=sort_by,
            )
            if err.startswith(("verification_required:", "rate_limit:")) or (mode == "api" and err.startswith("logged_out:")):
                return items, err
        if (mode == "browser" or (mode == "hybrid" and not items)) and identity is not None:
            b_items, b_err = await fetch_youtube_search(
                self.browser,
                identity,
                keyword,
                max_scrolls=max_pages,
                limit=target_items,
                upload_date=upload_date,
                sort_by=sort_by,
                block_media=self.cfg.engine.block_media_resources,
            )
            if b_items:
                items = b_items
                err = ""
            elif mode == "browser" or not items:
                err = b_err or err
        return items, ("" if items or err == "empty" else err)

    async def _youtube_comments(
        self,
        identity,
        work_id: str,
        limit: int,
        include_replies: bool,
        *,
        yt_client: YouTubeClient | None = None,
        target_url: str = "",
    ) -> tuple[list[dict], str]:
        if limit <= 0:
            return [], ""
        mode = str(getattr(self.cfg.engine, "youtube_read_mode", "hybrid") or "hybrid").lower()
        url = target_url or (work_id if str(work_id).startswith("http") else yt_video_url(work_id))
        max_pages = max(2, min(12, limit // 15 or 4))
        comments: list[dict] = []
        err = ""
        if mode in {"api", "hybrid"} and yt_client is not None:
            comments, err = await yt_client.fetch_video_comments(
                url,
                max_items=limit,
                max_pages=max_pages,
            )
            if err.startswith(("verification_required:", "rate_limit:")) or (mode == "api" and err.startswith("logged_out:")):
                if not include_replies:
                    comments = [c for c in comments if not c.get("reply_to")]
                return comments[:limit], err
        if (mode == "browser" or (mode == "hybrid" and not comments)) and identity is not None:
            b_comments, b_err = await fetch_youtube_comments(
                self.browser,
                identity,
                url,
                max_scrolls=max_pages,
                limit=limit,
                block_media=self.cfg.engine.block_media_resources,
            )
            if b_comments:
                comments = b_comments
                err = ""
            elif mode == "browser":
                err = b_err or err
        if err == "empty":
            err = ""
        if not include_replies:
            comments = [c for c in comments if not c.get("reply_to")]
        return comments[:limit], err
