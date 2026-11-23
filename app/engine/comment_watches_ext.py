"""小红书、海外小红书、B站、Instagram 与 Twitter 评论监控拉取。"""
from __future__ import annotations

from typing import Optional
import time

from sqlmodel import select

from app.browser import fetch_bili_comments, fetch_ig_comments, fetch_ig_posts, fetch_rednote_comments, fetch_rednote_notes, fetch_xhs_comments, fetch_xhs_notes
from app.db import get_session
from app.engine.monitor_helpers import log
from app.models import CommentRecord, CommentWatch
from app.platforms.bilibili import BilibiliClient
from app.platforms.instagram import InstagramClient, parse_ig_aweme
from app.platforms.rednote import RednoteApiClient, RednoteApiError, flatten_rednote_comments, parse_rednote_brief, parse_rednote_comment, parse_rednote_detail, parse_rednote_self_user, cookie_str_from_state as rednote_cookie_str_from_state, has_a1 as rednote_has_a1
from app.platforms.twitter import TwitterClient, parse_twitter_aweme
from app.platforms.xhs import XhsApiClient, XhsApiError, cookie_str_from_state, flatten_comments as flatten_xhs_comments, has_a1, parse_note_brief, parse_comment as parse_xhs_comment, parse_self_user as parse_xhs_self_user
from app.risk import RiskCategory, classify_platform_error


