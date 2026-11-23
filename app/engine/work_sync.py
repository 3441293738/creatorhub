"""本账号单作品评论同步与抖音直连关注列表读取。"""
from __future__ import annotations

from sqlmodel import select
import inspect

from app.browser import fetch_bili_comments, fetch_channels_comments, fetch_comments, fetch_ig_comments, fetch_ks_comments, fetch_rednote_comments
from app.browser.tiktok_fetcher import fetch_tiktok_comments
from app.db import get_session
from app.engine.monitor_helpers import log
from app.models import CommentRecord, DouyinAccount
from app.platforms.bilibili import BilibiliClient
from app.platforms.channels import ChannelsProtocolClient, flatten_channels_comments, parse_channels_comment
from app.platforms.douyin import DouyinClient, cookie_from_state as dy_cookie_from_state, parse_comment
from app.platforms.instagram import InstagramClient
from app.platforms.kuaishou import flatten_ks_comments, parse_ks_comment
from app.platforms.rednote import RednoteApiError, flatten_rednote_comments, parse_rednote_comment
from app.platforms.tiktok import parse_tiktok_comment
from app.platforms.xhs import XhsApiError
from app.risk import OperationKind, RiskCategory, classify_platform_error
from app.transport_matrix import douyin_client_environment, resolve_transport


