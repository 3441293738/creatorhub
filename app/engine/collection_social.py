"""哔哩哔哩、Instagram 与 X (Twitter) 采集发现与评论拉取。"""
from __future__ import annotations

from app.browser.bili_fetcher import fetch_bili_comments, fetch_bili_search, fetch_bili_videos
from app.browser.facebook_fetcher import fetch_facebook_comments, fetch_facebook_search, fetch_facebook_self_profile, fetch_facebook_user_posts
from app.browser.ig_fetcher import fetch_ig_comments, fetch_ig_posts, fetch_ig_search
from app.browser.twitter_fetcher import fetch_twitter_comments, fetch_twitter_search, fetch_twitter_self_profile, fetch_twitter_user_tweets
from app.browser.youtube_fetcher import fetch_youtube_channel_videos, fetch_youtube_comments, fetch_youtube_search, fetch_youtube_self_profile
from app.models import KeywordCollectionJob
from app.platforms.bilibili import BilibiliClient, resolve_bili_mid
from app.platforms.facebook import (
    FacebookClient,
    get_slot_operation,
    parse_facebook_aweme,
    post_url as fb_post_url,
    resolve_facebook_user_target,
)
from app.platforms.instagram import InstagramClient, resolve_ig_user_target
from app.platforms.twitter import TwitterClient, resolve_twitter_user_target
from app.platforms.youtube import YouTubeClient, resolve_youtube_channel_target, video_url as yt_video_url
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
        err_cat = classify_platform_error(err)[0] if err else RiskCategory.BUSINESS
        hard_block = err_cat in {RiskCategory.AUTH, RiskCategory.RISK}
        if (mode == "browser" or (mode == "hybrid" and not items and not hard_block)) and identity is not None:
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
            if p_err and classify_platform_error(p_err)[0] in {RiskCategory.AUTH, RiskCategory.RISK}:
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

    async def _discover_twitter_creator(
        self,
        job: KeywordCollectionJob,
        *,
        tw_client: TwitterClient | None = None,
        identity=None,
    ) -> tuple[list[dict], str]:
        user_ref = resolve_twitter_user_target(job.source_ref) or str(job.source_ref or "").strip()
        if not user_ref:
            return [], "无法解析 X (Twitter) 创作者 @username 或链接"
        mode = str(getattr(self.cfg.engine, "twitter_read_mode", "hybrid") or "hybrid").lower()
        max_pages = max(1, min(12, int(job.max_pages_per_keyword or 4)))
        items: list[dict] = []
        err = ""
        if mode in {"api", "hybrid"} and tw_client is not None:
            items, _uinfo, err = await tw_client.fetch_user_tweets(
                user_ref, max_pages=max_pages
            )
            if items:
                return items, ""
            if err.startswith(("logged_out:", "verification_required:", "rate_limit:")):
                return [], err
        if (mode == "browser" or (mode == "hybrid" and not items)) and identity is not None:
            b_items, _author, b_err = await fetch_twitter_user_tweets(
                self.browser,
                identity,
                user_ref,
                set(),
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

    async def _discover_twitter_account(
        self,
        account,
        job: KeywordCollectionJob,
        *,
        tw_client: TwitterClient | None,
        identity=None,
    ) -> tuple[list[dict], str]:
        user_ref = str(
            getattr(account, "douyin_id", "")
            or getattr(account, "sec_uid", "")
            or getattr(account, "uid", "")
            or ""
        ).strip()
        if not user_ref and tw_client is not None:
            prof, p_err = await tw_client.fetch_self_profile()
            if p_err.startswith(("logged_out:", "verification_required:")):
                return [], p_err
            user_ref = str(prof.get("unique_id") or prof.get("sec_uid") or prof.get("uid") or "")
        if not user_ref and identity is not None:
            prof, p_err = await fetch_twitter_self_profile(self.browser, identity)
            if p_err.startswith(("logged_out:", "verification_required:")):
                return [], p_err
            user_ref = str(prof.get("unique_id") or prof.get("sec_uid") or prof.get("uid") or "")
        if not user_ref:
            return [], "未获取到当前 X (Twitter) 账号标识，请先在账号页刷新资料"
        clone_job = KeywordCollectionJob(
            platform="twitter",
            account_id=getattr(account, "id", 0) or 0,
            source_type="creator",
            source_ref=user_ref,
            max_contents_per_keyword=job.max_contents_per_keyword,
            max_pages_per_keyword=job.max_pages_per_keyword,
        )
        return await self._discover_twitter_creator(clone_job, tw_client=tw_client, identity=identity)

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

    async def _discover_facebook_creator(
        self,
        job: KeywordCollectionJob,
        *,
        fb_client: FacebookClient | None = None,
        identity=None,
    ) -> tuple[list[dict], str]:
        user_ref = resolve_facebook_user_target(job.source_ref) or str(job.source_ref or "").strip()
        if not user_ref:
            return [], "无法解析 Facebook 个人主页、公共主页或群组标识"
        mode = str(getattr(self.cfg.engine, "facebook_read_mode", "hybrid") or "hybrid").lower()
        max_pages = max(1, min(12, int(job.max_pages_per_keyword or 4)))
        target_items = max(10, int(job.max_contents_per_keyword or 20) * 2)
        slot = "group_posts" if "/groups/" in user_ref.lower() else "profile_posts"
        items: list[dict] = []
        err = ""
        if mode in {"api", "hybrid"} and fb_client is not None and fb_client.has_login_cookies:
            items, _author, err = await fb_client.fetch_user_posts(
                user_ref,
                max_pages=max_pages,
                max_items=target_items,
            )
            if err.startswith(("logged_out:", "verification_required:", "rate_limit:")):
                return items, err
        need_fallback = (
            mode == "browser"
            or (
                mode == "hybrid"
                and (
                    not items
                    or (
                        max_pages > 1
                        and len(items) < min(target_items, 8)
                        and get_slot_operation(slot) is None
                    )
                )
            )
        )
        if need_fallback:
            if identity is None:
                if items:
                    return items, ""
                return [], err or "缺少 Facebook 账号浏览器隔离身份"
            b_items, _author, b_err = await fetch_facebook_user_posts(
                self.browser,
                identity,
                user_ref,
                set(),
                max_scrolls=max_pages,
                max_items=target_items,
            )
            if b_items:
                items = self._merge_facebook_posts(b_items, items) if items else b_items
                err = ""
            elif mode == "browser" or not items:
                err = b_err or err
        if err in {"empty", "api_empty_timeline"}:
            err = "" if items else err
        return items, err

    async def _discover_facebook_account(
        self,
        account,
        job: KeywordCollectionJob,
        *,
        fb_client: FacebookClient | None,
        identity=None,
    ) -> tuple[list[dict], str]:
        user_ref = str(
            getattr(account, "douyin_id", "")
            or getattr(account, "sec_uid", "")
            or getattr(account, "uid", "")
            or ""
        ).strip()
        if not user_ref and fb_client is not None:
            prof, p_err = await fb_client.fetch_self_profile()
            if p_err.startswith(("logged_out:", "verification_required:")):
                return [], p_err
            if prof:
                user_ref = str(prof.get("unique_id") or prof.get("sec_uid") or prof.get("uid") or "")
        if not user_ref and identity is not None:
            prof, p_err = await fetch_facebook_self_profile(self.browser, identity)
            if p_err.startswith(("logged_out:", "verification_required:")):
                return [], p_err
            user_ref = str(prof.get("unique_id") or prof.get("sec_uid") or prof.get("uid") or "")
        if not user_ref:
            return [], "未获取到当前 Facebook 账号标识，请先在账号页刷新资料"
        clone_job = KeywordCollectionJob(
            platform="facebook",
            account_id=getattr(account, "id", 0) or 0,
            source_type="creator",
            source_ref=user_ref,
            max_contents_per_keyword=job.max_contents_per_keyword,
            max_pages_per_keyword=job.max_pages_per_keyword,
        )
        return await self._discover_facebook_creator(clone_job, fb_client=fb_client, identity=identity)

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

    async def _discover_youtube_creator(
        self,
        job: KeywordCollectionJob,
        *,
        yt_client: YouTubeClient | None = None,
        identity=None,
    ) -> tuple[list[dict], str]:
        key, _url = resolve_youtube_channel_target(job.source_ref)
        ch_ref = key or str(job.source_ref or "").strip()
        if not ch_ref:
            return [], "无法解析 YouTube 频道标识"
        mode = str(getattr(self.cfg.engine, "youtube_read_mode", "hybrid") or "hybrid").lower()
        max_pages = max(1, min(12, int(job.max_pages_per_keyword or 4)))
        target_items = max(10, int(job.max_contents_per_keyword or 20) * 2)
        items: list[dict] = []
        err = ""
        if mode in {"api", "hybrid"} and yt_client is not None:
            items, _prof, err = await yt_client.fetch_channel_videos(
                ch_ref,
                tab="videos",
                max_items=target_items,
                max_pages=max_pages,
                include_shorts=True,
            )
            if err.startswith(("verification_required:", "rate_limit:")) or (mode == "api" and err.startswith("logged_out:")):
                return items, err
        if (mode == "browser" or (mode == "hybrid" and not items)) and identity is not None:
            b_items, _prof, b_err = await fetch_youtube_channel_videos(
                self.browser,
                identity,
                ch_ref,
                tab="videos",
                max_scrolls=max_pages,
                limit=target_items,
                include_shorts=True,
                block_media=self.cfg.engine.block_media_resources,
            )
            if b_items:
                items = b_items
                err = ""
            elif mode == "browser" or not items:
                err = b_err or err
        return items, ("" if items or err == "empty" else err)

    async def _discover_youtube_account(
        self,
        account,
        job: KeywordCollectionJob,
        *,
        yt_client: YouTubeClient | None = None,
        identity=None,
    ) -> tuple[list[dict], str]:
        def _clean_ref(v: Any) -> str:
            s = str(v or "").strip()
            if not s or ("@" in s and not s.startswith("@")):
                return ""
            return s

        ch_ref = (
            _clean_ref(getattr(account, "douyin_id", ""))
            or _clean_ref(getattr(account, "sec_uid", ""))
            or _clean_ref(getattr(account, "uid", ""))
        )
        if not ch_ref and yt_client is not None:
            prof, p_err = await yt_client.fetch_self_profile()
            if p_err.startswith("verification_required:") or (p_err.startswith("logged_out:") and identity is None):
                return [], p_err
            if prof:
                ch_ref = _clean_ref(prof.get("unique_id")) or _clean_ref(prof.get("sec_uid")) or _clean_ref(prof.get("uid"))
        if not ch_ref and identity is not None:
            prof, p_err = await fetch_youtube_self_profile(self.browser, identity)
            if p_err.startswith(("logged_out:", "verification_required:")):
                return [], p_err
            if prof:
                ch_ref = _clean_ref(prof.get("unique_id")) or _clean_ref(prof.get("sec_uid")) or _clean_ref(prof.get("uid"))
        if not ch_ref:
            return [], "未获取到当前 YouTube 账号频道标识（若尚未创建公开频道，请先在 YouTube 创建频道后再采集）"
        clone_job = KeywordCollectionJob(
            platform="youtube",
            account_id=getattr(account, "id", 0) or 0,
            source_type="creator",
            source_ref=ch_ref,
            max_contents_per_keyword=job.max_contents_per_keyword,
            max_pages_per_keyword=job.max_pages_per_keyword,
        )
        return await self._discover_youtube_creator(clone_job, yt_client=yt_client, identity=identity)

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
