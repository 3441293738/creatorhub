"""新作品/评论/弹幕通知推送、媒体文件下载及失败重试。"""
from __future__ import annotations

import json

from sqlmodel import select

from app.browser import fetch_rednote_note_detail, fetch_twitter_tweet_detail, fetch_xhs_note_detail
from app.db import get_session
from app.engine.monitor_helpers import MAX_AUTO_RETRY, _loads, _loads_list, _tiktok_download_session, log
from app.models import ContentRecord, DouyinAccount, MonitorTarget, NotificationChannel
from app.notifier import notify_all
from app.platforms.bilibili import BilibiliClient, cookie_map_from_state as bili_cookie_map
from app.platforms.douyin.extract import Aweme, MediaItem
from app.platforms.instagram import InstagramClient
from app.platforms.rednote import parse_rednote_detail
from app.platforms.tiktok.urls import profile_url as tiktok_profile_url, video_url as tiktok_video_url
from app.platforms.twitter import TwitterClient, parse_twitter_aweme
from app.platforms.xhs import parse_note_detail
from app.risk import OperationKind
from app.settings import get_setting


class MediaDownloadMixin:
    async def _notify_comments(self, target_name: str, work_desc: str, comments: list):
        with get_session() as s:
            chans = s.exec(select(NotificationChannel)
                           .where(NotificationChannel.enabled == True)).all()  # noqa: E712
            channels = [{"type": c.type, "config": _loads(c.config)} for c in chans]
        if not channels:
            return
        title = f"评论监控 · {target_name} 有 {len(comments)} 条新评论"
        head = (work_desc or "")[:20]
        lines = [f"作品:{head}"]
        for c in comments[:6]:
            lines.append(f"· {c['user_nickname']}: {c['text'][:40]}")
        if len(comments) > 6:
            lines.append(f"… 等共 {len(comments)} 条")
        try:
            await notify_all(channels, title, "\n".join(lines))
        except Exception as e:
            log.warning("评论通知失败: %s", e)

    async def _notify_danmaku(self, target_name: str, work_desc: str,
                              danmakus: list):
        with get_session() as s:
            chans = s.exec(select(NotificationChannel).where(
                NotificationChannel.enabled == True)).all()  # noqa: E712
            channels = [{"type": c.type, "config": _loads(c.config)} for c in chans]
        if not channels:
            return
        title = f"弹幕监控 · {target_name} 有 {len(danmakus)} 条新弹幕"
        lines = [f"作品:{(work_desc or '')[:20]}"]
        for item in danmakus[:6]:
            point = int(item.get("video_time_ms") or 0) // 1000
            stamp = f"{point // 60}:{point % 60:02d}"
            lines.append(f"· [{stamp}] {item.get('user_nickname') or '用户'}: "
                         f"{(item.get('text') or '')[:40]}")
        if len(danmakus) > 6:
            lines.append(f"… 等共 {len(danmakus)} 条")
        try:
            await notify_all(channels, title, "\n".join(lines))
        except Exception as e:
            log.warning("弹幕通知失败: %s", e)

    async def _notify_new(self, target_name: str, awemes: list):
        """有新作品时推送到所有启用的通知渠道。"""
        with get_session() as s:
            chans = s.exec(select(NotificationChannel)
                           .where(NotificationChannel.enabled == True)).all()  # noqa: E712
            channels = [{"type": c.type, "config": _loads(c.config)} for c in chans]
        if not channels:
            return
        title = f"作品监控 · {target_name} 新增 {len(awemes)} 个作品"
        lines = []
        for aw in awemes[:6]:
            tag = "图集" if aw.media_type == "images" else "视频"
            lines.append(f"· [{tag}] {(aw.desc or aw.aweme_id)[:30]}")
        if len(awemes) > 6:
            lines.append(f"… 等共 {len(awemes)} 个")
        try:
            await notify_all(channels, title, "\n".join(lines))
        except Exception as e:
            log.warning("通知发送失败: %s", e)

    async def _download(self, record_id: int, aweme, base_dir: str = "", proxy: str = "",
                        *, user_agent: str = "", cookies=None, referer: str = ""):
        async with self._sem:
            with get_session() as s:
                rec = s.get(ContentRecord, record_id)
                if rec:
                    rec.download_status = "downloading"
                    s.add(rec); s.commit()
            ok, path, err = await self.downloader.download_aweme(
                aweme, base_dir, self._dl_proxy(proxy), user_agent=user_agent,
                cookies=cookies, referer=referer)
            with get_session() as s:
                rec = s.get(ContentRecord, record_id)
                if rec:
                    rec.download_status = "done" if ok else "failed"
                    rec.local_path = path
                    rec.error = err
                    s.add(rec); s.commit()

    # ── 失败重试 ──
    def _rebuild_aweme(self, rec: ContentRecord, author_name: str) -> Aweme:
        aw = Aweme(aweme_id=rec.aweme_id, desc=rec.desc, create_time=rec.create_time,
                   author_name=author_name, media_type=rec.media_type,
                   object_nonce_id=rec.xsec_token if rec.platform == "shipinhao" else "")
        aw.platform = rec.platform or "douyin"
        for m in _loads_list(rec.media_json) if rec.media_json else []:
            if not isinstance(m, dict) or not m.get("url"):
                continue
            aw.medias.append(MediaItem(
                url=m["url"],
                kind=m.get("kind", "video"),
                ext=m.get("ext", "mp4"),
                index=m.get("index", 0),
                decode_key=str(m.get("decode_key") or ""),
                audio_url=str(m.get("audio_url") or ""),
            ))
        return aw

    async def retry_download(self, record_id: int) -> dict:
        """重新下载某条作品(用入库时存下的媒体直链;直链可能过期则需重抓目标)。
        小红书:若当初连详情都没拿到(无媒体快照),这里会用 xsec_token 重新拉一次详情。"""
        with get_session() as s:
            rec = s.get(ContentRecord, record_id)
            if not rec:
                return {"ok": False, "error": "记录不存在"}
            t = s.get(MonitorTarget, rec.target_id)
            base_dir = (t.download_dir if t else "") or get_setting(
                "download_dir", self.cfg.engine.media_dir)
            author_name = (t.nickname if t else "") or ""
            platform = rec.platform or "douyin"
            note_id = rec.aweme_id
            note_tok = rec.xsec_token or ""
            note_source = rec.xsec_source or ""
            kind = (t.target_kind if t else "creator")
            account_id = t.account_id if t else None
            acc_state = ""
            acc_proxy = ""
            acc = None
            identity = None
            if t and t.account_id:
                acc = s.get(DouyinAccount, t.account_id)
                if acc:
                    acc_state = acc.storage_state or ""
                    acc_proxy = acc.proxy or ""
                    identity_for = getattr(self.browser, "identity_for", None)
                    if callable(identity_for):
                        identity = identity_for(acc)
            media_json = rec.media_json
            aw = self._rebuild_aweme(rec, author_name)
            if platform == "tiktok" and t and t.sec_uid:
                aw.author_unique_id = t.sec_uid
                aw.source_url = tiktok_video_url(aw.aweme_id, t.sec_uid)
            needs_xhs_refetch = platform in {"xhs", "rednote"} and (
                not media_json or not aw.medias)
            needs_bili_refetch = platform == "bilibili" and (
                not media_json or not aw.medias)
            needs_ig_refetch = platform == "instagram" and (
                not media_json or not aw.medias)
            needs_tw_refetch = platform == "twitter" and (
                not media_json or not aw.medias)
            rec.download_status = "downloading"
            if not needs_xhs_refetch and not needs_bili_refetch and not needs_ig_refetch and not needs_tw_refetch:
                rec.retry_count = (rec.retry_count or 0) + 1
            s.add(rec); s.commit()

        if platform == "twitter" and (not media_json or not aw.medias):
            tcli = TwitterClient(
                acc_state,
                (acc.ua if acc and acc.ua else self.cfg.engine.user_agent),
                proxy=acc_proxy,
                timeout=self.cfg.engine.request_timeout_seconds,
            )
            async with tcli.session_scope():
                aw2, _raw_t, tw_err = await tcli.fetch_tweet_syndication(note_id)
            if (aw2 is None or not aw2.medias) and identity is not None:
                raw_node, b_err = await fetch_twitter_tweet_detail(
                    self.browser, identity, note_id, block_media=self.cfg.engine.block_media_resources
                )
                if raw_node:
                    aw2 = parse_twitter_aweme(raw_node)
                tw_err = tw_err or b_err
            if aw2 and aw2.medias:
                aw = aw2
                media_json = json.dumps([
                    {
                        "url": m.url,
                        "kind": m.kind,
                        "ext": m.ext,
                        "index": m.index,
                    }
                    for m in aw.medias
                ])
                with get_session() as s:
                    rec = s.get(ContentRecord, record_id)
                    if rec:
                        rec.retry_count = (rec.retry_count or 0) + 1
                        rec.media_json = media_json
                        s.add(rec); s.commit()
            else:
                with get_session() as s:
                    rec = s.get(ContentRecord, record_id)
                    if rec:
                        rec.retry_count = (rec.retry_count or 0) + 1
                        rec.download_status = "failed"
                        rec.error = tw_err or "重拉 Twitter 推文详情仍无可用媒体直链(可能为纯文本推文)"
                        s.add(rec); s.commit()
                return {"ok": False, "error": tw_err or "重拉 Twitter 推文详情仍无可用媒体直链"}

        if platform == "instagram" and (not media_json or not aw.medias):
            icli = InstagramClient(
                acc_state,
                (acc.ua if acc and acc.ua else self.cfg.engine.user_agent),
                proxy=acc_proxy,
                timeout=self.cfg.engine.request_timeout_seconds,
                account_seed=(
                    (getattr(identity, "fp_seed", "") or getattr(identity, "profile_dir", ""))
                    if identity is not None
                    else (acc.fp_seed or acc.profile_dir or "" if acc else "")
                ),
                timezone_id=(
                    getattr(identity, "timezone_id", "Asia/Shanghai")
                    if identity is not None
                    else (acc.timezone_id or "Asia/Shanghai" if acc else "Asia/Shanghai")
                ),
            )
            async with icli.session_scope():
                aw2, _raw_m, ig_err = await icli.fetch_media_info(note_id)
            if aw2 and aw2.medias:
                aw = aw2
                media_json = json.dumps([
                    {
                        "url": m.url,
                        "kind": m.kind,
                        "ext": m.ext,
                        "index": m.index,
                    }
                    for m in aw.medias
                ])
                with get_session() as s:
                    rec = s.get(ContentRecord, record_id)
                    if rec:
                        rec.retry_count = (rec.retry_count or 0) + 1
                        rec.media_json = media_json
                        s.add(rec); s.commit()
            else:
                with get_session() as s:
                    rec = s.get(ContentRecord, record_id)
                    if rec:
                        rec.retry_count = (rec.retry_count or 0) + 1
                        rec.download_status = "failed"
                        rec.error = ig_err or "重拉 Instagram 作品详情仍无可用媒体直链"
                        s.add(rec); s.commit()
                return {"ok": False, "error": ig_err or "重拉 Instagram 作品详情仍无可用媒体直链"}

        if platform == "bilibili" and (not media_json or not aw.medias):
            bcli = BilibiliClient(
                acc_state,
                (acc.ua if acc and acc.ua else self.cfg.engine.user_agent),
                proxy=acc_proxy,
                timeout=self.cfg.engine.request_timeout_seconds,
            )
            async with bcli.session_scope():
                aw2, berr = await bcli.fetch_aweme_by_work(note_id, with_play_url=True)
            if aw2 and aw2.medias:
                aw = aw2
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
                with get_session() as s:
                    rec = s.get(ContentRecord, record_id)
                    if rec:
                        rec.retry_count = (rec.retry_count or 0) + 1
                        rec.media_json = media_json
                        s.add(rec); s.commit()
            else:
                with get_session() as s:
                    rec = s.get(ContentRecord, record_id)
                    if rec:
                        rec.retry_count = (rec.retry_count or 0) + 1
                        rec.download_status = "failed"
                        rec.error = berr or "重拉 B 站稿件详情仍无可用直链"
                        s.add(rec); s.commit()
                return {"ok": False, "error": berr or "重拉 B 站稿件详情仍无可用直链"}

        # 小红书 / 海外小红书:无媒体快照时,重新拉详情补齐媒体直链
        if platform == "rednote" and (not media_json or not aw.medias):
            rmode = self._rednote_read_mode()
            rcli = (
                self._rednote_client(identity, acc_state, acc_proxy)
                if rmode in {"api", "hybrid"}
                else None
            )
            use_browser = rmode == "browser" or rcli is None
            if not account_id:
                derr = "监控目标未绑定海外小红书账号,请先编辑监控并选择账号"
            else:
                async def _refetch_rn_detail():
                    try:
                        if not use_browser and rcli is not None:
                            try:
                                async with rcli.session_scope():
                                    detail = await rcli.note_detail(
                                        note_id, xsec_token=note_tok,
                                        xsec_source=note_source or ("pc_search" if kind == "keyword" else "pc_user"))
                                if detail or rmode == "api":
                                    return detail, ""
                            except Exception as api_exc:
                                if rmode == "api":
                                    return {}, str(api_exc)
                        if identity is None:
                            return {}, "缺少浏览器身份"
                        return await fetch_rednote_note_detail(
                            self.browser, identity, note_id,
                            xsec_token=note_tok,
                            xsec_source=note_source or ("pc_search" if kind == "keyword" else "pc_feed"),
                            block_media=self.cfg.engine.block_media_resources)
                    except Exception as exc:
                        return {}, str(exc)

                card, derr = await self.guarded_read_pair(
                    account_id, OperationKind.READ_HEAVY,
                    f"retry-download:{record_id}", _refetch_rn_detail,
                    empty_result={})
                if not str(derr or "").startswith("risk_deferred:"):
                    with get_session() as s:
                        rec = s.get(ContentRecord, record_id)
                        if rec:
                            rec.retry_count = (rec.retry_count or 0) + 1
                            s.add(rec); s.commit()
            aw2 = parse_rednote_detail(card or {}, {"note_id": note_id}) if (account_id and card) else None
            if aw2 and aw2.medias:
                aw = aw2
                with get_session() as s:
                    rec = s.get(ContentRecord, record_id)
                    if rec:
                        rec.media_type = aw.media_type
                        rec.create_time = aw.create_time or rec.create_time
                        rec.like_count = aw.like_count or rec.like_count
                        rec.comment_count = aw.comment_count or rec.comment_count
                        rec.cover_url = aw.cover or rec.cover_url
                        rec.media_json = json.dumps([{"url": m.url, "kind": m.kind,
                                                      "ext": m.ext, "index": m.index}
                                                     for m in aw.medias])
                        s.add(rec); s.commit()
            else:
                with get_session() as s:
                    rec = s.get(ContentRecord, record_id)
                    if rec:
                        rec.download_status = "failed"
                        rec.error = derr or "重拉海外小红书详情仍无媒体(笔记可能已删/私密)"
                        s.add(rec); s.commit()
                return {"ok": False, "error": derr or "重拉海外小红书详情仍无媒体"}
        elif platform == "xhs" and (not media_json or not aw.medias):
            browser_reads = bool(identity is not None and self._xhs_browser_reads_enabled())
            client = None if browser_reads else self._xhs_client(
                identity, acc_state, acc_proxy)
            if not account_id:
                derr = "监控目标未绑定小红书账号,请先编辑监控并选择账号"
            elif browser_reads or client:
                derr = ""
            else:
                derr = "账号登录态缺少 a1,请重新扫码登录"
            card = {}
            if browser_reads or client:
                async def _refetch_note_detail():
                    try:
                        if browser_reads:
                            return await fetch_xhs_note_detail(
                                self.browser, identity, note_id,
                                xsec_token=note_tok,
                                xsec_source=note_source or ("pc_search" if kind == "keyword"
                                             else "pc_feed"),
                                block_media=self.cfg.engine.block_media_resources)
                        detail = await client.note_detail(
                            note_id, xsec_token=note_tok,
                            xsec_source=note_source or ("pc_search" if kind == "keyword"
                                         else "pc_user"))
                        return detail, ""
                    except Exception as exc:
                        return {}, str(exc)

                card, derr = await self.guarded_read_pair(
                    account_id, OperationKind.READ_HEAVY,
                    f"retry-download:{record_id}", _refetch_note_detail,
                    empty_result={})
                if not str(derr or "").startswith("risk_deferred:"):
                    with get_session() as s:
                        rec = s.get(ContentRecord, record_id)
                        if rec:
                            rec.retry_count = (rec.retry_count or 0) + 1
                            s.add(rec)
                            s.commit()
            aw2 = parse_note_detail(card or {}, {"note_id": note_id}) if card else None
            if aw2 and aw2.medias:
                aw = aw2
                with get_session() as s:
                    rec = s.get(ContentRecord, record_id)
                    if rec:
                        rec.media_type = aw.media_type
                        rec.create_time = aw.create_time or rec.create_time
                        rec.like_count = aw.like_count or rec.like_count
                        rec.comment_count = aw.comment_count or rec.comment_count
                        rec.cover_url = aw.cover or rec.cover_url
                        rec.media_json = json.dumps([{"url": m.url, "kind": m.kind,
                                                      "ext": m.ext, "index": m.index}
                                                     for m in aw.medias])
                        s.add(rec); s.commit()
            else:
                with get_session() as s:
                    rec = s.get(ContentRecord, record_id)
                    if rec:
                        rec.download_status = "failed"
                        rec.error = derr or "重拉详情仍无媒体(笔记可能已删/私密)"
                        s.add(rec); s.commit()
                return {"ok": False, "error": derr or "重拉详情仍无媒体"}
        elif not media_json or not aw.medias:
            with get_session() as s:
                rec = s.get(ContentRecord, record_id)
                if rec:
                    rec.download_status = "failed"
                    rec.error = "无媒体直链快照,请对该目标重新抓取"
                    s.add(rec); s.commit()
            return {"ok": False, "error": "无媒体直链快照"}

        download_ua = ""
        download_cookies = None
        download_referer = ""
        if platform == "tiktok" and acc is not None and identity is not None:
            download_ua, download_cookies = _tiktok_download_session(
                acc, identity, self.cfg.engine.user_agent)
            download_referer = aw.source_url or tiktok_profile_url(
                t.sec_uid if t else "")
        elif platform == "bilibili":
            download_ua = acc.ua if (acc and acc.ua) else self.cfg.engine.user_agent
            if acc_state:
                download_cookies = bili_cookie_map(acc_state)
            download_referer = aw.source_url or f"https://www.bilibili.com/video/{note_id}"
        elif platform == "instagram":
            download_ua = acc.ua if (acc and acc.ua) else self.cfg.engine.user_agent
            download_referer = "https://www.instagram.com/"
        elif platform == "twitter":
            download_ua = acc.ua if (acc and acc.ua) else self.cfg.engine.user_agent
            download_referer = "https://x.com/"
        ok, path, err = await self.downloader.download_aweme(
            aw, base_dir, self._dl_proxy(acc_proxy), user_agent=download_ua,
            cookies=download_cookies, referer=download_referer)
        if not ok and platform == "bilibili" and not needs_bili_refetch and "403" in str(err):
            # B 站 CDN 防盗链签名过期时自动重拉一次最新直链重试下载
            bcli_refresh = BilibiliClient(
                acc_state,
                download_ua or self.cfg.engine.user_agent,
                proxy=acc_proxy,
                timeout=self.cfg.engine.request_timeout_seconds,
            )
            async with bcli_refresh.session_scope():
                aw_fresh, _ = await bcli_refresh.fetch_aweme_by_work(note_id, with_play_url=True)
            if aw_fresh and aw_fresh.medias:
                aw = aw_fresh
                ok, path, err = await self.downloader.download_aweme(
                    aw, base_dir, self._dl_proxy(acc_proxy), user_agent=download_ua,
                    cookies=download_cookies, referer=download_referer)
        elif not ok and platform == "instagram" and not needs_ig_refetch and "403" in str(err):
            icli_refresh = InstagramClient(
                acc_state,
                download_ua or self.cfg.engine.user_agent,
                proxy=acc_proxy,
                timeout=self.cfg.engine.request_timeout_seconds,
            )
            async with icli_refresh.session_scope():
                aw_fresh, _raw_m, _ = await icli_refresh.fetch_media_info(note_id)
            if aw_fresh and aw_fresh.medias:
                aw = aw_fresh
                ok, path, err = await self.downloader.download_aweme(
                    aw, base_dir, self._dl_proxy(acc_proxy), user_agent=download_ua,
                    cookies=download_cookies, referer=download_referer)
        elif not ok and platform == "twitter" and not needs_tw_refetch and "403" in str(err):
            tcli_refresh = TwitterClient(
                acc_state,
                download_ua or self.cfg.engine.user_agent,
                proxy=acc_proxy,
                timeout=self.cfg.engine.request_timeout_seconds,
            )
            async with tcli_refresh.session_scope():
                aw_fresh, _raw_t, _ = await tcli_refresh.fetch_tweet_syndication(note_id)
            if aw_fresh and aw_fresh.medias:
                aw = aw_fresh
                ok, path, err = await self.downloader.download_aweme(
                    aw, base_dir, self._dl_proxy(acc_proxy), user_agent=download_ua,
                    cookies=download_cookies, referer=download_referer)
        with get_session() as s:
            rec = s.get(ContentRecord, record_id)
            if rec:
                rec.download_status = "done" if ok else "failed"
                rec.local_path = path
                rec.error = err
                s.add(rec); s.commit()
        return {"ok": ok, "error": err}

    async def _retry_failed(self):
        """自动重试可安全复用媒体快照的失败作品。

        小红书详情首抓未得到媒体时，继续自动打开详情页会反复消耗过期
        xsec_token，并可能连续弹出平台安全验证。此类记录保留给用户单次
        手动重试或目标下一轮刷新，不进入后台自动重试风暴。
        """
        with get_session() as s:
            rows = list(s.exec(
                select(ContentRecord.id)
                .where(ContentRecord.download_status == "failed")
                .where(ContentRecord.retry_count < MAX_AUTO_RETRY)).all())
            ids = []
            for rid in rows:
                record = s.get(ContentRecord, rid)
                if record is None:
                    continue
                if record.platform in {"xhs", "rednote"}:
                    media = _loads(record.media_json)
                    if not isinstance(media, list) or not any(
                            isinstance(item, dict) and isinstance(item.get("url"), str)
                            and item["url"].strip() for item in media):
                        continue
                ids.append(rid)
        for rid in ids:
            await self.retry_download(rid)
