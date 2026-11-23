"""按平台强身份标识合并重复账号登录记录并回收孤儿 Profile 目录。

支持全部 9 大平台：当同一平台内两个账号记录拥有相同的非空 ``sec_uid``、``uid`` 或
``douyin_id``（平台唯一账号标识）时，将登录态快照、任务外键与完整风控状态合并至最早
创建的稳定账号记录，避免同一平台账号被拆成两个并发轮询的影子环境。
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlmodel import SQLModel, Session, select

from .models import AccountRiskState, DmMonitorState, DouyinAccount


@dataclass(frozen=True)
class AccountMerge:
    kept_id: int
    removed_id: int
    removed_profile_dir: str = ""


def _identity_tokens(account: DouyinAccount) -> set[tuple[str, str, str]]:
    """返回 (platform, token_type, normalized_value) 强身份三元组集合。"""
    pf = str(account.platform or "").strip().lower()
    if not pf:
        return set()
    tokens: set[tuple[str, str, str]] = set()
    sec_uid = str(account.sec_uid or "").strip()
    uid = str(account.uid or "").strip()
    handle_id = str(account.douyin_id or "").strip()
    if sec_uid:
        tokens.add((pf, "sec_uid", sec_uid))
    if uid:
        tokens.add((pf, "uid", uid))
    if handle_id:
        tokens.add((pf, "handle_id", handle_id.casefold()))
    return tokens


def duplicate_platform_account_ids(
    session: Session, account_id: int
) -> list[int]:
    """查找同平台内包含 ``account_id`` 的传递性重复账号组。"""
    seed_acc = session.get(DouyinAccount, account_id)
    if seed_acc is None or not seed_acc.platform:
        return []
    pf = str(seed_acc.platform).strip().lower()
    accounts = session.exec(
        select(DouyinAccount).where(DouyinAccount.platform == pf)
    ).all()
    by_id = {int(account.id): account for account in accounts if account.id}
    if account_id not in by_id or not _identity_tokens(by_id[account_id]):
        return [account_id] if account_id in by_id else []

    selected = {account_id}
    tokens = set(_identity_tokens(by_id[account_id]))
    changed = True
    while changed:
        changed = False
        for candidate_id, candidate in by_id.items():
            if candidate_id in selected:
                continue
            candidate_tokens = _identity_tokens(candidate)
            if tokens.intersection(candidate_tokens):
                selected.add(candidate_id)
                tokens.update(candidate_tokens)
                changed = True
    return sorted(selected)


def duplicate_xhs_account_ids(
    session: Session, account_id: int
) -> list[int]:
    """兼容旧接口：查找小红书重复账号组。"""
    acc = session.get(DouyinAccount, account_id)
    if acc is None or str(acc.platform or "").lower() != "xhs":
        return []
    return duplicate_platform_account_ids(session, account_id)


def _later(left: datetime | None, right: datetime | None) -> datetime | None:
    values = [value for value in (left, right) if value is not None]
    return max(values) if values else None


def _merge_risk_state(
    session: Session, kept_id: int, removed_id: int
) -> None:
    kept = session.get(AccountRiskState, kept_id)
    removed = session.get(AccountRiskState, removed_id)
    if removed is None:
        return
    if kept is None:
        session.exec(
            AccountRiskState.__table__.update()
            .where(AccountRiskState.__table__.c.account_id == removed_id)
            .values(account_id=kept_id)
        )
        return

    kept_updated_at = kept.updated_at
    for name in (
        "risk_level", "consecutive_risk", "consecutive_network_failures",
        "recovery_successes", "session_operation_count",
    ):
        setattr(kept, name, max(
            int(getattr(kept, name, 0) or 0),
            int(getattr(removed, name, 0) or 0),
        ))
    for name in (
        "cooldown_until", "probe_only_until", "last_risk_at",
        "last_operation_at", "last_write_at", "last_heavy_read_at",
        "last_recovery_at", "retry_not_before", "operation_not_before",
        "session_rest_until", "updated_at",
    ):
        setattr(kept, name, _later(
            getattr(kept, name, None), getattr(removed, name, None)))
    if removed.manual_review_required and not kept.manual_review_required:
        kept.manual_review_required = True
        kept.manual_review_reason = (
            removed.manual_review_reason or kept.manual_review_reason
        )
    if (removed.updated_at or datetime.min) >= (kept_updated_at or datetime.min):
        kept.network_failure_key = (
            removed.network_failure_key or kept.network_failure_key)
        kept.last_risk_reason = removed.last_risk_reason or kept.last_risk_reason
        if removed.manual_review_reason:
            kept.manual_review_reason = removed.manual_review_reason
    session.add(kept)
    session.delete(removed)
    session.flush()


def _merge_dm_monitor_rows(session: Session, account_id: int) -> None:
    rows = session.exec(select(DmMonitorState).where(
        DmMonitorState.account_id == account_id
    ).order_by(DmMonitorState.id)).all()
    by_platform: dict[str, list[DmMonitorState]] = {}
    for row in rows:
        by_platform.setdefault(str(row.platform or ""), []).append(row)
    for group in by_platform.values():
        if len(group) < 2:
            continue
        kept, *duplicates = group
        for row in duplicates:
            kept_updated_at = kept.updated_at
            kept.baseline_initialized = (
                kept.baseline_initialized or row.baseline_initialized)
            for name in (
                "baseline_at", "last_poll_at", "last_push_at", "updated_at",
            ):
                setattr(kept, name, _later(
                    getattr(kept, name, None), getattr(row, name, None)))
            if row.last_error and (
                    row.updated_at or datetime.min) >= (
                    kept_updated_at or datetime.min):
                kept.last_error = row.last_error
            session.delete(row)
        session.add(kept)


def _copy_account_data(kept: DouyinAccount, accounts: list[DouyinAccount]) -> None:
    """把登录态快照和公开资料字段合并到需要保留的稳定账号记录。"""
    newest_first = sorted(
        accounts,
        key=lambda account: (
            account.created_at or datetime.min, int(account.id or 0)),
        reverse=True,
    )

    for name in ("storage_state", "creator_storage_state", "cookie"):
        value = next((str(getattr(row, name, "") or "")
                      for row in newest_first if getattr(row, name, "")), "")
        if value:
            setattr(kept, name, value)

    # 若保留记录尚未绑定代理而新记录已绑定代理，补齐代理与 proxy_id(不覆盖既有固定代理)
    if not kept.proxy:
        donor = next((row for row in newest_first if row.proxy), None)
        if donor is not None:
            kept.proxy = donor.proxy
            kept.proxy_id = donor.proxy_id
            kept.proxy_status = donor.proxy_status

    for name in ("nickname", "sec_uid", "douyin_id", "avatar", "uid"):
        value = next((getattr(row, name, "") for row in newest_first
                      if getattr(row, name, "")), "")
        if value:
            setattr(kept, name, value)
    kept.follower_count = max(int(row.follower_count or 0) for row in accounts)
    kept.aweme_count = max(int(row.aweme_count or 0) for row in accounts)
    kept.last_active_at = max(
        (row.last_active_at for row in accounts if row.last_active_at),
        default=kept.last_active_at,
    )
    kept.write_paused_until = max(
        (row.write_paused_until for row in accounts if row.write_paused_until),
        default=kept.write_paused_until,
    )
    if any(row.status == "active" for row in accounts):
        kept.status = "active"


def _merge_one(
    session: Session, kept: DouyinAccount, removed: DouyinAccount
) -> AccountMerge:
    kept_id, removed_id = int(kept.id), int(removed.id)
    removed_profile = str(removed.profile_dir or "")
    _copy_account_data(kept, [kept, removed])
    session.add(kept)
    session.flush()

    _merge_risk_state(session, kept_id, removed_id)
    for table in SQLModel.metadata.sorted_tables:
        if table.name in {
                DouyinAccount.__table__.name,
                AccountRiskState.__table__.name}:
            continue
        if "account_id" not in table.c:
            continue
        session.exec(
            table.update()
            .where(table.c.account_id == removed_id)
            .values(account_id=kept_id)
        )
    _merge_dm_monitor_rows(session, kept_id)
    session.delete(removed)
    session.flush()
    return AccountMerge(kept_id, removed_id, removed_profile)


def reconcile_platform_accounts(
    session: Session,
    *,
    platform: str | None = None,
    account_id: int | None = None,
) -> list[AccountMerge]:
    """自动合并同平台重复账号记录，并以事务方式提交。"""
    query = select(DouyinAccount).order_by(DouyinAccount.id)
    if platform:
        query = query.where(DouyinAccount.platform == str(platform).strip().lower())
    accounts = session.exec(query).all()
    by_id = {int(row.id): row for row in accounts if row.id}
    pending_ids = set(by_id)
    if account_id is not None:
        pending_ids.intersection_update(
            duplicate_platform_account_ids(session, account_id)
        )

    merges: list[AccountMerge] = []
    while pending_ids:
        seed_id = min(pending_ids)
        group_ids = [
            item for item in duplicate_platform_account_ids(session, seed_id)
            if item in pending_ids
        ]
        if len(group_ids) < 2:
            pending_ids.discard(seed_id)
            continue
        kept = by_id[min(group_ids)]
        for removed_id in sorted(group_ids):
            if removed_id == kept.id:
                continue
            removed = by_id[removed_id]
            merges.append(_merge_one(session, kept, removed))
            pending_ids.discard(removed_id)
        pending_ids.discard(int(kept.id))
    if merges:
        session.commit()
    return merges


def reconcile_xhs_accounts(
    session: Session, account_id: int | None = None
) -> list[AccountMerge]:
    """兼容接口：自动合并重复的小红书账号记录。"""
    return reconcile_platform_accounts(
        session, platform="xhs", account_id=account_id
    )


def cleanup_merged_profiles(
    merges: list[AccountMerge], profiles_root: str
) -> list[str]:
    """只清理受管目录内、合并后不再使用的浏览器 Profile。"""
    root = Path(profiles_root).expanduser().resolve()
    removed: list[str] = []
    for merge in merges:
        if not merge.removed_profile_dir:
            continue
        candidate = Path(merge.removed_profile_dir).expanduser().resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        if candidate == root:
            continue
        try:
            if candidate.exists():
                shutil.rmtree(candidate)
            removed.append(str(candidate))
        except OSError:
            continue
    return removed


def cleanup_orphan_profiles(session: Session, profiles_root: str) -> int:
    """启动时清理上次因 Windows 句柄未释放而残留的孤儿 account_<uuid> 目录。"""
    from .browser.cdp import profile_is_locked

    root = Path(profiles_root).expanduser().resolve()
    if not root.is_dir():
        return 0
    active_dirs = {
        os.path.normcase(str(Path(acc.profile_dir).expanduser().resolve()))
        for acc in session.exec(select(DouyinAccount)).all()
        if acc.profile_dir
    }
    cleaned = 0
    for child in root.iterdir():
        if not child.is_dir():
            continue
        if child.name == "_anon":
            shutil.rmtree(child, ignore_errors=True)
            continue
        if not child.name.startswith("account_"):
            continue
        norm = os.path.normcase(str(child.resolve()))
        if norm in active_dirs or profile_is_locked(child):
            continue
        try:
            shutil.rmtree(child)
            cleaned += 1
        except OSError:
            continue
    return cleaned
