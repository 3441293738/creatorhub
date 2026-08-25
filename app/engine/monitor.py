"""监控引擎。对应逆向 engine.MonitorEngine + ContentChecker。
后台循环:到点的目标 -> 真实浏览器抓新作品 -> 入库 -> 下载。
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from sqlmodel import select

from ..browser import (BrowserManager, fetch_videos, fetch_comments,
                       fetch_creator_comments, fetch_danmaku, fetch_creator_danmaku,
                       fetch_self_profile,
                       fetch_xhs_notes, fetch_xhs_search, fetch_xhs_note_detail,
                       fetch_xhs_comments, fetch_xhs_self_profile,
                       post_comment_browser,
                       fetch_ks_videos, fetch_ks_comments, fetch_ks_self_profile,
                       post_ks_comment,
                       fetch_channels_works, fetch_channels_comments,
                       fetch_channels_self_profile, post_channels_comment,
                       fetch_account_works,
                       do_follow, send_dm, send_dm_api)
from . import compose
from ..config import Config
from ..db import get_session
from ..platforms.douyin import (parse_aweme, parse_comment, parse_creator_comment,
                       parse_danmaku,
                       parse_self_user, DouyinClient, publish_douyin,
                      cookie_from_state as dy_cookie_from_state)
from ..platforms.douyin.extract import Aweme, MediaItem
from ..platforms.xhs import (parse_note_brief, parse_note_detail,
                   parse_comment as parse_xhs_comment,
                   flatten_comments as flatten_xhs_comments,
                   parse_self_user as parse_xhs_self_user,
                   XhsApiClient, XhsApiError, cookie_str_from_state, has_a1,
                   publish_xhs, creator_check, comment_xhs_browser)
from ..platforms.kuaishou import (parse_ks_feed, parse_ks_comment,
                   flatten_ks_comments, parse_self_user as parse_ks_self_user,
                   publish_kuaishou)
from ..platforms.channels import (parse_channels_feed, parse_channels_comment,
                   flatten_channels_comments, parse_self_user as parse_channels_self_user,
                   publish_channels)
from ..models import (ContentRecord, CommentRecord, CommentRule, CommentTask,
                      CommentWatch, DanmakuWatch, DanmakuRecord,
                       DouyinAccount, MonitorTarget, AccountRiskState,
                      NotificationChannel, PublishTask, AccountActionTask,
                       FollowEdge, DmConversation, AccountWork, AccountStatSnapshot,
                       KeywordCollectionJob)
from ..notifier import notify_all
from ..netfp import probe_ip_region
from ..risk import (
    classify_platform_error,
    OperationKind,
    RiskCategory,
    RiskController,
)
from ..settings import get_setting
from .downloader import Downloader
from .collection import KeywordCollector

MAX_AUTO_RETRY = 3
_BROWSER_SUBMIT_MARKER = "write_submitted:browser"

log = logging.getLogger("creatorhub.engine")

# 账号时区 -> 期望出口国家(ISO2)。仅列常见,匹配不到则跳过地区校验。
_TZ_COUNTRY = {
    "Asia/Shanghai": "CN", "Asia/Chongqing": "CN", "Asia/Urumqi": "CN",
    "Asia/Hong_Kong": "HK", "Asia/Macau": "MO", "Asia/Taipei": "TW",
}


def _loads(s: str) -> dict:
    try:
        return json.loads(s or "{}")
    except Exception:
        return {}


def _loads_list(s: str) -> list:
    try:
        v = json.loads(s or "[]")
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _monitor_terms(raw: str) -> list[str]:
    """Load case-insensitive monitor terms while tolerating legacy CSV values."""
    values = _loads_list(raw)
    if not values and raw and not str(raw).lstrip().startswith("["):
        values = str(raw).replace("，", ",").split(",")
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        term = " ".join(str(value or "").strip().split())
        key = term.casefold()
        if not term or key in seen:
            continue
        seen.add(key)
        result.append(term)
    return result


def _monitor_strategy(target: MonitorTarget, *, default_scrolls: int,
                      default_items: int = 0) -> dict:
    """Return a clamped strategy so legacy rows keep platform defaults."""
    configured_scrolls = int(getattr(target, "max_scrolls", 0) or 0)
    configured_items = int(getattr(target, "max_items_per_scan", 0) or 0)
    return {
        "max_scrolls": max(1, min(30, configured_scrolls or default_scrolls)),
        "max_items": max(0, min(100, configured_items or default_items)),
        "media_type": str(getattr(target, "record_media_filter", "all") or "all"),
        "min_likes": max(0, int(getattr(target, "min_like_count", 0) or 0)),
        "min_comments": max(0, int(getattr(target, "min_comment_count", 0) or 0)),
        "recent_days": max(0, int(getattr(target, "recent_days", 0) or 0)),
        "includes": _monitor_terms(getattr(target, "include_keywords", "[]") or "[]"),
        "excludes": _monitor_terms(getattr(target, "exclude_keywords", "[]") or "[]"),
    }


def _monitor_content_matches(aw: Aweme, strategy: dict,
                             *, now_ts: int | None = None) -> bool:
    """Apply target-level record filters after a platform item is normalized."""
    media_type = strategy.get("media_type") or "all"
    if media_type != "all" and aw.media_type != media_type:
        return False
    if int(aw.like_count or 0) < int(strategy.get("min_likes") or 0):
        return False
    if int(aw.comment_count or 0) < int(strategy.get("min_comments") or 0):
        return False
    recent_days = int(strategy.get("recent_days") or 0)
    if recent_days and aw.create_time:
        cutoff = (int(time.time()) if now_ts is None else int(now_ts)) - recent_days * 86400
        if int(aw.create_time) < cutoff:
            return False
    folded = str(aw.desc or "").casefold()
    includes = strategy.get("includes") or []
    excludes = strategy.get("excludes") or []
    if includes and not any(str(term).casefold() in folded for term in includes):
        return False
    if excludes and any(str(term).casefold() in folded for term in excludes):
        return False
    return True


def _danmaku_matches(item: dict, settings: dict) -> bool:
    text = str(item.get("text") or "")
    folded = text.casefold()
    point = max(0, int(item.get("video_time_ms") or 0))
    start_ms = settings.get("time_start_ms", 0)
    end_ms = settings.get("time_end_ms", 0)
    if start_ms and point < start_ms:
        return False
    if end_ms and point > end_ms:
        return False
    includes = settings.get("include_keywords") or []
    excludes = settings.get("exclude_keywords") or []
    if includes and not any(str(k).casefold() in folded for k in includes):
        return False
    if excludes and any(str(k).casefold() in folded for k in excludes):
        return False
    min_len = settings.get("min_text_length", 0)
    max_len = settings.get("max_text_length", 0)
    if min_len and len(text) < min_len:
        return False
    if max_len and len(text) > max_len:
        return False
    if int(item.get("like_count") or 0) < settings.get("min_like_count", 0):
        return False
    return True


def _select_douyin_awemes(items: list, quality: str, first_scan: bool,
                          monitor_since: int, initial_backfill_count: int) -> list[Aweme]:
    """按发布时间稳定排序，并应用“订阅后新增 + 可选首次回填”策略。"""
    parsed = []
    seen = set()
    for item in items:
        aw = parse_aweme(item, quality)
        if not aw or aw.aweme_id in seen:
            continue
        seen.add(aw.aweme_id)
        parsed.append(aw)
    parsed.sort(key=lambda aw: (aw.create_time, aw.aweme_id), reverse=True)

    if not first_scan:
        # create_time 缺失时宁可保留，避免平台字段小改后静默漏掉真正的新作品。
        return [aw for aw in parsed
                if not aw.create_time or aw.create_time >= monitor_since]
    if initial_backfill_count < 0:
        return parsed

    current = [aw for aw in parsed
               if aw.create_time and aw.create_time >= monitor_since]
    historical = [aw for aw in parsed
                  if aw.create_time and aw.create_time < monitor_since]
    return current + historical[:max(0, initial_backfill_count)]


def _douyin_scan_since(monitor_since: int, known_create_times: list[int]) -> int:
    """给旧版残缺首扫留出自愈窗口，但不回退到整个账号历史。"""
    latest_known = max((ts or 0 for ts in known_create_times), default=0)
    return min(monitor_since, latest_known) if latest_known else monitor_since


def _round_robin_by_account(rows: list[tuple[int, int | None]]) \
        -> list[tuple[int, int | None]]:
    """Interleave due rows so one account cannot monopolize a scheduler burst."""
    buckets: dict[object, list[tuple[int, int | None]]] = {}
    for row_id, account_id in rows:
        key: object = account_id if account_id is not None else f"anon:{row_id}"
        buckets.setdefault(key, []).append((row_id, account_id))
    ordered: list[tuple[int, int | None]] = []
    while buckets:
        for key in list(buckets):
            ordered.append(buckets[key].pop(0))
            if not buckets[key]:
                del buckets[key]
    return ordered


class MonitorEngine:
    def __init__(self, cfg: Config, browser: BrowserManager):
        self.cfg = cfg
        self.browser = browser
        self.downloader = Downloader(
            cfg.engine.media_dir, cfg.engine.user_agent,
            cfg.engine.download_timeout_seconds,
        )
        self.keyword_collector = KeywordCollector(cfg, browser, self.downloader)
        self._sem = asyncio.Semaphore(cfg.engine.worker_pool_size)
        # 限制并发抓取的目标数(多个浏览器上下文并行,但不无限开)
        self._scan_sem = asyncio.Semaphore(max(1, cfg.engine.scan_concurrency))
        # 同一时刻最多并发活跃的账号数(错峰,降低"多号同时活跃"特征)
        self._active_sem = asyncio.Semaphore(max(1, cfg.engine.active_accounts))
        self._inflight: set = set()           # 正在抓取的目标,避免同目标并发
        self._publish_sem = asyncio.Semaphore(1)   # 发布串行(有头浏览器,一次一个)
        self._publishing: set[int] = set()
        self._commenting: set[int] = set()         # 正在执行的评论任务 id
        self._actioning: set[int] = set()           # 正在执行的写操作任务 id
        self._collection_tasks: dict[int, asyncio.Task] = {}  # 一次性关键词采集
        self._last_acct_check = time.time()   # 上次账号体检时间
        self._geo_checked: dict = {}          # account_id -> 已校验过地区的代理(避免重复探测)
        self.risk = RiskController(cfg)
        self._last_risk_prune_day = None
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def _xhs_gap(self, seconds: float | None = None) -> None:
        base = max(0.0, float(
            self.cfg.engine.xhs_item_gap_seconds
            if seconds is None else seconds))
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

    @staticmethod
    def _raise_severe_xhs_read_error(error) -> None:
        if not error:
            return
        category, signal = classify_platform_error(error)
        if category not in {
                RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK}:
            return
        if isinstance(error, BaseException):
            raise error
        raise XhsApiError(
            str(error), category=category.value,
            signal=signal or "browser_read_error")

    def start(self):
        if self._task is None:
            self._running = True
            self._task = asyncio.create_task(self._loop())
            log.info("监控引擎已启动")

    def recover_interrupted_tasks(self, *, now: datetime | None = None,
                                  delay_seconds: int = 300) -> int:
        """Return crash-left transient write states to their durable queues."""
        scheduled_at = (now or datetime.utcnow()) + timedelta(
            seconds=max(1, delay_seconds))
        recovered = 0
        with get_session() as s:
            for model, transient in (
                    (CommentTask, "doing"),
                    (AccountActionTask, "doing"),
                    (PublishTask, "publishing")):
                rows = s.exec(select(model).where(model.status == transient)).all()
                for row in rows:
                    submitted = (
                        getattr(row, "platform", "") == "xhs"
                        and str(getattr(row, "error", "") or "")
                        .startswith(_BROWSER_SUBMIT_MARKER)
                    )
                    if submitted:
                        row.status = "uncertain"
                        row.scheduled_at = None
                        if hasattr(row, "done_at"):
                            row.done_at = None
                        row.error = (
                            "服务重启前浏览器已进入提交边界，结果需到平台核对；"
                            "任务不会自动重试")
                    else:
                        row.status = "pending"
                        row.scheduled_at = scheduled_at
                        row.error = "服务重启后已恢复到待执行队列"
                    s.add(row)
                    recovered += 1
            for job in s.exec(
                    select(KeywordCollectionJob)
                    .where(KeywordCollectionJob.status == "running")).all():
                if job.cancel_requested:
                    job.status = "canceled"
                    job.current_step = "已取消"
                    job.finished_at = now or datetime.utcnow()
                else:
                    job.status = "pending"
                    job.current_step = "服务重启后等待继续"
                    job.started_at = None
                    job.finished_at = None
                s.add(job)
                recovered += 1
            if recovered:
                s.commit()
        return recovered

    @staticmethod
    def _mark_browser_submit(model, task_id: int) -> None:
        """Durably mark the conservative no-retry boundary before one click."""
        with get_session() as s:
            row = s.get(model, task_id)
            if row is None:
                raise RuntimeError("待提交任务已不存在")
            row.error = _BROWSER_SUBMIT_MARKER
            s.add(row)
            s.commit()

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
        tasks = list(self._collection_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._collection_tasks.clear()

    # ── 账号隔离调度 ──
    @staticmethod
    def _load_account(account_id):
        if not account_id:
            return None
        with get_session() as s:
            return s.get(DouyinAccount, account_id)

    @asynccontextmanager
    async def _operation_guard(self, account_id, kind: OperationKind,
                               fallback_key: str = "", operation_target=None):
        """Serialize by global limit, network exit, then account profile."""
        account = operation_target or self._load_account(account_id)
        key = f"acc:{account_id}" if account_id else (fallback_key or "anon")
        lock = self.browser.lock_for(key)
        async with self._active_sem:
            async with self.risk.network_guard(account):
                async with lock:
                    yield account

    @asynccontextmanager
    async def operation_guard(self, account_id, kind: OperationKind,
                              fallback_key: str = "", operation_target=None):
        """Public unified gate for platform operations owned by API routes."""
        async with self._operation_guard(
                account_id, kind, fallback_key, operation_target) as account:
            yield account

    @asynccontextmanager
    async def _account_guard(self, account_id, fallback_key: str = ""):
        """Compatibility wrapper for read call sites not converted yet."""
        async with self._operation_guard(
                account_id, OperationKind.READ_LIGHT, fallback_key) as account:
            yield account

    async def _guarded_read_dict(self, account_id, kind: OperationKind,
                                 fallback_key: str, operation) -> dict:
        """Run one read through its budget and persist the logical outcome."""
        decision = self.risk.preflight(account_id, kind)
        if not decision.allowed:
            return {
                "ok": True,
                "skipped": True,
                "reason": decision.reason,
                "next_allowed_at": (
                    decision.next_allowed_at.isoformat()
                    if decision.next_allowed_at else None),
            }
        try:
            async with self._operation_guard(
                    account_id, kind, fallback_key=fallback_key):
                decision = self.risk.preflight(account_id, kind)
                if not decision.allowed:
                    return {
                        "ok": True,
                        "skipped": True,
                        "reason": decision.reason,
                        "next_allowed_at": (
                            decision.next_allowed_at.isoformat()
                            if decision.next_allowed_at else None),
                    }
                result = await operation()
                error = result.get("error")
                if account_id:
                    if result.get("ok") and not result.get("skipped"):
                        self.risk.record_success(account_id, kind)
                    elif error:
                        self.risk.record_failure(
                            account_id, kind, error)
                if isinstance(error, BaseException):
                    result = dict(result)
                    result["error"] = str(error)
        except Exception as exc:
            if account_id:
                self.risk.record_failure(account_id, kind, exc)
            raise
        return result

    async def guarded_read_pair(self, account_id, kind: OperationKind,
                                fallback_key: str, operation, *, empty_result,
                                allow_invalid_probe: bool = False):
        """Budget a direct read returning ``(payload, error)``."""
        decision = self.risk.preflight(
            account_id, kind,
            allow_invalid_probe=allow_invalid_probe)
        if not decision.allowed:
            return empty_result, f"risk_deferred:{decision.reason}"
        try:
            async with self._operation_guard(
                    account_id, kind, fallback_key=fallback_key):
                decision = self.risk.preflight(
                    account_id, kind,
                    allow_invalid_probe=allow_invalid_probe)
                if not decision.allowed:
                    return empty_result, f"risk_deferred:{decision.reason}"
                payload, error = await operation()
                if account_id:
                    if not error or error == "empty":
                        self.risk.record_success(account_id, kind)
                    else:
                        self.risk.record_failure(account_id, kind, error)
                if isinstance(error, BaseException):
                    error = str(error)
        except Exception as exc:
            if account_id:
                self.risk.record_failure(account_id, kind, exc)
            return empty_result, repr(exc)
        return payload, error

    def _identity_proxy(self, acc):
        """由账号行构建 (Identity, proxy)。acc 为空则匿名画像。"""
        if acc:
            ident = self.browser.identity_for(acc)
            return ident, (acc.proxy or "")
        return self.browser.anon_identity(), ""

    def _dl_proxy(self, proxy: str) -> str:
        """媒体下载实际使用的代理(受 route_download_via_proxy 开关控制)。"""
        return proxy if self.cfg.engine.route_download_via_proxy else ""

    @staticmethod
    def _proxy_bad(acc) -> bool:
        return bool(
            acc and acc.proxy
            and acc.proxy_status in {"bad", "auth_error", "blocked", "drifted"}
        )

    def _native_write_environment_error(
            self, acc, *, headed: bool = True,
            browser_mode: bool = True) -> str:
        """Run the BrowserManager's native-only hard gate when supported."""
        checker = getattr(self.browser, "native_write_gate_error", None)
        if not callable(checker) or acc is None:
            return ""
        return str(checker(
            acc, headed=headed, browser_mode=browser_mode) or "")

    @staticmethod
    def _blocked_signal(reason: str) -> str:
        text = str(reason or "")
        if "登录态" in text or "重新登录" in text:
            return "auth_required"
        if "代理" in text or "出口" in text:
            return "proxy_unavailable"
        if "非活跃时段" in text:
            return "quiet_hours"
        if "额度" in text or "上限" in text:
            return "quota"
        if "最小间隔" in text or "尚未达到" in text:
            return "operation_gap"
        if "渐进恢复" in text or "轻量状态探测" in text:
            return "probe_only"
        if "冷却" in text or "风控" in text or "验证" in text:
            return "cooldown"
        return "deferred"

    @staticmethod
    def _blocked_operation(row) -> str:
        if isinstance(row, PublishTask):
            return OperationKind.PUBLISH.value
        if isinstance(row, CommentTask):
            return OperationKind.COMMENT.value
        if isinstance(row, AccountActionTask):
            return (OperationKind.DM.value if row.action == "send_dm"
                    else OperationKind.SOCIAL.value)
        if isinstance(row, KeywordCollectionJob):
            return OperationKind.READ_HEAVY.value
        return ""

    @classmethod
    def _defer_row(cls, row, reason: str, next_at: datetime | None = None,
                   fallback_seconds: int = 300, signal: str = "") -> None:
        now = datetime.utcnow()
        proposed = next_at or (now + timedelta(seconds=max(1, fallback_seconds)))
        if hasattr(row, "scheduled_at") \
                and (row.scheduled_at is None or row.scheduled_at < proposed):
            row.scheduled_at = proposed
        row.status = "pending"
        row.error = str(reason or "平台操作已延后").strip()[:500]
        if hasattr(row, "blocked_reason"):
            row.blocked_reason = row.error
            row.blocked_signal = signal or cls._blocked_signal(row.error)
            row.blocked_operation = cls._blocked_operation(row)
            row.blocked_at = now
            row.next_allowed_at = proposed

    @staticmethod
    def _clear_row_block(row) -> None:
        for name, value in (
                ("blocked_reason", ""), ("blocked_signal", ""),
                ("blocked_operation", ""), ("blocked_at", None),
                ("next_allowed_at", None)):
            if hasattr(row, name):
                setattr(row, name, value)

    def _xhs_comment_write_mode(self) -> str:
        """Return the explicitly selected XHS comment write mode.

        Browser page writes are the default. Direct signed comment POSTs stay
        opt-in, while ``manual`` keeps the existing draft-only workflow.
        """
        mode = str(getattr(self.cfg.engine, "xhs_comment_write_mode", "browser")
                   or "browser").strip().lower()
        return mode if mode in {"browser", "api", "manual"} else "browser"

    def _xhs_publish_mode(self) -> str:
        """Use visible page publishing unless API compatibility is explicit."""
        mode = str(getattr(self.cfg.engine, "xhs_publish_mode", "browser")
                   or "browser").strip().lower()
        return mode if mode in {"browser", "api"} else "browser"

    def _write_pause_error(self, account_id) -> str:
        """Return a persisted account write pause, clearing an expired one."""
        if not self.risk.policy.enabled:
            return ""
        if not account_id:
            return ""
        now = datetime.utcnow()
        with get_session() as s:
            acc = s.get(DouyinAccount, account_id)
            if not acc:
                return ""
            until = acc.write_paused_until
            if until and until > now:
                reason = (acc.write_pause_reason or "平台拒绝写操作").strip()
                return f"账号写操作已暂停至 {until.isoformat(timespec='seconds')}: {reason[:120]}"
            if until:
                acc.write_paused_until = None
                acc.write_pause_reason = ""
                s.add(acc)
                s.commit()
        return ""

    def _prune_risk_events_if_due(self, now: datetime | None = None) -> int:
        """Prune retained risk events once for each attempted UTC day."""
        now = now or datetime.utcnow()
        prune_day = now.date()
        if self._last_risk_prune_day is not None \
                and prune_day <= self._last_risk_prune_day:
            return 0
        self._last_risk_prune_day = prune_day
        try:
            return self.risk.prune_events(now=now)
        except Exception:
            log.exception("risk event pruning failed for %s", prune_day.isoformat())
            return 0

    async def _collect_idle_browser_sessions(self, now: float | None = None) -> int:
        """Reuse the main scheduler to close idle resident browser sessions."""
        collector = getattr(self.browser, "collect_idle_sessions", None)
        if not callable(collector):
            collector = getattr(self.browser, "collect_idle_cdp", None)
        if not callable(collector):
            return 0
        try:
            return int(await collector(now=now))
        except Exception:
            log.exception("idle browser session collection failed")
            return 0

    async def _loop(self):
        while self._running:
            try:
                sampled_at = datetime.utcnow()
                sampled_epoch = time.time()
                self._prune_risk_events_if_due(sampled_at)
                await self._collect_idle_browser_sessions(sampled_epoch)
                await self._scan_once()
                await self._scan_comment_watches()
                await self._scan_danmaku_watches()
                await self._retry_failed()
                await self._process_risk_recovery()
                await self._check_accounts()
                await self._check_work_health()
                await self._process_publish()
                await self._process_comment_rules()
                await self._process_comment_tasks()
                await self._process_action_tasks()
                await self._process_collection_jobs()
            except Exception as e:
                log.exception("scan loop error: %s", e)
            await asyncio.sleep(15)

    def enqueue_collection_job(self, job_id: int) -> bool:
        """立即把关键词任务交给后台执行；同一时刻仅跑一个批量任务。"""
        if job_id in self._collection_tasks:
            return False
        if len(self._collection_tasks) >= 1:
            return False
        task = asyncio.create_task(self.run_collection_job(job_id))
        self._collection_tasks[job_id] = task

        def done(completed: asyncio.Task, jid: int = job_id):
            self._collection_tasks.pop(jid, None)
            try:
                completed.exception()
            except (asyncio.CancelledError, Exception):
                pass

        task.add_done_callback(done)
        return True

    async def _process_collection_jobs(self) -> None:
        if self._collection_tasks:
            return
        with get_session() as s:
            job = s.exec(
                select(KeywordCollectionJob)
                .where(KeywordCollectionJob.status == "pending")
                .where(KeywordCollectionJob.cancel_requested == False)  # noqa:E712
                .order_by(KeywordCollectionJob.created_at)
            ).first()
        if job:
            self.enqueue_collection_job(job.id)

    async def run_collection_job(self, job_id: int) -> dict:
        """执行一个持久化关键词任务，并维护可恢复的状态机。"""
        with get_session() as s:
            job = s.get(KeywordCollectionJob, job_id)
            if not job:
                return {"ok": False, "error": "任务不存在"}
            if job.cancel_requested or job.status == "canceled":
                job.status = "canceled"
                job.current_step = "已取消"
                job.finished_at = datetime.utcnow()
                s.add(job); s.commit()
                return {"ok": True, "canceled": True}
            account = s.get(DouyinAccount, job.account_id)
            if (not account or account.platform != job.platform
                    or account.status != "active" or not account.storage_state):
                job.status = "failed"
                job.current_step = "执行失败"
                job.error = "所选账号不存在、登录态失效或平台不匹配"
                job.error_count += 1
                job.finished_at = datetime.utcnow()
                s.add(job); s.commit()
                return {"ok": False, "error": job.error}
            account_id = account.id

        decision = self.risk.preflight(account_id, OperationKind.READ_HEAVY)
        if not decision.allowed:
            with get_session() as s:
                job = s.get(KeywordCollectionJob, job_id)
                if job and job.status == "pending":
                    job.current_step = "等待账号读取冷却"
                    job.blocked_reason = decision.reason
                    job.blocked_signal = decision.signal or self._blocked_signal(
                        decision.reason)
                    job.blocked_operation = OperationKind.READ_HEAVY.value
                    job.blocked_at = datetime.utcnow()
                    job.next_allowed_at = decision.next_allowed_at
                    s.add(job); s.commit()
            return {"ok": True, "deferred": True, "reason": decision.reason,
                    "signal": decision.signal,
                    "next_allowed_at": (decision.next_allowed_at.isoformat()
                                        if decision.next_allowed_at else None)}

        try:
            async with self._operation_guard(account_id, OperationKind.READ_HEAVY):
                decision = self.risk.preflight(account_id, OperationKind.READ_HEAVY)
                if not decision.allowed:
                    with get_session() as s:
                        job = s.get(KeywordCollectionJob, job_id)
                        if job and job.status == "pending":
                            job.current_step = "等待账号读取冷却"
                            job.blocked_reason = decision.reason
                            job.blocked_signal = decision.signal or self._blocked_signal(
                                decision.reason)
                            job.blocked_operation = OperationKind.READ_HEAVY.value
                            job.blocked_at = datetime.utcnow()
                            job.next_allowed_at = decision.next_allowed_at
                            s.add(job); s.commit()
                    return {"ok": True, "deferred": True,
                            "reason": decision.reason, "signal": decision.signal,
                            "next_allowed_at": (decision.next_allowed_at.isoformat()
                                                if decision.next_allowed_at else None)}
                with get_session() as s:
                    job = s.get(KeywordCollectionJob, job_id)
                    if not job:
                        return {"ok": False, "error": "任务不存在"}
                    job.status = "running"
                    job.current_step = "准备搜索"
                    self._clear_row_block(job)
                    job.started_at = job.started_at or datetime.utcnow()
                    job.finished_at = None
                    s.add(job); s.commit()
                    account = s.get(DouyinAccount, account_id)
                result = await self.keyword_collector.run(job_id, account)

            with get_session() as s:
                job = s.get(KeywordCollectionJob, job_id)
                if not job:
                    return {"ok": False, "error": "任务不存在"}
                if result.get("canceled") or job.cancel_requested:
                    job.status = "canceled"
                    job.current_step = "已取消"
                elif job.error_count and job.content_count:
                    job.status = "partial"
                    job.current_step = "完成，部分内容有错误"
                elif job.error_count and not job.content_count:
                    job.status = "failed"
                    job.current_step = "执行失败"
                else:
                    job.status = "done"
                    job.current_step = "已完成"
                job.finished_at = datetime.utcnow()
                s.add(job); s.commit()
                status = job.status
                job_error = job.error or ""
            category, _ = classify_platform_error(job_error)
            if job_error and category in {
                    RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK}:
                # 即使已经采到部分结果，验证码/登录态/网络异常也属于本次读取失败；
                # 不能再记 success，否则会立即放行下一次重读并覆盖风险信号。
                self.risk.record_failure(
                    account_id, OperationKind.READ_HEAVY, job_error)
            elif status in {"done", "partial"}:
                self.risk.record_success(account_id, OperationKind.READ_HEAVY)
                self._stamp_active(account_id)
            else:
                self.risk.record_failure(account_id, OperationKind.READ_HEAVY,
                                         "关键词采集未取得结果")
            return {"ok": status in {"done", "partial"}, "status": status, **result}
        except asyncio.CancelledError:
            with get_session() as s:
                job = s.get(KeywordCollectionJob, job_id)
                if job and job.status == "running":
                    if job.cancel_requested:
                        job.status = "canceled"
                        job.current_step = "已取消"
                        job.finished_at = datetime.utcnow()
                    else:
                        job.status = "pending"
                        job.current_step = "服务停止，等待恢复"
                    s.add(job); s.commit()
            raise
        except Exception as exc:
            self.risk.record_failure(account_id, OperationKind.READ_HEAVY, exc)
            with get_session() as s:
                job = s.get(KeywordCollectionJob, job_id)
                if job:
                    job.error_count += 1
                    job.error = (job.error + "\n" if job.error else "") + str(exc)[:500]
                    job.status = "partial" if job.content_count else "failed"
                    job.current_step = "异常中止"
                    job.finished_at = datetime.utcnow()
                    s.add(job); s.commit()
            return {"ok": False, "error": str(exc)}

    def _stamp_active(self, account_id) -> None:
        """记录账号「刚被成功摸活」的时刻。任何一次成功的网络/浏览器动作都算活跃,
        闲置保活据此跳过近期已活跃的账号,避免重复请求、减少风控暴露面。"""
        if not account_id:
            return
        with get_session() as s:
            a = s.get(DouyinAccount, account_id)
            if a:
                a.last_active_at = datetime.utcnow()
                s.add(a); s.commit()

    def _keepalive_due(self, last_active_at) -> bool:
        """闲置判定:从未活跃、或距上次活跃超过 idle_keepalive_hours(带 ±jitter 错峰)才需保活。
        idle_keepalive_hours<=0 时退回旧行为(每轮都摸)。"""
        hours = self.cfg.engine.idle_keepalive_hours
        if hours <= 0 or last_active_at is None:
            return True
        jitter = max(0.0, self.cfg.engine.scan_jitter)
        factor = 1.0 + random.uniform(-jitter, jitter) if jitter else 1.0
        return (datetime.utcnow() - last_active_at).total_seconds() >= hours * 3600 * factor

    async def _verify_proxy_region(self, account_id, proxy: str, timezone_id: str) -> None:
        """探测代理出口国家,与账号时区期望不一致时告警(best-effort,只记日志)。
        同一账号+代理只探测一次(缓存),失败静默 —— IP 在境外却时区东八区是强关联信号。"""
        if not self.cfg.engine.verify_proxy_region or not proxy:
            return
        if self._geo_checked.get(account_id) == proxy:
            return
        self._geo_checked[account_id] = proxy
        expected = _TZ_COUNTRY.get(timezone_id or "")
        if not expected:
            return
        geo = await probe_ip_region(proxy)
        if not geo:
            return
        # 把代理出口的真实经纬度写回账号 —— geolocation 伪造坐标据此对齐真实出口地,
        # 避免 navigator.geolocation(城市池兜底)与代理 IP 归属地对不上。
        lat, lon = geo.get("lat") or 0.0, geo.get("lon") or 0.0
        if lat and lon:
            try:
                with get_session() as s:
                    acc = s.get(DouyinAccount, account_id)
                    if acc and (round(acc.geo_lat, 3) != round(lat, 3)
                                or round(acc.geo_lon, 3) != round(lon, 3)):
                        acc.geo_lat, acc.geo_lon = lat, lon
                        s.add(acc)
                        s.commit()
            except Exception:
                pass
        if not geo.get("country"):
            return
        if geo["country"] != expected:
            log.warning("账号 %s 代理出口国家 %s 与时区 %s(期望 %s)不一致,IP=%s"
                        " —— 关联/风控风险,建议换地区一致的长效代理或改账号时区",
                        account_id, geo["country"], timezone_id, expected, geo.get("ip"))

    # ── 账号登录态体检 + 风险恢复探测 ──
    def _account_probe_tuple(self, account):
        return (account.id, account.platform, account.storage_state,
                account.creator_storage_state, account.proxy or "",
                self.browser.identity_for(account))

    def _wake_deferred_tasks(self, account_id: int) -> int:
        """Wake rows that were explicitly deferred by a now-cleared risk gate."""
        now = datetime.utcnow()
        woken = 0
        with get_session() as session:
            for model in (PublishTask, CommentTask, AccountActionTask):
                rows = session.exec(select(model).where(
                    model.account_id == account_id,
                    model.status == "pending",
                    model.blocked_reason != "",
                )).all()
                for row in rows:
                    row.scheduled_at = now
                    row.error = ""
                    self._clear_row_block(row)
                    session.add(row)
                    woken += 1
            jobs = session.exec(select(KeywordCollectionJob).where(
                KeywordCollectionJob.account_id == account_id,
                KeywordCollectionJob.status == "pending",
                KeywordCollectionJob.blocked_reason != "",
            )).all()
            for job in jobs:
                job.current_step = "账号已恢复，等待继续"
                job.error = ""
                self._clear_row_block(job)
                session.add(job)
                woken += 1
            if woken:
                session.commit()
        return woken

    async def _probe_account_health(self, probe) -> dict:
        aid, platform, state, creator_state, proxy, identity = probe
        decision = self.risk.preflight(aid, OperationKind.READ_LIGHT)
        if not decision.allowed:
            return {"ok": False, "deferred": True, "reason": decision.reason,
                    "next_allowed_at": decision.next_allowed_at}
        with get_session() as session:
            before = session.get(AccountRiskState, aid)
            was_recovering = bool(before and before.risk_level > 0)
            if was_recovering:
                before.last_operation_at = datetime.utcnow()
                before.updated_at = before.last_operation_at
                session.add(before)
                session.commit()
        u, err = {}, ""
        try:
            async with self._operation_guard(aid, OperationKind.READ_LIGHT):
                decision = self.risk.preflight(aid, OperationKind.READ_LIGHT)
                if not decision.allowed:
                    return {"ok": False, "deferred": True,
                            "reason": decision.reason,
                            "next_allowed_at": decision.next_allowed_at}
                await self._verify_proxy_region(aid, proxy, identity.timezone_id)
                if platform == "xhs" and creator_state:
                    chk = await creator_check(creator_state, proxy=proxy)
                    if chk is None:
                        return {"ok": False, "indeterminate": True}
                    u, err = ({"ok": 1}, "") if chk else ({}, "logged_out")
                elif platform == "xhs":
                    client = self._xhs_client(identity, state, proxy)
                    if client is None:
                        u, err = {}, "logged_out"
                    else:
                        try:
                            data = await client.self_info()
                            u, err = ((data, "") if data and not data.get("guest")
                                      else ({}, "logged_out"))
                        except XhsApiError as exc:
                            if exc.category == "auth":
                                u, err = {}, "logged_out"
                            else:
                                self.risk.record_failure(
                                    aid, OperationKind.READ_LIGHT, exc)
                                return {"ok": False, "error": str(exc)}
                elif platform == "kuaishou":
                    u, err = await fetch_ks_self_profile(self.browser, identity)
                elif platform == "shipinhao":
                    u, err = await fetch_channels_self_profile(self.browser, identity)
                else:
                    u, err = await fetch_self_profile(self.browser, identity)
                if u:
                    self.risk.record_success(aid, OperationKind.READ_LIGHT)
                elif err:
                    self.risk.record_failure(aid, OperationKind.READ_LIGHT, err)
        except Exception as exc:
            self.risk.record_failure(aid, OperationKind.READ_LIGHT, exc)
            return {"ok": False, "error": str(exc)}

        got_profile = False
        with get_session() as session:
            account = session.get(DouyinAccount, aid)
            if not account:
                return {"ok": False, "error": "account_missing"}
            if u:
                if platform == "xhs":
                    parsed = parse_xhs_self_user(u)
                elif platform == "kuaishou":
                    parsed = parse_ks_self_user(u)
                elif platform == "shipinhao":
                    parsed = parse_channels_self_user(u)
                else:
                    parsed = parse_self_user(u)
                account.status = "active"
                account.last_active_at = datetime.utcnow()
                if parsed.get("nickname"):
                    account.nickname = parsed["nickname"]
                account.sec_uid = parsed.get("sec_uid") or account.sec_uid
                account.douyin_id = parsed.get("douyin_id") or account.douyin_id
                account.avatar = parsed.get("avatar") or account.avatar
                account.follower_count = parsed.get("follower_count") or account.follower_count
                account.aweme_count = parsed.get("aweme_count") or account.aweme_count
                got_profile = True
            elif err == "logged_out":
                account.status = "invalid"
                log.warning("账号 %s(%s)登录态失效", aid, account.nickname)
            session.add(account)
            session.commit()
            risk_state = session.get(AccountRiskState, aid)
            recovered = bool(was_recovering and risk_state
                             and risk_state.risk_level == 0)

        if got_profile and self.cfg.engine.work_health_stat_snapshots:
            try:
                self._write_stat_snapshot(aid, platform, [])
            except Exception:
                pass
        woken = self._wake_deferred_tasks(aid) if recovered else 0
        if recovered:
            log.info("账号 %s 风险恢复完成，已唤醒 %s 条任务", aid, woken)
        return {"ok": got_profile, "recovered": recovered, "woken": woken,
                "error": err}

    async def _process_risk_recovery(self) -> int:
        """Probe recovering accounts independently of idle keepalive cadence."""
        now = datetime.utcnow()
        gap = max(1, self.cfg.risk_control.recovery_probe_gap_seconds)
        with get_session() as session:
            probes = []
            states = session.exec(select(AccountRiskState).where(
                AccountRiskState.risk_level > 0)).all()
            for risk_state in states:
                account = session.get(DouyinAccount, risk_state.account_id)
                if not account or account.status == "invalid" \
                        or not (account.storage_state or account.creator_storage_state):
                    continue
                if risk_state.cooldown_until and risk_state.cooldown_until > now:
                    continue
                last_attempt = max(
                    [value for value in (risk_state.last_recovery_at,
                                         risk_state.last_operation_at)
                     if value is not None], default=None)
                if last_attempt and (now - last_attempt).total_seconds() < gap:
                    continue
                probes.append(self._account_probe_tuple(account))
        completed = 0
        for probe in probes[:3]:
            result = await self._probe_account_health(probe)
            if result.get("ok"):
                completed += 1
        return completed

    async def _check_accounts(self):
        interval = self.cfg.engine.account_check_interval_seconds
        if interval <= 0 or time.time() - self._last_acct_check < interval:
            return
        self._last_acct_check = time.time()
        with get_session() as session:
            probes = [self._account_probe_tuple(account)
                      for account in session.exec(select(DouyinAccount)).all()
                      if (account.storage_state or account.creator_storage_state)
                      and account.status != "invalid"
                      and self._keepalive_due(account.last_active_at)]
        for probe in probes:
            await self._probe_account_health(probe)

    # ── 本账号作品健康监控(B5)+ 数据快照(B4)──
    async def _check_work_health(self):
        """定期同步本账号作品,检测「持续0播 / 违规下架」并推送;顺带写每日数据快照。
        默认关闭(work_health_enabled)。较重(每账号开一次浏览器抓自己作品),故独立节流。"""
        if not self.cfg.engine.work_health_enabled:
            return
        now = time.time()
        if now - getattr(self, "_last_work_health", 0.0) < \
                self.cfg.engine.work_health_interval_seconds:
            return
        self._last_work_health = now
        with get_session() as s:
            accs = [(a.id, a.platform, a.sec_uid or "", self.browser.identity_for(a))
                    for a in s.exec(select(DouyinAccount)).all()
                    if a.status != "invalid" and (a.storage_state or a.creator_storage_state)]
        for aid, platform, uid, identity in accs:
            decision = self.risk.preflight(aid, OperationKind.READ_HEAVY)
            if not decision.allowed:
                continue
            try:
                async with self._operation_guard(aid, OperationKind.READ_HEAVY):
                    if not self.risk.preflight(
                            aid, OperationKind.READ_HEAVY).allowed:
                        continue
                    items, err = await fetch_account_works(self.browser, identity,
                                                           platform, uid)
                    if err:
                        self.risk.record_failure(aid, OperationKind.READ_HEAVY, err)
                    else:
                        self.risk.record_success(aid, OperationKind.READ_HEAVY)
            except Exception as e:
                self.risk.record_failure(aid, OperationKind.READ_HEAVY, e)
                log.warning("作品健康:账号 %s 抓取失败 %s", aid, e)
                continue
            if not items:
                continue
            self._stamp_active(aid)
            try:
                await self._eval_work_health(aid, platform, items)
            except Exception as e:
                log.warning("作品健康:账号 %s 评估失败 %s", aid, e)

    # 视为「异常/受限」的状态关键词(命中即告警)
    _BAD_STATUS = ("违规", "删除", "下架", "不适宜", "限流", "私密", "仅自己", "审核不")

    async def _eval_work_health(self, account_id, platform, items):
        """upsert 本账号作品 + 判定 0播/违规告警 + 写数据快照。"""
        now = datetime.utcnow()
        zero_hours = self.cfg.engine.work_health_zero_play_hours
        cutoff = time.time() - self.cfg.engine.work_health_recent_days * 86400
        # 该平台是否真的暴露播放量:有任一作品 play>0 才启用「0播」判定,避免对不报播放量的
        # 平台(web 抖音等)误报。
        play_reliable = any((w.get("play_count") or 0) > 0 for w in items)
        alerts = []
        with get_session() as s:
            acc = s.get(DouyinAccount, account_id)
            nick = (acc.nickname if acc else "") or f"账号{account_id}"
            for w in items:
                rec = s.exec(select(AccountWork).where(
                    AccountWork.account_id == account_id,
                    AccountWork.item_id == w["item_id"])).first()
                if rec:
                    for k, v in w.items():
                        setattr(rec, k, v)
                    rec.fetched_at = now
                else:
                    rec = AccountWork(platform=platform, account_id=account_id,
                                      fetched_at=now, **w)
                    s.add(rec); s.flush()
                ct = rec.create_time or 0
                if ct and ct < cutoff:
                    s.add(rec); continue          # 太老,不体检
                title = (rec.desc or rec.item_id or "")[:20]
                st = rec.status or ""
                if st and any(k in st for k in self._BAD_STATUS) and rec.status_alerted != st:
                    alerts.append(("⚠️ 视频号/作品状态异常" if platform == "shipinhao"
                                   else "⚠️ 作品状态异常",
                                   f"{nick}:「{title}」当前状态「{st}」"))
                    rec.status_alerted = st
                if play_reliable and ct:
                    age_h = (time.time() - ct) / 3600
                    if (age_h >= zero_hours and (rec.play_count or 0) == 0
                            and not rec.zero_play_alerted):
                        alerts.append(("⚠️ 作品持续0播",
                                       f"{nick}:「{title}」发布 {age_h:.0f} 小时仍 0 播放,疑似限流"))
                        rec.zero_play_alerted = True
                s.add(rec)
            s.commit()
        if self.cfg.engine.work_health_stat_snapshots:
            self._write_stat_snapshot(account_id, platform, items)
        if alerts:
            try:
                with get_session() as s:
                    chans = s.exec(select(NotificationChannel)
                                   .where(NotificationChannel.enabled == True)).all()  # noqa: E712
                    channels = [{"type": c.type, "config": _loads(c.config)} for c in chans]
                for title, body in alerts:
                    if channels:
                        await notify_all(channels, title, body)
                    log.info("作品健康告警: %s | %s", title, body)
            except Exception:
                pass

    def _write_stat_snapshot(self, account_id, platform, items):
        """每账号每天一行数据快照(粉丝/作品/互动合计),供「数据」趋势视图。"""
        day = (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d")  # 东八区日期
        with get_session() as s:
            acc = s.get(DouyinAccount, account_id)
            snap = s.exec(select(AccountStatSnapshot).where(
                AccountStatSnapshot.account_id == account_id,
                AccountStatSnapshot.date == day)).first()
            if not snap:
                snap = AccountStatSnapshot(platform=platform, account_id=account_id, date=day)
            snap.follower_count = (acc.follower_count if acc else 0) or snap.follower_count
            snap.aweme_count = (acc.aweme_count if acc else 0) or snap.aweme_count or len(items)
            # 只有带作品列表(作品健康那趟)才更新互动合计;粉丝-only 快照不清零已有合计
            if items:
                snap.total_like = sum((w.get("like_count") or 0) for w in items)
                snap.total_comment = sum((w.get("comment_count") or 0) for w in items)
                snap.total_play = sum((w.get("play_count") or 0) for w in items)
            s.add(snap); s.commit()

    def _due(self, last_scan_at, interval_seconds) -> bool:
        """到点判断,叠加 ±jitter 随机,避免所有目标整点齐发(机器矩阵特征)。"""
        if last_scan_at is None:
            return True
        jitter = max(0.0, self.cfg.engine.scan_jitter)
        factor = 1.0 + random.uniform(-jitter, jitter) if jitter else 1.0
        return (datetime.utcnow() - last_scan_at).total_seconds() >= interval_seconds * factor

    async def _scan_once(self):
        due: list[tuple[int, int | None]] = []
        with get_session() as s:
            targets = s.exec(select(MonitorTarget).where(MonitorTarget.enabled == True)).all()  # noqa: E712
            for t in targets:
                if self._due(t.last_scan_at, t.interval_seconds):
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
        if platform == "kuaishou":
            return await self._scan_ks_target_locked(target_id)
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
            auto_download = target.download_enabled
            media_filter = target.media_filter or "all"
            strategy = _monitor_strategy(
                target, default_scrolls=12, default_items=0)

        items, author, error = await fetch_videos(
            self.browser, identity, sec_uid, known,
            max_scrolls=strategy["max_scrolls"],
            block_media=self.cfg.engine.block_media_resources,
            # 默认只监控订阅后的作品；显式首次回填时允许继续向历史翻页。
            stop_before=(0 if first_scan and backfill_count != 0 else scan_since))

        new_records = []
        selected = _select_douyin_awemes(
            items, quality, first_scan, scan_since, backfill_count)
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

        if new_records and not first_scan:
            await self._notify_new(target_name, [aw for _, aw, _ in new_records])

        await asyncio.gather(*(self._download(rec.id, aw, base_dir, proxy)
                               for rec, aw, should_download in new_records
                               if should_download))
        return {"ok": not error, "new": len(new_records), "error": error,
                "scanned": len(selected), "filtered": filtered_count}

    # ── 快手:创作者作品监控(浏览器拦截 GraphQL,与抖音同范式)──
    async def _scan_ks_target_locked(self, target_id: int) -> dict:
        with get_session() as s:
            target = s.get(MonitorTarget, target_id)
            if not target:
                return {"ok": False, "error": "target not found"}
            first_scan = target.last_scan_at is None
            identity = self.browser.anon_identity()
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
            user_id = target.sec_uid
            base_dir = target.download_dir or get_setting(
                "download_dir", self.cfg.engine.media_dir)
            quality = target.video_quality or get_setting("video_quality", "highest")
            auto_download = target.download_enabled
            media_filter = target.media_filter or "all"
            strategy = _monitor_strategy(
                target, default_scrolls=12, default_items=0)

        items, author, error = await fetch_ks_videos(
            self.browser, identity, user_id, known,
            max_scrolls=strategy["max_scrolls"],
            block_media=self.cfg.engine.block_media_resources)

        new_records = []
        seen = set()
        filtered_count = 0
        for item in items:
            aw = parse_ks_feed(item, quality)
            if not aw or aw.aweme_id in seen:
                continue
            seen.add(aw.aweme_id)
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

        if new_records and not first_scan:
            await self._notify_new(target_name, [aw for _, aw, _ in new_records])

        await asyncio.gather(*(self._download(rec.id, aw, base_dir, proxy)
                               for rec, aw, should_download in new_records
                               if should_download))
        return {"ok": not error, "new": len(new_records), "error": error,
                "scanned": len(seen), "filtered": filtered_count}

    def _mark_target_skip(self, target_id: int, msg: str) -> dict:
        """把跳过原因写到目标 last_error,并推进 last_scan_at(避免下轮立刻重试)。"""
        with get_session() as s:
            t = s.get(MonitorTarget, target_id)
            if t:
                t.last_scan_at = datetime.utcnow()
                t.last_error = msg
                s.add(t); s.commit()
        return {"ok": False, "new": 0, "error": msg, "skipped": True}

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
            identity = self.browser.anon_identity()
            if target.account_id:
                acc = s.get(DouyinAccount, target.account_id)
                if acc:
                    if self._proxy_bad(acc):
                        return self._mark_target_skip(
                            target_id, "账号代理标记为不可用(proxy bad),已跳过以免暴露真实 IP")
                    state = acc.storage_state or ""
                    proxy = acc.proxy or ""
                    identity = self.browser.identity_for(acc)
            known = set(s.exec(
                select(ContentRecord.aweme_id)
                .where(ContentRecord.target_id == target_id)).all())
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
                proxy=proxy)
        error = ""
        author = None
        briefs_raw: list = []
        try:
            if browser_reads and kind == "keyword":
                briefs_raw, browser_error = await fetch_xhs_search(
                    self.browser, identity, keyword, known,
                    max_scrolls=strategy["max_scrolls"],
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
                briefs_raw = await client.search_notes(keyword)
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
        next_long_pause = random.randint(3, 5)
        for raw in briefs_raw:
            if error and classify_platform_error(error)[0] in {
                    RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK}:
                break
            brief = parse_note_brief(raw)
            if not brief or brief["note_id"] in seen or brief["note_id"] in known:
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
            derr = ""
            card = {}
            try:
                if browser_reads:
                    card, derr = await fetch_xhs_note_detail(
                        self.browser, identity, brief["note_id"],
                        xsec_token=note_tok,
                        xsec_source=("pc_search" if kind == "keyword" else "pc_feed"),
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
                        xsec_source="pc_search" if kind == "keyword" else "pc_feed")
            except XhsApiError as e:
                error = e
                break
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
                # 详情抓取失败也建一条 failed 记录,保留 xsec_token 便于重试
                aw = Aweme(aweme_id=brief["note_id"], desc=brief.get("title", ""),
                           create_time=0, author_name="", media_type="images")
                aw.platform = "xhs"
                aw.cover = brief.get("cover", "")
            if not _monitor_content_matches(aw, strategy):
                filtered_count += 1
                continue
            should_download = bool(aw.medias) and auto_download and (
                media_filter == "all" or aw.media_type == media_filter)
            media_json = json.dumps([{"url": m.url, "kind": m.kind, "ext": m.ext,
                                      "index": m.index} for m in aw.medias])
            rec = ContentRecord(
                platform="xhs", target_id=target_id, aweme_id=aw.aweme_id, desc=aw.desc,
                media_type=aw.media_type, quality=aw.quality_label,
                create_time=aw.create_time, cover_url=aw.cover or "",
                like_count=aw.like_count, comment_count=aw.comment_count,
                duration=aw.duration, media_json=media_json, xsec_token=note_tok,
                download_status=("pending" if should_download
                                 else ("skipped" if aw.medias else "failed")),
                error="" if aw.medias else (derr or "未取到媒体直链"),
            )
            new_records.append((rec, aw, should_download))

        print(f"[xhs_scan] kind={kind} key={keyword or user_id} briefs={len(briefs_raw)} "
              f"new_records={len(new_records)} "
              f"details={detail_attempts}/{detail_budget or '不限'} "
              f"filtered={filtered_count} "
              f"with_media={sum(1 for _, a, _ in new_records if a.medias)} error={error!r}")

        target_name = ""
        with get_session() as s:
            for rec, _, _ in new_records:
                s.add(rec)
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
            s.commit()
            for rec, _, _ in new_records:
                s.refresh(rec)

        if new_records and not first_scan:
            await self._notify_new(target_name, [aw for _, aw, _ in new_records])

        await asyncio.gather(*(self._download(rec.id, aw, base_dir, proxy)
                               for rec, aw, should_download in new_records
                               if should_download))
        return {"ok": not error, "new": len(new_records), "error": error,
                "scanned": detail_attempts, "filtered": filtered_count}

    # ── 独立弹幕监控(DanmakuWatch)──
    async def _scan_danmaku_watches(self):
        due = []
        with get_session() as s:
            watches = s.exec(select(DanmakuWatch).where(
                DanmakuWatch.enabled == True)).all()  # noqa: E712
            for watch in watches:
                interval = watch.interval_seconds or self.cfg.engine.scan_interval_seconds
                if self._due(watch.last_scan_at, interval):
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
                    if platform != "douyin":
                        return {"ok": False, "error": "当前仅支持抖音短视频弹幕"}
                    if not acc.creator_storage_state:
                        return {"ok": False, "error": "需要先完成抖音创作者登录"}
                    identity = self.browser.identity_for(acc)
                    known = set(s.exec(select(DanmakuRecord.danmaku_id).where(
                        DanmakuRecord.watch_id == 0,
                        DanmakuRecord.aweme_id == item_id)).all())
                raw, err = await fetch_creator_danmaku(
                    self.browser, identity, known,
                    page_url=self.cfg.engine.creator_danmaku_url,
                    aweme_id=item_id,
                    max_scrolls=self.cfg.engine.danmaku_max_scrolls,
                    block_media=self.cfg.engine.block_media_resources,
                )
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
                          "added": added, "error": err}
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
            first_scan = watch.last_scan_at is None
            kind, mode = watch.kind, watch.mode
            aweme_id, sec_uid = watch.aweme_id, watch.sec_uid
            name = watch.title or aweme_id or (sec_uid[:12] if sec_uid else "watch")
            identity = self.browser.anon_identity()
            has_creator = False
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
                    identity = self.browser.identity_for(acc)

        if mode == "creator" and not has_creator:
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
                    raw, error = await fetch_creator_danmaku(
                        self.browser, identity, known,
                        page_url=self.cfg.engine.creator_danmaku_url,
                        aweme_id=aweme_id,
                        max_scrolls=settings["max_scrolls"],
                        max_items=raw_cap,
                        block_media=self.cfg.engine.block_media_resources,
                    )
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
                total_new = await self._ingest_danmaku(
                    watch_id, aweme_id, fresh, name, name, first_scan, source,
                    max_records_total=settings["max_records_total"])
            elif mode == "creator":
                with get_session() as s:
                    known = set(s.exec(select(DanmakuRecord.danmaku_id).where(
                        DanmakuRecord.watch_id == watch_id)).all())
                raw, error = await fetch_creator_danmaku(
                    self.browser, identity, known,
                    page_url=self.cfg.engine.creator_danmaku_url,
                    max_scrolls=settings["max_scrolls"],
                    max_items=raw_cap,
                    block_media=self.cfg.engine.block_media_resources,
                )
                grouped = {}
                for parsed in normalize_rows(raw):
                    if parsed and parsed.get("aweme_id"):
                        grouped.setdefault(parsed["aweme_id"], []).append(parsed)
                for aid, fresh in grouped.items():
                    total_new += await self._ingest_danmaku(
                        watch_id, aid, fresh, name, aid, first_scan, source,
                        max_records_total=settings["max_records_total"])
            else:
                items, _author, error = await fetch_videos(
                    self.browser, identity, sec_uid, set(),
                    max_scrolls=4, block_media=True)
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
                    raw, item_error = await fetch_danmaku(
                        self.browser, identity, aid, known,
                        max_rounds=max(1, min(settings["max_scrolls"], 2)),
                        start_ms=settings["time_start_ms"],
                        end_ms=settings["time_end_ms"],
                        step_seconds=settings["probe_step_seconds"],
                        max_points=settings["max_probe_points"],
                        max_items=raw_cap,
                        block_media=False)
                    if item_error and not error:
                        error = item_error
                    fresh = normalize_rows(raw, aid)
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

    async def _ingest_danmaku(self, watch_id: int, aweme_id: str, fresh: list,
                              name: str, work_desc: str, first_scan: bool,
                              source: str = "public",
                              max_records_total: int = 0) -> int:
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
                s.add(DanmakuRecord(platform="douyin", watch_id=watch_id,
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

    # ── 独立评论监控(CommentWatch)──
    async def _scan_comment_watches(self):
        due = []
        with get_session() as s:
            ws = s.exec(select(CommentWatch).where(CommentWatch.enabled == True)).all()  # noqa: E712
            for w in ws:
                if self._due(w.last_scan_at, w.interval_seconds):
                    due.append(w.id)
        for wid in due:
            await self.scan_comment_watch(wid)

    async def sync_work_comments(self, account_id: int, platform: str, item_id: str,
                                 xsec_token: str = "") -> dict:
        """抓「本账号某作品」的评论并落库(watch_id=0 标记本账号来源)。
        抖音直连(comment/list 分页 + 回复,参考 CommentAll),小红书走签名直连客户端,
        快手走浏览器拦截。返回 {ok, fetched, added, error}。"""
        key = f"wc:{account_id}:{item_id}"
        if key in self._inflight:
            return {"ok": True, "fetched": 0, "added": 0, "skipped": "正在抓取中"}
        self._inflight.add(key)
        try:
            return await self._guarded_read_dict(
                account_id, OperationKind.READ_HEAVY, key,
                lambda: self._sync_work_comments_locked(
                    account_id, platform, item_id, xsec_token))
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
            identity = self.browser.identity_for(acc)
            known = set(s.exec(select(CommentRecord.comment_id).where(
                CommentRecord.watch_id == 0,
                CommentRecord.aweme_id == item_id)).all())
        fresh: list = []
        error = ""
        try:
            if platform == "douyin":
                cookie = dy_cookie_from_state(state)
                if not cookie:
                    return {"ok": False, "error": "账号无抖音登录态 Cookie,无法直连抓评论"}
                client = DouyinClient(cookie, ua,
                                      timeout=self.cfg.engine.request_timeout_seconds,
                                      proxy=proxy)
                raw = await client.fetch_all_comments(item_id)
                fresh = [c for c in (parse_comment(rc) for rc in raw)
                         if c and c["comment_id"] not in known]
            elif platform == "xhs":
                client = self._xhs_client(identity, state, proxy)
                if client is None:
                    return {"ok": False, "error": "小红书账号缺 a1 Cookie,无法抓评论"}
                fresh = await self._xhs_fetch_comments(client, item_id, xsec_token, known)
            elif platform == "kuaishou":
                raw, err = await fetch_ks_comments(
                    self.browser, identity, item_id, known,
                    max_scrolls=self.cfg.engine.comment_max_scrolls,
                    block_media=self.cfg.engine.block_media_resources)
                error = err or ""
                fresh = [c for c in (parse_ks_comment(rc) for rc in flatten_ks_comments(raw))
                         if c and c["comment_id"] not in known]
            elif platform == "shipinhao":
                raw, err = await fetch_channels_comments(
                    self.browser, identity, item_id, known,
                    max_scrolls=self.cfg.engine.comment_max_scrolls,
                    block_media=self.cfg.engine.block_media_resources)
                error = err or ""
                fresh = [c for c in (parse_channels_comment(rc)
                                     for rc in flatten_channels_comments(raw))
                         if c and c["comment_id"] not in known]
            else:
                return {"ok": False, "error": f"不支持的平台:{platform}"}
        except XhsApiError as e:
            error = e
        except Exception as e:
            log.warning("本账号作品评论抓取失败 %s/%s: %s", platform, item_id, e)
            category, _signal = classify_platform_error(e)
            error = (e if category in {
                RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK
            } else repr(e))
            return {"ok": False, "error": error}
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
                "added": added, "error": error}

    async def fetch_douyin_follows_direct(self, account_id: int, direction: str):
        return await self.guarded_read_pair(
            account_id, OperationKind.READ_HEAVY,
            f"follows:{account_id}:{direction}",
            lambda: self._fetch_douyin_follows_direct_locked(
                account_id, direction),
            empty_result=[])

    async def _fetch_douyin_follows_direct_locked(self, account_id: int, direction: str):
        """抖音关注/粉丝直连(following/follower list 分页,比弹窗滚动抓得全)。
        返回 (归一用户列表, error);拿不到时上层回退浏览器拦截,故失败无副作用。"""
        from ..browser.account_hub import _norm_follow_user
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
        cookie = dy_cookie_from_state(state)
        if not cookie:
            return [], "no_cookie"
        client = DouyinClient(cookie, ua,
                              timeout=self.cfg.engine.request_timeout_seconds,
                              proxy=proxy)
        try:
            raw = await client.fetch_all_follows("", sec_uid, direction)
        except Exception as e:
            return [], repr(e)
        out = []
        for u in raw:
            n = _norm_follow_user(u, direction)
            if n:
                out.append(n)
        print(f"[follow-direct] dir={direction} sec_uid={sec_uid} raw={len(raw)} "
              f"norm={len(out)}")
        return out, ("" if out else "empty")

    async def scan_comment_watch(self, watch_id: int) -> dict:
        key = f"cw:{watch_id}"
        if key in self._inflight:
            return {"ok": True, "new_comments": 0, "skipped": "正在抓取中"}
        self._inflight.add(key)
        try:
            with get_session() as s:
                w = s.get(CommentWatch, watch_id)
                account_id = w.account_id if w else None
            return await self._guarded_read_dict(
                account_id, OperationKind.READ_HEAVY, key,
                lambda: self._scan_comment_watch_locked(watch_id))
        finally:
            self._inflight.discard(key)

    async def _scan_comment_watch_locked(self, watch_id: int) -> dict:
        with get_session() as s:
            w = s.get(CommentWatch, watch_id)
            if not w:
                return {"ok": False, "error": "watch not found"}
            first_scan = w.last_scan_at is None
            platform = w.platform
            kind, mode = w.kind, w.mode
            aweme_id, sec_uid = w.aweme_id, w.sec_uid
            xsec_token = w.xsec_token or ""
            name = w.title or aweme_id or (sec_uid[:12] if sec_uid else "watch")
            state = creator_state = proxy = ""
            identity = self.browser.anon_identity()
            has_creator = False
            if w.account_id:
                acc = s.get(DouyinAccount, w.account_id)
                if acc:
                    if self._proxy_bad(acc):
                        msg = "账号代理标记为不可用(proxy bad),已跳过以免暴露真实 IP"
                        w2 = s.get(CommentWatch, watch_id)
                        if w2:
                            w2.last_scan_at = datetime.utcnow()
                            w2.last_error = msg
                            s.add(w2); s.commit()
                        return {"ok": False, "new_comments": 0, "error": msg, "skipped": True}
                    state = acc.storage_state or ""
                    creator_state = acc.creator_storage_state or ""
                    proxy = acc.proxy or ""
                    has_creator = bool(creator_state)
                    identity = self.browser.identity_for(acc)

        error = ""
        total_new = 0
        author = None
        if platform == "xhs" and not state:
            msg = "小红书评论监控需要绑定一个已登录的小红书账号(笔记页需登录)"
            with get_session() as s:
                w = s.get(CommentWatch, watch_id)
                if w:
                    w.last_scan_at = datetime.utcnow()
                    w.last_error = msg
                    s.add(w); s.commit()
            return {"ok": False, "new_comments": 0, "error": msg}
        try:
            if platform == "xhs" and kind == "user":
                total_new, author = await self._cw_xhs_creator(
                    watch_id, identity, state, sec_uid,
                    xsec_token, name, first_scan, proxy)
            elif platform == "xhs":   # 单条笔记
                total_new, author = await self._cw_xhs_note(
                    watch_id, identity, state, aweme_id,
                    xsec_token, name, first_scan, proxy)
            elif platform == "kuaishou" and kind == "user":
                total_new, author = await self._cw_ks_user(watch_id, identity, sec_uid,
                                                           name, first_scan)
            elif platform == "kuaishou":   # 单条作品
                total_new, author = await self._cw_ks_video(watch_id, identity, aweme_id,
                                                            name, first_scan)
            elif kind == "user" and mode == "creator":
                total_new, author = await self._cw_creator(watch_id, identity, has_creator,
                                                           name, first_scan)
            elif kind == "user":
                total_new, author = await self._cw_user_public(watch_id, identity, sec_uid,
                                                               name, first_scan)
            else:  # video
                total_new, author = await self._cw_video(watch_id, identity, aweme_id,
                                                         name, first_scan)
        except XhsApiError as e:
            error = e
        except Exception as e:
            category, _signal = classify_platform_error(e)
            error = (e if category in {
                RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK
            } else repr(e))
            log.warning("评论监控 %s 失败: %s", watch_id, e)

        with get_session() as s:
            w = s.get(CommentWatch, watch_id)
            if w:
                w.last_scan_at = datetime.utcnow()
                w.last_error = str(error or "")
                if author:
                    if not w.title:
                        w.title = author.get("nickname") or w.title
                    if not w.avatar:
                        ava = (author.get("avatar_thumb") or {}).get("url_list") or []
                        w.avatar = ava[0] if ava else w.avatar
                w.comment_count = len(s.exec(select(CommentRecord.id)
                                             .where(CommentRecord.watch_id == watch_id)).all())
                s.add(w); s.commit()
        return {"ok": not error, "new_comments": total_new, "error": error}

    async def _ingest(self, watch_id, aweme_id, fresh, name, work_desc, first_scan,
                      platform="douyin") -> int:
        """fresh: parse_comment 结果(无 aweme_id)。入库 + 按时间水位线推送。"""
        if not fresh:
            return 0
        with get_session() as s:
            times = s.exec(select(CommentRecord.create_time)
                           .where(CommentRecord.watch_id == watch_id)
                           .where(CommentRecord.aweme_id == aweme_id)).all()
            prev_max = max([t for t in times if t] or [0])
            for c in fresh:
                s.add(CommentRecord(platform=platform, watch_id=watch_id,
                                    aweme_id=aweme_id, **c))
            s.commit()
        newer = [c for c in fresh if c["create_time"] > prev_max]
        if not first_scan and newer:
            await self._notify_comments(name, work_desc, newer)
        return len(fresh)

    def _comment_watch_settings(self, watch_id: int) -> dict:
        cfg = self.cfg.engine
        with get_session() as s:
            watch = s.get(CommentWatch, watch_id)
            return {
                "recent_works": ((watch.recent_works if watch else 0)
                                 or cfg.comment_recent_works),
                "recent_days": ((watch.recent_days if watch else 0)
                                or cfg.comment_recent_days),
                "max_scrolls": ((watch.max_scrolls if watch else 0)
                                or cfg.comment_max_scrolls),
            }

    async def _cw_video(self, watch_id, identity, aweme_id, name, first_scan):
        cfg = self.cfg.engine
        settings = self._comment_watch_settings(watch_id)
        with get_session() as s:
            known = set(s.exec(select(CommentRecord.comment_id)
                               .where(CommentRecord.watch_id == watch_id)
                               .where(CommentRecord.aweme_id == aweme_id)).all())
        raw, err = await fetch_comments(self.browser, identity, aweme_id, known,
                                        max_scrolls=settings["max_scrolls"],
                                        block_media=cfg.block_media_resources)
        if err:
            log.info("评论监控(视频)%s: %s", aweme_id, err)
        fresh = [c for c in (parse_comment(rc) for rc in raw) if c]
        n = await self._ingest(watch_id, aweme_id, fresh, name, name, first_scan)
        return n, None

    async def _cw_user_public(self, watch_id, identity, sec_uid, name, first_scan):
        cfg = self.cfg.engine
        settings = self._comment_watch_settings(watch_id)
        items, author, err = await fetch_videos(self.browser, identity, sec_uid, set(),
                                                max_scrolls=4,
                                                block_media=cfg.block_media_resources)
        if err:
            log.info("评论监控(账号)%s: %s", sec_uid, err)
        cutoff = int(time.time()) - settings["recent_days"] * 86400
        works = []
        for it in items:
            aid = str(it.get("aweme_id") or "")
            ct = int(it.get("create_time") or 0)
            if aid and ct >= cutoff:
                works.append((aid, (it.get("desc") or "")))
        works = works[:settings["recent_works"]]
        total = 0
        for aid, desc in works:
            with get_session() as s:
                known = set(s.exec(select(CommentRecord.comment_id)
                                   .where(CommentRecord.watch_id == watch_id)
                                   .where(CommentRecord.aweme_id == aid)).all())
            raw, _e = await fetch_comments(self.browser, identity, aid, known,
                                           max_scrolls=settings["max_scrolls"],
                                           block_media=cfg.block_media_resources)
            fresh = [c for c in (parse_comment(rc) for rc in raw) if c]
            total += await self._ingest(watch_id, aid, fresh, name, desc, first_scan)
        return total, author

    # ── 快手评论监控(浏览器拦截 GraphQL)──
    async def _cw_ks_video(self, watch_id, identity, photo_id, name, first_scan):
        cfg = self.cfg.engine
        settings = self._comment_watch_settings(watch_id)
        with get_session() as s:
            known = set(s.exec(select(CommentRecord.comment_id)
                               .where(CommentRecord.watch_id == watch_id)
                               .where(CommentRecord.aweme_id == photo_id)).all())
        raw, err = await fetch_ks_comments(self.browser, identity, photo_id, known,
                                           max_scrolls=settings["max_scrolls"],
                                           block_media=cfg.block_media_resources)
        if err:
            log.info("评论监控(快手作品)%s: %s", photo_id, err)
        fresh = [c for c in (parse_ks_comment(rc) for rc in flatten_ks_comments(raw)) if c]
        n = await self._ingest(watch_id, photo_id, fresh, name, name, first_scan,
                               platform="kuaishou")
        return n, None

    async def _cw_ks_user(self, watch_id, identity, user_id, name, first_scan):
        cfg = self.cfg.engine
        settings = self._comment_watch_settings(watch_id)
        items, author, err = await fetch_ks_videos(self.browser, identity, user_id, set(),
                                                   max_scrolls=4,
                                                   block_media=cfg.block_media_resources)
        if err:
            log.info("评论监控(快手账号)%s: %s", user_id, err)
        works = []
        cutoff = int(time.time()) - settings["recent_days"] * 86400
        for feed in items:
            aw = parse_ks_feed(feed)
            if aw and (not aw.create_time or aw.create_time >= cutoff):
                works.append((aw.aweme_id, aw.desc))
                if len(works) >= settings["recent_works"]:
                    break
        total = 0
        for pid, desc in works:
            with get_session() as s:
                known = set(s.exec(select(CommentRecord.comment_id)
                                   .where(CommentRecord.watch_id == watch_id)
                                   .where(CommentRecord.aweme_id == pid)).all())
            raw, _e = await fetch_ks_comments(self.browser, identity, pid, known,
                                              max_scrolls=settings["max_scrolls"],
                                              block_media=cfg.block_media_resources)
            fresh = [c for c in (parse_ks_comment(rc) for rc in flatten_ks_comments(raw)) if c]
            total += await self._ingest(watch_id, pid, fresh, name, desc, first_scan,
                                        platform="kuaishou")
        author_dict = parse_ks_self_user(author) if author else None
        return total, ({"nickname": author_dict["nickname"],
                        "avatar_thumb": {"url_list": [author_dict["avatar"]]}}
                       if author_dict else None)

    async def _cw_creator(self, watch_id, identity, has_creator, name, first_scan):
        if not has_creator:
            log.warning("评论监控 %s 选创作中心,但账号无创作者登录态", watch_id)
            return 0, None
        cfg = self.cfg.engine
        settings = self._comment_watch_settings(watch_id)
        with get_session() as s:
            known = set(s.exec(select(CommentRecord.comment_id)
                               .where(CommentRecord.watch_id == watch_id)).all())
            times = s.exec(select(CommentRecord.create_time)
                           .where(CommentRecord.watch_id == watch_id)).all()
            prev_max = max([t for t in times if t] or [0])
        raw, err = await fetch_creator_comments(self.browser, identity, known,
                                                page_url=cfg.creator_comment_url,
                                                max_scrolls=settings["max_scrolls"],
                                                block_media=cfg.block_media_resources)
        if err:
            log.info("评论监控(创作中心): %s", err)
        fresh = [c for c in (parse_creator_comment(rc) for rc in raw) if c]
        if not fresh:
            return 0, None
        with get_session() as s:
            for c in fresh:
                s.add(CommentRecord(watch_id=watch_id, **c))   # c 自带 aweme_id
            s.commit()
        newer = [c for c in fresh if c["create_time"] > prev_max]
        if not first_scan and newer:
            await self._notify_comments(name, "(创作中心)", newer)
        return len(fresh), None

    # ── 小红书评论监控(浏览器优先，签名 API 仅显式兼容)──
    def _xhs_client(self, identity, state: str, proxy: str = ""):
        cookie_str = cookie_str_from_state(state)
        if not has_a1(cookie_str):
            return None
        return XhsApiClient(
            cookie_str, self._direct_request_ua(identity),
                            timeout=self.cfg.engine.request_timeout_seconds, proxy=proxy)

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

    # ── 发布(小红书创作平台)+ 跨平台转发 ──
    def _content_files(self, rec: ContentRecord) -> list:
        """收集一条作品记录在本地的媒体文件路径。"""
        if not rec.local_path:
            return []
        p = Path(rec.local_path)
        if p.is_file():
            return [str(p)]
        folder = p if p.is_dir() else p.parent
        if not folder.exists():
            return []
        # 文件名形如 {aweme_id}_{title}_{index}.{ext};按末尾数字序号排(而非字典序,
        # 否则 10 张以上会 _10 排到 _2 前面 —— 图集顺序错乱、封面选错)。
        def _idx_key(f: Path):
            tail = f.stem.rsplit("_", 1)[-1]
            return (0, int(tail)) if tail.isdigit() else (1, f.name)
        cands = [f for f in folder.glob(f"{rec.aweme_id}_*")
                 if f.is_file() and not f.name.endswith(".part")]
        return [str(f) for f in sorted(cands, key=_idx_key)]

    def create_relay_publish(self, content_id: int, account_id: int,
                             target_platform: str = "xhs",
                             title: Optional[str] = None, desc: Optional[str] = None,
                             topics: Optional[str] = None,
                             visibility: str = "public", allow_save: bool = True,
                             media_order: Optional[list] = None
                             ) -> Optional[int]:
        """从已下载作品创建发往目标平台(小红书/抖音/视频号)的发布任务。返回任务 id。

        只接收作品 id,内部自开会话取记录,避免跨会话传入已绑定的 ORM 对象。
        target_platform: xhs / douyin / shipinhao。
        title/desc/topics 为 None 时沿用作品原始内容;传了则用编辑后的值(发布前可改)。
        """
        with get_session() as s:
            rec = s.get(ContentRecord, content_id)
            if not rec:
                return None
            files = self._content_files(rec)
            if not files:
                return None
            # 转发前若在弹窗里剔除/调序了图片,media_order 是保留下来的原始序号(按新顺序)。
            # 按它过滤+重排本地文件(首个=封面);越界序号忽略,全无效则回退全部原序。
            if media_order:
                picked = [files[i] for i in media_order
                          if isinstance(i, int) and 0 <= i < len(files)]
                if picked:
                    files = picked
            title_cap = {"douyin": 30, "shipinhao": 16}.get(target_platform, 20)
            t_title = (title if title is not None else (rec.desc or ""))[:title_cap]
            t_desc = desc if desc is not None else (rec.desc or "")
            t_topics = topics if topics is not None else ""
            task = PublishTask(
                platform=target_platform, account_id=account_id,
                media_type="video" if rec.media_type == "video" else "images",
                title=t_title, desc=t_desc, topics=t_topics,
                visibility=visibility, allow_save=allow_save,
                media_json=json.dumps(files),
                source_platform=rec.platform, source_content_id=rec.id,
            )
            s.add(task); s.commit(); s.refresh(task)
            return task.id

    async def _process_publish(self):
        due = []
        now = datetime.utcnow()
        with get_session() as s:
            tasks = s.exec(select(PublishTask)
                           .where(PublishTask.status == "pending")).all()
            for t in tasks:
                if t.scheduled_at is None or t.scheduled_at <= now:
                    due.append(t.id)
        for tid in due:
            await self.publish_task(tid)

    async def publish_task(self, task_id: int) -> dict:
        if task_id in self._publishing:
            return {"ok": False, "error": "正在发布中"}
        self._publishing.add(task_id)
        try:
            with get_session() as s:
                t = s.get(PublishTask, task_id)
                account_id = t.account_id if t else None
            # 发布串行 + 该账号串行(有头浏览器会接管该账号 profile,不能与抓取并发)
            async with self._publish_sem:
                async with self._operation_guard(
                        account_id, OperationKind.PUBLISH,
                        fallback_key=f"pub:{task_id}"):
                    return await self._publish_task_locked(task_id)
        finally:
            self._publishing.discard(task_id)

    async def _publish_task_locked(self, task_id: int) -> dict:
        with get_session() as s:
            t = s.get(PublishTask, task_id)
            if not t:
                return {"ok": False, "error": "任务不存在"}
            if t.status in ("done", "publishing"):
                return {"ok": False, "error": f"任务状态为 {t.status}"}
            acc = s.get(DouyinAccount, t.account_id) if t.account_id else None
            if not acc:
                t.status = "failed"
                t.error = "绑定账号不存在(可能已删除/重登成新号)"
                s.add(t); s.commit()
                return {"ok": False, "error": "account_missing"}
            if acc.status == "invalid":
                self._defer_row(t, "账号登录态已失效，等待重新登录", fallback_seconds=900)
                s.add(t); s.commit()
                return {"ok": False, "error": "account_invalid"}
            if self._proxy_bad(acc):
                self._defer_row(t, "账号代理当前不可用", fallback_seconds=300)
                s.add(t); s.commit()
                return {"ok": False, "error": "proxy unavailable"}
            environment_error = self._native_write_environment_error(
                acc, headed=True, browser_mode=True)
            if environment_error:
                self._defer_row(t, environment_error, fallback_seconds=300)
                s.add(t); s.commit()
                return {"ok": False, "error": environment_error}
            pause_error = self._write_pause_error(t.account_id)
            if pause_error:
                decision = self.risk.preflight(t.account_id, OperationKind.PUBLISH)
                self._defer_row(t, pause_error, decision.next_allowed_at,
                                signal=decision.signal)
                s.add(t); s.commit()
                return {"ok": False, "error": pause_error}
            if not self._in_active_window(t.account_id):
                self._defer_row(t, "当前处于非活跃时段，发布任务已保留在队列")
                s.add(t); s.commit()
                return {"ok": False, "error": t.error}
            decision = self.risk.preflight(t.account_id, OperationKind.PUBLISH)
            if not decision.allowed:
                self._defer_row(t, decision.reason, decision.next_allowed_at,
                                signal=decision.signal)
                s.add(t); s.commit()
                return {"ok": False, "error": decision.reason}
            # 发布用创作平台态;一次扫码已把创作 cookie 并入 storage_state,故回退它
            state = acc.creator_storage_state or acc.storage_state or ""
            native_mode = acc.identity_mode == "native"
            identity = self.browser.identity_for(acc)
            media_type, title, desc, topics = t.media_type, t.title, t.desc, t.topics
            visibility, allow_save = t.visibility, t.allow_save
            location = getattr(t, "location", "") or ""
            platform = t.platform
            files = _loads_list(t.media_json)
            t.status = "publishing"; t.error = ""
            self._clear_row_block(t)
            s.add(t); s.commit()

        if platform == "kuaishou":
            # 快手发布:登录态在该账号持久 profile 里(creator/storage 任一即可),走浏览器自动化
            if not state:
                return await self._finish_publish(
                    task_id, False, "", "该账号未完成快手「创作者登录」,请先在账号页点「创作者登录」")
            try:
                ok, url, err = await publish_kuaishou(self.browser, identity, state,
                                                      media_type, title, desc, files,
                                                      topics=topics, headed=True)
            except Exception as e:
                ok, url, err = False, "", f"发布异常: {e!r}"
            return await self._finish_publish(task_id, ok, url, err, platform="kuaishou")

        if platform == "shipinhao":
            # 视频号发布:登录态在该账号持久 profile 里,走浏览器自动化(wujie shadowRoot)
            if not state:
                return await self._finish_publish(
                    task_id, False, "", "该账号未完成视频号登录,请先在账号页点「视频号登录」")
            try:
                ok, url, err = await publish_channels(self.browser, identity, state,
                                                      media_type, title, desc, files,
                                                      topics=topics, headed=True,
                                                      location=location)
            except Exception as e:
                ok, url, err = False, "", f"发布异常: {e!r}"
            return await self._finish_publish(task_id, ok, url, err, platform="shipinhao")

        if platform == "douyin":
            # 抖音发布:同快手走浏览器自动化,登录态在该账号持久 profile 里
            if not state:
                return await self._finish_publish(
                    task_id, False, "", "该账号未完成抖音「创作者登录」,请先在账号页点「创作者登录」")
            try:
                ok, url, err = await publish_douyin(self.browser, identity, state,
                                                    media_type, title, desc, files,
                                                    topics=topics, visibility=visibility,
                                                    allow_save=allow_save, headed=True)
            except Exception as e:
                ok, url, err = False, "", f"发布异常: {e!r}"
            return await self._finish_publish(task_id, ok, url, err, platform="douyin")

        if not state:
            return await self._finish_publish(
                task_id, False, "", "该账号未完成小红书「创作者登录」,请先在账号页点「创作者登录」")

        xhs_mode = ("browser" if native_mode
                    else self._xhs_publish_mode())
        try:
            ok, url, err = await publish_xhs(self.browser, identity, state, media_type,
                                             title, desc, files, topics=topics,
                                             headed=True,
                                             mode=xhs_mode,
                                             on_submit=(
                                                 lambda: self._mark_browser_submit(
                                                     PublishTask, task_id)
                                                 if xhs_mode == "browser" else None))
        except Exception as e:
            ok, url, err = False, "", f"发布异常: {e!r}"
        return await self._finish_publish(task_id, ok, url, err)

    async def _finish_publish(self, task_id, ok, url, err, platform="xhs") -> dict:
        account_id = None
        failure = None
        uncertain = (not ok and isinstance(err, str)
                     and err.startswith("write_uncertain:"))
        if not ok and not uncertain:
            with get_session() as s:
                task = s.get(PublishTask, task_id)
                account_id = task.account_id if task else None
            if account_id:
                failure = self.risk.record_failure(
                    account_id, OperationKind.PUBLISH, err)
        with get_session() as s:
            t = s.get(PublishTask, task_id)
            if t:
                account_id = t.account_id
                if ok:
                    t.status = "done"
                    t.done_at = datetime.utcnow()
                elif uncertain:
                    # Submission crossed the click boundary but success evidence
                    # was lost.  Never enqueue it again automatically.
                    t.status = "uncertain"
                    t.scheduled_at = None
                    t.done_at = None
                elif failure and failure.controlled and failure.category in {
                        RiskCategory.RISK, RiskCategory.NETWORK, RiskCategory.AUTH}:
                    self._defer_row(t, err, failure.next_allowed_at,
                                    signal=failure.signal)
                else:
                    t.status = "failed"
                t.result_url = url or t.result_url
                t.error = "" if ok else err
                s.add(t); s.commit()
        if ok and account_id:
            self.risk.record_success(account_id, OperationKind.PUBLISH)
        if ok:
            try:
                with get_session() as s:
                    chans = s.exec(select(NotificationChannel)
                                   .where(NotificationChannel.enabled == True)).all()  # noqa: E712
                    channels = [{"type": c.type, "config": _loads(c.config)} for c in chans]
                if channels:
                    pname = {"kuaishou": "快手", "douyin": "抖音",
                             "shipinhao": "视频号"}.get(platform, "小红书")
                    await notify_all(channels, f"{pname}发布成功", url or "已发布一条作品")
            except Exception:
                pass
        return {"ok": ok, "url": url, "error": err}

    # ── 活跃时段(夜间静默)──
    def _in_active_window(self, account_id=None) -> bool:
        """Check active hours in the bound account's persisted timezone."""
        if not self.cfg.engine.quiet_hours_enabled:
            return True
        account = self._load_account(account_id)
        if account is not None:
            return self.risk._in_active_window(account, datetime.utcnow())
        start = self.cfg.engine.active_hours_start
        end = self.cfg.engine.active_hours_end
        if end <= start:
            return True
        h = (datetime.utcnow() + timedelta(hours=8)).hour   # 东八区(账号默认时区)
        if end <= 24:
            return start <= h < end
        return h >= start or h < (end - 24)                 # 跨零点

    # ── 自动评论:规则生成任务 + 任务执行 ──
    def _today_start(self, account_id=None) -> datetime:
        n = datetime.utcnow()
        account = self._load_account(account_id)
        if account is not None:
            return self.risk._local_day_start_utc(account, n)
        return datetime(n.year, n.month, n.day)

    @staticmethod
    def _hour_ago() -> datetime:
        return datetime.utcnow() - timedelta(hours=1)

    def _acct_today_count(self, s, account_id) -> int:
        """该账号今日已成功发出的评论数(跨所有规则,用于全局每日上限)。"""
        if not account_id:
            return 0
        return len(s.exec(select(CommentTask.id)
                          .where(CommentTask.account_id == account_id)
                          .where(CommentTask.status == "done")
                          .where(CommentTask.done_at >= self._today_start(account_id))).all())

    def _acct_hour_comment_count(self, account_id) -> int:
        """该账号近一小时已成功发出的评论数(每小时配额,比日上限更贴人类节律)。"""
        if not account_id:
            return 0
        with get_session() as s:
            return len(s.exec(select(CommentTask.id)
                              .where(CommentTask.account_id == account_id)
                              .where(CommentTask.status == "done")
                              .where(CommentTask.done_at >= self._hour_ago())).all())

    def _rule_today_count(self, s, rule_id) -> int:
        rule = s.get(CommentRule, rule_id)
        account_id = rule.account_id if rule else None
        return len(s.exec(select(CommentTask.id)
                          .where(CommentTask.rule_id == rule_id)
                          .where(CommentTask.status == "done")
                          .where(CommentTask.done_at >= self._today_start(account_id))).all())

    def _acct_gap_ok(self, account_id) -> bool:
        """距该账号上一条成功评论是否已超过全局最小间隔(防同账号连发)。"""
        if not account_id:
            return True
        gap = self.cfg.engine.comment_min_gap_seconds
        if gap <= 0:
            return True
        with get_session() as s:
            rows = s.exec(select(CommentTask.done_at)
                          .where(CommentTask.account_id == account_id)
                          .where(CommentTask.status == "done")).all()
        last = max([d for d in rows if d] or [None])
        return last is None or (datetime.utcnow() - last).total_seconds() >= gap

    def _comment_gate_error(self, account_id) -> str:
        """Return the reason a comment write must remain queued.

        This is deliberately checked again inside the account lock.  The
        scheduler check is only an optimization; API-triggered ``run-now``
        and concurrent callers must go through the same gate.
        """
        pause_error = self._write_pause_error(account_id)
        if pause_error:
            return pause_error
        if not self._in_active_window(account_id):
            return "当前处于非活跃时段，评论任务已保留在队列"
        hcap = self.cfg.engine.comment_hourly_cap_per_account
        if hcap > 0 and self._acct_hour_comment_count(account_id) >= hcap:
            return "已达到账号每小时评论上限"
        if not self._acct_gap_ok(account_id):
            return "尚未达到账号评论最小间隔"
        decision = self.risk.preflight(account_id, OperationKind.COMMENT)
        if not decision.allowed:
            return decision.reason
        return ""

    async def _process_comment_rules(self):
        due = []
        with get_session() as s:
            rules = s.exec(select(CommentRule).where(CommentRule.enabled == True)).all()  # noqa: E712
            for r in rules:
                if self._due(r.last_run_at, r.interval_seconds):
                    due.append(r.id)
        for rid in due:
            try:
                await self.run_comment_rule(rid)
            except Exception as e:
                log.warning("自动评论规则 %s 生成失败: %s", rid, e)
                self._mark_rule(rid, f"生成失败: {e!r}")

    def _ai_settings(self):
        """读全局 AI 文案设置;未启用返回 None(引擎据此决定是否调大模型)。"""
        if get_setting("ai_enabled", "0") != "1":
            return None
        return {
            "base_url": get_setting("ai_base_url", ""),
            "api_key": get_setting("ai_api_key", ""),
            "model": get_setting("ai_model", ""),
            "prompt": get_setting("ai_prompt", ""),
            "temperature": get_setting("ai_temperature", "0.9"),
        }

    def _mark_rule(self, rule_id, error: str):
        with get_session() as s:
            r = s.get(CommentRule, rule_id)
            if r:
                r.last_run_at = datetime.utcnow()
                r.last_error = error
                s.add(r); s.commit()

    async def run_comment_rule(self, rule_id: int) -> dict:
        """跑一轮规则:发现目标 -> 去重/过滤 -> 生成 CommentTask(错峰排期)。"""
        with get_session() as s:
            r = s.get(CommentRule, rule_id)
            if not r:
                return {"ok": False, "error": "规则不存在"}
            rf = dict(platform=r.platform, mode=r.mode, target_kind=r.target_kind,
                       keyword=r.keyword, sec_uid=r.sec_uid, aweme_id=r.aweme_id,
                       xsec_token=r.xsec_token, daily_cap=r.daily_cap,
                       min_gap=r.min_gap_seconds, max_per_run=r.max_per_run,
                       account_id=r.account_id, reply_filter=(r.reply_filter or "").strip(),
                       skip_keywords=r.skip_keywords or "",
                       require_review=bool(r.require_review))
            templates = compose.parse_templates(r.templates)
            use_ai = bool(r.use_ai)
            acc = s.get(DouyinAccount, r.account_id) if r.account_id else None
            rf["account_uid"] = acc.uid if acc else ""
            rf["has_creator"] = bool(acc and acc.creator_storage_state)
            if acc and acc.status == "invalid":
                self._mark_rule(rule_id, "账号登录态已失效")
                return {"ok": False, "error": "account_invalid"}
            if acc and self._proxy_bad(acc):
                self._mark_rule(rule_id, "账号代理标记为不可用(proxy bad),已跳过")
                return {"ok": False, "error": "proxy bad"}
            acc_state = acc.storage_state if acc else ""
            acc_proxy = acc.proxy if acc else ""
            acc_sec_uid = acc.sec_uid if acc else ""
            acc_nick = acc.nickname if acc else ""
            identity = self.browser.identity_for(acc) if acc else self.browser.anon_identity()

        if not rf["account_id"] or not acc:
            self._mark_rule(rule_id, "未绑定发评论账号")
            return {"ok": False, "error": "未绑定账号"}
        if not templates:
            self._mark_rule(rule_id, "未配置文案模板")
            return {"ok": False, "error": "未配置文案模板"}
        pause_error = self._write_pause_error(rf["account_id"])
        if pause_error:
            self._mark_rule(rule_id, pause_error)
            return {"ok": False, "error": pause_error}

        xhs_manual_only = (rf["platform"] == "xhs"
                           and self._xhs_comment_write_mode() == "manual")
        xhs_review_required = (rf["platform"] == "xhs"
                               and bool(getattr(
                                   self.cfg.engine,
                                   "xhs_comment_review_before_publish",
                                   True)))
        review_required = rf["require_review"] or xhs_review_required

        skip_words = [w.strip() for w in rf["skip_keywords"].split(",") if w.strip()]
        ai = self._ai_settings() if use_ai else None

        read_decision = self.risk.preflight(
            rf["account_id"], OperationKind.READ_HEAVY)
        if not read_decision.allowed:
            return {"ok": False, "error": read_decision.reason,
                    "skipped": True,
                    "next_allowed_at": (read_decision.next_allowed_at.isoformat()
                                        if read_decision.next_allowed_at else None)}
        async with self._operation_guard(
                rf["account_id"], OperationKind.READ_HEAVY,
                fallback_key=f"rule:{rule_id}"):
            read_decision = self.risk.preflight(
                rf["account_id"], OperationKind.READ_HEAVY)
            if not read_decision.allowed:
                return {"ok": False, "error": read_decision.reason,
                        "skipped": True,
                        "next_allowed_at": (
                            read_decision.next_allowed_at.isoformat()
                            if read_decision.next_allowed_at else None)}
            try:
                cands, error = await self._discover_targets(
                    rf, acc_state, acc_proxy, acc_sec_uid, acc_nick, identity)
            except Exception as e:
                self.risk.record_failure(
                    rf["account_id"], OperationKind.READ_HEAVY, e)
                self._mark_rule(rule_id, f"发现目标失败: {e!r}")
                return {"ok": False, "error": repr(e)}
            if error:
                self.risk.record_failure(
                    rf["account_id"], OperationKind.READ_HEAVY, error)
                category, _signal = classify_platform_error(error)
                error = str(error)
                if category in {
                        RiskCategory.RISK, RiskCategory.AUTH,
                        RiskCategory.NETWORK}:
                    self._mark_rule(rule_id, f"发现目标失败: {error}")
                    return {
                        "ok": False, "created": 0, "candidates": 0,
                        "error": error,
                    }
            else:
                self.risk.record_success(
                    rf["account_id"], OperationKind.READ_HEAVY)

        # 过滤 + 去重 + 生成
        created = 0
        with get_session() as s:
            existing = set()
            for row in s.exec(select(CommentTask.aweme_id, CommentTask.target_comment_id)
                              .where(CommentTask.rule_id == rule_id)).all():
                existing.add((row[0], row[1]))
            # 单列 select:exec().all() 直接返回标量(同 known= 查询的写法),勿用 (a,) 解包
            acct_commented = set(s.exec(
                select(CommentTask.aweme_id)
                .where(CommentTask.account_id == rf["account_id"])).all())
            remain = min(rf["max_per_run"],
                         max(0, rf["daily_cap"] - self._rule_today_count(s, rule_id)))
            cap = self.cfg.engine.comment_daily_cap_per_account
            if cap > 0:
                remain = min(remain, max(0, cap - self._acct_today_count(s, rf["account_id"])))

            base = datetime.utcnow()
            gap = max(1, rf["min_gap"], self.cfg.engine.comment_min_gap_seconds)
            jitter = max(0.0, self.cfg.engine.comment_jitter)
            offset = 0.0
            to_rest = random.randint(3, 6)   # 突发+休息:连发几条后插一段长歇,别匀速排队
            skip = {"dup": 0, "skip_kw": 0, "filter": 0, "empty": 0, "cap": 0}
            for c in cands:
                if remain <= 0:
                    skip["cap"] += 1
                    continue
                key = (c["aweme_id"], c.get("target_comment_id", ""))
                if key in existing:
                    skip["dup"] += 1
                    continue
                # auto_comment:同账号不在同一作品下重复评论
                if rf["mode"] == "auto_comment" and c["aweme_id"] in acct_commented:
                    skip["dup"] += 1
                    continue
                text_blob = (c.get("source_text", "") or "")
                if skip_words and any(w in text_blob for w in skip_words):
                    skip["skip_kw"] += 1
                    continue
                if rf["mode"] == "auto_reply" and rf["reply_filter"] \
                        and rf["reply_filter"] not in text_blob:
                    skip["filter"] += 1
                    continue
                content = ""
                if ai:   # 优先大模型生成,失败回退模板库
                    try:
                        gctx = dict(c.get("ctx", {}))
                        gctx.update(source_text=c.get("source_text", ""),
                                    platform=rf["platform"], mode=rf["mode"])
                        content = await compose.generate(gctx, ai)
                    except Exception as e:
                        log.info("AI 文案生成失败,回退模板: %s", e)
                        content = ""
                if not content:
                    content = compose.render(templates, c.get("ctx", {}))
                if not content:
                    skip["empty"] += 1
                    continue
                step = gap * (1.0 + random.uniform(-jitter, jitter)) if jitter else gap
                to_rest -= 1
                if to_rest <= 0:                 # 一簇发完,插一段 3~8 倍 gap 的长歇再继续
                    step += gap * random.uniform(3, 8)
                    to_rest = random.randint(3, 6)
                offset += step
                sched = base + timedelta(seconds=offset)
                # 小红书默认先生成草稿,人工通过后由队列自动发布;
                # manual 模式则始终只保留草稿,不调用签名直连评论接口。
                status = "draft" if (review_required or xhs_manual_only) else "pending"
                s.add(CommentTask(
                    platform=rf["platform"], rule_id=rule_id, account_id=rf["account_id"],
                    aweme_id=c["aweme_id"], xsec_token=c.get("xsec_token", ""),
                    target_comment_id=c.get("target_comment_id", ""),
                    target_nick=c.get("target_nick", ""),
                    target_text=(c.get("source_text", "") or "")[:200],
                    content=content,
                    method="manual" if xhs_manual_only else "",
                    scheduled_at=sched, status=status))
                existing.add(key)
                acct_commented.add(c["aweme_id"])
                created += 1

            # 跳过原因汇总(让"发现N个却生成0条"能解释清楚)
            parts = []
            if skip["filter"]:
                parts.append(f'{skip["filter"]}条不含回复过滤词「{rf["reply_filter"]}」')
            if skip["skip_kw"]:
                parts.append(f'{skip["skip_kw"]}条命中跳过词')
            if skip["dup"]:
                parts.append(f'{skip["dup"]}条已生成过/已评论过')
            if skip["empty"]:
                parts.append(f'{skip["empty"]}条文案渲染为空')
            if skip["cap"]:
                parts.append(f'{skip["cap"]}条超出本轮上限/每日上限')
            note = ";".join(parts)

            r = s.get(CommentRule, rule_id)
            if r:
                r.last_run_at = datetime.utcnow()
                if error:
                    r.last_error = error
                elif not cands:
                    r.last_error = "本轮未发现可评论目标"
                elif created == 0:
                    r.last_error = f"发现{len(cands)}个目标但生成0条:{note or '全部被排除'}"
                else:
                    r.last_error = ""
                s.add(r)
            s.commit()
        log.info("自动评论规则 %s:发现 %s 候选,生成 %s 条任务 (skip=%s)",
                 rule_id, len(cands), created, skip)
        return {"ok": True, "created": created, "candidates": len(cands),
                "skipped": skip, "note": note, "error": error,
                "review": review_required or xhs_manual_only,
                "manual_only": xhs_manual_only}

    @staticmethod
    def _is_self_comment(raw: dict, acc_nick: str, acc_sec_uid: str = "",
                         acc_uid: str = "") -> bool:
        """Prefer stable account ids over nickname-only self-comment filtering."""
        if not isinstance(raw, dict):
            return False
        user = (raw.get("user") or raw.get("commenter")
                or raw.get("user_info") or {})
        if not isinstance(user, dict):
            user = {}
        mine = {str(v).strip() for v in (acc_uid, acc_sec_uid) if str(v or "").strip()}
        seen = set()
        for obj in (raw, user):
            for key in ("uid", "user_id", "userId", "sec_uid", "secUid"):
                value = obj.get(key)
                if value not in (None, ""):
                    seen.add(str(value).strip())
        if mine and mine.intersection(seen):
            return True
        nick = (raw.get("user_nickname") or raw.get("nickname")
                or user.get("nickname") or user.get("name") or "")
        return bool(acc_nick and nick == acc_nick)

    async def _discover_targets(self, rf, state, proxy, acc_sec_uid, acc_nick, identity):
        """按规则模式发现可评论目标。返回 (candidates, error)。
        candidate: {aweme_id, xsec_token, target_comment_id, target_nick, ctx, source_text}"""
        platform, mode, kind = rf["platform"], rf["mode"], rf["target_kind"]
        cands: list = []
        # ── 小红书:签名直连 ──
        if platform == "xhs":
            client = self._xhs_client(identity, state, proxy)
            if client is None:
                return [], "账号登录态缺少 a1,请重新扫码登录"
            if mode == "auto_comment":
                if kind == "keyword":
                    raw = await client.search_notes(rf["keyword"])
                else:   # creator
                    d = await client.notes_by_creator(rf["sec_uid"], xsec_token=rf["xsec_token"])
                    raw = d.get("notes") or []
                for it in raw:
                    b = parse_note_brief(it)
                    if not b:
                        continue
                    cands.append({"aweme_id": b["note_id"],
                                  "xsec_token": b.get("xsec_token", ""),
                                  "target_comment_id": "", "target_nick": "",
                                  "ctx": {"kw": rf["keyword"]},
                                  "source_text": b.get("title", "")})
            else:   # auto_reply:回复自己作品的评论
                notes = []
                if kind == "work" and rf["aweme_id"]:
                    notes = [{"note_id": rf["aweme_id"], "xsec_token": rf["xsec_token"]}]
                else:
                    d = await client.notes_by_creator(acc_sec_uid, xsec_token=rf["xsec_token"])
                    for it in (d.get("notes") or [])[:self.cfg.engine.comment_recent_works]:
                        b = parse_note_brief(it)
                        if b:
                            notes.append({"note_id": b["note_id"],
                                          "xsec_token": b.get("xsec_token", "")})
                for nt in notes:
                    try:
                        d = await client.note_comments(nt["note_id"], xsec_token=nt["xsec_token"])
                        rawc = d.get("comments") or []
                    except XhsApiError as e:
                        return [], e
                    except Exception as exc:
                        category, _signal = classify_platform_error(exc)
                        if category in {
                                RiskCategory.RISK, RiskCategory.AUTH,
                                RiskCategory.NETWORK}:
                            return [], exc
                        continue
                    for rc in flatten_xhs_comments(rawc):
                        c = parse_xhs_comment(rc)
                        if not c or not c.get("comment_id"):
                            continue
                        if c.get("user_nickname") and c["user_nickname"] == acc_nick:
                            continue   # 不回复自己
                        cands.append({"aweme_id": nt["note_id"],
                                      "xsec_token": nt["xsec_token"],
                                      "target_comment_id": c["comment_id"],
                                      "target_nick": c.get("user_nickname", ""),
                                      "ctx": {"nick": c.get("user_nickname", "")},
                                      "source_text": c.get("text", "")})
            return cands, ""
        # ── 快手:浏览器自动化(拦截 GraphQL,与抖音同范式)──
        if platform == "kuaishou":
            if mode == "auto_comment":
                if kind == "keyword":
                    return [], "快手暂不支持关键词发现,请用「创作者」模式指定博主"
                items, _author, err = await fetch_ks_videos(
                    self.browser, identity, rf["sec_uid"], set(), max_scrolls=4,
                    block_media=self.cfg.engine.block_media_resources)
                for feed in items[:self.cfg.engine.comment_recent_works]:
                    aw = parse_ks_feed(feed)
                    if aw:
                        cands.append({"aweme_id": aw.aweme_id, "xsec_token": "",
                                      "target_comment_id": "", "target_nick": "",
                                      "ctx": {}, "source_text": aw.desc})
                return cands, err
            # auto_reply 快手:回复自己作品评论
            works = []
            if rf["target_kind"] == "work" and rf["aweme_id"]:
                works = [(rf["aweme_id"], "")]
            else:
                items, _a, err = await fetch_ks_videos(
                    self.browser, identity, acc_sec_uid, set(), max_scrolls=4,
                    block_media=self.cfg.engine.block_media_resources)
                if err and classify_platform_error(err)[0] in {
                        RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK}:
                    return [], err
                cutoff = int(time.time()) - max(0, self.cfg.engine.comment_recent_days) * 86400
                for feed in items[:self.cfg.engine.comment_recent_works]:
                    aw = parse_ks_feed(feed)
                    if aw and (not cutoff or not aw.create_time or aw.create_time >= cutoff):
                        works.append((aw.aweme_id, aw.desc))
            for pid, _desc in works:
                raw, comment_error = await fetch_ks_comments(
                    self.browser, identity, pid, set(),
                    max_scrolls=self.cfg.engine.comment_max_scrolls,
                    block_media=self.cfg.engine.block_media_resources)
                if comment_error and classify_platform_error(comment_error)[0] in {
                        RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK}:
                    return [], comment_error
                for rc in flatten_ks_comments(raw):
                    c = parse_ks_comment(rc)
                    if not c or not c.get("comment_id"):
                        continue
                    if c.get("user_nickname") and c["user_nickname"] == acc_nick:
                        continue
                    cands.append({"aweme_id": pid, "xsec_token": "",
                                  "target_comment_id": c["comment_id"],
                                  "target_nick": c.get("user_nickname", ""),
                                  "ctx": {"nick": c.get("user_nickname", "")},
                                  "source_text": c.get("text", "")})
            return cands, ""
        # ── 抖音:浏览器自动化(发现仍用拦截抓取)──
        if mode == "auto_comment":
            if kind == "keyword":
                return [], "抖音暂不支持关键词发现,请用「创作者」模式指定博主"
            items, _author, err = await fetch_videos(
                self.browser, identity, rf["sec_uid"], set(), max_scrolls=4,
                block_media=self.cfg.engine.block_media_resources)
            for it in items[:self.cfg.engine.comment_recent_works]:
                aid = str(it.get("aweme_id") or "")
                if aid:
                    cands.append({"aweme_id": aid, "xsec_token": "",
                                  "target_comment_id": "", "target_nick": "",
                                  "ctx": {}, "source_text": it.get("desc", "")})
            return cands, err
        # auto_reply 抖音:回复自己作品评论
        if rf.get("has_creator"):
            raw, creator_error = await fetch_creator_comments(
                self.browser, identity, set(),
                page_url=self.cfg.engine.creator_comment_url,
                max_scrolls=max(1, min(self.cfg.engine.comment_max_scrolls, 4)),
                block_media=self.cfg.engine.block_media_resources)
            selected_works = set()
            comment_cutoff = int(time.time()) - max(0, self.cfg.engine.comment_recent_days) * 86400
            for rc in raw:
                c = parse_creator_comment(rc)
                if not c or not c.get("comment_id") or not c.get("aweme_id"):
                    continue
                created_at = int(c.get("create_time") or 0)
                if created_at > 100_000_000_000:
                    created_at //= 1000
                if comment_cutoff and created_at and created_at < comment_cutoff:
                    continue
                if kind == "work" and c["aweme_id"] != rf["aweme_id"]:
                    continue
                if self._is_self_comment(rc, acc_nick, acc_sec_uid,
                                         rf.get("account_uid", "")):
                    continue
                if kind != "work" and c["aweme_id"] not in selected_works:
                    if len(selected_works) >= self.cfg.engine.comment_recent_works:
                        continue
                    selected_works.add(c["aweme_id"])
                cands.append({"aweme_id": c["aweme_id"], "xsec_token": "",
                              "target_comment_id": c["comment_id"],
                              "target_nick": c.get("user_nickname", ""),
                              "ctx": {"nick": c.get("user_nickname", "")},
                              "source_text": c.get("text", "")})
            return cands, creator_error
        works = []
        if rf["target_kind"] == "work" and rf["aweme_id"]:
            works = [(rf["aweme_id"], "")]
        else:
            items, _a, err = await fetch_videos(
                self.browser, identity, acc_sec_uid, set(), max_scrolls=4,
                block_media=self.cfg.engine.block_media_resources)
            if err and classify_platform_error(err)[0] in {
                    RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK}:
                return [], err
            cutoff = int(time.time()) - max(0, self.cfg.engine.comment_recent_days) * 86400
            for it in items[:self.cfg.engine.comment_recent_works]:
                aid = str(it.get("aweme_id") or "")
                create_time = int(it.get("create_time") or 0)
                if aid and (not cutoff or not create_time or create_time >= cutoff):
                    works.append((aid, it.get("desc", "")))
        for aid, _desc in works:
            raw, comment_error = await fetch_comments(
                self.browser, identity, aid, set(),
                max_scrolls=self.cfg.engine.comment_max_scrolls,
                block_media=self.cfg.engine.block_media_resources)
            if comment_error and classify_platform_error(comment_error)[0] in {
                    RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK}:
                return [], comment_error
            for rc in raw:
                c = parse_comment(rc)
                if not c or not c.get("comment_id"):
                    continue
                if self._is_self_comment(rc, acc_nick, acc_sec_uid,
                                         rf.get("account_uid", "")):
                    continue
                cands.append({"aweme_id": aid, "xsec_token": "",
                              "target_comment_id": c["comment_id"],
                              "target_nick": c.get("user_nickname", ""),
                              "ctx": {"nick": c.get("user_nickname", "")},
                              "source_text": c.get("text", "")})
        return cands, ""

    async def _process_comment_tasks(self):
        now = datetime.utcnow()
        due = []
        with get_session() as s:
            tasks = s.exec(select(CommentTask).where(CommentTask.status == "pending")).all()
            for t in tasks:
                if t.scheduled_at is None or t.scheduled_at <= now:
                    due.append((t.id, t.account_id))
        seen_acct = set()
        for tid, aid in due:
            # 同一轮每账号最多执行一条,且尊重全局最小间隔 + 每小时配额(其余下轮再发)
            if aid in seen_acct or self._comment_gate_error(aid):
                continue
            seen_acct.add(aid)
            try:
                await self.execute_comment_task(tid)
            except Exception as e:
                log.warning("评论任务 %s 执行异常: %s", tid, e)

    # ── 本账号写操作队列(取关/回关/发私信)──
    def _action_gap_ok(self, account_id, gap: int) -> bool:
        """距该账号上一次成功写操作是否已超过最小间隔(防同账号连发)。
        实际间隔取「任务级 min_gap」与「全局 action_min_gap_seconds」的较大者。"""
        if not account_id:
            return True
        gap = max(gap, self.cfg.engine.action_min_gap_seconds)
        if gap <= 0:
            return True
        with get_session() as s:
            rows = s.exec(select(AccountActionTask.done_at)
                          .where(AccountActionTask.account_id == account_id)
                          .where(AccountActionTask.status == "done")).all()
        last = max([d for d in rows if d] or [None])
        return last is None or (datetime.utcnow() - last).total_seconds() >= gap

    def _action_count_since(self, account_id, since: datetime) -> int:
        """该账号自 since 起已成功执行的写操作数(用于每日/每小时上限)。"""
        if not account_id:
            return 0
        with get_session() as s:
            return len(s.exec(select(AccountActionTask.id)
                              .where(AccountActionTask.account_id == account_id)
                              .where(AccountActionTask.status == "done")
                              .where(AccountActionTask.done_at >= since)).all())

    def _action_cap_ok(self, account_id) -> bool:
        """写操作是否还在每日 / 每小时配额内(关注取关是封号重灾区,双重限流)。"""
        dcap = self.cfg.engine.action_daily_cap_per_account
        hcap = self.cfg.engine.action_hourly_cap_per_account
        if dcap > 0 and self._action_count_since(
                account_id, self._today_start(account_id)) >= dcap:
            return False
        if hcap > 0 and self._action_count_since(account_id, self._hour_ago()) >= hcap:
            return False
        return True

    def _action_gate_error(self, account_id, gap: int, action: str = "follow") -> str:
        """Apply the same write gate to queued and API-triggered actions."""
        pause_error = self._write_pause_error(account_id)
        if pause_error:
            return pause_error
        if not self._in_active_window(account_id):
            return "当前处于非活跃时段，写操作已保留在队列"
        if not self._action_cap_ok(account_id):
            return "已达到账号写操作额度"
        if not self._action_gap_ok(account_id, gap):
            return "尚未达到账号写操作最小间隔"
        kind = OperationKind.DM if action == "send_dm" else OperationKind.SOCIAL
        decision = self.risk.preflight(account_id, kind)
        if not decision.allowed:
            return decision.reason
        return ""

    async def _process_action_tasks(self):
        now = datetime.utcnow()
        due = []
        with get_session() as s:
            tasks = s.exec(select(AccountActionTask).where(
                AccountActionTask.status == "pending")).all()
            for t in tasks:
                if t.scheduled_at is None or t.scheduled_at <= now:
                    due.append((t.id, t.account_id, t.min_gap_seconds))
        seen_acct = set()
        for tid, aid, gap in due:
            # 同账号每轮最多执行一条,尊重最小间隔 + 每日/每小时配额(其余下轮再发)
            if aid in seen_acct or not self._action_gap_ok(aid, gap):
                continue
            if not self._action_cap_ok(aid):
                continue
            seen_acct.add(aid)
            try:
                await self.execute_action_task(tid)
            except Exception as e:
                log.warning("写操作任务 %s 执行异常: %s", tid, e)

    async def execute_action_task(self, task_id: int) -> dict:
        if task_id in self._actioning:
            return {"ok": False, "error": "正在执行中"}
        self._actioning.add(task_id)
        try:
            with get_session() as s:
                t = s.get(AccountActionTask, task_id)
                account_id = t.account_id if t else None
                kind = (OperationKind.DM if t and t.action == "send_dm"
                        else OperationKind.SOCIAL)
            async with self._operation_guard(
                    account_id, kind, fallback_key=f"act:{task_id}"):
                return await self._execute_action_task_locked(task_id)
        finally:
            self._actioning.discard(task_id)

    async def _execute_action_task_locked(self, task_id: int) -> dict:
        with get_session() as s:
            t = s.get(AccountActionTask, task_id)
            if not t or t.status != "pending":
                return {"ok": False, "error": "任务不可执行"}
            account_id = t.account_id
            acc = s.get(DouyinAccount, t.account_id) if t.account_id else None
            if not acc:
                t.status = "failed"; t.error = "绑定账号不存在(可能已删除/重登成新号)"
                s.add(t); s.commit()
                return {"ok": False, "error": "account_missing"}
            if self._proxy_bad(acc):
                self._defer_row(t, "账号代理当前不可用", fallback_seconds=300)
                s.add(t); s.commit()
                return {"ok": False, "error": "proxy unavailable"}
            environment_error = self._native_write_environment_error(
                acc, headed=True, browser_mode=True)
            if environment_error:
                self._defer_row(t, environment_error, fallback_seconds=300)
                s.add(t); s.commit()
                return {"ok": False, "error": environment_error}
            if acc.status == "invalid":
                self._defer_row(t, "账号登录态已失效，等待重新登录", fallback_seconds=900)
                s.add(t); s.commit()
                return {"ok": False, "error": "account_invalid"}
            gate_error = self._action_gate_error(
                t.account_id, t.min_gap_seconds, t.action)
            if gate_error:
                kind = (OperationKind.DM if t.action == "send_dm"
                        else OperationKind.SOCIAL)
                decision = self.risk.preflight(t.account_id, kind)
                self._defer_row(t, gate_error, decision.next_allowed_at,
                                signal=decision.signal)
                s.add(t); s.commit()
                return {"ok": False, "error": gate_error}
            action = t.action
            target_uid, target_sec_uid, content = t.target_uid, t.target_sec_uid, t.content
            platform = t.platform
            # 抖音发私信优先走无头 API(imapi/send):取会话的 short_id+ticket
            dm_conv_id, dm_short_id, dm_ticket = t.conv_id, "", ""
            if action == "send_dm" and platform == "douyin" and t.conv_id:
                _conv = s.exec(select(DmConversation).where(
                    DmConversation.account_id == t.account_id,
                    DmConversation.conv_id == t.conv_id)).first()
                if _conv:
                    dm_short_id, dm_ticket = _conv.conv_short_id, _conv.ticket
            # commit 会 expire 本 session 内的实例,先把所需原语取出来再 commit
            native_mode = acc.identity_mode == "native"
            identity = self.browser.identity_for(acc)
            t.status = "doing"; t.method = "browser"; t.error = ""
            self._clear_row_block(t)
            s.add(t); s.commit()

        try:
            if action == "follow":
                ok, err = await do_follow(self.browser, identity, platform,
                                          target_uid, target_sec_uid)
            elif action == "unfollow":
                ok, err = await do_follow(self.browser, identity, platform,
                                          target_uid, target_sec_uid, unfollow=True)
            elif action == "send_dm":
                # 抖音:有会话信息就走无头 API 发送;失败或缺信息再回退 UI 自动化
                if (platform == "douyin" and not native_mode
                        and dm_conv_id and dm_short_id and dm_ticket):
                    ok, err = await send_dm_api(self.browser, identity, dm_conv_id,
                                                dm_short_id, dm_ticket, content)
                    category, _signal = classify_platform_error(err)
                    if not ok and category == RiskCategory.BUSINESS:
                        ok, err = await send_dm(self.browser, identity, platform,
                                                target_uid, target_sec_uid, content)
                else:
                    if platform == "xhs":
                        ok, err = await send_dm(
                            self.browser, identity, platform,
                            target_uid, target_sec_uid, content,
                            on_submit=lambda: self._mark_browser_submit(
                                AccountActionTask, task_id),
                        )
                    else:
                        ok, err = await send_dm(
                            self.browser, identity, platform,
                            target_uid, target_sec_uid, content)
            else:
                ok, err = False, f"未知动作 {action}"
        except Exception as e:
            ok, err = False, f"{e!r}"

        kind = OperationKind.DM if action == "send_dm" else OperationKind.SOCIAL
        uncertain = (
            not ok
            and platform == "xhs"
            and action == "send_dm"
            and str(err or "").startswith("write_uncertain:")
        )
        failure = None if ok or uncertain else self.risk.record_failure(
            account_id, kind, err)
        with get_session() as s:
            t = s.get(AccountActionTask, task_id)
            account_id = t.account_id if t else None
            if t:
                if ok:
                    t.status = "done"
                elif uncertain:
                    t.status = "uncertain"
                    t.scheduled_at = None
                    t.done_at = None
                elif failure and failure.controlled and failure.category in {
                        RiskCategory.RISK, RiskCategory.NETWORK, RiskCategory.AUTH}:
                    self._defer_row(t, err, failure.next_allowed_at,
                                    signal=failure.signal)
                else:
                    t.status = "failed"
                t.error = "" if ok else err
                t.result = "ok" if ok else ""
                t.done_at = datetime.utcnow() if ok else t.done_at
                s.add(t); s.commit()
                if ok and action in ("follow", "unfollow"):
                    # 同一个人可能同时有两行:关注列表(following)+ 粉丝列表(fan)。
                    # 两行都要维护 —— 回关是在粉丝列表点的,只动 following 行的话
                    # 粉丝列表那行 is_following 还是 0,界面继续显示「未关注」。
                    def _edge(direction: str):
                        return s.exec(select(FollowEdge).where(
                            FollowEdge.account_id == t.account_id,
                            FollowEdge.platform == t.platform,
                            FollowEdge.direction == direction,
                            FollowEdge.uid == target_uid)).first()

                    edge, fan = _edge("following"), _edge("fan")
                    if action == "unfollow":
                        # 关注列表按 direction 取行,不看 is_following,
                        # 只翻标记的话取关成功后这人还挂在列表里。
                        if edge:
                            s.delete(edge)
                        if fan:      # ta 还关注我,但已不再互关
                            fan.is_following = False
                            fan.is_mutual = False
                            s.add(fan)
                    else:
                        if edge:
                            edge.is_following = True
                            s.add(edge)
                        else:        # 回关的人本来不在关注列表里,补一行
                            s.add(FollowEdge(
                                platform=t.platform, account_id=t.account_id,
                                direction="following", uid=target_uid,
                                sec_uid=target_sec_uid, nickname=t.target_nick,
                                avatar=fan.avatar if fan else "",
                                signature=fan.signature if fan else "",
                                is_following=True, is_mutual=bool(fan),
                                fetched_at=datetime.utcnow()))
                        if fan:      # 回关 ta = 互关
                            fan.is_following = True
                            fan.is_mutual = True
                            s.add(fan)
                    s.commit()
        if ok and account_id:
            self.risk.record_success(account_id, kind)
        return {"ok": ok, "error": "" if ok else err}

    async def execute_comment_task(self, task_id: int) -> dict:
        if task_id in self._commenting:
            return {"ok": False, "error": "正在执行中"}
        self._commenting.add(task_id)
        try:
            with get_session() as s:
                t = s.get(CommentTask, task_id)
                account_id = t.account_id if t else None
            async with self._operation_guard(
                    account_id, OperationKind.COMMENT,
                    fallback_key=f"cmt:{task_id}"):
                return await self._execute_comment_task_locked(task_id)
        finally:
            self._commenting.discard(task_id)

    async def _execute_comment_task_locked(self, task_id: int) -> dict:
        with get_session() as s:
            t = s.get(CommentTask, task_id)
            if not t:
                return {"ok": False, "error": "任务不存在"}
            if t.status not in ("pending",):
                return {"ok": False, "error": f"任务状态为 {t.status}"}
            account_id = t.account_id
            # 执行前再查一次每日上限(生成到执行之间可能已超额)
            cap = self.cfg.engine.comment_daily_cap_per_account
            if cap > 0 and self._acct_today_count(s, t.account_id) >= cap:
                self._defer_row(
                    t, "已达账号每日评论上限",
                    datetime.utcnow() + timedelta(days=1))
                s.add(t); s.commit()
                return {"ok": False, "error": "已达每日上限"}
            platform = t.platform
            aweme_id, xsec_token = t.aweme_id, t.xsec_token
            target_cid, target_nick = t.target_comment_id, t.target_nick
            target_text = getattr(t, "target_text", "") or ""
            content = t.content
            acc = s.get(DouyinAccount, t.account_id) if t.account_id else None
            # 写操作必须有登录账号:绑定账号不存在(被删/重登成新号)时直接失败,
            # 绝不退回匿名 profile(那会开一个未登录窗口,看着像"发了"其实没登录)
            if not acc:
                t.status = "failed"
                t.error = "绑定的账号不存在(可能已删除或重登成了新账号),请编辑规则重新选择账号"
                s.add(t); s.commit()
                return {"ok": False, "error": "account_missing"}
            if self._proxy_bad(acc):
                self._defer_row(t, "账号代理当前不可用", fallback_seconds=300)
                s.add(t); s.commit()
                return {"ok": False, "error": "proxy unavailable"}
            environment_error = self._native_write_environment_error(
                acc, headed=True, browser_mode=True)
            if environment_error:
                self._defer_row(t, environment_error, fallback_seconds=300)
                s.add(t); s.commit()
                return {"ok": False, "error": environment_error}
            if acc.status == "invalid":
                self._defer_row(t, "账号登录态已失效，等待重新登录", fallback_seconds=900)
                s.add(t); s.commit()
                return {"ok": False, "error": "account_invalid"}
            gate_error = self._comment_gate_error(t.account_id)
            if gate_error:
                decision = self.risk.preflight(t.account_id, OperationKind.COMMENT)
                self._defer_row(t, gate_error, decision.next_allowed_at,
                                signal=decision.signal)
                s.add(t); s.commit()
                return {"ok": False, "error": gate_error}
            state = acc.storage_state or acc.creator_storage_state or ""
            proxy = acc.proxy or ""
            native_mode = acc.identity_mode == "native"
            identity = self.browser.identity_for(acc)
            t.status = "doing"; t.error = ""
            self._clear_row_block(t)
            s.add(t); s.commit()

        ok, result, err, method = False, "", "", ""
        uncertain = False
        xhs_mode = self._xhs_comment_write_mode()
        if native_mode and xhs_mode == "api":
            xhs_mode = "browser"
        manual_only = platform == "xhs" and xhs_mode == "manual"
        try:
            if platform == "xhs":
                method = xhs_mode
                if manual_only:
                    err = "小红书评论默认转人工发布草稿;未调用评论发布接口"
                elif xhs_mode == "api":
                    client = self._xhs_client(identity, state, proxy)
                    if client is None:
                        err = "账号登录态缺少 a1,请重新扫码登录"
                    else:
                        d = await client.post_comment(aweme_id, content, xsec_token=xsec_token,
                                                      target_comment_id=target_cid)
                        cid = (d.get("comment") or {}).get("id") if isinstance(d, dict) else ""
                        ok, result = True, (cid or "ok")
                else:
                    outcome = await comment_xhs_browser(
                        self.browser, identity, aweme_id, xsec_token, content,
                        target_comment_id=target_cid,
                        target_text=target_text,
                        on_submit=lambda: self._mark_browser_submit(
                            CommentTask, task_id))
                    ok = outcome.status == "success"
                    uncertain = outcome.status == "uncertain"
                    result, err, method = (
                        outcome.result, outcome.error, outcome.method)
            elif platform == "kuaishou":
                method = "browser"
                ok, err = await post_ks_comment(
                    self.browser, identity, aweme_id, content,
                    reply_to_text=target_text if target_cid else "",
                    headed=(True if native_mode
                            else self.cfg.engine.comment_browser_headed))
                result = "ok" if ok else ""
            elif platform == "shipinhao":
                # 视频号只能回复自己作品的评论(助手端无法主动去别人作品下评论)
                method = "browser"
                ok, err = await post_channels_comment(
                    self.browser, identity, aweme_id, content,
                    reply_to_text=target_nick if target_cid else "",
                    headed=(True if native_mode
                            else self.cfg.engine.comment_browser_headed))
                result = "ok" if ok else ""
            else:
                method = "browser"
                ok, err = await post_comment_browser(
                    self.browser, identity, aweme_id, content,
                    reply_to_text=target_text if target_cid else "",
                    require_reply=bool(target_cid),
                    headed=(True if native_mode
                            else self.cfg.engine.comment_browser_headed))
                result = "ok" if ok else ""
        except Exception as e:
            ok, err = False, repr(e)

        failure = None if ok or manual_only or uncertain else self.risk.record_failure(
            account_id, OperationKind.COMMENT, err)
        with get_session() as s:
            t = s.get(CommentTask, task_id)
            account_id = t.account_id if t else None
            if t:
                # “立即发”在 manual 模式下也只能回到草稿,不能变成失败后重试循环。
                if ok:
                    t.status = "done"
                elif manual_only:
                    t.status = "draft"
                elif uncertain:
                    t.status = "uncertain"
                    t.scheduled_at = None
                    t.done_at = None
                elif failure and failure.controlled and failure.category in {
                        RiskCategory.RISK, RiskCategory.NETWORK, RiskCategory.AUTH}:
                    self._defer_row(t, err, failure.next_allowed_at,
                                    signal=failure.signal)
                else:
                    t.status = "failed"
                t.result = result
                t.error = "" if ok else err
                t.method = method
                t.done_at = datetime.utcnow() if ok else t.done_at
                s.add(t); s.commit()
        if ok:
            self.risk.record_success(account_id, OperationKind.COMMENT)
            log.info("评论任务 %s 已发送(%s,作品 %s)", task_id, method, aweme_id)
        else:
            log.info("评论任务 %s 失败: %s", task_id, err)
        return {"ok": ok, "error": err}

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

    async def _download(self, record_id: int, aweme, base_dir: str = "", proxy: str = ""):
        async with self._sem:
            with get_session() as s:
                rec = s.get(ContentRecord, record_id)
                if rec:
                    rec.download_status = "downloading"
                    s.add(rec); s.commit()
            ok, path, err = await self.downloader.download_aweme(
                aweme, base_dir, self._dl_proxy(proxy))
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
                   author_name=author_name, media_type=rec.media_type)
        aw.platform = rec.platform or "douyin"
        for m in _loads(rec.media_json) if rec.media_json else []:
            aw.medias.append(MediaItem(url=m["url"], kind=m.get("kind", "video"),
                                       ext=m.get("ext", "mp4"), index=m.get("index", 0)))
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
            kind = (t.target_kind if t else "creator")
            account_id = t.account_id if t else None
            acc_state = ""
            acc_proxy = ""
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
            needs_xhs_refetch = platform == "xhs" and (
                not media_json or not aw.medias)
            rec.download_status = "downloading"
            if not needs_xhs_refetch:
                rec.retry_count = (rec.retry_count or 0) + 1
            s.add(rec); s.commit()

        # 小红书:无媒体快照时,重新拉详情补齐媒体直链
        if platform == "xhs" and (not media_json or not aw.medias):
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
                                xsec_source=("pc_search" if kind == "keyword"
                                             else "pc_feed"),
                                block_media=self.cfg.engine.block_media_resources)
                        detail = await client.note_detail(
                            note_id, xsec_token=note_tok,
                            xsec_source=("pc_search" if kind == "keyword"
                                         else "pc_feed"))
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

        ok, path, err = await self.downloader.download_aweme(
            aw, base_dir, self._dl_proxy(acc_proxy))
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
                if record.platform == "xhs" and not record.media_json:
                    continue
                ids.append(rid)
        for rid in ids:
            await self.retry_download(rid)