class CommentWatchPlatformsMixin:
    # ── 小红书评论监控(浏览器优先，签名 API 仅显式兼容)──
    def _xhs_client(self, identity, state: str, proxy: str = ""):
        cookie_str = cookie_str_from_state(state)
        if not has_a1(cookie_str):
            return None
        return XhsApiClient(
            cookie_str,
            self._direct_request_ua(identity),
            timeout=self.cfg.engine.request_timeout_seconds,
            proxy=proxy,
            storage_state=state,
        )

    async def _xhs_fetch_comments(self, client, note_id, xsec_token, known) -> list:
        try:
            d = await client.note_comments(note_id, xsec_token=xsec_token)
            raw = d.get("comments") or []
        except XhsApiError:
            raise
        except Exception as e:
            category, _signal = classify_platform_error(e)
            if category in {
                    RiskCategory.RISK, RiskCategory.AUTH,
                    RiskCategory.NETWORK}:
                raise
            log.info("评论监控(小红书)%s: %s", note_id, e)
            return []
        fresh = [c for c in (parse_xhs_comment(rc) for rc in flatten_xhs_comments(raw)) if c]
        return [c for c in fresh if c["comment_id"] not in known]

    async def _cw_xhs_note(
            self, watch_id, identity, state, note_id, xsec_token, name,
            first_scan, proxy=""):
        with get_session() as s:
            known = set(s.exec(select(CommentRecord.comment_id)
                               .where(CommentRecord.watch_id == watch_id)
                               .where(CommentRecord.aweme_id == note_id)).all())
        if self._xhs_browser_reads_enabled():
            raw, error = await fetch_xhs_comments(
                self.browser, identity, note_id, known,
                xsec_token=xsec_token, xsec_source="pc_feed",
                max_scrolls=self._comment_watch_settings(watch_id)["max_scrolls"],
                block_media=self.cfg.engine.block_media_resources)
            if error:
                log.info("评论监控(小红书)%s: %s", note_id, error)
                self._raise_severe_xhs_read_error(error)
            fresh = [c for c in
                     (parse_xhs_comment(rc)
                      for rc in flatten_xhs_comments(raw)) if c]
            fresh = [c for c in fresh if c["comment_id"] not in known]
        else:
            client = self._xhs_client(identity, state, proxy)
            if client is None:
                return 0, None
            fresh = await self._xhs_fetch_comments(
                client, note_id, xsec_token, known)
        n = await self._ingest(watch_id, note_id, fresh, name, name, first_scan,
                               platform="xhs")
        return n, None

    async def _cw_xhs_creator(
            self, watch_id, identity, state, user_id, xsec_token, name,
            first_scan, proxy=""):
        settings = self._comment_watch_settings(watch_id)
        client = None
        browser_reads = self._xhs_browser_reads_enabled()
        if browser_reads:
            briefs_raw, author, error = await fetch_xhs_notes(
                self.browser, identity, user_id, set(),
                xsec_token=xsec_token, xsec_source="pc_feed",
                max_scrolls=4,
                block_media=self.cfg.engine.block_media_resources)
            if error:
                log.info("评论监控(小红书创作者)%s: %s", user_id, error)
                self._raise_severe_xhs_read_error(error)
        else:
            client = self._xhs_client(identity, state, proxy)
            if client is None:
                return 0, None
            try:
                d = await client.notes_by_creator(user_id, xsec_token=xsec_token)
                briefs_raw = d.get("notes") or []
                author = await client.user_info(user_id)
            except XhsApiError:
                raise
            except Exception as e:
                category, _signal = classify_platform_error(e)
                if category in {
                        RiskCategory.RISK, RiskCategory.AUTH,
                        RiskCategory.NETWORK}:
                    raise
                log.info("评论监控(小红书创作者)%s: %s", user_id, e)
                briefs_raw, author = [], None
        briefs = [b for b in (parse_note_brief(r) for r in briefs_raw) if b]
        cutoff = int(time.time()) - settings["recent_days"] * 86400
        briefs = [b for b in briefs
                  if not b.get("create_time") or b["create_time"] >= cutoff]
        briefs = briefs[:settings["recent_works"]]
        total = 0
        for index, b in enumerate(briefs):
            nid = b["note_id"]
            with get_session() as s:
                known = set(s.exec(select(CommentRecord.comment_id)
                                   .where(CommentRecord.watch_id == watch_id)
                                   .where(CommentRecord.aweme_id == nid)).all())
            if index:
                await self._xhs_gap()
            if browser_reads:
                raw, read_error = await fetch_xhs_comments(
                    self.browser, identity, nid, known,
                    xsec_token=b.get("xsec_token", ""),
                    xsec_source="pc_feed",
                    max_scrolls=settings["max_scrolls"],
                    block_media=self.cfg.engine.block_media_resources)
                if read_error:
                    log.info("评论监控(小红书)%s: %s", nid, read_error)
                    self._raise_severe_xhs_read_error(read_error)
                fresh = [c for c in
                         (parse_xhs_comment(rc)
                          for rc in flatten_xhs_comments(raw)) if c]
                fresh = [c for c in fresh if c["comment_id"] not in known]
            else:
                fresh = await self._xhs_fetch_comments(
                    client, nid, b.get("xsec_token", ""), known)
            total += await self._ingest(watch_id, nid, fresh, name, b.get("title", ""),
                                        first_scan, platform="xhs")
        author_dict = parse_xhs_self_user(author) if author else None
        return total, ({"nickname": author_dict["nickname"],
                        "avatar_thumb": {"url_list": [author_dict["avatar"]]}}
                       if author_dict else None)

    def _rednote_read_mode(self) -> str:
        mode = str(getattr(self.cfg.engine, "rednote_read_mode", "hybrid") or "hybrid").lower()
        return mode if mode in {"browser", "hybrid", "api"} else "hybrid"

    def _rednote_client(self, identity, state: str, proxy: str = "") -> Optional[RednoteApiClient]:
        cookie_str = rednote_cookie_str_from_state(state)
        if not rednote_has_a1(cookie_str):
            return None
        return RednoteApiClient(
            cookie_str,
            self._direct_request_ua(identity),
            timeout=self.cfg.engine.request_timeout_seconds,
            proxy=proxy,
            storage_state=state,
        )

    async def _rednote_fetch_comments(self, client: RednoteApiClient, note_id, xsec_token, known) -> list:
        try:
            d = await client.note_comments(note_id, xsec_token=xsec_token)
            raw = d.get("comments") or []
        except RednoteApiError:
            raise
        except Exception as e:
            category, _signal = classify_platform_error(e)
            if category in {RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK}:
                raise
            log.info("评论监控(海外小红书)%s: %s", note_id, e)
            return []
        fresh = [c for c in (parse_rednote_comment(rc) for rc in flatten_rednote_comments(raw)) if c]
        return [c for c in fresh if c["comment_id"] not in known]

    async def _cw_rednote_note(
        self, watch_id, identity, state, note_id, xsec_token, name,
        first_scan, proxy=""
    ):
        with get_session() as s:
            known = set(s.exec(select(CommentRecord.comment_id)
                               .where(CommentRecord.watch_id == watch_id)
                               .where(CommentRecord.aweme_id == note_id)).all())
        rmode = self._rednote_read_mode()
        client = self._rednote_client(identity, state, proxy) if rmode in {"api", "hybrid"} else None
        fresh: list = []
        api_failed = False
        work_desc = name
        author_meta = None
        if client is not None and rmode in {"api", "hybrid"}:
            try:
                async with client.session_scope():
                    try:
                        card = await client.note_detail(
                            note_id, xsec_token=xsec_token, xsec_source="pc_feed"
                        )
                        aw_det = parse_rednote_detail(card or {}, {"note_id": note_id}) if card else None
                        raw_title = (
                            (aw_det.desc if aw_det and aw_det.desc else "")
                            or str((card or {}).get("title") or (card or {}).get("display_title") or (card or {}).get("desc") or "").strip()
                        )
                        if raw_title:
                            work_desc = raw_title[:60]
                            with get_session() as s:
                                w = s.get(CommentWatch, watch_id)
                                if w and (not w.title or w.title == f"笔记 {note_id}"):
                                    w.title = raw_title[:40]
                                    s.add(w)
                                    s.commit()
                        u_obj = (card or {}).get("user") if isinstance((card or {}).get("user"), dict) else {}
                        nick = (aw_det.author_name if aw_det else "") or str(u_obj.get("nickname") or "")
                        ava = (aw_det.avatar if aw_det else "") or str(u_obj.get("avatar") or "")
                        if nick or ava:
                            author_meta = {
                                "nickname": nick,
                                "avatar_thumb": {"url_list": [ava] if ava else []},
                            }
                    except Exception:
                        pass
                    fresh = await self._rednote_fetch_comments(
                        client, note_id, xsec_token, known)
            except Exception as e:
                if rmode == "api" or classify_platform_error(e)[0] == RiskCategory.AUTH:
                    raise
                api_failed = True
        if rmode == "browser" or (rmode == "hybrid" and (client is None or api_failed)):
            raw, error = await fetch_rednote_comments(
                self.browser, identity, note_id, known,
                xsec_token=xsec_token, xsec_source="pc_feed",
                max_scrolls=self._comment_watch_settings(watch_id)["max_scrolls"],
                block_media=self.cfg.engine.block_media_resources)
            if error:
                log.info("评论监控(海外小红书)%s: %s", note_id, error)
                self._raise_severe_xhs_read_error(error)
            fresh = [
                c for c in (parse_rednote_comment(rc) for rc in flatten_rednote_comments(raw))
                if c and c["comment_id"] not in known
            ]
        n = await self._ingest(watch_id, note_id, fresh, name, work_desc, first_scan,
                               platform="rednote")
        return n, author_meta

    async def _cw_rednote_creator(
        self, watch_id, identity, state, user_id, xsec_token, name,
        first_scan, proxy=""
    ):
        settings = self._comment_watch_settings(watch_id)
        rmode = self._rednote_read_mode()
        client = self._rednote_client(identity, state, proxy) if rmode in {"api", "hybrid"} else None
        use_browser = rmode == "browser" or client is None
        briefs_raw: list = []
        author = None
        if not use_browser and client is not None:
            try:
                async with client.session_scope():
                    d = await client.notes_by_creator(user_id, xsec_token=xsec_token)
                    briefs_raw = d.get("notes") or []
                    author = await client.user_info(user_id)
            except RednoteApiError as e:
                if rmode == "api" or e.category == "auth":
                    raise
                use_browser = True
            except Exception as e:
                if rmode == "api" or classify_platform_error(e)[0] in {
                        RiskCategory.AUTH, RiskCategory.NETWORK}:
                    raise
                use_browser = True
        if use_browser:
            briefs_raw, author, error = await fetch_rednote_notes(
                self.browser, identity, user_id, set(),
                xsec_token=xsec_token, xsec_source="pc_feed",
                max_scrolls=4,
                block_media=self.cfg.engine.block_media_resources)
            if error:
                log.info("评论监控(海外小红书创作者)%s: %s", user_id, error)
                self._raise_severe_xhs_read_error(error)
        briefs = [b for b in (parse_rednote_brief(r) for r in briefs_raw) if b]
        cutoff = int(time.time()) - settings["recent_days"] * 86400
        briefs = [b for b in briefs if not b.get("create_time") or b["create_time"] >= cutoff]
        briefs = briefs[:settings["recent_works"]]
        total = 0
        for index, b in enumerate(briefs):
            nid = b["note_id"]
            with get_session() as s:
                known = set(s.exec(select(CommentRecord.comment_id)
                                   .where(CommentRecord.watch_id == watch_id)
                                   .where(CommentRecord.aweme_id == nid)).all())
            if index:
                await self._xhs_gap()
            need_browser_comments = use_browser or client is None
            fresh = []
            if not need_browser_comments:
                try:
                    async with client.session_scope():
                        fresh = await self._rednote_fetch_comments(
                            client, nid, b.get("xsec_token", ""), known)
                except Exception as e:
                    if rmode == "api" or classify_platform_error(e)[0] == RiskCategory.AUTH:
                        raise
                    need_browser_comments = True
            if need_browser_comments:
                raw, read_error = await fetch_rednote_comments(
                    self.browser, identity, nid, known,
                    xsec_token=b.get("xsec_token", ""),
                    xsec_source="pc_feed",
                    max_scrolls=settings["max_scrolls"],
                    block_media=self.cfg.engine.block_media_resources)
                if read_error:
                    log.info("评论监控(海外小红书)%s: %s", nid, read_error)
                    self._raise_severe_xhs_read_error(read_error)
                fresh = [
                    c for c in (parse_rednote_comment(rc) for rc in flatten_rednote_comments(raw))
                    if c and c["comment_id"] not in known
                ]
            total += await self._ingest(watch_id, nid, fresh, name, b.get("title", ""),
                                        first_scan, platform="rednote")
        author_dict = parse_rednote_self_user(author) if author else None
        return total, ({"nickname": author_dict["nickname"],
                        "avatar_thumb": {"url_list": [author_dict["avatar"]]}}
                       if author_dict else None)

    async def _cw_bilibili_video(
        self, watch_id: int, identity, state: str, ua: str, proxy: str,
        work_id: str, name: str, first_scan: bool
    ):
        settings = self._comment_watch_settings(watch_id)
        with get_session() as s:
            known = set(
                s.exec(
                    select(CommentRecord.comment_id)
                    .where(CommentRecord.watch_id == watch_id)
                    .where(CommentRecord.aweme_id == work_id)
                ).all()
            )
        mode = str(getattr(self.cfg.engine, "bilibili_read_mode", "hybrid") or "hybrid").lower()
        max_c = max(30, int(settings["max_scrolls"] or 4) * 20)
        rows: list[dict] = []
        err = ""
        author_dict = None
        bcli = BilibiliClient(
            state, ua, proxy=proxy, timeout=self.cfg.engine.request_timeout_seconds
        )
        async with bcli.session_scope():
            if mode in {"api", "hybrid"}:
                detail, _ = await bcli.fetch_video_detail(work_id)
                if isinstance(detail, dict):
                    owner = detail.get("owner") if isinstance(detail.get("owner"), dict) else {}
                    if owner.get("name"):
                        author_dict = {
                            "nickname": str(detail.get("title") or owner.get("name") or ""),
                            "avatar_thumb": {"url_list": [str(owner.get("face") or "")]},
                        }
                rows, err = await bcli.fetch_comments(work_id, max_count=max_c, include_replies=True)
            if (mode == "browser" or (mode == "hybrid" and not rows and err)) and identity is not None:
                b_rows, b_err = await fetch_bili_comments(
                    self.browser, identity, work_id, max_count=max_c
                )
                if b_rows:
                    rows, err = b_rows, ""
                elif mode == "browser":
                    err = b_err
        if err and not rows:
            raise RuntimeError(err)
        fresh = [c for c in rows if c and c.get("comment_id") and c["comment_id"] not in known]
        total = await self._ingest(
            watch_id, work_id, fresh, name, name, first_scan, platform="bilibili"
        )
        return total, author_dict

    async def _cw_bilibili_user(
        self, watch_id: int, identity, state: str, ua: str, proxy: str,
        mid: str, name: str, first_scan: bool
    ):
        settings = self._comment_watch_settings(watch_id)
        bcli = BilibiliClient(
            state, ua, proxy=proxy, timeout=self.cfg.engine.request_timeout_seconds
        )
        total = 0
        author_dict = None
        async with bcli.session_scope():
            vlist, _, err = await bcli.fetch_user_videos(mid, pn=1, ps=30)
            if err and not vlist:
                raise RuntimeError(err)
            cutoff = int(time.time()) - settings["recent_days"] * 86400
            selected: list[tuple[str, str]] = []
            for v in vlist:
                if not author_dict and v.get("author"):
                    author_dict = {"nickname": str(v["author"]), "avatar_thumb": {"url_list": []}}
                bv = str(v.get("bvid") or "")
                ct = int(v.get("created") or v.get("pubdate") or 0)
                if bv and (not cutoff or not ct or ct >= cutoff):
                    selected.append((bv, str(v.get("title") or "")))
                if len(selected) >= settings["recent_works"]:
                    break
            max_c = max(20, int(settings["max_scrolls"] or 4) * 20)
            for bv, vtitle in selected:
                with get_session() as s:
                    known = set(
                        s.exec(
                            select(CommentRecord.comment_id)
                            .where(CommentRecord.watch_id == watch_id)
                            .where(CommentRecord.aweme_id == bv)
                        ).all()
                    )
                rows, _ = await bcli.fetch_comments(bv, max_count=max_c, include_replies=True)
                fresh = [c for c in rows if c and c.get("comment_id") and c["comment_id"] not in known]
                total += await self._ingest(
                    watch_id, bv, fresh, name, vtitle, first_scan, platform="bilibili"
                )
        return total, author_dict

    async def _cw_instagram_video(
        self, watch_id: int, identity, state: str, ua: str, proxy: str,
        work_id: str, name: str, first_scan: bool, work_desc: str = ""
    ):
        settings = self._comment_watch_settings(watch_id)
        with get_session() as s:
            known = set(
                s.exec(
                    select(CommentRecord.comment_id)
                    .where(CommentRecord.watch_id == watch_id)
                    .where(CommentRecord.aweme_id == work_id)
                ).all()
            )
        mode = str(getattr(self.cfg.engine, "instagram_read_mode", "hybrid") or "hybrid").lower()
        max_c = max(30, int(settings["max_scrolls"] or 4) * 20)
        rows: list[dict] = []
        err = ""
        author_dict = None
        icli = InstagramClient(
            state,
            ua,
            proxy=proxy,
            timeout=self.cfg.engine.request_timeout_seconds,
            account_seed=(
                (getattr(identity, "fp_seed", "") or getattr(identity, "profile_dir", ""))
                if identity is not None
                else ""
            ),
            timezone_id=(
                getattr(identity, "timezone_id", "Asia/Shanghai")
                if identity is not None
                else "Asia/Shanghai"
            ),
        )
        async with icli.session_scope():
            if mode in {"api", "hybrid"}:
                aw_info, _raw_m, _ = await icli.fetch_media_info(work_id)
                if aw_info is not None and (aw_info.author_name or aw_info.desc):
                    clean_desc = " ".join(str(aw_info.desc or "").split())[:36]
                    if aw_info.author_name and clean_desc:
                        display_title = f"{aw_info.author_name} · {clean_desc}"
                    else:
                        display_title = clean_desc or aw_info.author_name
                    if not work_desc and aw_info.desc:
                        work_desc = aw_info.desc[:80]
                    author_dict = {
                        "nickname": display_title,
                        "avatar_thumb": {"url_list": [str(aw_info.avatar or aw_info.cover or "")]},
                    }
                rows, err = await icli.fetch_comments(work_id, max_count=max_c, include_replies=True)
            auth_failed = classify_platform_error(err)[0] == RiskCategory.AUTH
            if (
                mode == "browser"
                or (mode == "hybrid" and not rows and err and not auth_failed)
            ) and identity is not None:
                ig_rows, ig_err = await fetch_ig_comments(
                    self.browser, identity, work_id, max_count=max_c
                )
                if ig_rows:
                    rows, err = ig_rows, ""
                elif mode == "browser":
                    err = ig_err
        if err and not rows:
            raise RuntimeError(err)
        fresh = [c for c in rows if c and c.get("comment_id") and c["comment_id"] not in known]
        total = await self._ingest(
            watch_id, work_id, fresh, name, work_desc or name, first_scan, platform="instagram"
        )
        return total, author_dict

    async def _cw_instagram_user(
        self, watch_id: int, identity, state: str, ua: str, proxy: str,
        user_ref: str, name: str, first_scan: bool
    ):
        settings = self._comment_watch_settings(watch_id)
        mode = str(getattr(self.cfg.engine, "instagram_read_mode", "hybrid") or "hybrid").lower()
        icli = InstagramClient(
            state,
            ua,
            proxy=proxy,
            timeout=self.cfg.engine.request_timeout_seconds,
            account_seed=(
                (getattr(identity, "fp_seed", "") or getattr(identity, "profile_dir", ""))
                if identity is not None
                else ""
            ),
            timezone_id=(
                getattr(identity, "timezone_id", "Asia/Shanghai")
                if identity is not None
                else "Asia/Shanghai"
            ),
        )
        total = 0
        author_dict = None
        raw_feed: list[dict] = []
        err = ""
        async with icli.session_scope():
            if mode in {"api", "hybrid"}:
                raw_feed, _next, _more, err = await icli.fetch_user_feed(user_ref)
            auth_failed = classify_platform_error(err)[0] == RiskCategory.AUTH
            if (
                mode == "browser"
                or (mode == "hybrid" and not raw_feed and err and not auth_failed)
            ) and identity is not None:
                b_items, b_author, b_err = await fetch_ig_posts(
                    self.browser, identity, user_ref, set(), max_scrolls=3
                )
                if b_items:
                    raw_feed, err = b_items, ""
                    if isinstance(b_author, dict) and b_author.get("nickname"):
                        author_dict = {
                            "nickname": str(b_author["nickname"]),
                            "avatar_thumb": {"url_list": [str(b_author.get("avatar") or "")]},
                        }
                elif mode == "browser":
                    err = b_err
            if err and not raw_feed:
                raise RuntimeError(err)
            cutoff = int(time.time()) - settings["recent_days"] * 86400
            selected: list[tuple[str, str]] = []
            for raw in raw_feed:
                aw = parse_ig_aweme(raw, require_media=False)
                if aw is None:
                    continue
                if not author_dict and aw.author_name:
                    author_dict = {
                        "nickname": str(aw.author_name),
                        "avatar_thumb": {"url_list": [str(aw.avatar or "")]},
                    }
                if not cutoff or not aw.create_time or aw.create_time >= cutoff:
                    selected.append((aw.aweme_id, aw.desc))
                if len(selected) >= settings["recent_works"]:
                    break
            max_c = max(20, int(settings["max_scrolls"] or 4) * 20)
            for code, desc in selected:
                with get_session() as s:
                    known = set(
                        s.exec(
                            select(CommentRecord.comment_id)
                            .where(CommentRecord.watch_id == watch_id)
                            .where(CommentRecord.aweme_id == code)
                        ).all()
                    )
                rows: list[dict] = []
                c_err = ""
                if mode in {"api", "hybrid"}:
                    rows, c_err = await icli.fetch_comments(code, max_count=max_c, include_replies=True)
                if (
                    mode == "browser"
                    or (mode == "hybrid" and not rows and c_err)
                ) and identity is not None:
                    b_rows, _ = await fetch_ig_comments(
                        self.browser, identity, code, max_count=max_c
                    )
                    if b_rows:
                        rows = b_rows
                fresh = [c for c in rows if c and c.get("comment_id") and c["comment_id"] not in known]
                total += await self._ingest(
                    watch_id, code, fresh, name, desc, first_scan, platform="instagram"
                )
        return total, author_dict

    async def _cw_twitter_video(
        self, watch_id: int, identity, state: str, ua: str, proxy: str,
        work_id: str, name: str, first_scan: bool, work_desc: str = ""
    ):
        settings = self._comment_watch_settings(watch_id)
        with get_session() as s:
            known = set(
                s.exec(
                    select(CommentRecord.comment_id)
                    .where(CommentRecord.watch_id == watch_id)
                    .where(CommentRecord.aweme_id == work_id)
                ).all()
            )
        max_c = max(30, int(settings["max_scrolls"] or 4) * 20)
        author_dict = None
        tcli = TwitterClient(
            state, ua, proxy=proxy, timeout=self.cfg.engine.request_timeout_seconds
        )
        async with tcli.session_scope():
            aw_info, _raw_m, _ = await tcli.fetch_tweet_syndication(work_id)
            if aw_info is not None and (aw_info.author_name or aw_info.desc):
                clean_desc = " ".join(str(aw_info.desc or "").split())[:36]
                display_title = (
                    f"{aw_info.author_name} · {clean_desc}"
                    if aw_info.author_name and clean_desc
                    else (clean_desc or aw_info.author_name)
                )
                if not work_desc and aw_info.desc:
                    work_desc = aw_info.desc[:80]
                author_dict = {
                    "nickname": display_title,
                    "avatar_thumb": {"url_list": [str(aw_info.avatar or aw_info.cover or "")]},
                }
        if identity is None:
            raise RuntimeError("缺少 Twitter 账号浏览器隔离身份")
        rows, err, _ = await self._tw_read_comments(
            identity,
            work_id,
            max_count=max_c,
            include_replies=True,
            state=state,
            ua=ua,
            proxy=proxy,
        )
        if err and err != "empty" and not rows:
            raise RuntimeError(err)
        fresh = [c for c in rows if c and c.get("comment_id") and c["comment_id"] not in known]
        total = await self._ingest(
            watch_id, work_id, fresh, name, work_desc or name, first_scan, platform="twitter"
        )
        return total, author_dict

    async def _cw_twitter_user(
        self, watch_id: int, identity, state: str, ua: str, proxy: str,
        user_ref: str, name: str, first_scan: bool
    ):
        _ = (state, ua, proxy)
        if identity is None:
            raise RuntimeError("缺少 Twitter 账号浏览器隔离身份")
        settings = self._comment_watch_settings(watch_id)
        raw_feed, b_author, err = await self._tw_read_user_tweets(
            identity,
            user_ref,
            set(),
            max_pages=3,
            state=state,
            ua=ua,
            proxy=proxy,
        )
        if err and err != "empty" and not raw_feed:
            raise RuntimeError(err)
        author_dict = None
        if isinstance(b_author, dict) and b_author.get("nickname"):
            author_dict = {
                "nickname": str(b_author["nickname"]),
                "avatar_thumb": {"url_list": [str(b_author.get("avatar") or "")]},
            }
        cutoff = int(time.time()) - settings["recent_days"] * 86400
        selected: list[tuple[str, str]] = []
        for raw in raw_feed:
            aw = parse_twitter_aweme(raw, skip_retweets=True)
            if aw is None:
                continue
            if not author_dict and aw.author_name:
                author_dict = {
                    "nickname": str(aw.author_name),
                    "avatar_thumb": {"url_list": [str(aw.avatar or "")]},
                }
            if not cutoff or not aw.create_time or aw.create_time >= cutoff:
                selected.append((aw.aweme_id, aw.desc))
            if len(selected) >= settings["recent_works"]:
                break
        max_c = max(20, int(settings["max_scrolls"] or 4) * 20)
        total = 0
        for tid, desc in selected:
            with get_session() as s:
                known = set(
                    s.exec(
                        select(CommentRecord.comment_id)
                        .where(CommentRecord.watch_id == watch_id)
                        .where(CommentRecord.aweme_id == tid)
                    ).all()
                )
            rows, _, _ = await self._tw_read_comments(
                identity,
                tid,
                max_count=max_c,
                include_replies=True,
                state=state,
                ua=ua,
                proxy=proxy,
            )
            fresh = [c for c in rows if c and c.get("comment_id") and c["comment_id"] not in known]
            total += await self._ingest(
                watch_id, tid, fresh, name, desc, first_scan, platform="twitter"
            )
        return total, author_dict
