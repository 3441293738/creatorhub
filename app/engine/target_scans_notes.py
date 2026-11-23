"""小红书与海外小红书 (RedNote) 作品监控目标扫描。"""
from __future__ import annotations

from datetime import datetime
import asyncio
import json
import random

from sqlmodel import select

from app.browser import fetch_rednote_note_detail, fetch_rednote_notes, fetch_rednote_search, fetch_xhs_note_detail, fetch_xhs_notes, fetch_xhs_search
from app.db import get_session
from app.engine.monitor_helpers import _loads, _monitor_content_matches, _monitor_strategy, log
from app.models import ContentRecord, DouyinAccount, MonitorTarget
from app.platforms.douyin.extract import Aweme
from app.platforms.rednote import RednoteApiError, parse_rednote_brief, parse_rednote_detail, parse_rednote_self_user, cookie_str_from_state as rednote_cookie_str_from_state
from app.platforms.xhs import XhsApiClient, XhsApiError, cookie_str_from_state, has_a1, parse_note_brief, parse_note_detail, parse_self_user as parse_xhs_self_user
from app.risk import RiskCategory, classify_platform_error
from app.services import thumb_cache
from app.settings import get_setting


class NoteTargetScanMixin:
    # ── 小红书:创作者笔记 / 关键词 监控 ──
    async def _scan_xhs_target_locked(self, target_id: int) -> dict:
        with get_session() as s:
            target = s.get(MonitorTarget, target_id)
            if not target:
                return {"ok": False, "error": "target not found"}
            first_scan = target.last_scan_at is None
            kind = target.target_kind
            user_id, keyword = target.sec_uid, target.keyword
            xsec_token = target.xsec_token or ""
            state = ""
            proxy = ""
            identity = None
            if target.account_id:
                acc = s.get(DouyinAccount, target.account_id)
                if acc:
                    if self._proxy_bad(acc):
                        return self._mark_target_skip(
                            target_id, "账号代理标记为不可用(proxy bad),已跳过以免暴露真实 IP")
                    state = acc.storage_state or ""
                    proxy = acc.proxy or ""
                    identity = (
                        self.browser.identity_for(acc)
                        if callable(getattr(self.browser, "identity_for", None))
                        else None
                    )
            if identity is None and not target.account_id:
                identity, proxy = self._anon_identity_proxy("xhs", session=s)
            known = set(s.exec(
                select(ContentRecord.aweme_id)
                .where(ContentRecord.target_id == target_id)).all())
            failed_access = {
                row.aweme_id: (row.id, row.xsec_token, row.xsec_source)
                for row in s.exec(select(ContentRecord).where(
                    ContentRecord.target_id == target_id,
                    ContentRecord.download_status == "failed")).all()
                if not _loads(row.media_json)
            }
            base_dir = target.download_dir or get_setting(
                "download_dir", self.cfg.engine.media_dir)
            auto_download = target.download_enabled
            media_filter = target.media_filter or "all"
            strategy = _monitor_strategy(
                target, default_scrolls=6, default_items=12)
            configured_max_items = max(
                0, int(target.max_items_per_scan or 0))

        detail_budget = int(strategy["max_items"] or 0)
        if detail_budget > 4 and not configured_max_items:
            detail_budget = random.randint(4, min(8, detail_budget))

        # 小红书签名直连需要登录态里的 a1 / web_session 等 Cookie
        cookie_str = cookie_str_from_state(state)
        if not state or not has_a1(cookie_str):
            msg = "小红书监控需要绑定一个已登录的小红书账号(登录态缺少 a1,请重新扫码登录)"
            with get_session() as s:
                t = s.get(MonitorTarget, target_id)
                if t:
                    t.last_scan_at = datetime.utcnow()
                    t.last_error = msg
                    s.add(t); s.commit()
            return {"ok": False, "new": 0, "error": msg}

        client = None
        browser_reads = self._xhs_browser_reads_enabled()
        if not browser_reads:
            client = XhsApiClient(
                cookie_str,
                self._direct_request_ua(identity),
                timeout=self.cfg.engine.request_timeout_seconds,
                proxy=proxy,
                storage_state=state,
            )
        error = ""
        author = None
        briefs_raw: list = []
        try:
            if browser_reads and kind == "keyword":
                briefs_raw, browser_error = await fetch_xhs_search(
                    self.browser, identity, keyword, known,
                    max_scrolls=strategy["max_scrolls"],
                    search_sort="latest",
                    block_media=self.cfg.engine.block_media_resources,
                    keep_context=True)
                if browser_error:
                    error = browser_error
            elif browser_reads:
                briefs_raw, author, browser_error = await fetch_xhs_notes(
                    self.browser, identity, user_id, known,
                    xsec_token=xsec_token, xsec_source="pc_feed",
                    max_scrolls=strategy["max_scrolls"],
                    block_media=self.cfg.engine.block_media_resources,
                    keep_context=True)
                if browser_error:
                    error = browser_error
            elif kind == "keyword":
                briefs_raw = await client.search_notes(
                    keyword, sort="time_descending")
            else:
                d = await client.notes_by_creator(user_id, xsec_token=xsec_token)
                briefs_raw = d.get("notes") or []
                try:
                    author = await client.user_info(user_id)
                except XhsApiError as e:
                    error = e
                    author = None
                except Exception as exc:
                    category, _signal = classify_platform_error(exc)
                    if category in {
                            RiskCategory.RISK, RiskCategory.AUTH,
                            RiskCategory.NETWORK}:
                        raise
                    author = None
        except XhsApiError as e:
            error = e
        except Exception as e:
            category, _signal = classify_platform_error(e)
            error = (e if category in {
                RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK
            } else f"小红书接口请求失败: {e!r}")

        # 逐条新笔记调 feed 接口拿完整媒体直链(单轮限量,避免请求过多被风控)
        new_records = []
        seen = set()
        detail_attempts = 0
        filtered_count = 0
        detail_failures = 0
        consecutive_detail_failures = 0
        first_detail_error = None
        access_updates = {}
        default_note_source = "pc_search" if kind == "keyword" else "pc_feed" if browser_reads else "pc_user"
        next_long_pause = random.randint(3, 5)
        for raw in briefs_raw:
            if error and classify_platform_error(error)[0] in {
                    RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK}:
                break
            brief = parse_note_brief(raw)
            if not brief or brief["note_id"] in seen:
                continue
            if brief["note_id"] in known:
                previous = failed_access.get(brief["note_id"])
                token = brief.get("xsec_token") or ""
                source = brief.get("xsec_source") or default_note_source
                if previous and token and (token, source) != previous[1:]:
                    access_updates[previous[0]] = (token, source)
                continue
            seen.add(brief["note_id"])
            if detail_budget and detail_attempts >= detail_budget:
                break
            detail_attempts += 1
            if seen and len(seen) > 1:
                if detail_attempts >= next_long_pause:
                    await self._xhs_gap(random.uniform(6.0, 11.0))
                    next_long_pause += random.randint(3, 6)
                else:
                    await self._xhs_gap()
            note_tok = brief.get("xsec_token", "")
            note_source = brief.get("xsec_source") or default_note_source
            derr = ""
            card = {}
            try:
                if browser_reads:
                    card, derr = await fetch_xhs_note_detail(
                        self.browser, identity, brief["note_id"],
                        xsec_token=note_tok,
                        xsec_source=note_source,
                        block_media=self.cfg.engine.block_media_resources,
                        keep_context=True)
                    card = card or {}
                    if derr and classify_platform_error(derr)[0] in {
                            RiskCategory.RISK, RiskCategory.AUTH,
                            RiskCategory.NETWORK}:
                        error = derr
                        break
                else:
                    card = await client.note_detail(
                        brief["note_id"], xsec_token=note_tok,
                        xsec_source=note_source)
            except XhsApiError as e:
                if e.category != "business" or not (
                        e.code == -510000 or e.signal in {"note_unavailable", "api_code_-510000"}):
                    error = e
                    break
                # A single inaccessible note is not an account-wide failure.
                # Keep its row/error and continue within the existing budget.
                derr = str(e)
                first_detail_error = first_detail_error or e
            except Exception as e:
                category, _signal = classify_platform_error(e)
                if category in {
                        RiskCategory.RISK, RiskCategory.AUTH,
                        RiskCategory.NETWORK}:
                    error = e
                    break
                derr = str(e)
            aw = parse_note_detail(card or {}, brief) if card else None
            if not aw:
                detail_failures += 1
                consecutive_detail_failures += 1
                first_detail_error = first_detail_error or XhsApiError(
                    derr or "笔记详情未取到媒体直链", signal="note_detail_unavailable")
                # 详情抓取失败也建一条 failed 记录,保留 xsec_token 便于重试
                aw = Aweme(aweme_id=brief["note_id"], desc=brief.get("title", ""),
                           create_time=int(brief.get("create_time") or 0), author_name="",
                           media_type="video" if brief.get("type") == "video" else "images")
                aw.platform = "xhs"
                aw.cover = brief.get("cover", "")
            else:
                consecutive_detail_failures = 0
            if aw.medias and not _monitor_content_matches(aw, strategy):
                filtered_count += 1
                continue
            should_download = bool(aw.medias) and auto_download and (
                media_filter == "all" or aw.media_type == media_filter)
            media_json = json.dumps([
                {
                    "url": m.url,
                    "kind": m.kind,
                    "ext": m.ext,
                    "index": m.index,
                    **({"backup_urls": m.backup_urls} if getattr(m, "backup_urls", None) else {}),
                }
                for m in aw.medias
            ]) if aw.medias else ""
            rec = ContentRecord(
                platform="xhs", target_id=target_id, aweme_id=aw.aweme_id, desc=aw.desc,
                media_type=aw.media_type, quality=aw.quality_label,
                create_time=aw.create_time, cover_url=aw.cover or "",
                like_count=aw.like_count, comment_count=aw.comment_count,
                duration=aw.duration, media_json=media_json, xsec_token=note_tok,
                xsec_source=note_source,
                download_status=("pending" if should_download
                                 else ("skipped" if aw.medias else "failed")),
                error="" if aw.medias else (derr or "未取到媒体直链"),
            )
            new_records.append((rec, aw, should_download))
            if consecutive_detail_failures >= 3:
                # Repeated unavailable results may indicate compatibility or
                # access-context trouble. Do not walk a whole list blindly.
                error = XhsApiError(
                    f"连续 3 条笔记详情未返回，已停止本轮请求。{first_detail_error}",
                    signal="note_detail_batch_unavailable")
                break

        if not error and first_detail_error:
            error = first_detail_error

        _scan_fn = log.warning if error else (log.info if new_records else log.debug)
        _scan_fn(
            "小红书监控扫描(target=%s, kind=%s): 候选=%d 新增=%d 详情=%d 过滤=%d%s",
            target_id,
            kind,
            len(briefs_raw),
            len(new_records),
            detail_attempts,
            filtered_count,
            f" 错误={error!r}" if error else "",
        )

        target_name = ""
        refreshed_access = 0
        with get_session() as s:
            for rec, _, _ in new_records:
                s.add(rec)
            for record_id, (token, source) in access_updates.items():
                rec = s.get(ContentRecord, record_id)
                if rec and rec.download_status == "failed" and not _loads(rec.media_json):
                    rec.xsec_token, rec.xsec_source = token, source
                    s.add(rec)
                    refreshed_access += 1
            t = s.get(MonitorTarget, target_id)
            if t:
                t.last_scan_at = datetime.utcnow()
                t.last_error = str(error or "")
                if author:  # 创作者资料(otherinfo)
                    p = parse_xhs_self_user(author)
                    if not t.nickname:
                        t.nickname = p.get("nickname") or t.nickname
                    if not t.avatar:
                        t.avatar = p.get("avatar") or t.avatar
                s.add(t)
                target_name = t.nickname or (("#" + keyword) if kind == "keyword"
                                             else (user_id[:12] if user_id else "xhs"))
                target_avatar = t.avatar or ""
            else:
                target_avatar = ""
            s.commit()
            for rec, _, _ in new_records:
                s.refresh(rec)

        thumb_cache.schedule_thumb_prewarm(
            [target_avatar, *(aw.cover for _, aw, dl in new_records if aw.cover and not dl)],
            platform="xhs",
            proxy=proxy,
        )

        captured = [aw for _, aw, _ in new_records if aw.medias]
        if captured and not first_scan:
            await self._notify_new(target_name, captured)

        await asyncio.gather(*(self._download(rec.id, aw, base_dir, proxy)
                               for rec, aw, should_download in new_records
                               if should_download))
        return {"ok": not error, "new": len(new_records), "error": error,
                "scanned": detail_attempts, "filtered": filtered_count,
                "captured": len(captured), "failed": detail_failures,
                "partial": bool(captured and error), "refreshed": refreshed_access}

    # ── 海外小红书(Rednote):创作者笔记 / 关键词 监控 ──
    async def _scan_rednote_target_locked(self, target_id: int) -> dict:
        with get_session() as s:
            target = s.get(MonitorTarget, target_id)
            if not target:
                return {"ok": False, "error": "target not found"}
            first_scan = target.last_scan_at is None
            kind = target.target_kind
            user_id, keyword = target.sec_uid, target.keyword
            xsec_token = target.xsec_token or ""
            state = ""
            proxy = ""
            identity = None
            if target.account_id:
                acc = s.get(DouyinAccount, target.account_id)
                if acc:
                    if self._proxy_bad(acc):
                        return self._mark_target_skip(
                            target_id, "账号代理标记为不可用(proxy bad),已跳过以免暴露真实 IP")
                    state = acc.storage_state or acc.creator_storage_state or ""
                    proxy = acc.proxy or ""
                    if callable(getattr(self.browser, "identity_for", None)):
                        identity = self.browser.identity_for(acc)
            if identity is None and not target.account_id:
                identity, proxy = self._anon_identity_proxy("rednote", session=s)
            known = set(s.exec(
                select(ContentRecord.aweme_id)
                .where(ContentRecord.target_id == target_id)).all())
            failed_access = {
                row.aweme_id: (row.id, row.xsec_token, row.xsec_source)
                for row in s.exec(select(ContentRecord).where(
                    ContentRecord.target_id == target_id,
                    ContentRecord.download_status == "failed")).all()
                if not _loads(row.media_json)
            }
            base_dir = target.download_dir or get_setting(
                "download_dir", self.cfg.engine.media_dir)
            auto_download = target.download_enabled
            media_filter = target.media_filter or "all"
            strategy = _monitor_strategy(
                target, default_scrolls=6, default_items=12)
            configured_max_items = max(0, int(target.max_items_per_scan or 0))

        detail_budget = int(strategy["max_items"] or 0)
        if detail_budget > 4 and not configured_max_items:
            detail_budget = random.randint(4, min(8, detail_budget))

        rmode = self._rednote_read_mode()
        cookie_str = rednote_cookie_str_from_state(state)
        if not state:
            msg = "海外小红书监控需要绑定一个已登录的海外小红书账号"
            with get_session() as s:
                t = s.get(MonitorTarget, target_id)
                if t:
                    t.last_scan_at = datetime.utcnow()
                    t.last_error = msg
                    s.add(t); s.commit()
            return {"ok": False, "new": 0, "error": msg}

        client = self._rednote_client(identity, state, proxy) if rmode in {"api", "hybrid"} else None
        if rmode == "api" and client is None:
            msg = "海外小红书直连模式需要登录态包含 a1 Cookie，请重新扫码登录或切换浏览器读取"
            with get_session() as s:
                t = s.get(MonitorTarget, target_id)
                if t:
                    t.last_scan_at = datetime.utcnow()
                    t.last_error = msg
                    s.add(t); s.commit()
            return {"ok": False, "new": 0, "error": msg}

        use_browser = rmode == "browser" or client is None
        error: object = ""
        author = None
        briefs_raw: list = []
        try:
            if not use_browser and client is not None:
                async with client.session_scope():
                    if kind == "keyword":
                        briefs_raw = await client.search_notes(keyword, sort="time_descending")
                    else:
                        d = await client.notes_by_creator(user_id, xsec_token=xsec_token)
                        briefs_raw = d.get("notes") or []
                        try:
                            author = await client.user_info(user_id)
                        except Exception:
                            author = None
            if use_browser or (rmode == "hybrid" and not briefs_raw):
                use_browser = True
                if kind == "keyword":
                    briefs_raw, browser_error = await fetch_rednote_search(
                        self.browser, identity, keyword, known,
                        max_scrolls=strategy["max_scrolls"],
                        search_sort="latest",
                        block_media=self.cfg.engine.block_media_resources,
                        keep_context=True)
                else:
                    briefs_raw, author, browser_error = await fetch_rednote_notes(
                        self.browser, identity, user_id, known,
                        xsec_token=xsec_token, xsec_source="pc_feed",
                        max_scrolls=strategy["max_scrolls"],
                        block_media=self.cfg.engine.block_media_resources,
                        ssr_fallback=True,
                        keep_context=True)
                if browser_error:
                    error = browser_error
        except RednoteApiError as e:
            if rmode == "hybrid" and e.category != "auth":
                use_browser = True
                if kind == "keyword":
                    briefs_raw, browser_error = await fetch_rednote_search(
                        self.browser, identity, keyword, known,
                        max_scrolls=strategy["max_scrolls"],
                        search_sort="latest",
                        block_media=self.cfg.engine.block_media_resources,
                        keep_context=True)
                else:
                    briefs_raw, author, browser_error = await fetch_rednote_notes(
                        self.browser, identity, user_id, known,
                        xsec_token=xsec_token, xsec_source="pc_feed",
                        max_scrolls=strategy["max_scrolls"],
                        block_media=self.cfg.engine.block_media_resources,
                        ssr_fallback=True,
                        keep_context=True)
                if browser_error:
                    error = browser_error
            else:
                error = e
        except Exception as e:
            category, _signal = classify_platform_error(e)
            error = (e if category in {
                RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK
            } else f"海外小红书请求失败: {e!r}")

        new_records = []
        seen = set()
        detail_attempts = 0
        filtered_count = 0
        detail_failures = 0
        consecutive_detail_failures = 0
        first_detail_error = None
        access_updates = {}
        default_note_source = "pc_search" if kind == "keyword" else ("pc_feed" if use_browser else "pc_user")
        next_long_pause = random.randint(3, 5)
        for raw in briefs_raw:
            if error and classify_platform_error(error)[0] in {
                    RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK}:
                break
            brief = parse_rednote_brief(raw)
            if not brief or brief["note_id"] in seen:
                continue
            if brief["note_id"] in known:
                previous = failed_access.get(brief["note_id"])
                token = brief.get("xsec_token") or ""
                source = brief.get("xsec_source") or default_note_source
                if previous and token and (token, source) != previous[1:]:
                    access_updates[previous[0]] = (token, source)
                continue
            seen.add(brief["note_id"])
            if detail_budget and detail_attempts >= detail_budget:
                break
            detail_attempts += 1
            if len(seen) > 1:
                if detail_attempts >= next_long_pause:
                    await self._xhs_gap(random.uniform(6.0, 11.0))
                    next_long_pause += random.randint(3, 6)
                else:
                    await self._xhs_gap()
            note_tok = brief.get("xsec_token", "")
            note_source = brief.get("xsec_source") or default_note_source
            derr = ""
            card = {}
            try:
                if rmode in {"api", "hybrid"} and client is not None:
                    try:
                        async with client.session_scope():
                            card = await client.note_detail(
                                brief["note_id"], xsec_token=note_tok,
                                xsec_source=note_source)
                    except RednoteApiError as api_det_err:
                        if rmode == "api" or api_det_err.category == "auth":
                            raise
                        if api_det_err.category == "business" and (
                            api_det_err.code == -510000
                            or api_det_err.signal in {"note_unavailable", "api_code_-510000"}
                        ):
                            raise
                        card = {}
                if not card and rmode in {"browser", "hybrid"}:
                    card, derr = await fetch_rednote_note_detail(
                        self.browser, identity, brief["note_id"],
                        xsec_token=note_tok,
                        xsec_source=note_source,
                        block_media=self.cfg.engine.block_media_resources,
                        keep_context=True)
                    card = card or {}
                    if derr and classify_platform_error(derr)[0] in {
                            RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK}:
                        error = derr
                        break
            except RednoteApiError as e:
                if e.category != "business" or not (
                        e.code == -510000 or e.signal in {"note_unavailable", "api_code_-510000"}):
                    error = e
                    break
                derr = str(e)
                first_detail_error = first_detail_error or e
            except Exception as e:
                category, _signal = classify_platform_error(e)
                if category in {RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK}:
                    error = e
                    break
                derr = str(e)
            aw = parse_rednote_detail(card or {}, brief) if card else None
            if not aw:
                detail_failures += 1
                consecutive_detail_failures += 1
                first_detail_error = first_detail_error or RednoteApiError(
                    derr or "海外小红书笔记详情未取到媒体直链", signal="note_detail_unavailable")
                aw = Aweme(aweme_id=brief["note_id"], desc=brief.get("title", ""),
                           create_time=int(brief.get("create_time") or 0), author_name="",
                           media_type="video" if brief.get("type") == "video" else "images")
                aw.platform = "rednote"
                aw.cover = brief.get("cover", "")
            else:
                consecutive_detail_failures = 0
            if aw.medias and not _monitor_content_matches(aw, strategy):
                filtered_count += 1
                continue
            should_download = bool(aw.medias) and auto_download and (
                media_filter == "all" or aw.media_type == media_filter)
            media_json = json.dumps([
                {
                    "url": m.url,
                    "kind": m.kind,
                    "ext": m.ext,
                    "index": m.index,
                    **({"backup_urls": m.backup_urls} if getattr(m, "backup_urls", None) else {}),
                }
                for m in aw.medias
            ]) if aw.medias else ""
            rec = ContentRecord(
                platform="rednote", target_id=target_id, aweme_id=aw.aweme_id, desc=aw.desc,
                media_type=aw.media_type, quality=aw.quality_label,
                create_time=aw.create_time, cover_url=aw.cover or "",
                like_count=aw.like_count, comment_count=aw.comment_count,
                duration=aw.duration, media_json=media_json, xsec_token=note_tok,
                xsec_source=note_source,
                download_status=("pending" if should_download
                                 else ("skipped" if aw.medias else "failed")),
                error="" if aw.medias else (derr or "未取到媒体直链"),
            )
            new_records.append((rec, aw, should_download))
            if consecutive_detail_failures >= 3:
                error = RednoteApiError(
                    f"连续 3 条海外小红书笔记详情未返回，已停止本轮请求。{first_detail_error}",
                    signal="note_detail_batch_unavailable")
                break

        if not error and first_detail_error:
            error = first_detail_error

        target_name = ""
        refreshed_access = 0
        with get_session() as s:
            for rec, _, _ in new_records:
                s.add(rec)
            for record_id, (token, source) in access_updates.items():
                rec = s.get(ContentRecord, record_id)
                if rec and rec.download_status == "failed" and not _loads(rec.media_json):
                    rec.xsec_token, rec.xsec_source = token, source
                    s.add(rec)
                    refreshed_access += 1
            t = s.get(MonitorTarget, target_id)
            if t:
                t.last_scan_at = datetime.utcnow()
                t.last_error = str(error or "")
                if author:
                    p = parse_rednote_self_user(author)
                    if not t.nickname:
                        t.nickname = p.get("nickname") or t.nickname
                    if not t.avatar:
                        t.avatar = p.get("avatar") or t.avatar
                s.add(t)
                target_name = t.nickname or (("#" + keyword) if kind == "keyword"
                                             else (user_id[:12] if user_id else "rednote"))
                target_avatar = t.avatar or ""
            else:
                target_avatar = ""
            s.commit()
            for rec, _, _ in new_records:
                s.refresh(rec)

        thumb_cache.schedule_thumb_prewarm(
            [target_avatar, *(aw.cover for _, aw, dl in new_records if aw.cover and not dl)],
            platform="rednote",
            proxy=proxy,
        )

        captured = [aw for _, aw, _ in new_records if aw.medias]
        if captured and not first_scan:
            await self._notify_new(target_name, captured)

        await asyncio.gather(*(self._download(
            rec.id, aw, base_dir, proxy, referer="https://www.rednote.com/"
        ) for rec, aw, should_download in new_records if should_download))
        return {"ok": not error, "new": len(new_records), "error": error,
                "scanned": detail_attempts, "filtered": filtered_count,
                "captured": len(captured), "failed": detail_failures,
                "partial": bool(captured and error), "refreshed": refreshed_access}
