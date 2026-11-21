"""作品监控周期判断与抖音、快手、TikTok、视频号目标扫描。"""
from __future__ import annotations

from datetime import datetime
import asyncio
import json

from sqlmodel import select

from app.browser import fetch_channels_works, fetch_ks_videos, fetch_videos
from app.browser.tiktok_fetcher import fetch_tiktok_profile_works
from app.db import get_session
from app.engine.cadence import periodic_deadline, row_deadline
from app.engine.monitor_helpers import _douyin_scan_since, _monitor_content_matches, _monitor_strategy, _round_robin_by_account, _select_channels_awemes, _select_douyin_awemes, _select_ks_awemes, _select_tiktok_awemes, _tiktok_download_session
from app.models import ContentRecord, DouyinAccount, MonitorTarget
from app.platforms.channels import ChannelsProtocolClient, parse_self_user as parse_channels_self_user
from app.platforms.kuaishou import parse_self_user as parse_ks_self_user
from app.platforms.tiktok import tiktok_display_text
from app.platforms.tiktok.urls import profile_url as tiktok_profile_url
from app.risk import OperationKind
from app.settings import get_setting


class TargetScanMixin:
    def _due(self, last_scan_at, interval_seconds, *, key: str = "") -> bool:
        deadline = periodic_deadline(last_scan_at, interval_seconds, key=key,
                                     jitter=self.cfg.engine.scan_jitter)
        return deadline is None or datetime.utcnow() >= deadline

    def _periodic_due(self, row, kind: str) -> bool:
        deadline = row_deadline(row, self.cfg, kind=kind)
        return row.enabled and (deadline is None or datetime.utcnow() >= deadline)

    async def _scan_once(self):
        due: list[tuple[int, int | None]] = []
        with get_session() as s:
            targets = s.exec(select(MonitorTarget).where(MonitorTarget.enabled == True)).all()  # noqa: E712
            for t in targets:
                if self._periodic_due(t, "monitor"):
                    due.append((t.id, t.account_id))
        if due:
            ordered = _round_robin_by_account(due)
            await asyncio.gather(*(self.scan_target(tid) for tid, _ in ordered))

    async def scan_target(self, target_id: int) -> dict:
        if target_id in self._inflight:
            return {"ok": True, "new": 0, "skipped": "正在抓取中"}
        self._inflight.add(target_id)
        try:
            with get_session() as s:
                t = s.get(MonitorTarget, target_id)
                account_id = t.account_id if t else None
            decision = self.risk.preflight(account_id, OperationKind.READ_LIGHT)
            if not decision.allowed:
                return {
                    "ok": True,
                    "new": 0,
                    "skipped": True,
                    "reason": decision.reason,
                    "next_allowed_at": (
                        decision.next_allowed_at.isoformat()
                        if decision.next_allowed_at else None),
                }
            async with self._operation_guard(
                    account_id, OperationKind.READ_LIGHT,
                    fallback_key=f"tgt:{target_id}"):
                decision = self.risk.preflight(account_id, OperationKind.READ_LIGHT)
                if not decision.allowed:
                    return {
                        "ok": True, "new": 0, "skipped": True,
                        "reason": decision.reason,
                        "next_allowed_at": (
                            decision.next_allowed_at.isoformat()
                            if decision.next_allowed_at else None),
                    }
                res = await self._scan_target_locked(target_id)
                error = res.get("error")
                if account_id and res.get("ok") and not res.get("skipped"):
                    self.risk.record_success(account_id, OperationKind.READ_LIGHT)
                    self._stamp_active(account_id)
                elif account_id and error:
                    self.risk.record_failure(
                        account_id, OperationKind.READ_LIGHT, error)
                if isinstance(error, BaseException):
                    res = dict(res)
                    res["error"] = str(error)
            # 用该账号成功抓取过=登录态被有效使用,顺带续期,免得再被闲置保活重复摸
            return res
        finally:
            self._inflight.discard(target_id)

    async def _scan_target_locked(self, target_id: int) -> dict:
        with get_session() as s:
            t0 = s.get(MonitorTarget, target_id)
            if not t0:
                return {"ok": False, "error": "target not found"}
            platform = t0.platform
        if platform == "xhs":
            return await self._scan_xhs_target_locked(target_id)
        if platform == "rednote":
            return await self._scan_rednote_target_locked(target_id)
        if platform == "kuaishou":
            return await self._scan_ks_target_locked(target_id)
        if platform == "tiktok":
            return await self._scan_tiktok_target_locked(target_id)
        if platform == "shipinhao":
            return await self._scan_channels_target_locked(target_id)
        if platform == "bilibili":
            return await self._scan_bilibili_target_locked(target_id)
        if platform == "instagram":
            return await self._scan_instagram_target_locked(target_id)
        if platform == "twitter":
            return await self._scan_twitter_target_locked(target_id)
        with get_session() as s:
            target = s.get(MonitorTarget, target_id)
            if not target:
                return {"ok": False, "error": "target not found"}
            first_scan = target.last_scan_at is None   # 首扫建立时间基线，可按目标配置回填历史
            if not target.account_id:
                return self._mark_target_skip(
                    target_id, "抖音作品监控必须绑定已登录账号,匿名主页可能返回陈旧或残缺作品")
            acc = s.get(DouyinAccount, target.account_id)
            if not acc or acc.platform != "douyin" or acc.status != "active":
                return self._mark_target_skip(
                    target_id, "绑定的抖音账号不存在或登录态已失效,请重新绑定/登录")
            if self._proxy_bad(acc):
                return self._mark_target_skip(
                    target_id, "账号代理标记为不可用(proxy bad),已跳过以免暴露真实 IP")
            identity, proxy = self._identity_proxy(acc)
            # 只取 aweme_id 列,避免把整行作品都加载进内存
            known = set(s.exec(
                select(ContentRecord.aweme_id)
                .where(ContentRecord.target_id == target_id)).all())
            known_create_times = list(s.exec(
                select(ContentRecord.create_time)
                .where(ContentRecord.target_id == target_id)).all())
            sec_uid = target.sec_uid
            # 有效下载目录:目标自定义 > 全局默认 > 配置兜底
            base_dir = target.download_dir or get_setting(
                "download_dir", self.cfg.engine.media_dir)
            # 有效画质:目标自定义 > 全局默认 > highest
            quality = target.video_quality or get_setting("video_quality", "highest")
            monitor_since = int(target.created_at.timestamp())
            scan_since = _douyin_scan_since(monitor_since, known_create_times)
            backfill_count = target.initial_backfill_count
            should_backfill = first_scan or (backfill_count != 0 and not known)
            auto_download = target.download_enabled
            media_filter = target.media_filter or "all"
            strategy = _monitor_strategy(
                target, default_scrolls=12, default_items=0)

        items, author, error = await fetch_videos(
            self.browser, identity, sec_uid, known,
            max_scrolls=strategy["max_scrolls"],
            block_media=self.cfg.engine.block_media_resources,
            # 默认只监控订阅后的作品；显式首次回填时允许继续向历史翻页。
            stop_before=(0 if should_backfill and backfill_count != 0 else scan_since))

        new_records = []
        selected = _select_douyin_awemes(
            items, quality, should_backfill, scan_since, backfill_count)
        filtered_count = 0
        for aw in selected:
            if not _monitor_content_matches(aw, strategy):
                filtered_count += 1
                continue
            if strategy["max_items"] and len(new_records) >= strategy["max_items"]:
                break
            should_download = auto_download and (
                media_filter == "all" or aw.media_type == media_filter)
            media_json = json.dumps([{"url": m.url, "kind": m.kind, "ext": m.ext,
                                      "index": m.index} for m in aw.medias])
            rec = ContentRecord(
                target_id=target_id, aweme_id=aw.aweme_id, desc=aw.desc,
                media_type=aw.media_type, quality=aw.quality_label,
                create_time=aw.create_time, cover_url=aw.cover or "",
                like_count=aw.like_count, comment_count=aw.comment_count,
                duration=aw.duration, media_json=media_json,
                download_status="pending" if should_download else "skipped",
            )
            new_records.append((rec, aw, should_download))

        target_name = ""
        with get_session() as s:
            for rec, _, _ in new_records:
                s.add(rec)
            t = s.get(MonitorTarget, target_id)
            if t:
                t.last_scan_at = datetime.utcnow()
                t.last_error = error
                if author:  # 首次抓到时补全昵称/头像
                    if not t.nickname:
                        t.nickname = author.get("nickname", "") or t.nickname
                    if not t.avatar:
                        ava = (author.get("avatar_thumb") or {}).get("url_list") or []
                        t.avatar = ava[0] if ava else t.avatar
                s.add(t)
                target_name = t.nickname or t.sec_uid[:12]
            s.commit()
            for rec, _, _ in new_records:
                s.refresh(rec)

        if new_records and not should_backfill:
            await self._notify_new(target_name, [aw for _, aw, _ in new_records])

        await asyncio.gather(*(self._download(rec.id, aw, base_dir, proxy)
                               for rec, aw, should_download in new_records
                               if should_download))
        return {"ok": not error, "new": len(new_records), "error": error,
                "scanned": len(selected), "filtered": filtered_count,
                "backfill_count": backfill_count}

    # ── 快手:创作者作品监控(浏览器拦截 GraphQL,与抖音同范式)──
    async def _scan_ks_target_locked(self, target_id: int) -> dict:
        with get_session() as s:
            target = s.get(MonitorTarget, target_id)
            if not target:
                return {"ok": False, "error": "target not found"}
            first_scan = target.last_scan_at is None
            identity = (
                self.browser.anon_identity()
                if callable(getattr(self.browser, "anon_identity", None))
                else None
            )
            proxy = ""
            if target.account_id:
                acc = s.get(DouyinAccount, target.account_id)
                if acc:
                    if self._proxy_bad(acc):
                        return self._mark_target_skip(
                            target_id, "账号代理标记为不可用(proxy bad),已跳过以免暴露真实 IP")
                    identity, proxy = self._identity_proxy(acc)
            known = set(s.exec(
                select(ContentRecord.aweme_id)
                .where(ContentRecord.target_id == target_id)).all())
            known_create_times = list(s.exec(
                select(ContentRecord.create_time)
                .where(ContentRecord.target_id == target_id)).all())
            user_id = target.sec_uid
            base_dir = target.download_dir or get_setting(
                "download_dir", self.cfg.engine.media_dir)
            quality = target.video_quality or get_setting("video_quality", "highest")
            monitor_since = int(target.created_at.timestamp())
            scan_since = _douyin_scan_since(monitor_since, known_create_times)
            backfill_count = target.initial_backfill_count
            should_backfill = first_scan or (backfill_count != 0 and not known)
            auto_download = target.download_enabled
            media_filter = target.media_filter or "all"
            strategy = _monitor_strategy(
                target, default_scrolls=12, default_items=0)

        items, author, error = await fetch_ks_videos(
            self.browser, identity, user_id, known,
            max_scrolls=strategy["max_scrolls"],
            block_media=self.cfg.engine.block_media_resources,
            # 默认只监控订阅后的作品；显式首次回填时允许继续向历史翻页。
            stop_before=(0 if should_backfill and backfill_count != 0 else scan_since))

        selected = _select_ks_awemes(
            items, quality, should_backfill, scan_since, backfill_count)
        new_records = []
        filtered_count = 0
        for aw in selected:
            if not _monitor_content_matches(aw, strategy):
                filtered_count += 1
                continue
            if strategy["max_items"] and len(new_records) >= strategy["max_items"]:
                break
            should_download = auto_download and (
                media_filter == "all" or aw.media_type == media_filter)
            media_json = json.dumps([{"url": m.url, "kind": m.kind, "ext": m.ext,
                                      "index": m.index} for m in aw.medias])
            rec = ContentRecord(
                platform="kuaishou", target_id=target_id, aweme_id=aw.aweme_id,
                desc=aw.desc, media_type=aw.media_type, quality=aw.quality_label,
                create_time=aw.create_time, cover_url=aw.cover or "",
                like_count=aw.like_count, comment_count=aw.comment_count,
                duration=aw.duration, media_json=media_json,
                download_status="pending" if should_download else "skipped",
            )
            new_records.append((rec, aw, should_download))

        target_name = ""
        with get_session() as s:
            for rec, _, _ in new_records:
                s.add(rec)
            t = s.get(MonitorTarget, target_id)
            if t:
                t.last_scan_at = datetime.utcnow()
                t.last_error = error
                if author:   # author 为 userProfile 形状
                    p = parse_ks_self_user(author)
                    if not t.nickname:
                        t.nickname = p.get("nickname") or t.nickname
                    if not t.avatar:
                        t.avatar = p.get("avatar") or t.avatar
                s.add(t)
                target_name = t.nickname or (user_id[:12] if user_id else "kuaishou")
            s.commit()
            for rec, _, _ in new_records:
                s.refresh(rec)

        if new_records and not should_backfill:
            await self._notify_new(target_name, [aw for _, aw, _ in new_records])

        await asyncio.gather(*(self._download(rec.id, aw, base_dir, proxy)
                               for rec, aw, should_download in new_records
                               if should_download))
        return {"ok": not error, "new": len(new_records), "error": error,
                "scanned": len(selected), "filtered": filtered_count,
                "backfill_count": backfill_count}

    async def _scan_tiktok_target_locked(self, target_id: int) -> dict:
        with get_session() as s:
            target = s.get(MonitorTarget, target_id)
            if not target:
                return {"ok": False, "error": "target not found"}
            first_scan = target.last_scan_at is None
            if not target.account_id:
                return self._mark_target_skip(
                    target_id, "TikTok 作品监控必须绑定已登录账号")
            acc = s.get(DouyinAccount, target.account_id)
            if not acc or acc.platform != "tiktok" or acc.status != "active":
                return self._mark_target_skip(
                    target_id, "绑定的 TikTok 账号不存在或登录态已失效")
            if not (acc.storage_state or acc.creator_storage_state):
                return self._mark_target_skip(
                    target_id, "绑定的 TikTok 账号缺少登录会话，请重新登录")
            if self._proxy_bad(acc):
                return self._mark_target_skip(
                    target_id, "账号代理标记为不可用(proxy bad),已跳过以免暴露真实 IP")
            identity, proxy = self._identity_proxy(acc)
            known = set(s.exec(select(ContentRecord.aweme_id).where(
                ContentRecord.target_id == target_id)).all())
            known_create_times = list(s.exec(select(ContentRecord.create_time).where(
                ContentRecord.target_id == target_id)).all())
            unique_id = target.sec_uid
            base_dir = target.download_dir or get_setting(
                "download_dir", self.cfg.engine.media_dir)
            quality = target.video_quality or get_setting("video_quality", "highest")
            monitor_since = int(target.created_at.timestamp())
            scan_since = _douyin_scan_since(monitor_since, known_create_times)
            backfill_count = target.initial_backfill_count
            should_backfill = first_scan or (backfill_count != 0 and not known)
            auto_download = target.download_enabled
            media_filter = target.media_filter or "all"
            strategy = _monitor_strategy(target, default_scrolls=12, default_items=0)
            download_ua, download_cookies = _tiktok_download_session(
                acc, identity, self.cfg.engine.user_agent)

        items, error = await fetch_tiktok_profile_works(
            self.browser, identity, unique_id,
            max_results=max(100, strategy["max_items"] or 0),
            max_scrolls=strategy["max_scrolls"],
            block_media=self.cfg.engine.block_media_resources)
        selected = _select_tiktok_awemes(
            items, quality, should_backfill, scan_since, backfill_count)
        new_records = []
        filtered_count = 0
        for aw in selected:
            if aw.aweme_id in known:
                continue
            if not _monitor_content_matches(aw, strategy):
                filtered_count += 1
                continue
            if strategy["max_items"] and len(new_records) >= strategy["max_items"]:
                break
            wants_download = auto_download and (
                media_filter == "all" or aw.media_type == media_filter)
            should_download = wants_download and bool(aw.medias)
            media_json = json.dumps([{"url": m.url, "kind": m.kind,
                                      "ext": m.ext, "index": m.index}
                                     for m in aw.medias])
            rec = ContentRecord(
                platform="tiktok", target_id=target_id, aweme_id=aw.aweme_id,
                desc=aw.desc, media_type=aw.media_type, quality=aw.quality_label,
                create_time=aw.create_time, cover_url=aw.cover or "",
                like_count=aw.like_count, comment_count=aw.comment_count,
                duration=aw.duration, media_json=media_json,
                download_status=("pending" if should_download else
                                 "failed" if wants_download else "skipped"),
                error=("" if aw.medias or not wants_download
                       else "TikTok 主页数据未返回媒体直链"),
            )
            new_records.append((rec, aw, should_download))

        target_name = ""
        with get_session() as s:
            for rec, _, _ in new_records:
                s.add(rec)
            target = s.get(MonitorTarget, target_id)
            if target:
                target.last_scan_at = datetime.utcnow()
                target.last_error = error or ""
                first_aweme = next((aw for _, aw, _ in new_records), None)
                if first_aweme:
                    if not tiktok_display_text(target.nickname):
                        target.nickname = (first_aweme.author_name
                                           or f"@{unique_id}")
                    target.avatar = target.avatar or first_aweme.avatar
                s.add(target)
                target_name = (tiktok_display_text(target.nickname)
                               or (f"@{unique_id}" if unique_id else "tiktok"))
            s.commit()
            for rec, _, _ in new_records:
                s.refresh(rec)

        if new_records and not should_backfill:
            await self._notify_new(target_name, [aw for _, aw, _ in new_records])
        await asyncio.gather(*(self._download(
            rec.id, aw, base_dir, proxy, user_agent=download_ua,
            cookies=download_cookies,
            referer=aw.source_url or tiktok_profile_url(unique_id))
            for rec, aw, should_download in new_records if should_download))
        return {"ok": not error, "new": len(new_records), "error": error,
                "scanned": len(selected), "filtered": filtered_count,
                "backfill_count": backfill_count}

    # ── 视频号:创作者主页作品监控(优先微信 Finder 本地 CDP 协议 + WxIsaac64 解密下载)──
    async def _scan_channels_target_locked(self, target_id: int) -> dict:
        with get_session() as s:
            target = s.get(MonitorTarget, target_id)
            if not target:
                return {"ok": False, "error": "target not found"}
            is_first_scan = target.last_scan_at is None
            identity = (
                self.browser.anon_identity()
                if callable(getattr(self.browser, "anon_identity", None))
                else None
            )
            proxy = ""
            acc_sec_uid = ""
            has_active_account = False
            if target.account_id:
                acc = s.get(DouyinAccount, target.account_id)
                if acc:
                    if self._proxy_bad(acc):
                        return self._mark_target_skip(
                            target_id, "账号代理标记为不可用(proxy bad),已跳过以免暴露真实 IP")
                    identity, proxy = self._identity_proxy(acc)
                    acc_sec_uid = acc.sec_uid or ""
                    has_active_account = (
                        acc.platform == "shipinhao"
                        and acc.status == "active"
                        and bool(acc.storage_state or acc.creator_storage_state)
                    )
            known = set(s.exec(
                select(ContentRecord.aweme_id)
                .where(ContentRecord.target_id == target_id)).all())
            known_create_times = list(s.exec(
                select(ContentRecord.create_time)
                .where(ContentRecord.target_id == target_id)).all())
            user_id = target.sec_uid
            base_dir = target.download_dir or get_setting(
                "download_dir", self.cfg.engine.media_dir)
            quality = target.video_quality or get_setting("video_quality", "highest")
            monitor_since = int(target.created_at.timestamp())
            scan_since = _douyin_scan_since(monitor_since, known_create_times)
            backfill_count = target.initial_backfill_count
            should_backfill = is_first_scan or (backfill_count != 0 and not known)
            auto_download = target.download_enabled
            media_filter = target.media_filter or "all"
            strategy = _monitor_strategy(
                target, default_scrolls=10, default_items=0)

        read_mode = str(getattr(self.cfg.engine, "channels_read_mode", "auto") or "auto").lower()
        proto_client = (
            ChannelsProtocolClient.from_config(self.cfg)
            if read_mode in {"auto", "protocol"}
            else None
        )
        items: list = []
        author = None
        error = ""
        if proto_client is not None:
            try:
                items, author, error = await proto_client.fetch_user_feeds(
                    user_id,
                    max_results=max(40, strategy["max_items"] or 40),
                    max_pages=strategy["max_scrolls"],
                    stop_before=(0 if should_backfill and backfill_count != 0 else scan_since),
                )
            finally:
                await proto_client.close()
        if not items and (proto_client is None or (read_mode == "auto" and error)):
            if has_active_account and (not user_id or user_id == acc_sec_uid):
                items, author, browser_err = await fetch_channels_works(
                    self.browser, identity, known,
                    max_scrolls=strategy["max_scrolls"],
                    block_media=self.cfg.engine.block_media_resources,
                )
                error = browser_err or ("" if items else error)
            elif proto_client is None:
                return self._mark_target_skip(
                    target_id,
                    "视频号外部创作者监控需要连接本机已登录微信，请先在「设置 → 视频号微信通道」开启一键本地代理并检测连通",
                )

        selected = _select_channels_awemes(
            items, quality, should_backfill, scan_since, backfill_count)
        new_records = []
        filtered_count = 0
        for aw in selected:
            if aw.aweme_id in known:
                continue
            if not _monitor_content_matches(aw, strategy):
                filtered_count += 1
                continue
            if strategy["max_items"] and len(new_records) >= strategy["max_items"]:
                break
            wants_download = auto_download and (
                media_filter == "all" or aw.media_type == media_filter)
            should_download = wants_download and bool(aw.medias)
            media_rows = []
            for m in aw.medias:
                entry = {"url": m.url, "kind": m.kind, "ext": m.ext, "index": m.index}
                if getattr(m, "decode_key", ""):
                    entry["decode_key"] = m.decode_key
                media_rows.append(entry)
            media_json = json.dumps(media_rows, ensure_ascii=False)
            rec = ContentRecord(
                platform="shipinhao", target_id=target_id, aweme_id=aw.aweme_id,
                desc=aw.desc, media_type=aw.media_type, quality=aw.quality_label,
                create_time=aw.create_time, cover_url=aw.cover or "",
                like_count=aw.like_count, comment_count=aw.comment_count,
                duration=aw.duration, media_json=media_json,
                xsec_token=aw.object_nonce_id or "",
                download_status="pending" if should_download else "skipped",
                error=("" if aw.medias or not wants_download
                       else "视频号助手未返回媒体直链，仅保存作品元数据"),
            )
            new_records.append((rec, aw, should_download))

        target_name = ""
        with get_session() as s:
            for rec, _, _ in new_records:
                s.add(rec)
            t = s.get(MonitorTarget, target_id)
            if t:
                t.last_scan_at = datetime.utcnow()
                t.last_error = str(error or "")
                if author:
                    p = parse_channels_self_user(author)
                    if not t.nickname:
                        t.nickname = p.get("nickname") or t.nickname
                    if not t.avatar:
                        t.avatar = p.get("avatar") or t.avatar
                first_aw = next((aw for _, aw, _ in new_records), None)
                if first_aw:
                    if not t.nickname and first_aw.author_name:
                        t.nickname = first_aw.author_name
                    if not t.avatar and first_aw.avatar:
                        t.avatar = first_aw.avatar
                s.add(t)
                target_name = t.nickname or (user_id[:16] if user_id else "shipinhao")
            s.commit()
            for rec, _, _ in new_records:
                s.refresh(rec)

        if new_records and not should_backfill:
            await self._notify_new(target_name, [aw for _, aw, _ in new_records])

        await asyncio.gather(*(
            self._download(
                rec.id, aw, base_dir, proxy,
                referer="https://channels.weixin.qq.com/",
            )
            for rec, aw, should_download in new_records
            if should_download
        ))
        return {"ok": not error, "new": len(new_records), "error": error,
                "scanned": len(selected), "filtered": filtered_count,
                "backfill_count": backfill_count}

    def _mark_target_skip(self, target_id: int, msg: str) -> dict:
        """把跳过原因写到目标 last_error,并推进 last_scan_at(避免下轮立刻重试)。"""
        with get_session() as s:
            t = s.get(MonitorTarget, target_id)
            if t:
                t.last_scan_at = datetime.utcnow()
                t.last_error = msg
                s.add(t); s.commit()
        return {"ok": False, "new": 0, "error": msg, "skipped": True}
