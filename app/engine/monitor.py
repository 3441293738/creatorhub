"""多平台监控、发布与社交互动调度引擎核心编排器（生命周期、风控闸门与主调度循环）。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional
import asyncio
import json
import random
import time

from sqlalchemy import or_
from sqlmodel import select

from app.account_lifecycle import AccountUnavailableError
from app.browser import BrowserManager
from app.config import Config
from app.db import get_session
from app.engine.collection import KeywordCollector
from app.engine.dm_automation import XhsDmAutomation
from app.engine.downloader import Downloader
from app.engine.monitor_helpers import _AccountCheckBusy, _WRITE_SUBMIT_PREFIX, log
from app.models import AccountActionTask, CommentTask, CommentWatch, DanmakuWatch, DouyinAccount, KeywordCollectionJob, MonitorTarget, PublishTask
from app.operations import record_account_operation
from app.platforms.xhs import XhsApiError
from app.risk import OperationKind, RiskCategory, RiskController, classify_platform_error
from app.engine.action_jobs import ActionJobMixin
from app.engine.comment_rules_runner import CommentRuleRunnerMixin
from app.engine.comment_task_runner import CommentTaskRunnerMixin
from app.engine.comment_watches import CommentWatchMixin
from app.engine.comment_watches_ext import CommentWatchPlatformsMixin
from app.engine.danmaku_watches import DanmakuWatchMixin
from app.engine.download_jobs import MediaDownloadMixin
from app.engine.health_probes import HealthProbeMixin
from app.engine.publish_jobs import PublishJobMixin
from app.engine.target_scans import TargetScanMixin
from app.engine.target_scans_notes import NoteTargetScanMixin
from app.engine.target_scans_social import SocialTargetScanMixin
from app.engine.work_sync import WorkSyncMixin


class MonitorEngine(
    HealthProbeMixin,
    TargetScanMixin,
    SocialTargetScanMixin,
    NoteTargetScanMixin,
    DanmakuWatchMixin,
    CommentWatchMixin,
    WorkSyncMixin,
    CommentWatchPlatformsMixin,
    PublishJobMixin,
    CommentRuleRunnerMixin,
    ActionJobMixin,
    CommentTaskRunnerMixin,
    MediaDownloadMixin,
):
    def __init__(self, cfg: Config, browser: BrowserManager):
        self.cfg = cfg
        self.browser = browser
        self.downloader = Downloader(
            cfg.engine.media_dir, cfg.engine.user_agent,
            cfg.engine.download_timeout_seconds,
        )
        self.keyword_collector = KeywordCollector(cfg, browser, self.downloader)
        self.dm_automation = XhsDmAutomation(cfg, browser)
        self.dm_automation.set_wake_callback(self._on_xhs_dm_wake)
        self._dm_poll_locks: dict[int, asyncio.Lock] = {}
        self._dm_event_sink = None
        self._sem = asyncio.Semaphore(cfg.engine.worker_pool_size)
        # 限制并发抓取的目标数(多个浏览器上下文并行,但不无限开)
        self._scan_sem = asyncio.Semaphore(max(1, cfg.engine.scan_concurrency))
        # 同一时刻最多并发活跃的账号数(错峰,降低"多号同时活跃"特征)
        self._active_sem = asyncio.Semaphore(max(1, cfg.engine.active_accounts))
        self._inflight: set = set()           # 正在抓取的目标,避免同目标并发
        self._publish_sem = asyncio.Semaphore(1)   # 发布串行(有头浏览器,一次一个)
        self._publishing: set[int] = set()
        self._publish_stages: dict[int, str] = {}
        self._commenting: set[int] = set()         # 正在执行的评论任务 id
        self._actioning: set[int] = set()           # 正在执行的写操作任务 id
        self._collection_tasks: dict[int, asyncio.Task] = {}  # 一次性关键词采集
        self._last_acct_check = 0.0   # 启动后首轮校验，不沿用历史“正常”状态
        self._pending_account_checks: set[int] = set()
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
        """Never replay a write whose outcome was lost during a restart."""
        # Keep delay_seconds for older callers; interrupted writes no longer
        # have an automatic retry deadline.
        recovered = 0
        with get_session() as s:
            for model, transient in (
                    (CommentTask, "doing"),
                    (AccountActionTask, "doing"),
                    (PublishTask, "publishing")):
                rows = s.exec(select(model).where(model.status == transient)).all()
                for row in rows:
                    # Not every platform adapter can durably signal the exact
                    # submit boundary. A missing marker is not proof of no
                    # submission, including for legacy API-mode writes.
                    row.status = "uncertain"
                    row.scheduled_at = None
                    if hasattr(row, "done_at"):
                        row.done_at = None
                    self._clear_row_block(row)
                    row.error = (
                        "服务重启/中断时任务正在执行，结果需到平台核对；"
                        "任务不会自动重试")
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
    def _mark_write_submit(model, task_id: int, *, channel: str = "browser") -> None:
        """Durably mark the no-retry boundary before one click or API POST."""
        if channel not in {"browser", "api"}:
            raise ValueError("未知的提交通道")
        with get_session() as s:
            row = s.get(model, task_id)
            if row is None:
                raise RuntimeError("待提交任务已不存在")
            row.error = _WRITE_SUBMIT_PREFIX + channel
            s.add(row)
            s.commit()

    async def stop(self):
        self._running = False
        if self._task:
            task = self._task
            task.cancel()
            if task is not asyncio.current_task():
                await asyncio.gather(task, return_exceptions=True)
            self._task = None
        tasks = list(self._collection_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._collection_tasks.clear()
        await self.dm_automation.stop()

    def set_dm_event_sink(self, sink) -> None:
        self._dm_event_sink = sink

    def _publish_dm_events(self, result: dict) -> None:
        if not callable(self._dm_event_sink):
            return
        for event in result.get("events") or []:
            try:
                self._dm_event_sink(int(event.get("account_id") or 0), event)
            except Exception:
                log.exception("publish XHS DM event failed")

    # ── 账号隔离调度 ──
    @staticmethod
    def _load_account(account_id):
        if not account_id:
            return None
        with get_session() as s:
            return s.get(DouyinAccount, account_id)

    @asynccontextmanager
    async def _operation_guard(self, account_id, kind: OperationKind,
                               fallback_key: str = "", operation_target=None,
                               skip_busy: bool = False):
        """Serialize by global limit, network exit, then account profile."""
        account = operation_target or self._load_account(account_id)
        key = f"acc:{account_id}" if account_id else (fallback_key or "anon")
        lock = self.browser.lock_for(key)
        async with self._active_sem:
            async with self.risk.network_guard(account):
                if skip_busy and lock.locked():
                    raise _AccountCheckBusy()
                async with lock:
                    if account_id and self._load_account(account_id) is None:
                        raise AccountUnavailableError("绑定账号已删除，操作已停止")
                    yield account

    def _fail_missing_account_task(self, model, task_id: int) -> dict:
        with get_session() as session:
            row = session.get(model, task_id)
            if row and row.status in {"pending", "failed"}:
                row.status = "failed"
                row.scheduled_at = None
                self._clear_row_block(row)
                row.error = "绑定账号已删除，请重新选择账号"
                session.add(row)
                session.commit()
        return {"ok": False, "error": "account_missing"}

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
                                allow_invalid_probe: bool = False,
                                interactive_read: bool = False):
        """Budget a direct read returning ``(payload, error)``."""
        decision = self.risk.preflight(
            account_id, kind,
            allow_invalid_probe=allow_invalid_probe,
            interactive_read=interactive_read)
        if not decision.allowed:
            return empty_result, f"risk_deferred:{decision.reason}"
        try:
            async with self._operation_guard(
                    account_id, kind, fallback_key=fallback_key):
                decision = self.risk.preflight(
                    account_id, kind,
                    allow_invalid_probe=allow_invalid_probe,
                    interactive_read=interactive_read)
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

    async def guarded_interactive_read_pair(
            self, account_id, kind: OperationKind, fallback_key: str,
            operation, *, empty_result):
        """Keep adjacent UI reads responsive, but honor every hard risk hold."""
        return await self.guarded_read_pair(
            account_id, kind, fallback_key, operation,
            empty_result=empty_result, interactive_read=True)

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
            and acc.proxy_status in {"bad", "auth_error", "blocked"}
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
        # User scheduling and policy deferral are independent deadlines.
        # Never replace an appointment with a retry/cooldown timestamp.
        row.status = "pending"
        row.error = str(reason or "平台操作已延后").strip()[:500]
        if hasattr(row, "blocked_reason"):
            row.blocked_reason = row.error
            row.blocked_signal = signal or cls._blocked_signal(row.error)
            row.blocked_operation = cls._blocked_operation(row)
            row.blocked_at = now
            row.next_allowed_at = max(row.next_allowed_at or proposed, proposed)

    @staticmethod
    def _task_due(row, now: datetime) -> bool:
        return all(value is None or value <= now for value in (
            row.scheduled_at, row.next_allowed_at))

    @staticmethod
    def _task_deferral(row) -> dict | None:
        if row.next_allowed_at and row.next_allowed_at > datetime.utcnow():
            return {"ok": False, "deferred": True,
                    "error": row.blocked_reason or row.error or "任务仍在等待执行间隔",
                    "next_allowed_at": row.next_allowed_at.isoformat() + "Z"}
        return None

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

    def _rednote_publish_mode(self) -> str:
        """Use creator RPA unless experimental pure-API Rednote publishing is explicit."""
        mode = str(getattr(self.cfg.engine, "rednote_publish_mode", "browser")
                   or "browser").strip().lower()
        return mode if mode in {"browser", "api"} else "browser"

    def _douyin_write_mode(self) -> str:
        """Return the selected Douyin write transport.

        ``hybrid`` is API-first only for a positively rejected response.  A
        timeout or malformed/empty successful response is non-idempotent and
        therefore becomes an uncertain task instead of being submitted again
        through the page.
        """
        mode = str(getattr(self.cfg.engine, "douyin_write_mode", "browser")
                   or "browser").strip().lower()
        return mode if mode in {"browser", "api", "hybrid"} else "browser"

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

    def _monitor_poll_seconds(self) -> int:
        """有启用的秒级监控时细化调度，不改变任务周期和账号限速。"""
        with get_session() as session:
            for model in (MonitorTarget, CommentWatch, DanmakuWatch):
                query = select(model.id).where(
                    model.enabled == True,  # noqa: E712
                    model.interval_seconds > 0,
                    model.interval_seconds % 60 != 0).limit(1)
                if session.exec(query).first() is not None:
                    return 1
            if self.cfg.engine.scan_interval_seconds > 0 and self.cfg.engine.scan_interval_seconds % 60:
                inherited = select(DanmakuWatch.id).where(
                    DanmakuWatch.enabled == True,  # noqa: E712
                    DanmakuWatch.interval_seconds == 0).limit(1)
                if session.exec(inherited).first() is not None:
                    return 1
        return 15

    async def _loop(self):
        next_maintenance_at = 0.0
        while self._running:
            poll_seconds = 15
            try:
                maintenance_due = time.monotonic() >= next_maintenance_at
                if maintenance_due:
                    # 秒级读取不连带加速重试、保活或写操作队列；异常也保留等待。
                    next_maintenance_at = time.monotonic() + 15
                    self._prune_risk_events_if_due(datetime.utcnow())
                    await self._collect_idle_browser_sessions(time.time())
                await self._scan_once()
                await self._scan_comment_watches()
                await self._scan_danmaku_watches()
                if maintenance_due:
                    await self._retry_failed()
                    await self._process_risk_recovery()
                    await self._check_accounts()
                    await self._check_work_health()
                    await self._process_xhs_dm_automation()
                    await self._process_publish()
                    await self._process_comment_rules()
                    await self._process_comment_tasks()
                    await self._process_action_tasks()
                    await self._process_collection_jobs()
                    next_maintenance_at = time.monotonic() + 15
                poll_seconds = self._monitor_poll_seconds()
            except Exception as e:
                log.exception("scan loop error: %s", e)
            await asyncio.sleep(poll_seconds)

    async def poll_xhs_dm_now(self, account_id: int, *, trigger: str = "manual") -> dict:
        with get_session() as session:
            account = session.get(DouyinAccount, account_id)
            if not account or account.platform != "xhs":
                return {"ok": False, "error": "小红书账号不存在"}

        lock = self._dm_poll_locks.setdefault(account_id, asyncio.Lock())
        async with lock:
            async def operation():
                result = await self.dm_automation.poll(account, trigger=trigger)
                return result, str(result.get("error") or "")

            if trigger in {"push", "reconnect"}:
                # Passive pushes justify one compact read, not bypassing a
                # challenge, network backoff, cooldown or session rest.
                result, error = await self.guarded_interactive_read_pair(
                    account_id, OperationKind.READ_LIGHT,
                    f"xhs-dm-{trigger}:{account_id}", operation, empty_result={})
            else:
                result, error = await self.guarded_read_pair(
                    account_id, OperationKind.READ_HEAVY, f"xhs-dm:{account_id}",
                    operation, empty_result={})
        if trigger != "push":
            self.dm_automation.postpone(account_id)
        if error.startswith("risk_deferred:"):
            return {"ok": True, "skipped": True,
                    "reason": error.split(":", 1)[-1]}
        if error and not result:
            return {"ok": False, "error": error}
        self._publish_dm_events(result)
        return result

    async def _on_xhs_dm_wake(self, account_id: int, reason: str) -> None:
        result = await self.poll_xhs_dm_now(account_id, trigger=reason)
        if not result.get("ok"):
            log.warning("XHS DM %s wake failed for account %s: %s",
                        reason, account_id, result.get("error"))

    async def _process_xhs_dm_automation(self) -> None:
        enabled = bool(self.cfg.engine.xhs_dm_monitor_enabled
                       or self.cfg.engine.xhs_dm_auto_reply_enabled)
        if not enabled:
            return
        with get_session() as session:
            accounts = session.exec(select(DouyinAccount).where(
                DouyinAccount.platform == "xhs",
                DouyinAccount.status == "active",
            ).order_by(DouyinAccount.last_active_at.asc())).all()
        for account in accounts:
            account_id = int(account.id or 0)
            if not account_id:
                continue
            # A creator-center-only login has publishing cookies but no
            # consumer-web session.  Opening /chat for such a row can only show
            # the login dialog, and the 15-second scheduler used to keep trying
            # forever.  Leave it available for publishing, but do not bootstrap
            # a DM browser for it until the normal XHS login has been completed.
            if not (str(account.storage_state or "").strip()
                    or str(account.cookie or "").strip()):
                continue
            # poll() attaches the observer inside the unified account/risk
            # guard. Never open a page here before its periodic deadline.
            if not self.dm_automation.due(account_id):
                continue
            await self.poll_xhs_dm_now(account_id, trigger="scheduled")
            # Stagger accounts across engine ticks instead of synchronized polling.
            break

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

    async def cancel_collection_job(self, job_id: int, *, timeout: float = 2.0) -> bool:
        """立即中断执行中的采集协程（或清理孤立 running 记录），落库为已取消并释放队列槽位。"""
        task = self._collection_tasks.get(job_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait({task}, timeout=max(0.1, float(timeout)))
            except Exception:
                pass
        self._collection_tasks.pop(job_id, None)
        canceled_now = False
        with get_session() as s:
            job = s.get(KeywordCollectionJob, job_id)
            if job and job.status in {"pending", "running"}:
                job.cancel_requested = True
                job.status = "canceled"
                job.current_step = "已取消"
                job.finished_at = job.finished_at or datetime.utcnow()
                s.add(job)
                s.commit()
                self._log_collection_operation(job, "canceled", "canceled", "用户取消任务")
                canceled_now = True
            elif job and job.status == "canceled":
                canceled_now = True
        if self._running and not self._collection_tasks:
            try:
                asyncio.create_task(self._process_collection_jobs())
            except RuntimeError:
                pass
        return canceled_now

    async def _process_collection_jobs(self) -> None:
        if self._collection_tasks:
            return
        with get_session() as s:
            orphans = s.exec(
                select(KeywordCollectionJob).where(KeywordCollectionJob.status == "running")
            ).all()
            if orphans:
                now_utc = datetime.utcnow()
                for orphan in orphans:
                    if orphan.cancel_requested:
                        orphan.status = "canceled"
                        orphan.current_step = "已取消"
                        orphan.finished_at = orphan.finished_at or now_utc
                    else:
                        orphan.status = "pending"
                        orphan.current_step = "等待继续"
                        orphan.finished_at = None
                    s.add(orphan)
                s.commit()
            jobs = s.exec(
                select(KeywordCollectionJob)
                .where(KeywordCollectionJob.status == "pending")
                .where(KeywordCollectionJob.cancel_requested == False)  # noqa:E712
                .where(or_(KeywordCollectionJob.next_allowed_at == None,  # noqa:E711
                           KeywordCollectionJob.next_allowed_at <= datetime.utcnow()))
                .order_by(KeywordCollectionJob.created_at)
            ).all()
        for job in jobs:
            decision = self.risk.preflight(job.account_id, OperationKind.READ_HEAVY)
            if not decision.allowed and decision.signal != "account_missing":
                continue
            # A held account must not block later jobs on healthy accounts.
            if self.enqueue_collection_job(job.id):
                break

    async def run_collection_job(self, job_id: int) -> dict:
        """执行一个持久化关键词任务，并维护可恢复的状态机。"""
        started_clock = time.monotonic()
        with get_session() as s:
            job = s.get(KeywordCollectionJob, job_id)
            if not job:
                return {"ok": False, "error": "任务不存在"}
            if job.cancel_requested or job.status == "canceled":
                job.status = "canceled"
                job.current_step = "已取消"
                job.finished_at = datetime.utcnow()
                s.add(job); s.commit()
                self._log_collection_operation(job, "canceled", "canceled")
                return {"ok": True, "canceled": True}
            if job.status != "pending":
                return {"ok": False, "error": f"任务状态为 {job.status}，请先通过重试入口重新排队"}
            if job.next_allowed_at and job.next_allowed_at > datetime.utcnow():
                return {"ok": True, "deferred": True,
                        "reason": job.blocked_reason or "等待执行间隔",
                        "next_allowed_at": job.next_allowed_at.isoformat() + "Z"}
            requires_account = not (
                job.platform == "shipinhao"
                and job.source_type in {"keyword", "creator"}
            )
            account = s.get(DouyinAccount, job.account_id) if job.account_id else None
            if requires_account and (
                not account or account.platform != job.platform
                or account.status != "active" or not account.storage_state
            ):
                job.status = "failed"
                job.current_step = "执行失败"
                job.error = "所选账号不存在、登录态失效或平台不匹配"
                job.error_count += 1
                job.finished_at = datetime.utcnow()
                s.add(job); s.commit()
                self._log_collection_operation(job, "failed", "failed", job.error)
                return {"ok": False, "error": job.error}
            account_id = (
                account.id
                if (account is not None and account.platform == job.platform
                    and (requires_account or (account.status == "active" and account.storage_state)))
                else 0
            )

        decision = self.risk.preflight(account_id, OperationKind.READ_HEAVY)
        if not decision.allowed:
            with get_session() as s:
                job = s.get(KeywordCollectionJob, job_id)
                if job and job.status == "pending":
                    job.current_step = (
                        "等待人工验证（请前往风控中心解除）"
                        if decision.signal == "manual_review_required"
                        else "等待账号读取冷却"
                    )
                    job.blocked_reason = decision.reason
                    job.blocked_signal = decision.signal or self._blocked_signal(
                        decision.reason)
                    job.blocked_operation = OperationKind.READ_HEAVY.value
                    job.blocked_at = datetime.utcnow()
                    job.next_allowed_at = decision.next_allowed_at
                    s.add(job); s.commit()
                    self._log_collection_operation(
                        job, "deferred", "blocked", decision.reason,
                        risk_signal=decision.signal)
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
                            job.current_step = (
                                "等待人工验证（请前往风控中心解除）"
                                if decision.signal == "manual_review_required"
                                else "等待账号读取冷却"
                            )
                            job.blocked_reason = decision.reason
                            job.blocked_signal = decision.signal or self._blocked_signal(
                                decision.reason)
                            job.blocked_operation = OperationKind.READ_HEAVY.value
                            job.blocked_at = datetime.utcnow()
                            job.next_allowed_at = decision.next_allowed_at
                            s.add(job); s.commit()
                            self._log_collection_operation(
                                job, "deferred", "blocked", decision.reason,
                                risk_signal=decision.signal)
                    return {"ok": True, "deferred": True,
                            "reason": decision.reason, "signal": decision.signal,
                            "next_allowed_at": (decision.next_allowed_at.isoformat()
                                                if decision.next_allowed_at else None)}
                with get_session() as s:
                    job = s.get(KeywordCollectionJob, job_id)
                    if not job:
                        return {"ok": False, "error": "任务不存在"}
                    if job.status != "pending" or job.cancel_requested:
                        return {"ok": False, "error": "任务状态已变化，停止执行"}
                    if job.next_allowed_at and job.next_allowed_at > datetime.utcnow():
                        return {"ok": True, "deferred": True,
                                "reason": job.blocked_reason or "等待执行间隔"}
                    job.status = "running"
                    job.current_step = "准备搜索"
                    self._clear_row_block(job)
                    job.started_at = job.started_at or datetime.utcnow()
                    job.finished_at = None
                    s.add(job); s.commit()
                    account = s.get(DouyinAccount, account_id) if account_id else None
                    self._log_collection_operation(job, "started", "running")
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
                log_job = job
            category, signal = classify_platform_error(job_error)
            if job_error and category in {
                    RiskCategory.RISK, RiskCategory.AUTH, RiskCategory.NETWORK}:
                # 即使已经采到部分结果，验证码/登录态/网络异常也属于本次读取失败；
                # 不能再记 success，否则会立即放行下一次重读并覆盖风险信号。
                failure = self.risk.record_failure(
                    account_id, OperationKind.READ_HEAVY, job_error)
                if category == RiskCategory.RISK:
                    manual_hold = bool(
                        self.risk.policy.enabled
                        and self.risk._needs_manual_review(job_error, signal)
                    )
                    with get_session() as s:
                        deferred_job = s.get(KeywordCollectionJob, job_id)
                        if deferred_job and deferred_job.status not in {"canceled", "done"}:
                            deferred_job.status = "pending"
                            deferred_job.current_step = (
                                "等待人工验证（请前往风控中心解除）"
                                if manual_hold else "API 风控冷却，等待重试"
                            )
                            deferred_job.blocked_reason = job_error
                            deferred_job.blocked_signal = signal
                            deferred_job.blocked_operation = OperationKind.READ_HEAVY.value
                            deferred_job.blocked_at = datetime.utcnow()
                            deferred_job.next_allowed_at = failure.next_allowed_at
                            deferred_job.finished_at = None
                            s.add(deferred_job); s.commit()
                            self._log_collection_operation(
                                deferred_job, "deferred", "pending", job_error,
                                risk_signal=signal,
                                duration_ms=int((time.monotonic() - started_clock) * 1000))
                    return {
                        "ok": True, "deferred": True, "status": "pending",
                        "reason": job_error, "signal": signal,
                        "next_allowed_at": (failure.next_allowed_at.isoformat()
                                            if failure.next_allowed_at else None),
                        **result,
                    }
            elif status in {"done", "partial"}:
                self.risk.record_success(account_id, OperationKind.READ_HEAVY)
                self._stamp_active(account_id)
            else:
                self.risk.record_failure(account_id, OperationKind.READ_HEAVY,
                                         "关键词采集未取得结果")
            self._log_collection_operation(
                log_job, "completed" if status in {"done", "partial"} else "failed",
                status, job_error,
                duration_ms=int((time.monotonic() - started_clock) * 1000))
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
                    self._log_collection_operation(
                        job, "canceled" if job.status == "canceled" else "deferred",
                        job.status, job.current_step,
                        duration_ms=int((time.monotonic() - started_clock) * 1000))
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
                    self._log_collection_operation(
                        job, "failed", job.status, str(exc),
                        duration_ms=int((time.monotonic() - started_clock) * 1000))
            return {"ok": False, "error": str(exc)}

    @staticmethod
    def _log_collection_operation(job: KeywordCollectionJob, action: str,
                                  status: str, detail: str = "", *,
                                  risk_signal: str = "",
                                  duration_ms: int = 0) -> None:
        try:
            if job.source_type == "keyword":
                try:
                    target = "、".join(json.loads(job.keywords or "[]"))
                except (TypeError, ValueError):
                    target = job.keywords
            else:
                target = job.source_name or job.source_type
            record_account_operation(
                platform=job.platform, account_id=job.account_id,
                task_type="collection", task_id=job.id, action=action,
                status=status, target=target, channel="worker",
                risk_signal=risk_signal, detail=detail,
                duration_ms=duration_ms)
        except Exception:
            log.exception("record collection operation failed for job %s", job.id)
