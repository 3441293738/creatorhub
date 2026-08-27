"""按平台强身份标识合并账号登录记录。

小红书主站与创作平台使用两套独立登录流程，因此同一个平台账号可能被保存成
两条新记录。取得可信的 ``user_id`` 或 ``red_id`` 后，本模块会把两条记录及其
读取、创作登录态合并为一个持久账号。
"""
from __future__ import annotations

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


def _identity_tokens(account: DouyinAccount) -> set[tuple[str, str]]:
    """只返回平台内唯一且可信的身份标识。"""
    if str(account.platform or "").lower() != "xhs":
        return set()
    tokens: set[tuple[str, str]] = set()
    user_id = str(account.sec_uid or "").strip()
    red_id = str(account.douyin_id or "").strip()
    if user_id:
        tokens.add(("user_id", user_id))
    if red_id:
        tokens.add(("red_id", red_id.casefold()))
    return tokens


def duplicate_xhs_account_ids(
        session: Session, account_id: int) -> list[int]:
    """查找包含 ``account_id`` 的传递性重复账号组。"""
    accounts = session.exec(
        select(DouyinAccount).where(DouyinAccount.platform == "xhs")
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


def _later(left: datetime | None, right: datetime | None) -> datetime | None:
    values = [value for value in (left, right) if value is not None]
    return max(values) if values else None


def _merge_risk_state(
        session: Session, kept_id: int, removed_id: int) -> None:
    kept = session.get(AccountRiskState, kept_id)
    removed = session.get(AccountRiskState, removed_id)
    if removed is None:
        return
    if kept is None:
        # 目标账号尚无风控状态，直接迁移主键即可完整保留原有风控历史，
        # 无需重新构造状态对象。
        session.exec(
            AccountRiskState.__table__.update()
            .where(AccountRiskState.__table__.c.account_id == removed_id)
            .values(account_id=kept_id)
        )
        return

    kept_updated_at = kept.updated_at
    for name in (
        "risk_level", "consecutive_risk", "consecutive_network_failures",
        "recovery_successes",
    ):
        setattr(kept, name, max(
            int(getattr(kept, name, 0) or 0),
            int(getattr(removed, name, 0) or 0),
        ))
    for name in (
        "cooldown_until", "probe_only_until", "last_risk_at",
        "last_operation_at", "last_write_at", "last_heavy_read_at",
        "last_recovery_at", "updated_at",
    ):
        setattr(kept, name, _later(
            getattr(kept, name, None), getattr(removed, name, None)))
    if (removed.updated_at or datetime.min) >= (kept_updated_at or datetime.min):
        kept.network_failure_key = (
            removed.network_failure_key or kept.network_failure_key)
        kept.last_risk_reason = removed.last_risk_reason or kept.last_risk_reason
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

    # 主站读取登录与创作平台登录分别保留各自最新的快照，
    # 这是合并两套登录态的核心逻辑。
    for name in ("storage_state", "creator_storage_state", "cookie"):
        value = next((str(getattr(row, name, "") or "")
                      for row in newest_first if getattr(row, name, "")), "")
        if value:
            setattr(kept, name, value)

    # 身份与公开资料使用最新的非空值补齐；稳定账号原有的 Profile 目录、
    # 代理和浏览器指纹保持不变，避免环境发生漂移。
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
        session: Session, kept: DouyinAccount,
        removed: DouyinAccount) -> AccountMerge:
    kept_id, removed_id = int(kept.id), int(removed.id)
    removed_profile = str(removed.profile_dir or "")
    _copy_account_data(kept, [kept, removed])
    session.add(kept)
    session.flush()

    _merge_risk_state(session, kept_id, removed_id)
    # 把所有持久化账号关联统一迁移到保留账号。以模型元数据作为唯一表清单，
    # 后续新增包含 account_id 的业务表也会自动继承该迁移行为。
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


def reconcile_xhs_accounts(
        session: Session, account_id: int | None = None) -> list[AccountMerge]:
    """自动合并重复的小红书账号记录，并以事务方式提交。

    保留最早创建的账号 id，使已有页面选择和任务引用保持稳定。传入
    ``account_id`` 时，只处理该账号所属的重复账号组。
    """
    accounts = session.exec(
        select(DouyinAccount).where(DouyinAccount.platform == "xhs")
        .order_by(DouyinAccount.id)
    ).all()
    by_id = {int(row.id): row for row in accounts if row.id}
    pending_ids = set(by_id)
    if account_id is not None:
        pending_ids.intersection_update(
            duplicate_xhs_account_ids(session, account_id))

    merges: list[AccountMerge] = []
    while pending_ids:
        seed_id = min(pending_ids)
        group_ids = [item for item in duplicate_xhs_account_ids(session, seed_id)
                     if item in pending_ids]
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


def cleanup_merged_profiles(
        merges: list[AccountMerge], profiles_root: str) -> list[str]:
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
            # 外部遗留的 Chrome 进程可能仍占用文件。数据库合并已经完成，
            # 后续维护或删除流程可再次回收该目录。
            continue
    return removed