class WorkSyncMixin:
    async def sync_work_comments(self, account_id: int, platform: str, item_id: str,
                                 xsec_token: str = "") -> dict:
        """抓「本账号某作品」的评论并落库(watch_id=0 标记本账号来源)。
        抖音直连(comment/list 分页 + 回复),小红书走签名直连客户端,
        快手走浏览器拦截。返回 {ok, fetched, added, error}。"""
        key = f"wc:{account_id}:{item_id}"
        if key in self._inflight:
            return {"ok": True, "fetched": 0, "added": 0, "skipped": "正在抓取中"}
        self._inflight.add(key)
        try:
            result = await self._guarded_read_dict(
                account_id, OperationKind.READ_HEAVY, key,
                lambda: self._sync_work_comments_locked(
                    account_id, platform, item_id, xsec_token))
            # A risk-deferred read used to return only ok/skipped/reason.  The
            # work-comments UI then rendered "undefined" as a successful count.
            # Keep one stable response contract for completed and deferred runs.
            normalized = dict(result or {})
            normalized.setdefault("fetched", 0)
            normalized.setdefault("added", 0)
            normalized.setdefault("error", "")
            if platform == "douyin":
                normalized.setdefault(
                    "configured_mode",
                    resolve_transport(
                        self.cfg, "douyin", "own_work_comments")[
                            "configured_mode"])
            normalized.setdefault(
                "source", "deferred" if normalized.get("skipped") else "unknown")
            return normalized
        finally:
            self._inflight.discard(key)

    async def _sync_work_comments_locked(self, account_id, platform, item_id,
                                         xsec_token) -> dict:
        with get_session() as s:
            acc = s.get(DouyinAccount, account_id)
            if not acc:
                return {"ok": False, "error": "账号不存在"}
            if acc.status == "invalid":
                return {"ok": False, "error": "账号登录态已失效"}
            if self._proxy_bad(acc):
                return {"ok": False, "error": "账号代理不可用"}
            state = acc.storage_state or acc.creator_storage_state or ""
            ua = acc.ua or self.cfg.engine.user_agent
            proxy = acc.proxy or ""
            tiktok_author_unique_id = (acc.douyin_id or "") if platform == "tiktok" else ""
            dy_transport = (resolve_transport(
                self.cfg, "douyin", "own_work_comments", acc)
                if platform == "douyin" else None)
            identity = (None if dy_transport and
                        dy_transport["effective_mode"] == "api"
                        else self.browser.identity_for(acc))
            direct_environment = douyin_client_environment(acc)
            known = set(s.exec(select(CommentRecord.comment_id).where(
                CommentRecord.watch_id == 0,
                CommentRecord.aweme_id == item_id)).all())
        fresh: list = []
        error = ""
        source = "browser"
        configured_mode = "browser"
        try:
            if platform == "douyin":
                transport = dy_transport
                configured_mode = transport["configured_mode"]
                mode = transport["effective_mode"]
                raw: list = []
                api_error = ""
                if mode in {"api", "hybrid"}:
                    cookie = dy_cookie_from_state(state)
                    if not cookie:
                        api_error = "api_missing_cookie"
                    else:
                        client = DouyinClient(
                            cookie, ua,
                            timeout=self.cfg.engine.request_timeout_seconds,
                            proxy=proxy, **direct_environment)
                        try:
                            async with client.session_scope():
                                raw = await client.fetch_all_comments(item_id)
                            api_error = client.last_error or ""
                        except Exception as exc:
                            api_error = f"api:{type(exc).__name__}"
                    # A parsed response with an empty comments array is a valid
                    # zero-comment result.  Only an explicit client error may
                    # enter hybrid fallback.
                    if not api_error:
                        source = "api"
                    elif mode == "api":
                        return {
                            "ok": False, "fetched": 0, "added": 0,
                            "error": f"douyin_api_comments:{api_error}",
                            "source": "api", "configured_mode": configured_mode,
                        }
                if mode == "browser" or (mode == "hybrid" and api_error):
                    raw, browser_error = await fetch_comments(
                        self.browser, identity, item_id, known,
                        max_scrolls=self.cfg.engine.comment_max_scrolls,
                        block_media=self.cfg.engine.block_media_resources)
                    source = "browser_fallback" if mode == "hybrid" else "browser"
                    error = browser_error or ""
                    if error and not raw:
                        return {
                            "ok": False, "fetched": 0, "added": 0,
                            "error": error, "source": source,
                            "configured_mode": configured_mode,
                        }
                fresh = [c for c in (parse_comment(rc) for rc in raw)
                         if c and c["comment_id"] not in known]
            elif platform == "xhs":
                source = "api"
                configured_mode = "api"
                client = self._xhs_client(identity, state, proxy)
                if client is None:
                    return {"ok": False, "fetched": 0, "added": 0,
                            "error": "小红书账号缺 a1 Cookie,无法抓评论",
                            "source": source, "configured_mode": configured_mode}
                fresh = await self._xhs_fetch_comments(client, item_id, xsec_token, known)
            elif platform == "kuaishou":
                source = configured_mode = "browser"
                raw, err = await fetch_ks_comments(
                    self.browser, identity, item_id, known,
                    max_scrolls=self.cfg.engine.comment_max_scrolls,
                    block_media=self.cfg.engine.block_media_resources)
                error = err or ""
                fresh = [c for c in (parse_ks_comment(rc) for rc in flatten_ks_comments(raw))
                         if c and c["comment_id"] not in known]
            elif platform == "shipinhao":
                read_mode = str(
                    getattr(self.cfg.engine, "channels_read_mode", "auto") or "auto"
                ).lower()
                configured_mode = (
                    read_mode if read_mode in {"auto", "protocol", "browser"} else "auto"
                )
                proto_client = (
                    ChannelsProtocolClient.from_config(self.cfg)
                    if configured_mode in {"auto", "protocol"}
                    else None
                )
                used_protocol = False
                if proto_client is not None:
                    try:
                        proto_comments, _audit, proto_err = await proto_client.collect_comments(
                            item_id,
                            xsec_token,
                            limit=300,
                            include_replies=True,
                        )
                    finally:
                        await proto_client.close()
                    if proto_comments or not proto_err or configured_mode == "protocol":
                        used_protocol = True
                        source = "protocol"
                        error = proto_err or ""
                        fresh = [
                            c for c in proto_comments
                            if c and c.get("comment_id") and c["comment_id"] not in known
                        ]
                elif configured_mode == "protocol":
                    used_protocol = True
                    source = "protocol"
                    error = "尚未连接本机微信视频号通道，请在「设置 → 视频号微信通道」开启一键本地代理并在微信内打开一次视频号页面"
                    fresh = []
                if not used_protocol:
                    source = "browser_fallback" if proto_client is not None else "browser"
                    raw, err = await fetch_channels_comments(
                        self.browser, identity, item_id, known,
                        max_scrolls=self.cfg.engine.comment_max_scrolls,
                        block_media=self.cfg.engine.block_media_resources)
                    error = err or ""
                    fresh = [c for c in (parse_channels_comment(rc)
                                         for rc in flatten_channels_comments(raw))
                             if c and c["comment_id"] not in known]
            elif platform == "tiktok":
                source = configured_mode = "browser"
                raw, err = await fetch_tiktok_comments(
                    self.browser, identity, item_id, known,
                    max_comments=300,
                    max_scrolls=self.cfg.engine.comment_max_scrolls,
                    include_replies=True,
                    block_media=self.cfg.engine.block_media_resources,
                    author_unique_id=tiktok_author_unique_id)
                error = err or ""
                fresh = [c for c in (parse_tiktok_comment(rc) for rc in raw)
                         if c and c["comment_id"] not in known]
            elif platform == "bilibili":
                configured_mode = str(
                    getattr(self.cfg.engine, "bilibili_read_mode", "hybrid") or "hybrid"
                ).lower()
                bcli = BilibiliClient(
                    state, ua, proxy=proxy, timeout=self.cfg.engine.request_timeout_seconds
                )
                collected: list[dict] = []
                api_err = ""
                if configured_mode in {"api", "hybrid"}:
                    async with bcli.session_scope():
                        up_rows, _, up_err = await bcli.fetch_up_fulllist_comments(
                            bvid=item_id, pn=1, ps=50
                        )
                        for r in up_rows:
                            row_copy = dict(r)
                            row_copy.pop("aweme_id", None)
                            collected.append(row_copy)
                        pub_rows, pub_err = await bcli.fetch_comments(
                            item_id, max_count=200, include_replies=True
                        )
                        collected.extend(pub_rows)
                        api_err = pub_err if (not collected and pub_err) else (up_err if not collected else "")
                    if not api_err or collected:
                        source = "api"
                if configured_mode == "browser" or (
                    configured_mode == "hybrid" and not collected and api_err and identity is not None
                ):
                    b_rows, b_err = await fetch_bili_comments(
                        self.browser, identity, item_id, max_count=200
                    )
                    source = "browser_fallback" if configured_mode == "hybrid" else "browser"
                    if b_rows:
                        collected = b_rows
                        error = ""
                    else:
                        error = b_err or api_err
                else:
                    error = api_err
                dedup: dict[str, dict] = {}
                for c in collected:
                    cid = c.get("comment_id")
                    if cid and cid not in known and cid not in dedup:
                        dedup[cid] = c
                fresh = list(dedup.values())
            elif platform == "rednote":
                configured_mode = self._rednote_read_mode()
                rcli = (
                    self._rednote_client(identity, state, proxy)
                    if configured_mode in {"api", "hybrid"}
                    else None
                )
                if configured_mode == "api" and rcli is None:
                    return {
                        "ok": False, "fetched": 0, "added": 0,
                        "error": "海外小红书账号缺 a1 Cookie,无法直连抓评论",
                        "source": "api", "configured_mode": configured_mode,
                    }
                if rcli is not None and configured_mode in {"api", "hybrid"}:
                    try:
                        async with rcli.session_scope():
                            fresh = await self._rednote_fetch_comments(
                                rcli, item_id, xsec_token, known
                            )
                        source = "api"
                    except Exception as exc:
                        if configured_mode == "api":
                            raise
                        error = str(exc)
                if configured_mode == "browser" or (configured_mode == "hybrid" and not fresh and (rcli is None or error)):
                    raw, b_err = await fetch_rednote_comments(
                        self.browser, identity, item_id, known,
                        xsec_token=xsec_token, xsec_source="pc_feed",
                        max_scrolls=self.cfg.engine.comment_max_scrolls,
                        block_media=self.cfg.engine.block_media_resources,
                    )
                    source = "browser_fallback" if (configured_mode == "hybrid" and rcli is not None) else "browser"
                    error = b_err or ""
                    fresh = [
                        c for c in (parse_rednote_comment(rc) for rc in flatten_rednote_comments(raw))
                        if c and c["comment_id"] not in known
                    ]
            elif platform == "instagram":
                configured_mode = str(
                    getattr(self.cfg.engine, "instagram_read_mode", "hybrid") or "hybrid"
                ).lower()
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
                collected_ig: list[dict] = []
                api_err = ""
                if configured_mode in {"api", "hybrid"}:
                    async with icli.session_scope():
                        collected_ig, api_err = await icli.fetch_comments(
                            item_id, max_count=200, include_replies=True
                        )
                    if not api_err or collected_ig:
                        source = "api"
                auth_failed = classify_platform_error(api_err)[0] == RiskCategory.AUTH
                if configured_mode == "browser" or (
                    configured_mode == "hybrid" and not collected_ig and api_err and not auth_failed and identity is not None
                ):
                    ig_rows, ig_err = await fetch_ig_comments(
                        self.browser, identity, item_id, max_count=200
                    )
                    source = "browser_fallback" if configured_mode == "hybrid" else "browser"
                    if ig_rows:
                        collected_ig = ig_rows
                        error = ""
                    else:
                        error = ig_err or api_err
                else:
                    error = api_err
                dedup_ig: dict[str, dict] = {}
                for c in collected_ig:
                    cid = c.get("comment_id")
                    if cid and cid not in known and cid not in dedup_ig:
                        dedup_ig[cid] = c
                fresh = list(dedup_ig.values())
            elif platform == "twitter":
                configured_mode = str(
                    getattr(self.cfg.engine, "twitter_read_mode", "hybrid") or "hybrid"
                ).lower()
                if identity is None:
                    error = "缺少 Twitter 账号浏览器身份"
                else:
                    tw_rows, tw_err, source = await self._tw_read_comments(
                        identity,
                        item_id,
                        max_count=200,
                        include_replies=True,
                    )
                    error = "" if tw_rows or tw_err == "empty" else tw_err
                    dedup_tw: dict[str, dict] = {}
                    for c in tw_rows:
                        cid = c.get("comment_id")
                        if cid and cid not in known and cid not in dedup_tw:
                            dedup_tw[cid] = c
                    fresh = list(dedup_tw.values())
            else:
                return {"ok": False, "fetched": 0, "added": 0,
                        "error": f"不支持的平台:{platform}",
                        "source": "unavailable", "configured_mode": "unavailable"}
        except (XhsApiError, RednoteApiError) as e:
            error = e
        except Exception as e:
            log.warning("本账号作品评论抓取失败 %s/%s: %s", platform, item_id, e)
            category, _signal = classify_platform_error(e)
            error = (e if category in {
                RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK
            } else repr(e))
            return {"ok": False, "fetched": 0, "added": 0,
                    "error": error, "source": source,
                    "configured_mode": configured_mode}
        # 去重落库(watch_id=0 = 本账号作品来源)
        added = 0
        with get_session() as s:
            for c in fresh:
                cid = c.get("comment_id")
                if not cid:
                    continue
                exists = s.exec(select(CommentRecord).where(
                    CommentRecord.watch_id == 0,
                    CommentRecord.aweme_id == item_id,
                    CommentRecord.comment_id == cid)).first()
                if exists:
                    continue
                s.add(CommentRecord(platform=platform, watch_id=0, aweme_id=item_id, **c))
                added += 1
            s.commit()
        return {"ok": not error or added > 0, "fetched": len(fresh),
                "added": added, "error": error, "source": source,
                "configured_mode": configured_mode}

    async def fetch_douyin_follows_direct(self, account_id: int, direction: str):
        return await self.guarded_read_pair(
            account_id, OperationKind.READ_HEAVY,
            f"follows:{account_id}:{direction}",
            lambda: self._fetch_douyin_follows_direct_locked(
                account_id, direction),
            empty_result=[])

    async def _fetch_douyin_follows_direct_locked(
            self, account_id: int, direction: str, progress=None):
        """抖音关注/粉丝直连(following/follower list 分页,比弹窗滚动抓得全)。
        返回 (归一用户列表, error);拿不到时上层回退浏览器拦截,故失败无副作用。"""
        from app.browser.account_hub import _norm_follow_user
        with get_session() as s:
            acc = s.get(DouyinAccount, account_id)
            if not acc:
                return [], "账号不存在"
            if acc.status == "invalid":
                return [], "账号登录态已失效"
            if self._proxy_bad(acc):
                return [], "账号代理不可用"
            state = acc.storage_state or acc.creator_storage_state or ""
            ua = acc.ua or self.cfg.engine.user_agent
            proxy = acc.proxy or ""
            sec_uid = acc.sec_uid or ""
            direct_environment = douyin_client_environment(acc)
        cookie = dy_cookie_from_state(state)
        if not cookie:
            return [], "no_cookie"
        client = DouyinClient(cookie, ua,
                              timeout=self.cfg.engine.request_timeout_seconds,
                              proxy=proxy, **direct_environment)
        out = []

        async def receive_page(rows: list[dict], meta: dict):
            for row in rows:
                normalized = _norm_follow_user(row, direction)
                if normalized:
                    out.append(normalized)
            if progress is not None:
                update = dict(meta)
                update["fetched"] = len(out)
                result = progress(update)
                if inspect.isawaitable(result):
                    await result

        try:
            async with client.session_scope():
                await client.fetch_all_follows(
                    "", sec_uid, direction, on_page=receive_page,
                    collect=False)
        except Exception as e:
            return [], repr(e)
        meta = getattr(client, "last_follow_meta", {})
        log.debug(
            "抖音关系直连同步 dir=%s pages=%s norm=%d complete=%s",
            direction, meta.get("pages", 0), len(out), meta.get("complete", False),
        )
        # HTTP 200 + 空 body、非法 JSON 等是传输/风控失败，不是“有效空列表”。
        # 保留 DouyinClient 的分类，让上层决定是否回退浏览器且不清空旧快照。
        return out, (client.last_error or ("" if out else "empty"))
