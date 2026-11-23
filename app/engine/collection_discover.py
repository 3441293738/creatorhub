"""抖音、快手、TikTok、视频号、小红书与海外小红书候选作品发现。"""
from __future__ import annotations

import math

from app.browser import fetch_channels_works, fetch_douyin_search, fetch_ks_search, fetch_ks_videos, fetch_rednote_comments, fetch_rednote_favorites, fetch_rednote_note_detail, fetch_rednote_notes, fetch_rednote_search, fetch_videos, fetch_xhs_favorites, fetch_xhs_note_detail, fetch_xhs_notes, fetch_xhs_search
from app.browser.tiktok_fetcher import fetch_tiktok_search
from app.engine.collection_helpers import _XHS_NOTE_TYPE, _XHS_SEARCH_SORT, _sort_channels_search_items, _sort_xhs_search_items
from app.engine.monitor_helpers import log
from app.models import KeywordCollectionJob
from app.platforms.channels import ChannelsProtocolClient, parse_channels_feed
from app.platforms.douyin import DouyinClient
from app.platforms.douyin.extract import Aweme
from app.platforms.rednote import RednoteApiClient, flatten_rednote_comments, parse_rednote_brief, parse_rednote_comment, parse_rednote_detail
from app.platforms.tiktok.client import TikTokNativeClient
from app.platforms.xhs import XhsApiClient, parse_note_brief, parse_note_detail
from app.risk import RiskCategory, classify_platform_error


