"""弹幕监控周期扫描、单作品弹幕同步与入库通知。"""
from __future__ import annotations

from datetime import datetime
import time

from sqlmodel import select

from app.browser import fetch_creator_danmaku, fetch_danmaku, fetch_videos
from app.db import get_session
from app.engine.monitor_helpers import _danmaku_matches, _loads_list, log
from app.models import DanmakuRecord, DanmakuWatch, DouyinAccount
from app.platforms.bilibili import BilibiliClient
from app.platforms.douyin import DouyinClient, cookie_from_state as dy_cookie_from_state, parse_danmaku
from app.risk import OperationKind
from app.transport_matrix import douyin_client_environment, resolve_transport


class DanmakuWatchMixin:
    # ── 独立弹幕监控(DanmakuWatch)──
    async def _scan_danmaku_watches(self):
        due = []
        with get_session() as s:
            watches = s.exec(select(DanmakuWatch).where(
                DanmakuWatch.enabled == True)).all()  # noqa: E712
            for watch in watches:
                if self._periodic_due(watch, "danmaku"):
                    due.append(watch.id)
        for watch_id in due:
            await self.scan_danmaku_watch(watch_id)

    async def sync_work_danmaku(self, account_id: int, platform: str,
                                item_id: str) -> dict:
        """抓取本账号某条作品的弹幕，watch_id=0 表示账号管理入口。"""
        key = f"wd:{account_id}:{item_id}"
        if key in self._inflight:
            return {"ok": True, "fetched": 0, "added": 0, "skipped": "正在抓取中"}
        self._inflight.add(key)
        try:
            decision = self.risk.preflight(account_id, OperationKind.READ_HEAVY)
            if not decision.allowed:
                return {"ok": True, "fetched": 0, "added": 0,
                        "skipped": True, "reason": decision.reason,
                        "next_allowed_at": (decision.next_allowed_at.isoformat()
                                            if decision.next_allowed_at else None)}
            async with self._operation_guard(
                    account_id, OperationKind.READ_HEAVY, fallback_key=key):
                decision = self.risk.preflight(account_id, OperationKind.READ_HEAVY)
                if not decision.allowed:
                    return {"ok": True, "fetched": 0, "added": 0,
                            "skipped": True, "reason": decision.reason,
                            "next_allowed_at": (decision.next_allowed_at.isoformat()
                                                if decision.next_allowed_at else None)}
                with get_session() as s:
                    acc = s.get(DouyinAccount, account_id)
                    if not acc:
                        return {"ok": False, "error": "账号不存在"}
                    if platform == "bilibili":
                        bstate = acc.storage_state or acc.cookie or ""
                        bua = acc.ua or self.cfg.engine.user_agent
                        bproxy = acc.proxy or ""
                        known = set(s.exec(select(DanmakuRecord.danmaku_id).where(
                            DanmakuRecord.watch_id == 0,
                            DanmakuRecord.aweme_id == item_id)).all())
                    elif platform != "douyin":
                        return {"ok": False, "error": "当前仅支持抖音与哔哩哔哩弹幕"}
                    else:
                        transport = resolve_transport(
                            self.cfg, "douyin", "creator_danmaku", acc)
                        if transport["effective_mode"] == "unavailable":
                            return {"ok": False, "error": transport["reason"],
                                    "configured_mode": transport["configured_mode"]}
                        mode = transport["effective_mode"]
                        if mode == "browser" and not acc.creator_storage_state:
                            return {"ok": False, "error": "需要先完成抖音创作者登录"}
                        state = acc.storage_state or acc.creator_storage_state or ""
                        identity = (None if mode == "api"
                                    else self.browser.identity_for(acc))
                        ua = acc.ua or self.cfg.engine.user_agent
                        proxy = acc.proxy or ""
                        direct_environment = douyin_client_environment(acc)
                        known = set(s.exec(select(DanmakuRecord.danmaku_id).where(
                            DanmakuRecord.watch_id == 0,
                            DanmakuRecord.aweme_id == item_id)).all())
                if platform == "bilibili":
                    bcli = BilibiliClient(
                        bstate, bua, proxy=bproxy, timeout=self.cfg.engine.request_timeout_seconds
                    )
                    async with bcli.session_scope():
                        fresh, err = await bcli.fetch_danmaku(item_id)
                    added = 0
                    with get_session() as s:
                        for item in fresh:
                            did = item.get("danmaku_id") or ""
                            if not did or did in known:
                                continue
                            known.add(did)
                            s.add(DanmakuRecord(
                                platform="bilibili",
                                watch_id=0,
                                aweme_id=item_id,
                                source="public",
                                **{k: v for k, v in item.items() if k != "aweme_id"},
                            ))
                            added += 1
                        s.commit()
                    result = {
                        "ok": bool(added or not err),
                        "fetched": len(fresh),
                        "added": added,
                        "error": err,
                        "source": "api",
                        "configured_mode": str(getattr(self.cfg.engine, "bilibili_read_mode", "hybrid")),
                    }
                    if result["ok"]:
                        self.risk.record_success(account_id, OperationKind.READ_HEAVY)
                    elif err:
                        self.risk.record_failure(account_id, OperationKind.READ_HEAVY, err)
                    return result
                raw, err = [], ""
                source = "api" if mode == "api" else "browser"
                if mode in {"api", "hybrid"}:
                    cookie = dy_cookie_from_state(state)
                    if not cookie:
                        err = "api_missing_cookie"
                    else:
                        client = DouyinClient(
                            cookie, ua,
                            timeout=self.cfg.engine.request_timeout_seconds,
                            proxy=proxy, **direct_environment)
                        try:
                            async with client.session_scope():
                                raw = await client.fetch_all_danmaku(item_id)
                            err = client.last_error or ""
                        except Exception as exc:
                            err = f"douyin_api_danmaku:{type(exc).__name__}"
                    if raw or not err:
                        source = "api"
                if mode == "browser" or (mode == "hybrid" and err):
                    if not acc.creator_storage_state:
                        return {"ok": False,
                                "error": "API 读取失败且未完成抖音创作者登录，无法回退浏览器",
                                "source": "api", "configured_mode": mode}
                    raw, browser_error = await fetch_creator_danmaku(
                        self.browser, identity, known,
                        page_url=self.cfg.engine.creator_danmaku_url,
                        aweme_id=item_id,
                        max_scrolls=self.cfg.engine.danmaku_max_scrolls,
                        block_media=self.cfg.engine.block_media_resources,
                    )
                    source = "browser_fallback" if mode == "hybrid" else "browser"
                    err = browser_error or err
                fresh = [p for p in (parse_danmaku(row, item_id) for row in raw) if p]
                added = 0
                with get_session() as s:
                    for item in fresh:
                        did = item.get("danmaku_id") or ""
                        if not did:
                            continue
                        exists = s.exec(select(DanmakuRecord).where(
                            DanmakuRecord.watch_id == 0,
                            DanmakuRecord.aweme_id == item_id,
                            DanmakuRecord.danmaku_id == did)).first()
                        if exists:
                            continue
                        s.add(DanmakuRecord(platform=platform, watch_id=0,
                                            aweme_id=item_id, source="creator",
                                            **{k: v for k, v in item.items()
                                               if k != "aweme_id"}))
                        added += 1
                    s.commit()
                result = {"ok": bool(added or not err), "fetched": len(fresh),
                          "added": added, "error": err, "source": source,
                          "configured_mode": transport["configured_mode"]}
                if result["ok"]:
                    self.risk.record_success(account_id, OperationKind.READ_HEAVY)
                elif err:
                    self.risk.record_failure(
                        account_id, OperationKind.READ_HEAVY, err)
                return result
        except Exception as e:
            log.warning("本账号作品弹幕抓取失败 %s/%s: %s", platform, item_id, e)
            self.risk.record_failure(account_id, OperationKind.READ_HEAVY, e)
            return {"ok": False, "fetched": 0, "added": 0, "error": repr(e)}
        finally:
            self._inflight.discard(key)

    async def scan_danmaku_watch(self, watch_id: int) -> dict:
        key = f"dw:{watch_id}"
        if key in self._inflight:
            return {"ok": True, "new_danmaku": 0, "skipped": "正在抓取中"}
        self._inflight.add(key)
        try:
            with get_session() as s:
                watch = s.get(DanmakuWatch, watch_id)
                account_id = watch.account_id if watch else None
            return await self._guarded_read_dict(
                account_id, OperationKind.READ_HEAVY, key,
                lambda: self._scan_danmaku_watch_locked(watch_id))
        finally:
            self._inflight.discard(key)

    async def _scan_danmaku_watch_locked(self, watch_id: int) -> dict:
        with get_session() as s:
            watch = s.get(DanmakuWatch, watch_id)
            if not watch:
                return {"ok": False, "error": "watch not found"}
            if watch.platform == "bilibili":
                return await self._scan_bilibili_danmaku_watch_locked(watch_id)
            first_scan = watch.last_scan_at is None
            kind, mode = watch.kind, watch.mode
            aweme_id, sec_uid = watch.aweme_id, watch.sec_uid
            name = watch.title or aweme_id or (sec_uid[:12] if sec_uid else "watch")
            identity = None
            direct_environment_source = None
            has_creator = False
            creator_transport = None
            state = ""
            ua = self.cfg.engine.user_agent
            proxy = ""
            if watch.account_id:
                acc = s.get(DouyinAccount, watch.account_id)
                if acc:
                    if self._proxy_bad(acc):
                        msg = "账号代理标记为不可用(proxy bad),已跳过"
                        watch.last_scan_at = datetime.utcnow()
                        watch.last_error = msg
                        s.add(watch)
                        s.commit()
                        return {"ok": False, "new_danmaku": 0, "error": msg, "skipped": True}
                    has_creator = bool(acc.creator_storage_state)
                    state = acc.storage_state or acc.creator_storage_state or ""
                    ua = acc.ua or self.cfg.engine.user_agent
                    proxy = acc.proxy or ""
                    direct_environment_source = acc
                    account_mode = (resolve_transport(
                        self.cfg, "douyin", "creator_danmaku", acc)["effective_mode"]
                        if mode == "creator" else self.cfg.engine.douyin_read_mode)
                    if account_mode != "api":
                        identity = (
                        self.browser.identity_for(acc)
                        if callable(getattr(self.browser, "identity_for", None))
                        else None
                    )
            if identity is None and not watch.account_id:
                identity = (
                self.browser.anon_identity()
                if callable(getattr(self.browser, "anon_identity", None))
                else None
            )

        if mode == "creator" and watch.account_id:
            creator_transport = resolve_transport(
                self.cfg, "douyin", "creator_danmaku", acc)
            if creator_transport["effective_mode"] == "unavailable":
                msg = creator_transport["reason"] or "当前创作中心弹幕通道不可用"
                with get_session() as s:
                    watch = s.get(DanmakuWatch, watch_id)
                    if watch:
                        watch.last_scan_at = datetime.utcnow()
                        watch.last_error = msg
                        s.add(watch)
                        s.commit()
                return {"ok": False, "new_danmaku": 0, "error": msg,
                        "configured_mode": creator_transport["configured_mode"]}

        if (mode == "creator" and creator_transport
                and creator_transport["effective_mode"] == "browser"
                and not has_creator):
            msg = "创作中心弹幕监控需要绑定已完成创作者登录的抖音账号"
            with get_session() as s:
                watch = s.get(DanmakuWatch, watch_id)
                if watch:
                    watch.last_scan_at = datetime.utcnow()
                    watch.last_error = msg
                    s.add(watch)
                    s.commit()
            return {"ok": False, "new_danmaku": 0, "error": msg}

        error = ""
        total_new = 0
        api_client = None
        direct_mode = (creator_transport["effective_mode"]
                       if mode == "creator" and creator_transport
                       else self.cfg.engine.douyin_read_mode)
        if direct_mode in {"api", "hybrid"}:
            cookie = dy_cookie_from_state(state)
            if cookie:
                api_client = DouyinClient(
                    cookie, ua,
                    timeout=self.cfg.engine.request_timeout_seconds,
                    proxy=proxy,
                    **douyin_client_environment(direct_environment_source or identity))
        try:
            settings = {
                "recent_works": watch.recent_works or self.cfg.engine.danmaku_recent_works,
                "recent_days": watch.recent_days or self.cfg.engine.danmaku_recent_days,
                "max_scrolls": watch.max_scrolls or self.cfg.engine.danmaku_max_scrolls,
                "time_start_ms": max(0, watch.time_start_ms or 0),
                "time_end_ms": max(0, watch.time_end_ms or 0),
                "probe_step_seconds": watch.probe_step_seconds or self.cfg.engine.danmaku_probe_step_seconds,
                "max_probe_points": max(1, self.cfg.engine.danmaku_max_probe_points),
                "include_keywords": [str(x).strip() for x in _loads_list(watch.include_keywords) if str(x).strip()],
                "exclude_keywords": [str(x).strip() for x in _loads_list(watch.exclude_keywords) if str(x).strip()],
                "min_text_length": max(0, watch.min_text_length or 0),
                "max_text_length": max(0, watch.max_text_length or 0),
                "min_like_count": max(0, watch.min_like_count or 0),
                "max_records_per_scan": watch.max_records_per_scan or self.cfg.engine.danmaku_max_records_per_scan,
                "max_records_total": watch.max_records_total or self.cfg.engine.danmaku_max_records_total,
            }
            remaining = [settings["max_records_per_scan"] if settings["max_records_per_scan"] > 0 else None]

            def normalize_rows(raw_rows: list, default_id: str = "") -> list:
                parsed = [parse_danmaku(row, default_id)
                          for row in raw_rows if isinstance(row, dict)]
                parsed = [row for row in parsed if row and _danmaku_matches(row, settings)]
                parsed.sort(key=lambda row: (int(row.get("video_time_ms") or 0),
                                             str(row.get("danmaku_id") or "")))
                if remaining[0] is not None:
                    parsed = parsed[:remaining[0]]
                    remaining[0] -= len(parsed)
                return parsed

            raw_cap = (settings["max_records_per_scan"] * 5
                       if settings["max_records_per_scan"] else 0)
            source = "creator" if mode == "creator" else "public"
            if kind == "video":
                with get_session() as s:
                    known = set(s.exec(select(DanmakuRecord.danmaku_id).where(
                        DanmakuRecord.watch_id == watch_id,
                        DanmakuRecord.aweme_id == aweme_id)).all())
                if mode == "creator":
                    raw, error = [], ""
                    if api_client is not None:
                        try:
                            async with api_client.session_scope():
                                raw = await api_client.fetch_all_danmaku(
                                    aweme_id,
                                    start_time=settings["time_start_ms"],
                                    end_time=settings["time_end_ms"],
                                    max_pages=max(1, min(settings["max_scrolls"], 10)))
                            error = api_client.last_error or ""
                        except Exception as exc:
                            error = f"douyin_api_danmaku:{type(exc).__name__}"
                    if direct_mode == "browser" or (direct_mode == "hybrid" and (
                            error or api_client is None)):
                        if not has_creator:
                            error = (error + "; " if error else "") \
                                + "未完成创作者登录，无法回退浏览器"
                        else:
                            raw, browser_error = await fetch_creator_danmaku(
                                self.browser, identity, known,
                                page_url=self.cfg.engine.creator_danmaku_url,
                                aweme_id=aweme_id,
                                max_scrolls=settings["max_scrolls"],
                                max_items=raw_cap,
                                block_media=self.cfg.engine.block_media_resources,
                            )
                            error = browser_error or error
                    elif api_client is None and direct_mode == "api":
                        error = "douyin_api_danmaku:no_cookie"
                elif api_client is not None:
                    try:
                        async with api_client.session_scope():
                            raw = await api_client.fetch_all_danmaku(
                                aweme_id,
                                start_time=settings["time_start_ms"],
                                end_time=settings["time_end_ms"],
                                max_pages=max(1, min(settings["max_scrolls"], 10)))
                        if raw or not api_client.last_error:
                            error = ""
                        elif self.cfg.engine.douyin_read_mode == "api":
                            error = f"douyin_api_danmaku:{api_client.last_error or 'empty_response'}"
                        else:
                            print(f"[dy-danmaku-watch] API 空响应({api_client.last_error}),回退浏览器")
                            raw, error = await fetch_danmaku(
                                self.browser, identity, aweme_id, known,
                                max_rounds=max(1, min(settings["max_scrolls"], 2)),
                                start_ms=settings["time_start_ms"],
                                end_ms=settings["time_end_ms"],
                                step_seconds=settings["probe_step_seconds"],
                                max_points=settings["max_probe_points"],
                                max_items=raw_cap,
                                block_media=False)
                    except Exception as exc:
                        if self.cfg.engine.douyin_read_mode == "api":
                            raw, error = [], f"douyin_api_danmaku:{type(exc).__name__}"
                        else:
                            print(f"[dy-danmaku-watch] API 异常({type(exc).__name__}),回退浏览器")
                            raw, error = await fetch_danmaku(
                                self.browser, identity, aweme_id, known,
                                max_rounds=max(1, min(settings["max_scrolls"], 2)),
                                start_ms=settings["time_start_ms"],
                                end_ms=settings["time_end_ms"],
                                step_seconds=settings["probe_step_seconds"],
                                max_points=settings["max_probe_points"],
                                max_items=raw_cap,
                                block_media=False)
                elif self.cfg.engine.douyin_read_mode == "api":
                    raw, error = [], "douyin_api_danmaku:no_cookie"
                else:
                    raw, error = await fetch_danmaku(
                        self.browser, identity, aweme_id, known,
                        max_rounds=max(1, min(settings["max_scrolls"], 2)),
                        start_ms=settings["time_start_ms"],
                        end_ms=settings["time_end_ms"],
                        step_seconds=settings["probe_step_seconds"],
                        max_points=settings["max_probe_points"],
                        max_items=raw_cap,
                        block_media=False,
                    )
                fresh = normalize_rows(raw, aweme_id)
                fresh = [row for row in fresh
                         if row.get("danmaku_id") not in known]
                total_new = await self._ingest_danmaku(
                    watch_id, aweme_id, fresh, name, name, first_scan, source,
                    max_records_total=settings["max_records_total"])
            elif mode == "creator":
                with get_session() as s:
                    known = set(s.exec(select(DanmakuRecord.danmaku_id).where(
                        DanmakuRecord.watch_id == watch_id)).all())
                raw, error = [], ""
                if api_client is not None:
                    try:
                        async with api_client.session_scope():
                            works = await api_client.fetch_all_video_list(sec_uid)
                            for item in works[:settings["recent_works"]]:
                                aid = str(item.get("aweme_id") or "")
                                if not aid:
                                    continue
                                raw.extend(await api_client.fetch_all_danmaku(
                                    aid,
                                    start_time=settings["time_start_ms"],
                                    end_time=settings["time_end_ms"],
                                    max_pages=max(1, min(settings["max_scrolls"], 10))))
                        error = api_client.last_error or ""
                    except Exception as exc:
                        error = f"douyin_api_danmaku:{type(exc).__name__}"
                if direct_mode == "browser" or (direct_mode == "hybrid" and (
                        error or api_client is None)):
                    if not has_creator:
                        error = (error + "; " if error else "") \
                            + "未完成创作者登录，无法回退浏览器"
                    else:
                        raw, browser_error = await fetch_creator_danmaku(
                            self.browser, identity, known,
                            page_url=self.cfg.engine.creator_danmaku_url,
                            max_scrolls=settings["max_scrolls"],
                            max_items=raw_cap,
                            block_media=self.cfg.engine.block_media_resources,
                        )
                        error = browser_error or error
                elif api_client is None and direct_mode == "api":
                    error = "douyin_api_danmaku:no_cookie"
                grouped = {}
                for parsed in normalize_rows(raw):
                    if parsed and parsed.get("aweme_id"):
                        grouped.setdefault(parsed["aweme_id"], []).append(parsed)
                for aid, fresh in grouped.items():
                    total_new += await self._ingest_danmaku(
                        watch_id, aid, fresh, name, aid, first_scan, source,
                        max_records_total=settings["max_records_total"])
            else:
                items, _author, error = [], None, ""
                if api_client is not None:
                    try:
                        async with api_client.session_scope():
                            items = await api_client.fetch_all_video_list(sec_uid)
                            _author = await api_client.fetch_profile(sec_uid)
                        if not items and api_client.last_error:
                            error = api_client.last_error
                    except Exception as exc:
                        error = f"douyin_api_works:{type(exc).__name__}"
                if not items and self.cfg.engine.douyin_read_mode != "api":
                    items, _author, browser_error = await fetch_videos(
                        self.browser, identity, sec_uid, set(),
                        max_scrolls=4, block_media=True)
                    error = browser_error or error
                elif not items and self.cfg.engine.douyin_read_mode == "api" and not error:
                    error = "douyin_api_works:empty_response"
                cutoff = int(time.time()) - settings["recent_days"] * 86400
                works = []
                for item in items:
                    aid = str(item.get("aweme_id") or "")
                    create_time = int(item.get("create_time") or 0)
                    if aid and (not cutoff or not create_time or create_time >= cutoff):
                        works.append((aid, item.get("desc") or ""))
                for aid, desc in works[:settings["recent_works"]]:
                    if remaining[0] is not None and remaining[0] <= 0:
                        break
                    with get_session() as s:
                        known = set(s.exec(select(DanmakuRecord.danmaku_id).where(
                            DanmakuRecord.watch_id == watch_id,
                            DanmakuRecord.aweme_id == aid)).all())
                    raw, item_error = [], ""
                    if api_client is not None:
                        try:
                            async with api_client.session_scope():
                                raw = await api_client.fetch_all_danmaku(
                                    aid,
                                    start_time=settings["time_start_ms"],
                                    end_time=settings["time_end_ms"],
                                    max_pages=max(1, min(settings["max_scrolls"], 10)))
                            if not raw and api_client.last_error:
                                item_error = api_client.last_error
                        except Exception as exc:
                            item_error = f"douyin_api_danmaku:{type(exc).__name__}"
                    if not raw and self.cfg.engine.douyin_read_mode != "api":
                        raw, browser_error = await fetch_danmaku(
                            self.browser, identity, aid, known,
                            max_rounds=max(1, min(settings["max_scrolls"], 2)),
                            start_ms=settings["time_start_ms"],
                            end_ms=settings["time_end_ms"],
                            step_seconds=settings["probe_step_seconds"],
                            max_points=settings["max_probe_points"],
                            max_items=raw_cap,
                            block_media=False)
                        item_error = browser_error or item_error
                    elif not raw and not item_error and self.cfg.engine.douyin_read_mode == "api":
                        item_error = "douyin_api_danmaku:empty_response"
                    if item_error and not error:
                        error = item_error
                    fresh = normalize_rows(raw, aid)
                    fresh = [row for row in fresh
                             if row.get("danmaku_id") not in known]
                    total_new += await self._ingest_danmaku(
                        watch_id, aid, fresh, name, desc, first_scan, source,
                        max_records_total=settings["max_records_total"])
        except Exception as e:
            error = repr(e)
            log.warning("弹幕监控 %s 失败: %s", watch_id, e)

        with get_session() as s:
            watch = s.get(DanmakuWatch, watch_id)
            if watch:
                watch.last_scan_at = datetime.utcnow()
                watch.last_error = error
                watch.danmaku_count = len(s.exec(select(DanmakuRecord.id).where(
                    DanmakuRecord.watch_id == watch_id)).all())
                s.add(watch)
                s.commit()
        return {"ok": not error or total_new > 0,
                "new_danmaku": total_new, "error": error}

    async def _scan_bilibili_danmaku_watch_locked(self, watch_id: int) -> dict:
        with get_session() as s:
            watch = s.get(DanmakuWatch, watch_id)
            if not watch:
                return {"ok": False, "error": "watch not found"}
            first_scan = watch.last_scan_at is None
            kind = watch.kind
            aweme_id, sec_uid = watch.aweme_id, watch.sec_uid
            name = watch.title or aweme_id or (sec_uid[:12] if sec_uid else "watch")
            state = ""
            ua = self.cfg.engine.user_agent
            proxy = ""
            if watch.account_id:
                acc = s.get(DouyinAccount, watch.account_id)
                if acc:
                    if self._proxy_bad(acc):
                        msg = "账号代理标记为不可用(proxy bad),已跳过"
                        watch.last_scan_at = datetime.utcnow()
                        watch.last_error = msg
                        s.add(watch)
                        s.commit()
                        return {"ok": False, "new_danmaku": 0, "error": msg, "skipped": True}
                    state = acc.storage_state or acc.cookie or ""
                    ua = acc.ua or self.cfg.engine.user_agent
                    proxy = acc.proxy or ""
            settings = {
                "recent_works": watch.recent_works or self.cfg.engine.danmaku_recent_works,
                "recent_days": watch.recent_days or self.cfg.engine.danmaku_recent_days,
                "time_start_ms": max(0, watch.time_start_ms or 0),
                "time_end_ms": max(0, watch.time_end_ms or 0),
                "include_keywords": [str(x).strip() for x in _loads_list(watch.include_keywords) if str(x).strip()],
                "exclude_keywords": [str(x).strip() for x in _loads_list(watch.exclude_keywords) if str(x).strip()],
                "min_text_length": max(0, watch.min_text_length or 0),
                "max_text_length": max(0, watch.max_text_length or 0),
                "min_like_count": max(0, watch.min_like_count or 0),
                "max_records_per_scan": watch.max_records_per_scan or self.cfg.engine.danmaku_max_records_per_scan,
                "max_records_total": watch.max_records_total or self.cfg.engine.danmaku_max_records_total,
            }

        bcli = BilibiliClient(
            state, ua, proxy=proxy, timeout=self.cfg.engine.request_timeout_seconds
        )
        total_new = 0
        error = ""
        remaining = settings["max_records_per_scan"] if settings["max_records_per_scan"] > 0 else None

        try:
            async with bcli.session_scope():
                target_videos: list[tuple[str, str]] = []
                if kind == "video":
                    target_videos.append((aweme_id, name))
                else:
                    vlist, _, verr = await bcli.fetch_user_videos(sec_uid, pn=1, ps=30)
                    if verr:
                        error = verr
                    cutoff = int(time.time()) - settings["recent_days"] * 86400
                    for v in vlist:
                        bv = str(v.get("bvid") or "")
                        ct = int(v.get("created") or v.get("pubdate") or 0)
                        if bv and (not cutoff or not ct or ct >= cutoff):
                            target_videos.append((bv, str(v.get("title") or "")))
                        if len(target_videos) >= settings["recent_works"]:
                            break

                for bv, vtitle in target_videos:
                    if remaining is not None and remaining <= 0:
                        break
                    with get_session() as s:
                        known = set(
                            s.exec(
                                select(DanmakuRecord.danmaku_id).where(
                                    DanmakuRecord.watch_id == watch_id,
                                    DanmakuRecord.aweme_id == bv,
                                )
                            ).all()
                        )
                    rows, derr = await bcli.fetch_danmaku(bv)
                    if derr and not error:
                        error = derr
                    filtered = [
                        r for r in rows
                        if r.get("danmaku_id") not in known and _danmaku_matches(r, settings)
                    ]
                    if remaining is not None:
                        filtered = filtered[:remaining]
                        remaining -= len(filtered)
                    total_new += await self._ingest_danmaku(
                        watch_id,
                        bv,
                        filtered,
                        name,
                        vtitle or bv,
                        first_scan,
                        "public",
                        max_records_total=settings["max_records_total"],
                        platform="bilibili",
                    )
        except Exception as exc:
            error = repr(exc)

        with get_session() as s:
            watch = s.get(DanmakuWatch, watch_id)
            if watch:
                watch.last_scan_at = datetime.utcnow()
                watch.last_error = error
                watch.danmaku_count = len(
                    s.exec(select(DanmakuRecord.id).where(DanmakuRecord.watch_id == watch_id)).all()
                )
                s.add(watch)
                s.commit()
        return {"ok": not error or total_new > 0, "new_danmaku": total_new, "error": error}

    async def _ingest_danmaku(self, watch_id: int, aweme_id: str, fresh: list,
                              name: str, work_desc: str, first_scan: bool,
                              source: str = "public",
                              max_records_total: int = 0,
                              platform: str = "douyin") -> int:
        if not fresh and max_records_total <= 0:
            return 0
        added = []
        with get_session() as s:
            for item in fresh:
                aid = item.get("aweme_id") or aweme_id
                did = item.get("danmaku_id") or ""
                if not aid or not did:
                    continue
                exists = s.exec(select(DanmakuRecord).where(
                    DanmakuRecord.watch_id == watch_id,
                    DanmakuRecord.aweme_id == aid,
                    DanmakuRecord.danmaku_id == did)).first()
                if exists:
                    continue
                row = dict(item)
                row.pop("aweme_id", None)
                s.add(DanmakuRecord(platform=platform, watch_id=watch_id,
                                    aweme_id=aid, source=source, **row))
                added.append(dict(item, aweme_id=aid))
            if max_records_total > 0:
                old_ids = s.exec(select(DanmakuRecord.id).where(
                    DanmakuRecord.watch_id == watch_id).order_by(
                        DanmakuRecord.created_at.desc(),
                        DanmakuRecord.id.desc()).offset(max_records_total)).all()
                for old_id in old_ids:
                    old = s.get(DanmakuRecord, old_id)
                    if old:
                        s.delete(old)
            s.commit()
        if not first_scan and added:
            await self._notify_danmaku(name, work_desc, added)
        return len(added)
