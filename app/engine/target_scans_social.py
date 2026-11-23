"""哔哩哔哩、Instagram 与 X (Twitter) 作品监控扫描。"""
from __future__ import annotations

from datetime import datetime
import json

from sqlmodel import select

from app.browser import fetch_bili_search, fetch_bili_videos, fetch_ig_posts, fetch_ig_search, fetch_twitter_comments, fetch_twitter_search, fetch_twitter_user_tweets
from app.db import get_session
from app.engine.monitor_helpers import _douyin_scan_since, _monitor_content_matches, _monitor_strategy
from app.models import ContentRecord, DouyinAccount, MonitorTarget
from app.platforms.bilibili import BilibiliClient, cookie_map_from_state as bili_cookie_map, parse_bili_aweme
from app.platforms.douyin.extract import Aweme
from app.platforms.instagram import InstagramClient, parse_ig_aweme
from app.platforms.twitter import TwitterClient, parse_twitter_aweme
from app.risk import RiskCategory, classify_platform_error
from app.services import thumb_cache
from app.settings import get_setting


class SocialTargetScanMixin:
    async def _scan_bilibili_target_locked(self, target_id: int) -> dict:
        with get_session() as s:
            target = s.get(MonitorTarget, target_id)
            if not target:
                return {"ok": False, "error": "target not found"}
            first_scan = target.last_scan_at is None
            acc = s.get(DouyinAccount, target.account_id) if target.account_id else None
            if target.account_id and (not acc or acc.platform != "bilibili" or acc.status != "active"):
                return self._mark_target_skip(
                    target_id, "绑定的哔哩哔哩账号不存在或登录态已失效，请重新绑定/登录"
                )
            if acc and self._proxy_bad(acc):
                return self._mark_target_skip(
                    target_id, "账号代理标记为不可用(proxy bad)，已跳过以免暴露真实 IP"
                )
            if acc is not None:
                identity = (
                    self.browser.identity_for(acc)
                    if callable(getattr(self.browser, "identity_for", None))
                    else None
                )
                proxy = acc.proxy or ""
            else:
                identity, proxy = self._anon_identity_proxy("bilibili", session=s)
            state = (acc.storage_state or acc.cookie or "") if acc is not None else ""
            ua = (acc.ua if acc and acc.ua else self.cfg.engine.user_agent)
            known = set(
                s.exec(select(ContentRecord.aweme_id).where(ContentRecord.target_id == target_id)).all()
            )
            known_create_times = list(
                s.exec(select(ContentRecord.create_time).where(ContentRecord.target_id == target_id)).all()
            )
            target_kind = target.target_kind or "creator"
            sec_uid = (target.sec_uid or "").strip()
            keyword = (target.keyword or "").strip()
            base_dir = target.download_dir or get_setting("download_dir", self.cfg.engine.media_dir)
            quality = target.video_quality or get_setting("video_quality", "highest")
            monitor_since = int(target.created_at.timestamp())
            scan_since = _douyin_scan_since(monitor_since, known_create_times)
            backfill_count = target.initial_backfill_count
            should_backfill = first_scan or (backfill_count != 0 and not known)
            auto_download = target.download_enabled
            media_filter = target.media_filter or "all"
            strategy = _monitor_strategy(target, default_scrolls=4, default_items=0)

        mode = str(getattr(self.cfg.engine, "bilibili_read_mode", "hybrid") or "hybrid").lower()
        bcli = BilibiliClient(
            state, ua, proxy=proxy, timeout=self.cfg.engine.request_timeout_seconds
        )
        raw_items: list[dict] = []
        author_name = ""
        author_avatar = ""
        error = ""

        async with bcli.session_scope():
            if mode in {"api", "hybrid"}:
                max_pages = max(1, min(6, strategy["max_scrolls"] or 2))
                for pn in range(1, max_pages + 1):
                    if target_kind == "keyword":
                        batch, has_more, err = await bcli.search_videos(
                            keyword, page=pn, page_size=20, order="pubdate"
                        )
                    else:
                        batch, has_more, err = await bcli.fetch_user_videos(
                            sec_uid, pn=pn, ps=30, order="pubdate"
                        )
                    if err:
                        error = err
                        break
                    raw_items.extend(batch)
                    if not has_more or (known and batch and all(str(r.get("bvid") or "") in known for r in batch)):
                        break

            if (mode == "browser" or (mode == "hybrid" and not raw_items and error)) and identity is not None:
                if target_kind == "keyword":
                    b_items, b_err = await fetch_bili_search(
                        self.browser, identity, keyword, max_scrolls=max(1, min(4, strategy["max_scrolls"] or 2))
                    )
                    if b_items:
                        raw_items, error = b_items, ""
                    elif mode == "browser":
                        error = b_err
                else:
                    b_items, b_author, b_err = await fetch_bili_videos(
                        self.browser, identity, sec_uid, known,
                        max_scrolls=max(1, min(4, strategy["max_scrolls"] or 2))
                    )
                    if b_items:
                        raw_items, author_name, error = b_items, b_author, ""
                    elif mode == "browser":
                        error = b_err

            candidates: list[Aweme] = []
            seen_ids: set[str] = set()
            for r in raw_items:
                aw = parse_bili_aweme(r, require_media=False)
                if aw is None or aw.aweme_id in known or aw.aweme_id in seen_ids:
                    continue
                seen_ids.add(aw.aweme_id)
                if aw.author_name and not author_name and target_kind == "creator":
                    author_name = aw.author_name
                if aw.avatar and not author_avatar and target_kind == "creator":
                    author_avatar = aw.avatar
                candidates.append(aw)

            candidates.sort(key=lambda a: int(a.create_time or 0), reverse=True)
            if should_backfill:
                if backfill_count == 0:
                    candidates = [a for a in candidates if not a.create_time or a.create_time >= monitor_since]
                elif backfill_count > 0:
                    candidates = candidates[:backfill_count]
            else:
                candidates = [a for a in candidates if not a.create_time or a.create_time >= scan_since]

            new_records = []
            filtered_count = 0
            for aw in candidates:
                if not _monitor_content_matches(aw, strategy):
                    filtered_count += 1
                    continue
                if strategy["max_items"] and len(new_records) >= strategy["max_items"]:
                    break
                should_download = auto_download and (
                    media_filter == "all" or aw.media_type == media_filter
                )
                play_err = ""
                if should_download and not aw.medias:
                    full_aw, play_err = await bcli.fetch_aweme_by_work(
                        aw.aweme_id, with_play_url=True, quality=quality
                    )
                    if full_aw is not None:
                        aw = full_aw
                media_json = json.dumps([
                    {
                        "url": m.url,
                        "kind": m.kind,
                        "ext": m.ext,
                        "index": m.index,
                        **(
                            {"audio_url": m.audio_url}
                            if getattr(m, "audio_url", "")
                            else {}
                        ),
                    }
                    for m in aw.medias
                ])
                rec = ContentRecord(
                    platform="bilibili",
                    target_id=target_id,
                    aweme_id=aw.aweme_id,
                    desc=aw.desc,
                    media_type=aw.media_type,
                    quality=aw.quality_label,
                    create_time=aw.create_time,
                    cover_url=aw.cover or "",
                    like_count=aw.like_count,
                    comment_count=aw.comment_count,
                    duration=aw.duration,
                    media_json=media_json,
                    download_status="pending" if (should_download and aw.medias) else (
                        "failed" if should_download else "skipped"
                    ),
                    error=(
                        (play_err or "未获取到视频直链")
                        if (should_download and not aw.medias)
                        else ""
                    ),
                )
                new_records.append((rec, aw, bool(should_download and aw.medias)))

        target_name = ""
        with get_session() as s:
            for rec, _, _ in new_records:
                s.add(rec)
            t = s.get(MonitorTarget, target_id)
            if t:
                t.last_scan_at = datetime.utcnow()
                t.last_error = error
                if target_kind == "creator":
                    if author_name and not t.nickname:
                        t.nickname = author_name
                    if author_avatar and not t.avatar:
                        t.avatar = author_avatar
                target_name = t.nickname or t.keyword or t.sec_uid
                target_avatar = t.avatar or ""
                s.add(t)
            else:
                target_avatar = ""
            s.commit()
            for rec, _, _ in new_records:
                s.refresh(rec)

        thumb_cache.schedule_thumb_prewarm(
            [target_avatar, *(aw.cover for _, aw, dl in new_records if aw.cover and not dl)],
            platform="bilibili",
            proxy=proxy,
        )

        dl_cookies = bili_cookie_map(state) if state else None
        for rec, aw, should_download in new_records:
            if not should_download:
                continue
            ok, path, dl_err = await self.downloader.download_aweme(
                aw,
                base_dir,
                self._dl_proxy(proxy),
                user_agent=ua,
                cookies=dl_cookies,
                referer=aw.source_url or "https://www.bilibili.com/",
            )
            with get_session() as s:
                r = s.get(ContentRecord, rec.id)
                if r:
                    r.download_status = "done" if ok else "failed"
                    r.local_path = path
                    r.error = dl_err
                    s.add(r)
                    s.commit()

        if not first_scan and new_records:
            await self._notify_new(target_name, [aw for _, aw, _ in new_records])
        return {
            "ok": not error,
            "new": len(new_records),
            "new_works": len(new_records),
            "scanned": len(candidates),
            "filtered": filtered_count,
            "backfill_count": backfill_count,
            "error": error,
        }

    async def _scan_instagram_target_locked(self, target_id: int) -> dict:
        with get_session() as s:
            target = s.get(MonitorTarget, target_id)
            if not target:
                return {"ok": False, "error": "target not found"}
            first_scan = target.last_scan_at is None
            if not target.account_id:
                return self._mark_target_skip(
                    target_id, "Instagram 作品监控必须绑定已登录的 Instagram 账号"
                )
            acc = s.get(DouyinAccount, target.account_id)
            if not acc or acc.platform != "instagram" or acc.status != "active":
                return self._mark_target_skip(
                    target_id, "绑定的 Instagram 账号不存在或登录态已失效，请重新绑定/登录"
                )
            if self._proxy_bad(acc):
                return self._mark_target_skip(
                    target_id, "账号代理标记为不可用(proxy bad)，已跳过以免暴露真实 IP"
                )
            identity = (
                self.browser.identity_for(acc)
                if callable(getattr(self.browser, "identity_for", None))
                else None
            )
            proxy = acc.proxy or ""
            state = acc.storage_state or acc.creator_storage_state or acc.cookie or ""
            ua = acc.ua or self.cfg.engine.user_agent
            account_seed = (
                (getattr(identity, "fp_seed", "") or getattr(identity, "profile_dir", ""))
                if identity is not None
                else (acc.fp_seed or acc.profile_dir or "")
            )
            tz_id = (
                getattr(identity, "timezone_id", "Asia/Shanghai")
                if identity is not None
                else (acc.timezone_id or "Asia/Shanghai")
            )
            known = set(
                s.exec(select(ContentRecord.aweme_id).where(ContentRecord.target_id == target_id)).all()
            )
            known_create_times = list(
                s.exec(select(ContentRecord.create_time).where(ContentRecord.target_id == target_id)).all()
            )
            target_kind = target.target_kind or "creator"
            sec_uid = (target.sec_uid or "").strip()
            keyword = (target.keyword or "").strip()
            base_dir = target.download_dir or get_setting("download_dir", self.cfg.engine.media_dir)
            monitor_since = int(target.created_at.timestamp())
            scan_since = _douyin_scan_since(monitor_since, known_create_times)
            backfill_count = target.initial_backfill_count
            should_backfill = first_scan or (backfill_count != 0 and not known)
            auto_download = target.download_enabled
            media_filter = target.media_filter or "all"
            strategy = _monitor_strategy(target, default_scrolls=4, default_items=0)

        mode = str(getattr(self.cfg.engine, "instagram_read_mode", "hybrid") or "hybrid").lower()
        icli = InstagramClient(
            state,
            ua,
            proxy=proxy,
            timeout=self.cfg.engine.request_timeout_seconds,
            account_seed=account_seed,
            timezone_id=tz_id,
        )
        raw_items: list[dict] = []
        author_name = ""
        author_avatar = ""
        error = ""

        async with icli.session_scope():
            if mode in {"api", "hybrid"}:
                max_pages = max(1, min(6, strategy["max_scrolls"] or 2))
                if target_kind == "keyword":
                    raw_items, error = await icli.search_posts(keyword, max_pages=max_pages)
                else:
                    cursor = ""
                    for _ in range(max_pages):
                        batch, next_cursor, more, err = await icli.fetch_user_feed(
                            sec_uid, max_id=cursor
                        )
                        if err:
                            error = err
                            break
                        raw_items.extend(batch)
                        if (
                            not more
                            or not next_cursor
                            or next_cursor == cursor
                            or (
                                known
                                and batch
                                and all(
                                    (parse_ig_aweme(r, require_media=False) is not None)
                                    and parse_ig_aweme(r, require_media=False).aweme_id in known  # type: ignore[union-attr]
                                    for r in batch
                                )
                            )
                        ):
                            break
                        cursor = next_cursor

            auth_failed = classify_platform_error(error)[0] == RiskCategory.AUTH
            if (
                mode == "browser"
                or (mode == "hybrid" and not raw_items and error and not auth_failed)
            ) and identity is not None:
                scrolls = max(1, min(4, strategy["max_scrolls"] or 2))
                if target_kind == "keyword":
                    b_items, b_err = await fetch_ig_search(
                        self.browser, identity, keyword, max_scrolls=scrolls
                    )
                    if b_items:
                        raw_items, error = b_items, ""
                    elif mode == "browser":
                        error = b_err
                else:
                    b_items, b_author, b_err = await fetch_ig_posts(
                        self.browser, identity, sec_uid, known, max_scrolls=scrolls
                    )
                    if b_items:
                        raw_items, error = b_items, ""
                        if isinstance(b_author, dict):
                            author_name = str(b_author.get("nickname") or author_name)
                            author_avatar = str(b_author.get("avatar") or author_avatar)
                    elif mode == "browser":
                        error = b_err

            candidates: list[Aweme] = []
            seen_ids: set[str] = set()
            for r in raw_items:
                aw = parse_ig_aweme(r, require_media=False)
                if aw is None or aw.aweme_id in known or aw.aweme_id in seen_ids:
                    continue
                seen_ids.add(aw.aweme_id)
                if aw.author_name and not author_name and target_kind == "creator":
                    author_name = aw.author_name
                if aw.avatar and not author_avatar and target_kind == "creator":
                    author_avatar = aw.avatar
                candidates.append(aw)

            candidates.sort(key=lambda a: int(a.create_time or 0), reverse=True)
            if should_backfill:
                if backfill_count == 0:
                    candidates = [a for a in candidates if not a.create_time or a.create_time >= monitor_since]
                elif backfill_count > 0:
                    candidates = candidates[:backfill_count]
            else:
                candidates = [a for a in candidates if not a.create_time or a.create_time >= scan_since]

            new_records = []
            filtered_count = 0
            for aw in candidates:
                if not _monitor_content_matches(aw, strategy):
                    filtered_count += 1
                    continue
                if strategy["max_items"] and len(new_records) >= strategy["max_items"]:
                    break
                should_download = auto_download and (
                    media_filter == "all" or aw.media_type == media_filter
                )
                media_err = ""
                if should_download and not aw.medias:
                    full_aw, _raw_m, media_err = await icli.fetch_media_info(aw.aweme_id)
                    if full_aw is not None and full_aw.medias:
                        aw = full_aw
                media_json = json.dumps([
                    {
                        "url": m.url,
                        "kind": m.kind,
                        "ext": m.ext,
                        "index": m.index,
                    }
                    for m in aw.medias
                ])
                rec = ContentRecord(
                    platform="instagram",
                    target_id=target_id,
                    aweme_id=aw.aweme_id,
                    desc=aw.desc,
                    media_type=aw.media_type,
                    quality=aw.quality_label,
                    create_time=aw.create_time,
                    cover_url=aw.cover or "",
                    like_count=aw.like_count,
                    comment_count=aw.comment_count,
                    duration=aw.duration,
                    media_json=media_json,
                    download_status="pending" if (should_download and aw.medias) else (
                        "failed" if should_download else "skipped"
                    ),
                    error=(
                        (media_err or "未获取到 Instagram 媒体直链")
                        if (should_download and not aw.medias)
                        else ""
                    ),
                )
                new_records.append((rec, aw, bool(should_download and aw.medias)))

        target_name = ""
        with get_session() as s:
            for rec, _, _ in new_records:
                s.add(rec)
            t = s.get(MonitorTarget, target_id)
            if t:
                t.last_scan_at = datetime.utcnow()
                t.last_error = error
                if target_kind == "creator":
                    if author_name and not t.nickname:
                        t.nickname = author_name
                    if author_avatar and not t.avatar:
                        t.avatar = author_avatar
                target_name = t.nickname or t.keyword or t.sec_uid
                target_avatar = t.avatar or ""
                s.add(t)
            else:
                target_avatar = ""
            s.commit()
            for rec, _, _ in new_records:
                s.refresh(rec)

        thumb_cache.schedule_thumb_prewarm(
            [target_avatar, *(aw.cover for _, aw, dl in new_records if aw.cover and not dl)],
            platform="instagram",
            proxy=proxy,
        )

        for rec, aw, should_download in new_records:
            if not should_download:
                continue
            ok, path, dl_err = await self.downloader.download_aweme(
                aw,
                base_dir,
                self._dl_proxy(proxy),
                user_agent=ua,
                referer="https://www.instagram.com/",
            )
            with get_session() as s:
                r = s.get(ContentRecord, rec.id)
                if r:
                    r.download_status = "done" if ok else "failed"
                    r.local_path = path
                    r.error = dl_err
                    s.add(r)
                    s.commit()

        if not first_scan and new_records:
            await self._notify_new(target_name, [aw for _, aw, _ in new_records])
        return {
            "ok": not error,
            "new": len(new_records),
            "new_works": len(new_records),
            "scanned": len(candidates),
            "filtered": filtered_count,
            "backfill_count": backfill_count,
            "error": error,
        }

    def _make_twitter_client(self, identity=None, state: str = "", ua: str = "", proxy: str = "") -> TwitterClient:
        raw_state = state
        if not raw_state and identity is not None:
            b_states = getattr(identity, "bridge_states", None) or ()
            if b_states:
                raw_state = b_states[0]
        eff_ua = ua or (getattr(identity, "ua", "") if identity is not None else "") or self.cfg.engine.user_agent
        eff_proxy = proxy or (getattr(identity, "proxy", "") if identity is not None else "") or ""
        return TwitterClient(
            raw_state,
            eff_ua,
            proxy=eff_proxy,
            timeout=self.cfg.engine.request_timeout_seconds,
        )

    async def _tw_read_search(
        self, identity, keyword: str, *, latest: bool = True, content_type: str = "all", max_pages: int = 3, state: str = "", ua: str = "", proxy: str = ""
    ) -> tuple[list[dict], str]:
        mode = str(getattr(self.cfg.engine, "twitter_read_mode", "hybrid") or "hybrid").lower()
        tcli = self._make_twitter_client(identity, state=state, ua=ua, proxy=proxy)
        items: list[dict] = []
        err = ""
        if mode in {"api", "hybrid"} and tcli.has_auth:
            async with tcli.session_scope():
                items, err = await tcli.search_posts(
                    keyword, max_pages=max_pages, latest=latest, content_type=content_type
                )
            if items or err.startswith(("logged_out:", "verification_required:", "rate_limit:")):
                return items, ("" if items else err)
        if (mode == "browser" or (mode == "hybrid" and not items)) and identity is not None:
            b_items, b_err = await fetch_twitter_search(
                self.browser,
                identity,
                keyword,
                latest=latest,
                content_type=content_type,
                max_scrolls=max_pages,
                block_media=self.cfg.engine.block_media_resources,
            )
            if b_items:
                return b_items, ""
            if mode == "browser" or not err:
                err = b_err
        return items, err

    async def _tw_read_user_tweets(
        self, identity, user_ref: str, known: set[str] | None = None, *, max_pages: int = 3, state: str = "", ua: str = "", proxy: str = ""
    ) -> tuple[list[dict], dict, str]:
        mode = str(getattr(self.cfg.engine, "twitter_read_mode", "hybrid") or "hybrid").lower()
        tcli = self._make_twitter_client(identity, state=state, ua=ua, proxy=proxy)
        items: list[dict] = []
        uinfo: dict = {}
        err = ""
        if mode in {"api", "hybrid"} and tcli.has_auth:
            async with tcli.session_scope():
                items, uinfo, err = await tcli.fetch_user_tweets(user_ref, max_pages=max_pages)
            if items or err.startswith(("logged_out:", "verification_required:", "rate_limit:")):
                return items, uinfo, ("" if items else err)
        if (mode == "browser" or (mode == "hybrid" and not items)) and identity is not None:
            b_items, b_author, b_err = await fetch_twitter_user_tweets(
                self.browser,
                identity,
                user_ref,
                known or set(),
                max_scrolls=max_pages,
                block_media=self.cfg.engine.block_media_resources,
            )
            if b_items:
                return b_items, (b_author or uinfo), ""
            if mode == "browser" or not err:
                err = b_err
        return items, uinfo, err

    async def _tw_read_comments(
        self, identity, work_id: str, *, max_count: int = 60, include_replies: bool = True, state: str = "", ua: str = "", proxy: str = ""
    ) -> tuple[list[dict], str, str]:
        mode = str(getattr(self.cfg.engine, "twitter_read_mode", "hybrid") or "hybrid").lower()
        tcli = self._make_twitter_client(identity, state=state, ua=ua, proxy=proxy)
        rows: list[dict] = []
        err = ""
        source = "api"
        if mode in {"api", "hybrid"} and tcli.has_auth:
            async with tcli.session_scope():
                rows, err = await tcli.fetch_comments(
                    work_id, max_count=max_count, include_replies=include_replies
                )
            if rows or err.startswith(("logged_out:", "verification_required:", "rate_limit:")):
                return rows, ("" if rows else err), "api"
        if (mode == "browser" or (mode == "hybrid" and not rows and err)) and identity is not None:
            b_rows, b_err = await fetch_twitter_comments(
                self.browser,
                identity,
                work_id,
                max_count=max_count,
                include_replies=include_replies,
                block_media=self.cfg.engine.block_media_resources,
            )
            source = "browser_fallback" if mode == "hybrid" else "browser"
            if b_rows:
                return b_rows, "", source
            if mode == "browser" or not err:
                err = b_err
        return rows, err, source

    async def _scan_twitter_target_locked(self, target_id: int) -> dict:
        with get_session() as s:
            target = s.get(MonitorTarget, target_id)
            if not target:
                return {"ok": False, "error": "target not found"}
            first_scan = target.last_scan_at is None
            if not target.account_id:
                return self._mark_target_skip(
                    target_id, "X (Twitter) 作品监控必须绑定已登录的 Twitter 账号"
                )
            acc = s.get(DouyinAccount, target.account_id)
            if not acc or acc.platform != "twitter" or acc.status != "active":
                return self._mark_target_skip(
                    target_id, "绑定的 Twitter 账号不存在或登录态已失效，请重新绑定/登录"
                )
            if self._proxy_bad(acc):
                return self._mark_target_skip(
                    target_id, "账号代理标记为不可用(proxy bad)，已跳过以免暴露真实 IP"
                )
            identity = (
                self.browser.identity_for(acc)
                if callable(getattr(self.browser, "identity_for", None))
                else None
            )
            proxy = acc.proxy or ""
            state = acc.storage_state or acc.creator_storage_state or acc.cookie or ""
            ua = acc.ua or self.cfg.engine.user_agent
            known = set(
                s.exec(select(ContentRecord.aweme_id).where(ContentRecord.target_id == target_id)).all()
            )
            known_create_times = list(
                s.exec(select(ContentRecord.create_time).where(ContentRecord.target_id == target_id)).all()
            )
            target_kind = target.target_kind or "creator"
            sec_uid = (target.sec_uid or "").strip()
            keyword = (target.keyword or "").strip()
            base_dir = target.download_dir or get_setting("download_dir", self.cfg.engine.media_dir)
            monitor_since = int(target.created_at.timestamp())
            scan_since = _douyin_scan_since(monitor_since, known_create_times)
            backfill_count = target.initial_backfill_count
            should_backfill = first_scan or (backfill_count != 0 and not known)
            auto_download = target.download_enabled
            media_filter = target.media_filter or "all"
            strategy = _monitor_strategy(target, default_scrolls=4, default_items=0)

        if identity is None:
            return self._mark_target_skip(target_id, "缺少 Twitter 账号浏览器隔离身份")

        raw_items: list[dict] = []
        author_name = ""
        author_avatar = ""
        error = ""
        scrolls = max(1, min(6, strategy["max_scrolls"] or 3))

        if target_kind == "keyword":
            raw_items, error = await self._tw_read_search(
                identity,
                keyword,
                latest=True,
                content_type=strategy.get("media_type") or "all",
                max_pages=scrolls,
                state=state,
                ua=ua,
                proxy=proxy,
            )
        else:
            raw_items, b_author, error = await self._tw_read_user_tweets(
                identity,
                sec_uid,
                known,
                max_pages=scrolls,
                state=state,
                ua=ua,
                proxy=proxy,
            )
            if isinstance(b_author, dict):
                author_name = str(b_author.get("nickname") or author_name)
                author_avatar = str(b_author.get("avatar") or author_avatar)
        if error == "empty":
            error = ""

        candidates: list[Aweme] = []
        seen_ids: set[str] = set()
        for r in raw_items:
            aw = parse_twitter_aweme(r, skip_retweets=(target_kind == "creator"))
            if aw is None or aw.aweme_id in known or aw.aweme_id in seen_ids:
                continue
            seen_ids.add(aw.aweme_id)
            if aw.author_name and not author_name and target_kind == "creator":
                author_name = aw.author_name
            if aw.avatar and not author_avatar and target_kind == "creator":
                author_avatar = aw.avatar
            candidates.append(aw)

        candidates.sort(key=lambda a: int(a.create_time or 0), reverse=True)
        if should_backfill:
            if backfill_count == 0:
                candidates = [a for a in candidates if not a.create_time or a.create_time >= monitor_since]
            elif backfill_count > 0:
                candidates = candidates[:backfill_count]
        else:
            candidates = [a for a in candidates if not a.create_time or a.create_time >= scan_since]

        tcli = TwitterClient(
            state,
            ua,
            proxy=proxy,
            timeout=self.cfg.engine.request_timeout_seconds,
        )
        new_records = []
        filtered_count = 0
        async with tcli.session_scope():
            for aw in candidates:
                if not _monitor_content_matches(aw, strategy):
                    filtered_count += 1
                    continue
                if strategy["max_items"] and len(new_records) >= strategy["max_items"]:
                    break
                should_download = auto_download and (
                    media_filter == "all" or aw.media_type == media_filter
                )
                if should_download and not aw.medias:
                    full_aw, _raw_m, _m_err = await tcli.fetch_tweet_syndication(
                        aw.source_url or aw.aweme_id
                    )
                    if full_aw is not None and full_aw.medias:
                        aw = full_aw
                media_json = json.dumps([
                    {
                        "url": m.url,
                        "kind": m.kind,
                        "ext": m.ext,
                        "index": m.index,
                    }
                    for m in aw.medias
                ])
                # 纯文本推文无附件属于正常情况，标记 skipped 避免误报下载失败
                dl_status = (
                    "pending"
                    if (should_download and aw.medias)
                    else "skipped"
                )
                rec = ContentRecord(
                    platform="twitter",
                    target_id=target_id,
                    aweme_id=aw.aweme_id,
                    desc=aw.desc,
                    media_type=aw.media_type,
                    quality=aw.quality_label,
                    create_time=aw.create_time,
                    cover_url=aw.cover or "",
                    like_count=aw.like_count,
                    comment_count=aw.comment_count,
                    duration=aw.duration,
                    media_json=media_json,
                    download_status=dl_status,
                    error="",
                )
                new_records.append((rec, aw, bool(should_download and aw.medias)))

        target_name = ""
        with get_session() as s:
            for rec, _, _ in new_records:
                s.add(rec)
            t = s.get(MonitorTarget, target_id)
            if t:
                t.last_scan_at = datetime.utcnow()
                t.last_error = error
                if target_kind == "creator":
                    if author_name and not t.nickname:
                        t.nickname = author_name
                    if author_avatar and not t.avatar:
                        t.avatar = author_avatar
                target_name = t.nickname or t.keyword or t.sec_uid
                target_avatar = t.avatar or ""
                s.add(t)
            else:
                target_avatar = ""
            s.commit()
            for rec, _, _ in new_records:
                s.refresh(rec)

        thumb_cache.schedule_thumb_prewarm(
            [target_avatar, *(aw.cover for _, aw, dl in new_records if aw.cover and not dl)],
            platform="twitter",
            proxy=proxy,
        )

        for rec, aw, should_download in new_records:
            if not should_download:
                continue
            ok, path, dl_err = await self.downloader.download_aweme(
                aw,
                base_dir,
                self._dl_proxy(proxy),
                user_agent=ua,
                referer="https://x.com/",
            )
            with get_session() as s:
                r = s.get(ContentRecord, rec.id)
                if r:
                    r.download_status = "done" if ok else "failed"
                    r.local_path = path
                    r.error = dl_err
                    s.add(r)
                    s.commit()

        if not first_scan and new_records:
            await self._notify_new(target_name, [aw for _, aw, _ in new_records])
        return {
            "ok": not error,
            "new": len(new_records),
            "new_works": len(new_records),
            "scanned": len(candidates),
            "filtered": filtered_count,
            "backfill_count": backfill_count,
            "error": error,
        }