class CollectionDiscoverMixin:
    async def _discover_douyin(self, account, keyword: str,
                               job: KeywordCollectionJob,
                               client: DouyinClient | None = None,
                               context=None) -> tuple[list[dict], str]:
        mode = str(getattr(self.cfg.engine, "douyin_read_mode", "hybrid") or "hybrid")
        if mode in {"api", "hybrid"} and client is not None:
            items, error = [], ""
            api_compatibility_failure = False
            try:
                log.debug("抖音搜索执行 HTTP search/single API 请求 (job=%s)", job.id)
                items, error = await client.search_awemes(
                    keyword,
                    max_results=job.max_contents_per_keyword,
                    max_pages=job.max_pages_per_keyword,
                    search_sort=job.search_sort,
                    publish_time=job.publish_time,
                    content_type=job.content_type,
                    min_likes=job.min_likes,
                    min_comments=job.min_comments,
                )
            except Exception as exc:
                message = f"抖音搜索接口异常: {exc!r}"
                category, _ = classify_platform_error(exc)
                if mode == "api" or category in {
                        RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK}:
                    return [], message
                log.info("抖音搜索 API 兼容性异常，回退浏览器: %s", type(exc).__name__)
                api_compatibility_failure = True
            if items:
                return items, ("抖音搜索接口失败: " + error if error else "")
            if not error and not api_compatibility_failure:
                return [], ""
            if error:
                message = f"抖音搜索接口失败: {error}"
                category, _ = classify_platform_error(message)
                if error == "verification_required" or category in {
                        RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK}:
                    log.warning("抖音搜索 API 返回验证/风控信号(%s)，进入冷却且不打开浏览器", error)
                    return [], message
                if mode == "api":
                    return [], message
                log.info("抖音搜索 API 兼容性失败(%s)，回退浏览器", error)
        elif mode == "api":
            return [], "抖音搜索接口异常: API 客户端不可用"
        identity = self.browser.identity_for(account)
        # 抖音搜索在后台无头上下文中极易直落“验证码中间页”；hybrid 模式仅在真正需要
        # 回退页面搜索时按需申请临时有头窗口，避免正常 API 请求时弹窗或无头回退锁死。
        headed_factory = (
            getattr(self.browser, "temporary_headed_context", None)
            if context is None else None
        )
        if callable(headed_factory):
            async with headed_factory(identity) as fallback_context:
                return await fetch_douyin_search(
                    self.browser, identity, keyword,
                    max_results=job.max_contents_per_keyword,
                    max_scrolls=job.max_pages_per_keyword,
                    stagnant_limit=job.stagnant_pages,
                    search_sort=job.search_sort,
                    publish_time=job.publish_time,
                    content_type=job.content_type,
                    min_likes=job.min_likes,
                    min_comments=job.min_comments,
                    captcha_wait_seconds=self.cfg.engine.douyin_captcha_wait_seconds,
                    block_media=self.cfg.engine.block_media_resources,
                    context=fallback_context,
                )
        return await fetch_douyin_search(
            self.browser, identity, keyword,
            max_results=job.max_contents_per_keyword,
            max_scrolls=job.max_pages_per_keyword,
            stagnant_limit=job.stagnant_pages,
            search_sort=job.search_sort,
            publish_time=job.publish_time,
            content_type=job.content_type,
            min_likes=job.min_likes,
            min_comments=job.min_comments,
            captcha_wait_seconds=self.cfg.engine.douyin_captcha_wait_seconds,
            block_media=self.cfg.engine.block_media_resources,
            context=context,
        )

    async def _discover_douyin_creator(
            self, account, job: KeywordCollectionJob,
            client: DouyinClient | None) -> tuple[list[dict], str]:
        mode = str(getattr(self.cfg.engine, "douyin_read_mode", "hybrid") or "hybrid")
        if mode in {"api", "hybrid"} and client is not None:
            try:
                values = await client.fetch_all_video_list(
                    job.source_ref,
                    max_pages=job.max_pages_per_keyword,
                    count=min(20, job.max_contents_per_keyword))
                if values or mode == "api":
                    return values[:job.max_contents_per_keyword], ""
            except Exception as exc:
                if mode == "api":
                    return [], f"抖音创作者作品读取失败: {exc}"
        values, _author, error = await fetch_videos(
            self.browser, self.browser.identity_for(account), job.source_ref, set(),
            max_scrolls=job.max_pages_per_keyword,
            block_media=self.cfg.engine.block_media_resources)
        return values[:job.max_contents_per_keyword], error

    async def _discover_ks(self, account, keyword: str,
                           job: KeywordCollectionJob) -> tuple[list[dict], str]:
        return await fetch_ks_search(
            self.browser,
            self.browser.identity_for(account),
            keyword,
            set(),
            max_scrolls=max(1, min(40, int(job.max_pages_per_keyword or 1))),
            stagnant_limit=max(1, min(8, int(job.stagnant_pages or 1))),
            block_media=self.cfg.engine.block_media_resources,
        )

    async def _discover_tiktok(self, account, identity, keyword: str,
                               job: KeywordCollectionJob) -> tuple[list[dict], str]:
        mode = str(getattr(self.cfg.engine, "tiktok_read_mode", "native") or "native")
        if mode in {"auto", "native"}:
            client = TikTokNativeClient(
                storage_state=getattr(account, "storage_state", "") or "",
                user_agent=identity.ua,
                proxy=identity.proxy,
                locale=identity.locale,
                timezone=identity.timezone_id,
                viewport=(identity.viewport_w, identity.viewport_h),
                region="US",
                device_seed=identity.fp_seed or str(identity.account_id or "creatorhub"),
                timeout=max(10, int(self.cfg.engine.request_timeout_seconds)),
            )
            native = await client.search(
                keyword, count=job.max_contents_per_keyword, offset=0)
            if native.items or mode == "native":
                return native.items, native.error
            if native.category == "empty_valid":
                return [], ""
            log.info(
                "TikTok 搜索离线接口兼容失败，回退页面: %s (HTTP=%s, bytes=%s)",
                native.category, native.http_status, native.body_length,
            )
        if mode == "auto":
            async with self.browser.temporary_headed_context(identity):
                return await fetch_tiktok_search(
                    self.browser, identity, keyword,
                    max_results=job.max_contents_per_keyword,
                    max_scrolls=job.max_pages_per_keyword,
                    stagnant_limit=job.stagnant_pages,
                    block_media=self.cfg.engine.block_media_resources,
                )
        return await fetch_tiktok_search(
            self.browser, identity, keyword,
            max_results=job.max_contents_per_keyword,
            max_scrolls=job.max_pages_per_keyword,
            stagnant_limit=job.stagnant_pages,
            block_media=self.cfg.engine.block_media_resources,
        )

    async def _discover_ks_creator(
            self, account, job: KeywordCollectionJob) -> tuple[list[dict], str]:
        values, _author, error = await fetch_ks_videos(
            self.browser,
            self.browser.identity_for(account),
            job.source_ref,
            set(),
            max_scrolls=max(1, min(40, int(job.max_pages_per_keyword or 1))),
            block_media=self.cfg.engine.block_media_resources,
        )
        return values[:job.max_contents_per_keyword], error

    async def _discover_channels_account(
            self, identity, job: KeywordCollectionJob, *,
            account=None,
            proto_client: ChannelsProtocolClient | None = None) -> tuple[list[dict], str]:
        mode = self._channels_read_mode()
        sec_uid = str(getattr(account, "sec_uid", "") or "").strip()
        has_finder_username = bool(sec_uid and (sec_uid.startswith("v2_") or "@finder" in sec_uid))

        if mode == "protocol":
            if proto_client is None:
                return [], "视频号原生协议采集尚未连接本机微信通道（请在「设置 → 视频号微信通道」开启一键本地代理或配置 CDP）"
            if not has_finder_username:
                return [], "当前视频号账号缺少 finderUsername(v2_...@finder)，无法走原生协议读取作品"
            values, _author, err = await proto_client.fetch_user_feeds(
                sec_uid,
                max_results=job.max_contents_per_keyword,
                max_pages=job.max_pages_per_keyword,
            )
            return values[:job.max_contents_per_keyword], err

        if mode == "auto" and proto_client is not None and has_finder_username:
            values, _author, proto_err = await proto_client.fetch_user_feeds(
                sec_uid,
                max_results=job.max_contents_per_keyword,
                max_pages=job.max_pages_per_keyword,
            )
            if values or not proto_err:
                return values[:job.max_contents_per_keyword], proto_err

        needed_pages = max(
            int(job.max_pages_per_keyword or 1),
            ((max(1, int(job.max_contents_per_keyword or 20)) + 4) // 5) + 1,
        )
        values, _author, error = await fetch_channels_works(
            self.browser,
            identity,
            set(),
            max_scrolls=max(1, min(40, int(job.max_pages_per_keyword or 1))),
            block_media=self.cfg.engine.block_media_resources,
            max_pages=min(120, needed_pages),
        )
        if job.max_contents_per_keyword and job.max_contents_per_keyword > 0:
            values = values[:job.max_contents_per_keyword]
        if values and job.download_media and proto_client is not None:
            enriched: list[dict] = []
            for item in values:
                brief = parse_channels_feed(item, job.video_quality or "highest")
                if brief is not None and not brief.medias and brief.aweme_id:
                    detail_obj, _ = await proto_client.fetch_feed_detail(
                        object_id=brief.aweme_id
                    )
                    if detail_obj:
                        enriched.append(detail_obj)
                        continue
                enriched.append(item)
            values = enriched
        return values, error

    async def _discover_channels_creator(
            self, job: KeywordCollectionJob, *,
            proto_client: ChannelsProtocolClient | None = None) -> tuple[list[dict], str]:
        if self._channels_read_mode() == "browser" or proto_client is None:
            return (
                [],
                "视频号创作者主页采集需要连接本机已登录微信（请在「设置 → 视频号微信通道」开启一键本地代理并在微信内打开一次视频号页面）",
            )
        values, _author, err = await proto_client.fetch_user_feeds(
            job.source_ref,
            max_results=job.max_contents_per_keyword,
            max_pages=job.max_pages_per_keyword,
        )
        return values[:job.max_contents_per_keyword], err

    async def _discover_channels_keyword(
            self, keyword: str, job: KeywordCollectionJob, *,
            proto_client: ChannelsProtocolClient | None = None) -> tuple[list[dict], str]:
        if self._channels_read_mode() == "browser" or proto_client is None:
            return (
                [],
                "视频号关键词搜索需要连接本机已登录微信（请在「设置 → 视频号微信通道」开启一键本地代理并在微信内打开一次视频号页面）",
            )
        pages = max(1, min(40, int(job.max_pages_per_keyword or 1)))
        needs_local_filter = bool(
            job.publish_time != "all"
            or job.min_likes
            or job.min_comments
            or job.search_sort in {"latest", "most_liked"}
        )
        candidate_limit = (
            max(job.max_contents_per_keyword, min(45, pages * 15))
            if needs_local_filter
            else (
                max(job.max_contents_per_keyword, min(15, job.max_contents_per_keyword * 3))
                if job.max_comments_per_content > 0
                else job.max_contents_per_keyword
            )
        )
        prefer_engaged = bool(
            job.max_comments_per_content > 0 or job.search_sort == "most_liked"
        )
        values, err = await proto_client.search_feeds(
            keyword,
            max_results=candidate_limit,
            max_pages=pages,
            prefer_engaged=prefer_engaged,
        )
        return _sort_channels_search_items(
            values,
            job.search_sort,
            prefer_engaged=(job.max_comments_per_content > 0),
        ), err

    async def _discover_xhs(self, client: XhsApiClient, keyword: str,
                            job: KeywordCollectionJob) -> tuple[list[dict], str]:
        found: dict[str, dict] = {}
        pages = max(1, min(40, int(job.max_pages_per_keyword or 1)))
        needs_local_filter = bool(
            job.publish_time != "all" or job.min_likes or job.min_comments)
        candidate_limit = (pages * 20 if needs_local_filter
                           else job.max_contents_per_keyword)
        stagnant = 0
        try:
            for page in range(1, pages + 1):
                raw_items = await client.search_notes(
                    keyword,
                    page=page,
                    page_size=20,
                    sort=_XHS_SEARCH_SORT.get(job.search_sort, "general"),
                    note_type=_XHS_NOTE_TYPE.get(job.content_type, 0),
                )
                before = len(found)
                for raw in raw_items:
                    brief = parse_note_brief(raw)
                    if brief:
                        found.setdefault(brief["note_id"], raw)
                    if len(found) >= candidate_limit:
                        break
                stagnant = stagnant + 1 if len(found) == before else 0
                if len(found) >= candidate_limit:
                    break
                if not raw_items or stagnant >= max(1, int(job.stagnant_pages or 1)):
                    break
                await self._xhs_gap(0.6)
        except Exception as exc:
            return _sort_xhs_search_items(
                list(found.values()), job.search_sort), f"小红书搜索失败: {exc}"
        return _sort_xhs_search_items(list(found.values()), job.search_sort), ""

    async def _discover_xhs_browser(
            self, identity, keyword: str, job: KeywordCollectionJob
            ) -> tuple[list[dict], str]:
        values, error = await fetch_xhs_search(
            self.browser,
            identity,
            keyword,
            set(),
            max_scrolls=max(1, min(40, int(job.max_pages_per_keyword or 1))),
            stagnant_limit=max(1, min(8, int(job.stagnant_pages or 1))),
            search_sort=job.search_sort,
            content_type=job.content_type,
            block_media=self.cfg.engine.block_media_resources,
        )
        return _sort_xhs_search_items(values, job.search_sort), error

    async def _discover_xhs_favorites_browser(
            self, identity, account,
            job: KeywordCollectionJob) -> tuple[list[dict], str]:
        values, error = await fetch_xhs_favorites(
            self.browser, identity, str(getattr(account, "sec_uid", "") or ""),
            board_id=job.source_ref if job.source_type == "favorite_board" else "",
            board_name=job.source_name,
            max_scrolls=max(1, min(40, int(job.max_pages_per_keyword or 1))),
            stagnant_limit=max(1, min(8, int(job.stagnant_pages or 1))),
        )
        return values, error

    async def _discover_xhs_favorites_api(
            self, client: XhsApiClient, job: KeywordCollectionJob) -> tuple[list[dict], str]:
        found: dict[str, dict] = {}
        cursor = ""
        pages = max(1, min(40, int(job.max_pages_per_keyword or 1)))
        try:
            for page in range(pages):
                data = await client.favorite_notes(
                    board_id=(job.source_ref if job.source_type == "favorite_board" else ""),
                    cursor=cursor, page_size=min(50, job.max_contents_per_keyword))
                values = data.get("notes") or data.get("items") or []
                before = len(found)
                for raw in values:
                    brief = parse_note_brief(raw)
                    if brief:
                        raw.setdefault("xsec_source", "pc_user")
                        found.setdefault(brief["note_id"], raw)
                    if len(found) >= job.max_contents_per_keyword:
                        break
                if len(found) >= job.max_contents_per_keyword:
                    break
                next_cursor = str(data.get("cursor") or data.get("next_cursor") or "")
                has_more = bool(data.get("has_more"))
                if len(found) == before or not has_more or not next_cursor or next_cursor == cursor:
                    break
                cursor = next_cursor
                if page + 1 < pages:
                    await self._xhs_gap(0.6)
        except Exception as exc:
            return list(found.values()), f"小红书收藏读取失败: {exc}"
        return list(found.values()), ""

    async def _discover_xhs_creator_browser(
            self, identity, job: KeywordCollectionJob) -> tuple[list[dict], str]:
        values, _author, error = await fetch_xhs_notes(
            self.browser, identity, job.source_ref, set(),
            max_scrolls=max(1, min(40, job.max_pages_per_keyword)),
            block_media=self.cfg.engine.block_media_resources,
            ssr_fallback=True)
        return values[:job.max_contents_per_keyword], error

    async def _discover_xhs_creator_api(
            self, client: XhsApiClient, job: KeywordCollectionJob) -> tuple[list[dict], str]:
        found: dict[str, dict] = {}
        cursor = ""
        try:
            for page in range(max(1, min(40, job.max_pages_per_keyword))):
                data = await client.notes_by_creator(
                    job.source_ref, cursor=cursor,
                    page_size=min(30, job.max_contents_per_keyword),
                    xsec_source="pc_feed")
                values = data.get("notes") or []
                for raw in values:
                    brief = parse_note_brief(raw)
                    if brief:
                        raw.setdefault("xsec_source", "pc_feed")
                        found.setdefault(brief["note_id"], raw)
                    if len(found) >= job.max_contents_per_keyword:
                        break
                next_cursor = str(data.get("cursor") or data.get("next_cursor") or "")
                if (len(found) >= job.max_contents_per_keyword or not data.get("has_more")
                        or not next_cursor or next_cursor == cursor):
                    break
                cursor = next_cursor
                if page + 1 < job.max_pages_per_keyword:
                    await self._xhs_gap(0.6)
        except Exception as exc:
            return list(found.values()), f"小红书创作者作品读取失败: {exc}"
        return list(found.values()), ""

    async def _materialize_xhs(self, client: XhsApiClient, raw: dict) \
            -> tuple[Aweme | None, dict, str]:
        brief = parse_note_brief(raw)
        if not brief:
            return None, {}, "搜索结果缺少 note_id"
        card = {}
        error = ""
        try:
            card = await client.note_detail(
                brief["note_id"], xsec_token=brief.get("xsec_token", ""),
                xsec_source=brief.get("xsec_source") or "pc_search")
        except Exception as exc:
            error = f"详情抓取失败: {exc}"
        # Media absence is a download failure, not a reason to erase the
        # detail's author/title/counts (or the list metadata if detail failed).
        aweme = parse_note_detail(
            card if isinstance(card, dict) else {}, brief, require_media=False)
        return aweme, {"brief": brief, "card": card}, error

    async def _materialize_xhs_browser(self, identity, raw: dict) \
            -> tuple[Aweme | None, dict, str]:
        brief = parse_note_brief(raw)
        if not brief:
            return None, {}, "搜索结果缺少 note_id"
        card, error = await fetch_xhs_note_detail(
            self.browser,
            identity,
            brief["note_id"],
            xsec_token=brief.get("xsec_token", ""),
            xsec_source=brief.get("xsec_source") or "pc_search",
            block_media=self.cfg.engine.block_media_resources,
        )
        aweme = parse_note_detail(
            card if isinstance(card, dict) else {}, brief, require_media=False)
        return aweme, {"brief": brief, "card": card or {}}, error

    async def _discover_rednote_source(
        self,
        identity,
        account,
        keyword: str,
        job: KeywordCollectionJob,
        *,
        rednote_client: RednoteApiClient | None = None,
    ) -> tuple[list[dict], str]:
        mode = self._rednote_read_mode()
        items: list[dict] = []
        err = ""
        if mode in {"api", "hybrid"} and rednote_client is not None:
            if job.source_type in {"favorite_all", "favorite_board"}:
                items, err = await self._discover_xhs_favorites_api(rednote_client, job)
            elif job.source_type == "creator":
                items, err = await self._discover_xhs_creator_api(rednote_client, job)
            else:
                items, err = await self._discover_xhs(rednote_client, keyword, job)
            if err:
                err = err.replace("小红书", "海外小红书")
            if items or mode == "api":
                return items, err
            if err and classify_platform_error(err)[0] in {
                RiskCategory.AUTH,
                RiskCategory.NETWORK,
            }:
                return [], err
        elif mode == "api":
            return [], "海外小红书 API 客户端不可用"

        if job.source_type in {"favorite_all", "favorite_board"}:
            values, b_err = await fetch_rednote_favorites(
                self.browser,
                identity,
                str(getattr(account, "sec_uid", "") or ""),
                board_id=job.source_ref if job.source_type == "favorite_board" else "",
                board_name=job.source_name,
                max_scrolls=max(1, min(40, int(job.max_pages_per_keyword or 1))),
                stagnant_limit=max(1, min(8, int(job.stagnant_pages or 1))),
            )
            return values, b_err
        if job.source_type == "creator":
            values, _author, b_err = await fetch_rednote_notes(
                self.browser,
                identity,
                job.source_ref,
                set(),
                max_scrolls=max(1, min(40, int(job.max_pages_per_keyword or 1))),
                block_media=self.cfg.engine.block_media_resources,
                ssr_fallback=True,
            )
            return values[: job.max_contents_per_keyword], b_err
        values, b_err = await fetch_rednote_search(
            self.browser,
            identity,
            keyword,
            set(),
            max_scrolls=max(1, min(40, int(job.max_pages_per_keyword or 1))),
            stagnant_limit=max(1, min(8, int(job.stagnant_pages or 1))),
            search_sort=job.search_sort,
            content_type=job.content_type,
            block_media=self.cfg.engine.block_media_resources,
        )
        return _sort_xhs_search_items(values, job.search_sort), b_err

    async def _materialize_rednote(
        self,
        identity,
        raw: dict,
        *,
        rednote_client: RednoteApiClient | None = None,
    ) -> tuple[Aweme | None, dict, str]:
        brief = parse_rednote_brief(raw)
        if not brief:
            return None, {}, "搜索结果缺少 note_id"
        mode = self._rednote_read_mode()
        card: dict = {}
        error = ""
        if mode in {"api", "hybrid"} and rednote_client is not None:
            try:
                card = await rednote_client.note_detail(
                    brief["note_id"],
                    xsec_token=brief.get("xsec_token", ""),
                    xsec_source=brief.get("xsec_source") or "pc_search",
                )
            except Exception as exc:
                error = f"详情抓取失败: {exc}"
            if (
                not card
                and mode == "hybrid"
                and (
                    not error
                    or classify_platform_error(error)[0]
                    not in {RiskCategory.AUTH, RiskCategory.NETWORK}
                )
            ):
                b_card, b_err = await fetch_rednote_note_detail(
                    self.browser,
                    identity,
                    brief["note_id"],
                    xsec_token=brief.get("xsec_token", ""),
                    xsec_source=brief.get("xsec_source") or "pc_search",
                    block_media=self.cfg.engine.block_media_resources,
                    keep_context=True,
                )
                if b_card:
                    card, error = b_card, ""
                elif b_err:
                    error = b_err
        elif mode == "api":
            error = "详情抓取失败: 海外小红书 API 客户端不可用"
        else:
            b_card, error = await fetch_rednote_note_detail(
                self.browser,
                identity,
                brief["note_id"],
                xsec_token=brief.get("xsec_token", ""),
                xsec_source=brief.get("xsec_source") or "pc_search",
                block_media=self.cfg.engine.block_media_resources,
            )
            card = b_card if isinstance(b_card, dict) else {}
        aweme = parse_rednote_detail(
            card if isinstance(card, dict) else {}, brief, require_media=False
        )
        return aweme, {"brief": brief, "card": card or {}}, error

    async def _rednote_comments(
        self,
        identity,
        note_id: str,
        xsec_token: str,
        xsec_source: str,
        limit: int,
        include_replies: bool,
        *,
        rednote_client: RednoteApiClient | None = None,
    ) -> tuple[list[dict], str]:
        if limit <= 0:
            return [], ""
        mode = self._rednote_read_mode()
        parsed: list[dict] = []
        error = ""
        if mode in {"api", "hybrid"} and rednote_client is not None:
            try:
                page = await rednote_client.collect_note_comments(
                    note_id,
                    xsec_token=xsec_token,
                    xsec_source=xsec_source or "pc_search",
                    max_comments=limit,
                    max_requests=max(1, min(20, math.ceil(limit / 10) + 1)),
                    include_replies=include_replies,
                    request_interval=max(0.5, self.cfg.engine.xhs_item_gap_seconds),
                )
                parsed = [
                    item
                    for item in (parse_rednote_comment(v) for v in page["comments"])
                    if item
                ]
            except Exception as exc:
                error = f"评论抓取失败: {exc}"
        if (
            mode == "browser"
            or (
                mode == "hybrid"
                and not parsed
                and (
                    rednote_client is None
                    or (
                        error
                        and classify_platform_error(error)[0]
                        not in {RiskCategory.AUTH, RiskCategory.NETWORK}
                    )
                )
            )
        ):
            raw, b_error = await fetch_rednote_comments(
                self.browser,
                identity,
                note_id,
                set(),
                xsec_token=xsec_token,
                xsec_source=xsec_source or "pc_search",
                max_scrolls=max(1, min(20, math.ceil(limit / 10) + 1)),
                block_media=self.cfg.engine.block_media_resources,
                keep_context=True,
                include_replies=include_replies,
            )
            values = flatten_rednote_comments(raw) if include_replies else raw
            b_parsed = [
                item for item in (parse_rednote_comment(v) for v in values) if item
            ]
            if b_parsed or not b_error:
                parsed, error = b_parsed, ""
            else:
                error = b_error
        if not include_replies:
            parsed = [item for item in parsed if not item.get("reply_to")]
        return self._dedupe_comments(parsed, limit), error
