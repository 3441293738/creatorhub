"""Unified platform-risk decisions shared by all account operations."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from enum import Enum
from functools import lru_cache
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func
from sqlmodel import select

from .browser.manager import normalize_proxy
from .db import get_session
from .models import (
    AccountActionTask,
    AccountRiskState,
    CommentTask,
    DouyinAccount,
    ProxyPool,
    PublishTask,
    RiskEvent,
)


log = logging.getLogger("creatorhub.risk")


@lru_cache(maxsize=128)
def _resolve_timezone(timezone_id: str) -> tzinfo:
    try:
        return ZoneInfo(timezone_id)
    except ZoneInfoNotFoundError:
        log.warning("Invalid account timezone %s; falling back to Asia/Shanghai", timezone_id)
        try:
            return ZoneInfo("Asia/Shanghai")
        except ZoneInfoNotFoundError:
            return timezone(timedelta(hours=8), name="Asia/Shanghai")


class OperationKind(str, Enum):
    READ_LIGHT = "read_light"
    READ_HEAVY = "read_heavy"
    DOWNLOAD = "download"
    PUBLISH = "publish"
    COMMENT = "comment"
    SOCIAL = "social"
    DM = "dm"
    LOGIN = "login"


class RiskCategory(str, Enum):
    SUCCESS = "success"
    RISK = "risk"
    AUTH = "auth"
    NETWORK = "network"
    BUSINESS = "business"


@dataclass(frozen=True)
class RiskDecision:
    allowed: bool
    reason: str = ""
    next_allowed_at: Optional[datetime] = None
    signal: str = ""


@dataclass(frozen=True)
class FailureDecision:
    category: RiskCategory
    signal: str
    next_allowed_at: Optional[datetime] = None
    controlled: bool = True


def _utcnow() -> datetime:
    return datetime.utcnow()


def _kind_value(kind: OperationKind | str) -> str:
    return kind.value if isinstance(kind, OperationKind) else str(kind)


def network_key(proxy: str) -> str:
    """Return a non-secret stable key for a network exit."""
    raw = normalize_proxy(str(proxy or "").strip())
    if not raw:
        return "direct"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"proxy:{digest}"


def classify_platform_error(
    error: object,
    status_code: int | None = None,
    payload: object = None,
) -> tuple[RiskCategory, str]:
    """Classify a platform result without retaining its response body."""
    explicit_category = getattr(error, "category", None)
    explicit_signal = str(getattr(error, "signal", "") or "")
    explicit_status = getattr(error, "status_code", None)
    if status_code is None and isinstance(explicit_status, int):
        status_code = explicit_status
    if explicit_category:
        if isinstance(explicit_category, RiskCategory):
            category = explicit_category
        else:
            try:
                category = RiskCategory(str(explicit_category))
            except ValueError:
                category = RiskCategory.BUSINESS
        return category, explicit_signal or category.value

    if status_code in (403, 429, 461, 471):
        return RiskCategory.RISK, f"http_{status_code}"
    if status_code in (401,):
        return RiskCategory.AUTH, f"http_{status_code}"
    if status_code == 407:
        return RiskCategory.NETWORK, "proxy_auth"
    if status_code is not None and 500 <= status_code:
        return RiskCategory.NETWORK, f"http_{status_code}"
    if status_code is not None and 200 <= status_code < 300 \
            and payload in ("", b"", {}, []):
        return RiskCategory.RISK, "ambiguous_empty_response"
    if isinstance(error, (TimeoutError, ConnectionError)):
        return RiskCategory.NETWORK, "network_failure"

    text = str(error or "").strip().lower()
    # Browser interception failures used to include a list of possible causes,
    # for example "可能未登录/被风控/无结果".  Those messages are diagnostic
    # guesses, not evidence of an expired session.  Classifying them by a
    # substring such as "未登录" incorrectly invalidates a healthy account.
    if "未拦截到" in text and any(marker in text for marker in (
            "可能未登录", "或未登录", "未登录/")):
        return RiskCategory.BUSINESS, "ambiguous_browser_result"
    auth_markers = (
        "登录态已失效", "登录已失效", "未登录", "logged_out", "login expired",
        "session expired", "cookie expired",
    )
    if any(marker in text for marker in auth_markers):
        return RiskCategory.AUTH, "auth_expired"

    network_markers = (
        "proxyerror", "proxy error", "proxy_auth", "connection timeout",
        "connecttimeout", "readtimeout", "timed out", "dns", "tls",
        "connection reset", "connection refused", "networkerror",
    )
    if any(marker in text for marker in network_markers):
        return RiskCategory.NETWORK, "network_failure"

    risk_markers = (
        "风控", "频控", "访问频繁", "操作频繁", "环境异常", "验证码", "验证",
        "限流", "安全验证", "设备异常", "账号异常", "滑块", "人机验证",
        "risk", "captcha", "security verification", "verifycenter",
        "website-login/captcha", "rate limit", "too frequent", "http 429",
        "http 403", "status_code=8",
    )
    if any(marker in text for marker in risk_markers):
        return RiskCategory.RISK, "platform_risk"
    return RiskCategory.BUSINESS, "business_error"


class RiskController:
    """Persistent cross-feature limits plus in-process network serialization."""

    _WRITE_KINDS = {
        OperationKind.PUBLISH,
        OperationKind.COMMENT,
        OperationKind.SOCIAL,
        OperationKind.DM,
    }
    _CONSERVATIVE_LIMITS = {
        OperationKind.COMMENT: (600, 3, 10),
        OperationKind.SOCIAL: (900, 2, 8),
        OperationKind.DM: (900, 2, 8),
        OperationKind.PUBLISH: (7200, 1, 3),
    }
    _CONSERVATIVE_SHARED_WRITE_GAP = 300
    _CONSERVATIVE_COMBINED_ACTION_CAPS = (3, 10)

    def __init__(self, cfg):
        self.cfg = cfg
        self.policy = cfg.risk_control
        self._decision_lock = threading.RLock()
        self._network_locks: dict[str, asyncio.Semaphore] = {}
        self._network_lock_guard = threading.Lock()

    def update_policy(self, policy) -> None:
        """Apply a saved policy to subsequent decisions without restarting."""
        self.policy = policy
        # Semaphores encode the configured capacity at construction time.
        # Existing holders keep their own reference and release normally;
        # newly scheduled work receives semaphores with the new capacity.
        with self._network_lock_guard:
            self._network_locks.clear()

    @staticmethod
    def _account_id(account_or_id: DouyinAccount | int | None) -> int | None:
        if isinstance(account_or_id, int):
            return account_or_id
        return getattr(account_or_id, "id", None)

    def _load_account(self, session, account_or_id) -> DouyinAccount | None:
        if isinstance(account_or_id, DouyinAccount):
            account_id = account_or_id.id
            return session.get(DouyinAccount, account_id) if account_id else account_or_id
        account_id = self._account_id(account_or_id)
        return session.get(DouyinAccount, account_id) if account_id else None

    @staticmethod
    def _state(session, account_id: int) -> AccountRiskState:
        state = session.get(AccountRiskState, account_id)
        if state is None:
            state = AccountRiskState(account_id=account_id)
            session.add(state)
            session.flush()
        return state

    def _apply_network_group_circuit_breaker(
            self, session, account: DouyinAccount,
            *, now: datetime) -> datetime | None:
        """Pause native accounts sharing an exit after distinct accounts hit risk."""
        if getattr(account, "identity_mode", "legacy") != "native":
            return None
        threshold = max(0, int(getattr(
            self.policy, "network_group_risk_accounts", 0)))
        if threshold < 2:
            return None
        window = max(1, int(getattr(
            self.policy, "network_group_risk_window_seconds", 900)))
        key = network_key(account.proxy)
        recent_ids = set(session.exec(select(RiskEvent.account_id).where(
            RiskEvent.network_key == key,
            RiskEvent.outcome == RiskCategory.RISK.value,
            RiskEvent.occurred_at >= now - timedelta(seconds=window),
        )).all())
        native_ids = {
            account_id for account_id in recent_ids
            if (lambda row: row is not None and row.identity_mode == "native")(
                session.get(DouyinAccount, account_id))
        }
        if len(native_ids) < threshold:
            return None

        cooldown = max(1, int(getattr(
            self.policy, "network_group_cooldown_seconds", 7200)))
        until = now + timedelta(seconds=cooldown)
        reason = (
            f"共享网络出口在 {window} 秒内有 {len(native_ids)} 个账号"
            "命中平台风险，已触发出口组熔断")
        for target in session.exec(select(DouyinAccount)).all():
            if target.identity_mode != "native" \
                    or network_key(target.proxy) != key:
                continue
            target_state = self._state(session, target.id)
            target_state.risk_level = max(1, target_state.risk_level)
            if target_state.cooldown_until is None \
                    or target_state.cooldown_until < until:
                target_state.cooldown_until = until
            if target_state.probe_only_until is None \
                    or target_state.probe_only_until < until:
                target_state.probe_only_until = until
            target_state.last_risk_at = now
            target_state.last_risk_reason = reason[:240]
            target_state.recovery_successes = 0
            target_state.last_recovery_at = None
            target_state.updated_at = now
            target.write_paused_until = until
            target.write_pause_reason = reason[:240]
            session.add(target_state)
            session.add(target)
        return until

    @staticmethod
    def _timezone(account: DouyinAccount) -> tzinfo:
        return _resolve_timezone(account.timezone_id or "Asia/Shanghai")

    def _local_day_start_utc(self, account: DouyinAccount, now: datetime) -> datetime:
        aware = now.replace(tzinfo=timezone.utc).astimezone(self._timezone(account))
        local_start = aware.replace(hour=0, minute=0, second=0, microsecond=0)
        return local_start.astimezone(timezone.utc).replace(tzinfo=None)

    def _in_active_window(self, account: DouyinAccount, now: datetime) -> bool:
        engine = self.cfg.engine
        if not engine.quiet_hours_enabled:
            return True
        start, end = engine.active_hours_start, engine.active_hours_end
        if end <= start:
            return True
        hour = now.replace(tzinfo=timezone.utc).astimezone(self._timezone(account)).hour
        if end <= 24:
            return start <= hour < end
        return hour >= start or hour < (end - 24)

    def _limits(self, kind: OperationKind) -> tuple[int, int, int]:
        p = self.policy
        if kind == OperationKind.COMMENT:
            configured = (p.comment_min_gap_seconds, p.comment_hourly_cap,
                          p.comment_daily_cap)
            return self._effective_limits(kind, configured)
        if kind == OperationKind.SOCIAL:
            configured = (p.social_min_gap_seconds, p.social_hourly_cap,
                          p.social_daily_cap)
            return self._effective_limits(kind, configured)
        if kind == OperationKind.DM:
            configured = (p.dm_min_gap_seconds, p.dm_hourly_cap, p.dm_daily_cap)
            return self._effective_limits(kind, configured)
        if kind == OperationKind.PUBLISH:
            configured = (p.publish_min_gap_seconds, p.publish_hourly_cap,
                          p.publish_daily_cap)
            return self._effective_limits(kind, configured)
        if kind == OperationKind.READ_HEAVY:
            return p.read_heavy_gap_seconds, 0, 0
        if kind == OperationKind.READ_LIGHT:
            return p.read_light_gap_seconds, 0, 0
        return 0, 0, 0

    def _effective_limits(
        self, kind: OperationKind, configured: tuple[int, int, int],
    ) -> tuple[int, int, int]:
        if self._is_custom_mode():
            return configured
        hard_gap, hard_hourly, hard_daily = self._CONSERVATIVE_LIMITS[kind]
        gap, hourly, daily = configured
        return (
            max(hard_gap, max(0, gap)),
            min(hard_hourly, hourly) if hourly > 0 else hard_hourly,
            min(hard_daily, daily) if daily > 0 else hard_daily,
        )

    def _shared_write_gap(self) -> int:
        configured = max(0, self.policy.shared_write_gap_seconds)
        if self._is_custom_mode():
            return configured
        return max(self._CONSERVATIVE_SHARED_WRITE_GAP, configured)

    def _combined_action_caps(self) -> tuple[int, int]:
        hourly = self.policy.combined_action_hourly_cap
        daily = self.policy.combined_action_daily_cap
        if self._is_custom_mode():
            return hourly, daily
        hard_hourly, hard_daily = self._CONSERVATIVE_COMBINED_ACTION_CAPS
        return (
            min(hard_hourly, hourly) if hourly > 0 else hard_hourly,
            min(hard_daily, daily) if daily > 0 else hard_daily,
        )

    def _network_concurrency(self) -> int:
        if self._is_custom_mode():
            return max(1, self.policy.network_group_concurrency)
        return 1

    def _is_custom_mode(self) -> bool:
        return str(self.policy.mode or "").strip().lower() == "custom"

    @staticmethod
    def _latest_success(session, account_id: int, kinds: list[str]) -> datetime | None:
        row = session.exec(
            select(RiskEvent)
            .where(RiskEvent.account_id == account_id)
            .where(RiskEvent.outcome == RiskCategory.SUCCESS.value)
            .where(RiskEvent.operation_kind.in_(kinds))
            .order_by(RiskEvent.occurred_at.desc())
        ).first()
        latest = row.occurred_at if row else None
        legacy: list[datetime] = []
        if OperationKind.COMMENT.value in kinds:
            value = session.exec(select(CommentTask.done_at)
                                 .where(CommentTask.account_id == account_id)
                                 .where(CommentTask.status == "done")
                                 .order_by(CommentTask.done_at.desc())).first()
            if value:
                legacy.append(value)
        if OperationKind.PUBLISH.value in kinds:
            publish_completed_at = func.coalesce(
                PublishTask.done_at, PublishTask.created_at)
            value = session.exec(select(publish_completed_at)
                                 .where(PublishTask.account_id == account_id)
                                 .where(PublishTask.status == "done")
                                 .order_by(publish_completed_at.desc())).first()
            if value:
                legacy.append(value)
        if OperationKind.DM.value in kinds:
            value = session.exec(select(AccountActionTask.done_at)
                                 .where(AccountActionTask.account_id == account_id)
                                 .where(AccountActionTask.status == "done")
                                 .where(AccountActionTask.action == "send_dm")
                                 .order_by(AccountActionTask.done_at.desc())).first()
            if value:
                legacy.append(value)
        if OperationKind.SOCIAL.value in kinds:
            value = session.exec(select(AccountActionTask.done_at)
                                 .where(AccountActionTask.account_id == account_id)
                                 .where(AccountActionTask.status == "done")
                                 .where(AccountActionTask.action != "send_dm")
                                 .order_by(AccountActionTask.done_at.desc())).first()
            if value:
                legacy.append(value)
        return max(([latest] if latest else []) + legacy, default=None)

    @staticmethod
    def _count_successes(session, account_id: int, kinds: list[str], since: datetime) -> int:
        event_count = len(session.exec(
            select(RiskEvent.id)
            .where(RiskEvent.account_id == account_id)
            .where(RiskEvent.outcome == RiskCategory.SUCCESS.value)
            .where(RiskEvent.operation_kind.in_(kinds))
            .where(RiskEvent.occurred_at >= since)
        ).all())
        legacy_count = 0
        if OperationKind.COMMENT.value in kinds:
            legacy_count += len(session.exec(
                select(CommentTask.id)
                .where(CommentTask.account_id == account_id)
                .where(CommentTask.status == "done")
                .where(CommentTask.done_at >= since)).all())
        if OperationKind.PUBLISH.value in kinds:
            publish_completed_at = func.coalesce(
                PublishTask.done_at, PublishTask.created_at)
            legacy_count += len(session.exec(
                select(PublishTask.id)
                .where(PublishTask.account_id == account_id)
                .where(PublishTask.status == "done")
                .where(publish_completed_at >= since)).all())
        if OperationKind.DM.value in kinds:
            legacy_count += len(session.exec(
                select(AccountActionTask.id)
                .where(AccountActionTask.account_id == account_id)
                .where(AccountActionTask.status == "done")
                .where(AccountActionTask.action == "send_dm")
                .where(AccountActionTask.done_at >= since)).all())
        if OperationKind.SOCIAL.value in kinds:
            legacy_count += len(session.exec(
                select(AccountActionTask.id)
                .where(AccountActionTask.account_id == account_id)
                .where(AccountActionTask.status == "done")
                .where(AccountActionTask.action != "send_dm")
                .where(AccountActionTask.done_at >= since)).all())
        # New code writes both the legacy task row and a RiskEvent. ``max``
        # reconstructs upgrade-day history without double-counting new writes.
        return max(event_count, legacy_count)

    def preflight(
        self,
        account_or_id: DouyinAccount | int | None,
        kind: OperationKind,
        *,
        now: datetime | None = None,
        allow_invalid_probe: bool = False,
    ) -> RiskDecision:
        if not self.policy.enabled:
            return RiskDecision(True)
        account_id = self._account_id(account_or_id)
        if not account_id:
            return RiskDecision(True)
        now = now or _utcnow()

        with self._decision_lock, get_session() as session:
            account = self._load_account(session, account_or_id)
            if account is None:
                return RiskDecision(False, "账号不存在", signal="account_missing")
            if account.status == "invalid" and kind != OperationKind.LOGIN:
                if not allow_invalid_probe:
                    return RiskDecision(
                        False,
                        "账号登录态已失效，等待重新登录",
                        signal="auth_required",
                    )
                # A manual profile refresh is the recovery probe for accounts
                # that were marked invalid by an inconclusive login check.
                # A real auth failure still gets a 15-minute retry gap.
                latest_auth = session.exec(
                    select(RiskEvent)
                    .where(RiskEvent.account_id == account_id)
                    .where(RiskEvent.outcome == RiskCategory.AUTH.value)
                    .order_by(RiskEvent.occurred_at.desc())
                    .limit(1)
                ).first()
                if latest_auth is not None:
                    next_at = latest_auth.occurred_at + timedelta(minutes=15)
                    if next_at > now:
                        return RiskDecision(
                            False,
                            "登录态校验失败后需等待再探测",
                            next_at,
                            "auth_probe_gap",
                        )
            if account.proxy and account.proxy_status in {
                    "bad", "auth_error", "blocked", "drifted"} \
                    and kind != OperationKind.LOGIN:
                return RiskDecision(
                    False,
                    "账号绑定代理当前不可用",
                    now + timedelta(minutes=5),
                    "proxy_unavailable",
                )
            state = self._state(session, account_id)
            if state.cooldown_until and state.cooldown_until > now:
                return RiskDecision(
                    False,
                    f"账号处于平台风险冷却期，截止 {state.cooldown_until.isoformat(timespec='seconds')}",
                    state.cooldown_until,
                    "cooldown",
                )

            if state.risk_level > 0 and kind != OperationKind.READ_LIGHT:
                next_at = state.probe_only_until or state.cooldown_until or now
                return RiskDecision(
                    False,
                    "账号处于渐进恢复阶段，仅允许轻量状态探测",
                    next_at,
                    "probe_only",
                )

            if state.risk_level > 0 and kind == OperationKind.READ_LIGHT \
                    and state.last_recovery_at:
                next_probe = state.last_recovery_at + timedelta(
                    seconds=max(1, self.policy.recovery_probe_gap_seconds))
                if next_probe > now:
                    return RiskDecision(False, "恢复探测间隔未到", next_probe, "probe_gap")

            if kind in self._WRITE_KINDS and not self._in_active_window(account, now):
                local = now.replace(tzinfo=timezone.utc).astimezone(self._timezone(account))
                next_local = local.replace(
                    hour=max(0, min(23, self.cfg.engine.active_hours_start)),
                    minute=0, second=0, microsecond=0)
                if next_local <= local:
                    next_local += timedelta(days=1)
                next_at = next_local.astimezone(timezone.utc).replace(tzinfo=None)
                return RiskDecision(False, "当前处于非活跃时段", next_at, "quiet_hours")

            gap, hourly_cap, daily_cap = self._limits(kind)
            kind_value = kind.value
            latest = self._latest_success(session, account_id, [kind_value])
            if latest and gap > 0:
                next_at = latest + timedelta(seconds=gap)
                if next_at > now:
                    return RiskDecision(False, "尚未达到该操作最小间隔", next_at, "kind_gap")

            if kind in self._WRITE_KINDS and state.last_write_at:
                next_at = state.last_write_at + timedelta(
                    seconds=self._shared_write_gap())
                if next_at > now:
                    return RiskDecision(False, "尚未达到账号共享写操作间隔", next_at,
                                        "shared_write_gap")

            if hourly_cap > 0:
                count = self._count_successes(
                    session, account_id, [kind_value], now - timedelta(hours=1))
                if count >= hourly_cap:
                    return RiskDecision(False, "已达到账号每小时操作上限",
                                        now + timedelta(hours=1), "hourly_cap")
            if daily_cap > 0:
                day_start = self._local_day_start_utc(account, now)
                count = self._count_successes(session, account_id, [kind_value], day_start)
                if count >= daily_cap:
                    local = now.replace(tzinfo=timezone.utc).astimezone(self._timezone(account))
                    next_local = (local + timedelta(days=1)).replace(
                        hour=0, minute=0, second=0, microsecond=0)
                    next_at = next_local.astimezone(timezone.utc).replace(tzinfo=None)
                    return RiskDecision(False, "已达到账号每日操作上限", next_at,
                                        "daily_cap")

            if kind in (OperationKind.SOCIAL, OperationKind.DM):
                action_kinds = [OperationKind.SOCIAL.value, OperationKind.DM.value]
                combined_hourly_cap, combined_daily_cap = self._combined_action_caps()
                hour_count = self._count_successes(
                    session, account_id, action_kinds, now - timedelta(hours=1))
                if combined_hourly_cap > 0 and hour_count >= combined_hourly_cap:
                    return RiskDecision(False, "已达到账号每小时账号动作总上限",
                                        now + timedelta(hours=1), "combined_hourly_cap")
                day_count = self._count_successes(
                    session, account_id, action_kinds,
                    self._local_day_start_utc(account, now))
                if combined_daily_cap > 0 and day_count >= combined_daily_cap:
                    local = now.replace(tzinfo=timezone.utc).astimezone(
                        self._timezone(account))
                    next_local = (local + timedelta(days=1)).replace(
                        hour=0, minute=0, second=0, microsecond=0)
                    next_at = next_local.astimezone(timezone.utc).replace(tzinfo=None)
                    return RiskDecision(False, "已达到账号每日账号动作总上限",
                                        next_at, "combined_daily_cap")
            session.commit()
        return RiskDecision(True)

    def record_success(
        self,
        account_or_id: DouyinAccount | int,
        kind: OperationKind,
        *,
        now: datetime | None = None,
    ) -> None:
        if not self.policy.enabled:
            return
        account_id = self._account_id(account_or_id)
        if not account_id:
            return
        now = now or _utcnow()
        with self._decision_lock, get_session() as session:
            account = self._load_account(session, account_or_id)
            if account is None:
                return
            state = self._state(session, account_id)
            state.last_operation_at = now
            state.updated_at = now
            state.consecutive_network_failures = 0
            state.network_failure_key = ""
            if kind in self._WRITE_KINDS:
                state.last_write_at = now
            if kind == OperationKind.READ_HEAVY:
                state.last_heavy_read_at = now
            if state.risk_level > 0 and kind == OperationKind.READ_LIGHT:
                gap = max(1, self.policy.recovery_probe_gap_seconds)
                if state.last_recovery_at is None \
                        or (now - state.last_recovery_at).total_seconds() >= gap:
                    state.recovery_successes += 1
                    state.last_recovery_at = now
                    if state.recovery_successes >= max(1, self.policy.recovery_successes):
                        state.risk_level = max(0, state.risk_level - 1)
                        state.consecutive_risk = state.risk_level
                        state.recovery_successes = 0
                        if state.risk_level == 0:
                            state.cooldown_until = None
                            state.probe_only_until = None
                            account.write_paused_until = None
                            account.write_pause_reason = ""
            session.add(RiskEvent(
                account_id=account_id,
                network_key=network_key(account.proxy),
                operation_kind=kind.value,
                outcome=RiskCategory.SUCCESS.value,
                occurred_at=now,
            ))
            session.add(state)
            session.add(account)
            session.commit()

    def record_failure(
        self,
        account_or_id: DouyinAccount | int,
        kind: OperationKind,
        error: object,
        *,
        status_code: int | None = None,
        payload: object = None,
        now: datetime | None = None,
    ) -> FailureDecision:
        account_id = self._account_id(account_or_id)
        category, signal = classify_platform_error(error, status_code, payload)
        if not self.policy.enabled:
            return FailureDecision(category, signal, controlled=False)
        now = now or _utcnow()
        if not account_id:
            return FailureDecision(category, signal)

        next_at: datetime | None = None
        with self._decision_lock, get_session() as session:
            account = self._load_account(session, account_or_id)
            if account is None:
                return FailureDecision(category, signal)
            state = self._state(session, account_id)
            state.last_operation_at = now
            state.updated_at = now
            if category == RiskCategory.RISK:
                state.consecutive_network_failures = 0
                state.network_failure_key = ""
                state.risk_level = min(4, max(1, state.risk_level + 1))
                state.consecutive_risk += 1
                state.recovery_successes = 0
                state.last_recovery_at = None
                steps = self.policy.cooldown_steps_seconds or [1800, 7200, 21600, 86400]
                index = min(state.risk_level - 1, len(steps) - 1)
                next_at = now + timedelta(seconds=max(1, int(steps[index])))
                state.cooldown_until = next_at
                state.probe_only_until = next_at
                state.last_risk_at = now
                state.last_risk_reason = str(error or signal).strip()[:240]
                account.write_paused_until = next_at
                account.write_pause_reason = state.last_risk_reason
            elif category == RiskCategory.NETWORK:
                next_at = now + timedelta(minutes=5)
                proxy_failure = bool(account.proxy) and signal in {
                    "network_failure", "proxy_auth"}
                current_key = network_key(account.proxy) if proxy_failure else ""
                if not proxy_failure:
                    state.consecutive_network_failures = 0
                    state.network_failure_key = ""
                elif state.network_failure_key == current_key:
                    state.consecutive_network_failures += 1
                else:
                    state.network_failure_key = current_key
                    state.consecutive_network_failures = 1
                if proxy_failure and state.consecutive_network_failures >= 2:
                    unavailable_status = (
                        "auth_error" if signal == "proxy_auth" else "bad")
                    account.proxy_status = unavailable_status
                    account_proxy = normalize_proxy(account.proxy)
                    for proxy_row in session.exec(select(ProxyPool)).all():
                        if normalize_proxy(proxy_row.url) == account_proxy:
                            proxy_row.status = unavailable_status
                            proxy_row.last_checked_at = now
                            session.add(proxy_row)
            elif category == RiskCategory.AUTH:
                state.consecutive_network_failures = 0
                state.network_failure_key = ""
                account.status = "invalid"
                next_at = now + timedelta(minutes=15)
            else:
                state.consecutive_network_failures = 0
                state.network_failure_key = ""
            event = RiskEvent(
                account_id=account_id,
                network_key=network_key(account.proxy),
                operation_kind=kind.value,
                outcome=category.value,
                signal=signal,
                detail=str(error or signal).strip()[:240],
                occurred_at=now,
            )
            session.add(event)
            session.flush()
            if category == RiskCategory.RISK:
                group_until = self._apply_network_group_circuit_breaker(
                    session, account, now=now)
                if group_until is not None \
                        and (next_at is None or group_until > next_at):
                    next_at = group_until
            session.add(state)
            session.add(account)
            session.commit()
        return FailureDecision(category, signal, next_at)

    def clear_account(self, account_id: int, *, reason: str = "人工检查完成",
                      actor: str = "local-ui") -> None:
        with self._decision_lock, get_session() as session:
            state = session.get(AccountRiskState, account_id)
            if state:
                state.risk_level = 0
                state.cooldown_until = None
                state.probe_only_until = None
                state.consecutive_risk = 0
                state.consecutive_network_failures = 0
                state.network_failure_key = ""
                state.recovery_successes = 0
                state.last_risk_reason = ""
                state.last_recovery_at = None
                state.updated_at = _utcnow()
                session.add(state)
            account = session.get(DouyinAccount, account_id)
            if account:
                account.write_paused_until = None
                account.write_pause_reason = ""
                session.add(account)
            session.add(RiskEvent(
                account_id=account_id,
                network_key=network_key(account.proxy) if account else "direct",
                operation_kind=OperationKind.READ_LIGHT.value,
                outcome="manual",
                signal="risk_cleared",
                detail=(f"{actor} 解除账号风控状态：{reason}".strip())[:240],
                occurred_at=_utcnow(),
            ))
            session.commit()

    def next_write_at(self, account_id: int) -> datetime | None:
        """Return the earliest known time any write may pass shared gates."""
        with self._decision_lock, get_session() as session:
            state = session.get(AccountRiskState, account_id)
            if state is None:
                return None
            candidates = [value for value in (state.cooldown_until,)
                          if value is not None]
            if state.last_write_at is not None:
                candidates.append(
                    state.last_write_at + timedelta(seconds=self._shared_write_gap()))
            return max(candidates) if candidates else None

    def prune_events(self, *, now: datetime | None = None) -> int:
        cutoff = (now or _utcnow()) - timedelta(
            days=max(1, self.policy.event_retention_days))
        removed = 0
        with self._decision_lock, get_session() as session:
            rows = session.exec(select(RiskEvent).where(
                RiskEvent.occurred_at < cutoff)).all()
            for row in rows:
                session.delete(row)
                removed += 1
            session.commit()
        return removed

    @asynccontextmanager
    async def network_guard(self, account_or_id: DouyinAccount | int | None):
        if not self.policy.enabled:
            yield "disabled"
            return
        proxy = ""
        if not isinstance(account_or_id, (int, type(None))) \
                and hasattr(account_or_id, "proxy"):
            proxy = getattr(account_or_id, "proxy", "") or ""
        else:
            account_id = self._account_id(account_or_id)
            if account_id:
                with get_session() as session:
                    account = session.get(DouyinAccount, account_id)
                    proxy = account.proxy if account else ""
        key = network_key(proxy)
        with self._network_lock_guard:
            sem = self._network_locks.get(key)
            if sem is None:
                sem = asyncio.Semaphore(self._network_concurrency())
                self._network_locks[key] = sem
        async with sem:
            yield key
