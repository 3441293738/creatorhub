"""哔哩哔哩、Instagram 与 X (Twitter) 采集发现与评论拉取。"""
from __future__ import annotations

from app.browser.bili_fetcher import fetch_bili_comments, fetch_bili_search, fetch_bili_videos
from app.browser.ig_fetcher import fetch_ig_comments, fetch_ig_posts, fetch_ig_search
from app.browser.twitter_fetcher import fetch_twitter_comments, fetch_twitter_search, fetch_twitter_self_profile, fetch_twitter_user_tweets
from app.models import KeywordCollectionJob
from app.platforms.bilibili import BilibiliClient, resolve_bili_mid
from app.platforms.instagram import InstagramClient, resolve_ig_user_target
from app.platforms.twitter import TwitterClient, resolve_twitter_user_target


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
