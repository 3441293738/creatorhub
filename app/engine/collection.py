"""抖音 / 小红书关键词批量采集流水线。"""
from __future__ import annotations

import asyncio
import json
import math
import random
from pathlib import Path

from sqlalchemy import func
from sqlmodel import select

from ..browser import (
    fetch_comments,
    fetch_douyin_search,
    fetch_xhs_comments,
    fetch_xhs_note_detail,
    fetch_xhs_search,
)
from ..db import get_session
from ..models import (
    KeywordCollectionComment,
    KeywordCollectionContent,
    KeywordCollectionJob,
)
from ..platforms.douyin import (
    DouyinClient,
    cookie_from_state as douyin_cookie_from_state,
    parse_aweme,
    parse_comment,
    safe_title,
)
from ..platforms.douyin.extract import Aweme
from ..platforms.xhs import (
    XhsApiClient,
    cookie_str_from_state,
    flatten_comments as flatten_xhs_comments,
    has_a1,
    parse_comment as parse_xhs_comment,
    parse_note_brief,
    parse_note_detail,
)
from ..risk import classify_platform_error, RiskCategory


def _loads_keywords(raw: str) -> list[str]:
    try:
        values = json.loads(raw or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [str(value).strip() for value in values if str(value).strip()]


def _media_json(aweme: Aweme) -> str:
    return json.dumps([
        {"url": media.url, "kind": media.kind, "ext": media.ext,
         "index": media.index}
        for media in aweme.medias
    ], ensure_ascii=False)


def _author_id(raw: dict, platform: str) -> str:
    if platform == "douyin":
        author = raw.get("author") or {}
        return str(author.get("sec_uid") or author.get("uid") or "")
    card = raw.get("note_card") or raw
    user = card.get("user") or {}
    return str(user.get("user_id") or user.get("userid") or user.get("id") or "")


class KeywordCollector:
    """执行单个持久化采集任务；调度与账号风控由 MonitorEngine 负责。"""

    def __init__(self, cfg, browser, downloader):
        self.cfg = cfg
        self.browser = browser
        self.downloader = downloader

    async def _xhs_gap(self, seconds: float) -> None:
        base = max(0.0, float(seconds or 0.0))
        if not base:
            return
        jitter = min(1.0, max(
            0.0, float(self.cfg.engine.xhs_request_jitter or 0.0)))
        await asyncio.sleep(base * random.uniform(1.0, 1.0 + jitter))

    def _xhs_browser_reads_enabled(self) -> bool:
        return bool(
            self.cfg.engine.xhs_read_mode == "browser"
            and callable(getattr(self.browser, "visible_page", None))
        )

    def _direct_request_ua(self, identity) -> str:
        resolver = getattr(self.browser, "direct_request_user_agent", None)
        if callable(resolver):
            return resolver(identity)
        return str(getattr(identity, "ua", "") or self.cfg.engine.user_agent)

    def _result(self, job_id: int, *, stopped: bool = False) -> dict:
        contents, comments = self._refresh_counts(job_id)
        final = self._job(job_id)
        return {
            "canceled": False,
            "stopped": stopped,
            "errors": final.error_count if final else 0,
            "contents": contents,
            "comments": comments,
        }

    @staticmethod
    def _job(job_id: int) -> KeywordCollectionJob | None:
        with get_session() as session:
            return session.get(KeywordCollectionJob, job_id)

    @staticmethod
    def _cancel_requested(job_id: int) -> bool:
        job = KeywordCollector._job(job_id)
        return not job or job.cancel_requested or job.status == "canceled"

    @staticmethod
    def _progress(job_id: int, *, keyword: str | None = None,
                  step: str | None = None) -> None:
        with get_session() as session:
            job = session.get(KeywordCollectionJob, job_id)
            if not job:
                return
            if keyword is not None:
                job.current_keyword = keyword
            if step is not None:
                job.current_step = step
            session.add(job)
            session.commit()

    @staticmethod
    def _record_error(job_id: int, message: str) -> None:
        message = str(message or "").strip()
        if not message:
            return
        with get_session() as session:
            job = session.get(KeywordCollectionJob, job_id)
            if not job:
                return
            job.error_count += 1
            lines = [line for line in (job.error or "").splitlines() if line]
            lines.append(message[:360])
            job.error = "\n".join(lines[-8:])[:2400]
            session.add(job)
            session.commit()

    @staticmethod
    def _mark_account_invalid(account_id: int) -> None:
        """搜索页明确出现登录墙时同步账号状态，避免继续把旧 Cookie 当作可用。"""
        from ..models import DouyinAccount
        with get_session() as session:
            account = session.get(DouyinAccount, account_id)
            if account and account.status != "invalid":
                account.status = "invalid"
                session.add(account)
                session.commit()

    @staticmethod
    def _refresh_counts(job_id: int) -> tuple[int, int]:
        with get_session() as session:
            contents = int(session.exec(
                select(func.count(KeywordCollectionContent.id))
                .where(KeywordCollectionContent.job_id == job_id)
            ).one() or 0)
            comments = int(session.exec(
                select(func.count(KeywordCollectionComment.id))
                .where(KeywordCollectionComment.job_id == job_id)
            ).one() or 0)
            job = session.get(KeywordCollectionJob, job_id)
            if job:
                job.content_count = contents
                job.comment_count = comments
                session.add(job)
                session.commit()
        return contents, comments

    async def _discover_douyin(self, account, keyword: str,
                               job: KeywordCollectionJob,
                               context=None) -> tuple[list[dict], str]:
        identity = self.browser.identity_for(account)
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

    async def _discover_xhs(self, client: XhsApiClient, keyword: str,
                            limit: int) -> tuple[list[dict], str]:
        found: dict[str, dict] = {}
        pages = max(1, min(10, math.ceil(limit / 20) + 1))
        try:
            for page in range(1, pages + 1):
                raw_items = await client.search_notes(keyword, page=page, page_size=20)
                before = len(found)
                for raw in raw_items:
                    brief = parse_note_brief(raw)
                    if brief:
                        found.setdefault(brief["note_id"], raw)
                    if len(found) >= limit:
                        break
                if len(found) >= limit or not raw_items or len(found) == before:
                    break
                await asyncio.sleep(0.6)
        except Exception as exc:
            return list(found.values())[:limit], f"小红书搜索失败: {exc}"
        return list(found.values())[:limit], ""

    async def _discover_xhs_browser(
            self, identity, keyword: str, job: KeywordCollectionJob
            ) -> tuple[list[dict], str]:
        values, error = await fetch_xhs_search(
            self.browser,
            identity,
            keyword,
            set(),
            max_scrolls=max(1, min(10, int(job.max_pages_per_keyword or 1))),
            block_media=self.cfg.engine.block_media_resources,
        )
        return values[:job.max_contents_per_keyword], error

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
                xsec_source="pc_search")
        except Exception as exc:
            error = f"详情抓取失败: {exc}"
        aweme = parse_note_detail(card, brief) if card else None
        if aweme is None:
            aweme = Aweme(
                aweme_id=brief["note_id"], desc=brief.get("title", ""),
                create_time=int(brief.get("create_time") or 0), author_name="",
                media_type="video" if brief.get("type") == "video" else "images",
            )
            aweme.platform = "xhs"
            aweme.cover = brief.get("cover") or ""
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
            xsec_source="pc_search",
            block_media=self.cfg.engine.block_media_resources,
        )
        aweme = parse_note_detail(card or {}, brief) if card else None
        if aweme is None:
            aweme = Aweme(
                aweme_id=brief["note_id"], desc=brief.get("title", ""),
                create_time=int(brief.get("create_time") or 0), author_name="",
                media_type="video" if brief.get("type") == "video" else "images",
            )
            aweme.platform = "xhs"
            aweme.cover = brief.get("cover") or ""
        return aweme, {"brief": brief, "card": card or {}}, error

    @staticmethod
    def _upsert_content(job: KeywordCollectionJob, keyword: str, aweme: Aweme,
                        author_id: str, xsec_token: str, detail_error: str) \
            -> KeywordCollectionContent:
        with get_session() as session:
            row = session.exec(
                select(KeywordCollectionContent)
                .where(KeywordCollectionContent.job_id == job.id)
                .where(KeywordCollectionContent.keyword == keyword)
                .where(KeywordCollectionContent.aweme_id == aweme.aweme_id)
            ).first()
            if row is None:
                row = KeywordCollectionContent(
                    job_id=job.id, platform=job.platform, keyword=keyword,
                    aweme_id=aweme.aweme_id,
                )
            row.desc = aweme.desc
            row.author_name = aweme.author_name
            row.author_id = author_id
            row.media_type = aweme.media_type
            row.create_time = aweme.create_time
            row.cover_url = aweme.cover or ""
            row.like_count = aweme.like_count
            row.comment_count = aweme.comment_count
            row.media_json = _media_json(aweme)
            row.xsec_token = xsec_token
            row.error = detail_error
            if job.download_media and aweme.medias and row.download_status != "done":
                row.download_status = "pending"
            elif job.download_media and not aweme.medias:
                row.download_status = "failed"
                row.error = detail_error or "未取得媒体地址"
            elif not job.download_media:
                row.download_status = "skipped"
            session.add(row)
            session.commit()
            session.refresh(row)
            return row

    async def _download(self, job: KeywordCollectionJob,
                        content: KeywordCollectionContent, aweme: Aweme,
                        proxy: str) -> None:
        if not job.download_media or not aweme.medias or content.download_status == "done":
            return
        with get_session() as session:
            row = session.get(KeywordCollectionContent, content.id)
            if row:
                row.download_status = "downloading"
                session.add(row)
                session.commit()
        root = Path(job.download_dir).expanduser() if job.download_dir else \
            Path(self.cfg.engine.media_dir) / "keyword-collections"
        base_dir = root / f"job_{job.id}" / safe_title(content.keyword)
        routed_proxy = proxy if self.cfg.engine.route_download_via_proxy else ""
        ok, local_path, error = await self.downloader.download_aweme(
            aweme, str(base_dir), routed_proxy)
        with get_session() as session:
            row = session.get(KeywordCollectionContent, content.id)
            if row:
                row.download_status = "done" if ok else "failed"
                row.local_path = local_path
                if error:
                    row.error = (row.error + "; " if row.error else "") + error
                session.add(row)
                session.commit()
        if not ok:
            self._record_error(
                job.id,
                f"{content.keyword}/{content.aweme_id}: 媒体下载失败: "
                f"{error or '下载器未返回成功状态'}",
            )

    async def _douyin_comments(self, account, client: DouyinClient,
                                aweme: Aweme, limit: int,
                                include_replies: bool,
                                context=None) -> tuple[list[dict], str]:
        if limit <= 0:
            return [], ""
        error = ""
        raw: list[dict] = []
        try:
            raw = await client.fetch_all_comments(
                aweme.aweme_id, max_pages=max(1, min(20, math.ceil(limit / 20))),
                with_replies=include_replies,
                max_reply_pages=max(1, min(8, math.ceil(limit / 20))),
            )
        except Exception as exc:
            error = f"评论接口失败: {exc}"
        parsed = [item for item in (parse_comment(value) for value in raw) if item]
        if not include_replies:
            parsed = [item for item in parsed if not item.get("reply_to")]

        # 直连接口被静默降级时，用当前账号的浏览器评论区作一次兜底。
        if not parsed and aweme.comment_count > 0:
            identity = self.browser.identity_for(account)
            fallback, browser_error = await fetch_comments(
                self.browser, identity, aweme.aweme_id, set(),
                max_scrolls=max(2, min(20, math.ceil(limit / 12))),
                block_media=self.cfg.engine.block_media_resources,
                context=context,
            )
            parsed = [item for item in (parse_comment(value) for value in fallback) if item]
            if not include_replies:
                parsed = [item for item in parsed if not item.get("reply_to")]
            error = "" if parsed else (browser_error or error)
        return self._dedupe_comments(parsed, limit), error

    async def _xhs_comments(self, client: XhsApiClient, note_id: str,
                            xsec_token: str, limit: int,
                            include_replies: bool) -> tuple[list[dict], str]:
        if limit <= 0:
            return [], ""
        parsed: list[dict] = []
        cursor = ""
        error = ""
        try:
            for _ in range(max(1, min(20, math.ceil(limit / 10) + 1))):
                page = await client.note_comments(
                    note_id, xsec_token=xsec_token, cursor=cursor,
                    xsec_source="pc_search")
                roots = page.get("comments") or []
                values = flatten_xhs_comments(roots) if include_replies else roots
                parsed.extend(item for item in
                              (parse_xhs_comment(value) for value in values) if item)
                if len(parsed) >= limit or not page.get("has_more"):
                    break
                next_cursor = str(page.get("cursor") or "")
                if not next_cursor or next_cursor == cursor:
                    break
                cursor = next_cursor
                await asyncio.sleep(0.5)
        except Exception as exc:
            error = f"评论抓取失败: {exc}"
        if not include_replies:
            parsed = [item for item in parsed if not item.get("reply_to")]
        return self._dedupe_comments(parsed, limit), error

    async def _xhs_comments_browser(
            self, identity, note_id: str, xsec_token: str, limit: int,
            include_replies: bool) -> tuple[list[dict], str]:
        if limit <= 0:
            return [], ""
        raw, error = await fetch_xhs_comments(
            self.browser,
            identity,
            note_id,
            set(),
            xsec_token=xsec_token,
            xsec_source="pc_search",
            max_scrolls=max(1, min(20, math.ceil(limit / 10) + 1)),
            block_media=self.cfg.engine.block_media_resources,
        )
        values = flatten_xhs_comments(raw) if include_replies else raw
        parsed = [item for item in
                  (parse_xhs_comment(value) for value in values) if item]
        if not include_replies:
            parsed = [item for item in parsed if not item.get("reply_to")]
        return self._dedupe_comments(parsed, limit), error

    @staticmethod
    def _dedupe_comments(values: list[dict], limit: int) -> list[dict]:
        found: dict[str, dict] = {}
        for value in values:
            cid = str(value.get("comment_id") or "")
            if cid:
                found.setdefault(cid, value)
            if len(found) >= limit:
                break
        return list(found.values())[:limit]

    @staticmethod
    def _persist_comments(job_id: int, content_id: int, platform: str,
                          aweme_id: str, comments: list[dict]) -> int:
        with get_session() as session:
            known = set(session.exec(
                select(KeywordCollectionComment.comment_id)
                .where(KeywordCollectionComment.job_id == job_id)
                .where(KeywordCollectionComment.content_id == content_id)
            ).all())
            added = 0
            for item in comments:
                cid = str(item.get("comment_id") or "")
                if not cid or cid in known:
                    continue
                known.add(cid)
                session.add(KeywordCollectionComment(
                    job_id=job_id, content_id=content_id, platform=platform,
                    aweme_id=aweme_id, comment_id=cid,
                    text=item.get("text") or "",
                    user_nickname=item.get("user_nickname") or "",
                    like_count=int(item.get("like_count") or 0),
                    create_time=int(item.get("create_time") or 0),
                    reply_to=item.get("reply_to") or "",
                ))
                added += 1
            content = session.get(KeywordCollectionContent, content_id)
            if content:
                content.collected_comment_count = len(known)
                session.add(content)
            session.commit()
        return added

    async def run(self, job_id: int, account) -> dict:
        job = self._job(job_id)
        if not job:
            raise RuntimeError("采集任务不存在")
        keywords = _loads_keywords(job.keywords)
        if not keywords:
            raise RuntimeError("采集任务没有关键词")

        identity = self.browser.identity_for(account)
        proxy = account.proxy or ""
        douyin_client = None
        xhs_client = None
        if job.platform == "douyin":
            douyin_client = DouyinClient(
                douyin_cookie_from_state(account.storage_state), identity.ua,
                timeout=self.cfg.engine.request_timeout_seconds, proxy=proxy)
        elif not self._xhs_browser_reads_enabled():
            cookie = cookie_str_from_state(account.storage_state)
            if not has_a1(cookie):
                raise RuntimeError("小红书登录态缺少 a1，请重新扫码登录")
            xhs_client = XhsApiClient(
                cookie, self._direct_request_ua(identity),
                timeout=self.cfg.engine.request_timeout_seconds, proxy=proxy)

        if job.platform == "douyin":
            # 抖音会针对无头搜索单独返回验证码中间页，即使同一登录态在用户打开的
            # 有头浏览器中完全正常。关键词任务因此复用一次临时可见 context，整个
            # 任务结束后自动关窗并保存 profile。
            self._progress(job_id, step="启动抖音可见采集窗口")
            async with self.browser.temporary_headed_context(identity) as context:
                return await self._run_keywords(
                    job_id, job, account, keywords, proxy,
                    douyin_client=douyin_client, context=context)
        return await self._run_keywords(
            job_id, job, account, keywords, proxy,
            xhs_client=xhs_client, identity=identity)

    async def _run_keywords(self, job_id: int, job: KeywordCollectionJob,
                            account, keywords: list[str], proxy: str,
                             douyin_client: DouyinClient | None = None,
                             xhs_client: XhsApiClient | None = None,
                             context=None, identity=None) -> dict:

        browser_reads = self._xhs_browser_reads_enabled()
        for keyword_index, keyword in enumerate(keywords):
            if self._cancel_requested(job_id):
                return {"canceled": True, "errors": self._job(job_id).error_count}
            if job.platform == "douyin" and keyword_index:
                gap = max(0.0, float(self.cfg.engine.douyin_keyword_gap_seconds))
                if gap:
                    await asyncio.sleep(gap * random.uniform(1.0, 1.35))
            elif job.platform == "xhs" and keyword_index:
                await self._xhs_gap(
                    self.cfg.engine.xhs_keyword_gap_seconds)
            self._progress(job_id, keyword=keyword, step="搜索作品")
            if job.platform == "douyin":
                raw_items, search_error = await self._discover_douyin(
                    account, keyword, job,
                    context=context)
            elif browser_reads:
                raw_items, search_error = await self._discover_xhs_browser(
                    identity, keyword, job)
            else:
                raw_items, search_error = await self._discover_xhs(
                    xhs_client, keyword, job.max_contents_per_keyword)
            if search_error:
                self._record_error(job_id, f"{keyword}: {search_error}")
                if "登录态已失效" in search_error:
                    self._mark_account_invalid(account.id)
                category, _ = classify_platform_error(search_error)
                if category in {
                        RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK}:
                    # 风险/登录态/网络异常后停止后续关键词，避免一次任务继续放大信号。
                    break
            if not raw_items:
                continue

            for index, raw in enumerate(raw_items[:job.max_contents_per_keyword], 1):
                if self._cancel_requested(job_id):
                    return {"canceled": True, "errors": self._job(job_id).error_count}
                self._progress(
                    job_id, keyword=keyword,
                    step=f"处理作品 {index}/{min(len(raw_items), job.max_contents_per_keyword)}")
                detail_error = ""
                detail_raw = raw
                if job.platform == "douyin":
                    aweme = parse_aweme(raw, job.video_quality or "highest")
                    if aweme is None and raw.get("aweme_id"):
                        author = raw.get("author") or {}
                        aweme = Aweme(
                            aweme_id=str(raw.get("aweme_id")),
                            desc=(raw.get("desc") or "").strip(),
                            create_time=int(raw.get("create_time") or 0),
                            author_name=author.get("nickname") or "",
                            media_type="images" if raw.get("images") else "video",
                        )
                        detail_error = "未取得媒体地址"
                    token = ""
                elif browser_reads:
                    aweme, parts, detail_error = \
                        await self._materialize_xhs_browser(identity, raw)
                    brief = parts.get("brief") or {}
                    card = parts.get("card") or {}
                    detail_raw = card or raw
                    token = brief.get("xsec_token") or ""
                else:
                    aweme, parts, detail_error = await self._materialize_xhs(xhs_client, raw)
                    brief = parts.get("brief") or {}
                    card = parts.get("card") or {}
                    detail_raw = card or raw
                    token = brief.get("xsec_token") or ""
                if job.platform == "xhs" and detail_error:
                    category, _ = classify_platform_error(detail_error)
                    if category in {
                            RiskCategory.RISK, RiskCategory.AUTH,
                            RiskCategory.NETWORK}:
                        note_id = str(
                            (parts.get("brief") or {}).get("note_id") or "")
                        self._record_error(
                            job_id, f"{keyword}/{note_id}: {detail_error}")
                        return self._result(job_id, stopped=True)
                if aweme is None:
                    self._record_error(job_id, f"{keyword}: 无法解析一条搜索结果")
                    continue

                current = self._job(job_id)
                content = self._upsert_content(
                    current, keyword, aweme, _author_id(detail_raw, job.platform),
                    token, detail_error)
                if detail_error:
                    self._record_error(
                        job_id, f"{keyword}/{aweme.aweme_id}: {detail_error}")
                self._refresh_counts(job_id)

                if job.download_media:
                    self._progress(job_id, keyword=keyword, step=f"下载作品 {index}")
                    await self._download(current, content, aweme, proxy)

                if job.max_comments_per_content > 0:
                    self._progress(job_id, keyword=keyword, step=f"抓取评论 {index}")
                    if job.platform == "douyin":
                        comments, comment_error = await self._douyin_comments(
                            account, douyin_client, aweme,
                            job.max_comments_per_content, job.include_replies,
                            context=context)
                    elif browser_reads:
                        comments, comment_error = await self._xhs_comments_browser(
                            identity, aweme.aweme_id, token,
                            job.max_comments_per_content, job.include_replies)
                    else:
                        comments, comment_error = await self._xhs_comments(
                            xhs_client, aweme.aweme_id, token,
                            job.max_comments_per_content, job.include_replies)
                    self._persist_comments(
                        job_id, content.id, job.platform, aweme.aweme_id, comments)
                    if comment_error:
                        self._record_error(
                            job_id, f"{keyword}/{aweme.aweme_id}: {comment_error}")
                        if job.platform == "xhs" and classify_platform_error(
                                comment_error)[0] in {
                                RiskCategory.RISK, RiskCategory.AUTH,
                                RiskCategory.NETWORK}:
                            return self._result(job_id, stopped=True)
                self._refresh_counts(job_id)
                if job.platform == "xhs":
                    await self._xhs_gap(self.cfg.engine.xhs_item_gap_seconds)
                else:
                    await asyncio.sleep(0.4)

        return self._result(job_id)
