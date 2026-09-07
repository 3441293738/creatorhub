"""Transactional account retirement without deleting historical content."""
from __future__ import annotations

from datetime import datetime

from sqlmodel import select

from .models import (
    AccountActionTask, CommentRule, CommentTask, CommentWatch, DanmakuWatch,
    DmAutoReplyRule, KeywordCollectionJob, MonitorTarget, PublishTask,
)


class AccountUnavailableError(RuntimeError):
    """A queued operation lost its bound account before taking the work lock."""


def active_account_task(session, account_id: int) -> str:
    for model, statuses, label in (
        (PublishTask, ("publishing",), "发布"),
        (CommentTask, ("doing",), "评论"),
        (AccountActionTask, ("doing",), "账号操作"),
        (KeywordCollectionJob, ("running",), "采集"),
    ):
        row = session.exec(select(model).where(
            model.account_id == account_id, model.status.in_(statuses))).first()
        if row is not None:
            return f"{label}任务 #{row.id} 正在执行，请等待完成或先停止任务"
    return ""


def retire_account_tasks(session, account_id: int) -> dict[str, int]:
    """Call in the same transaction as account deletion, under its work lock.

    Keep account references and terminal/uncertain history for audit. Numeric
    account IDs are permanently reserved and will never identify a new user.
    """
    counts = {"canceled_tasks": 0, "disabled_rules": 0, "disabled_monitors": 0}
    reason = "绑定账号已删除；历史保留，任务已停止"
    for model in (PublishTask, CommentTask, AccountActionTask):
        for row in session.exec(select(model).where(
                model.account_id == account_id,
                model.status.in_(("draft", "pending", "failed")))).all():
            row.status = "canceled"
            row.scheduled_at = None
            row.next_allowed_at = None
            row.blocked_reason = ""
            row.blocked_signal = ""
            row.blocked_operation = ""
            row.blocked_at = None
            row.error = reason
            session.add(row)
            counts["canceled_tasks"] += 1
    for row in session.exec(select(KeywordCollectionJob).where(
            KeywordCollectionJob.account_id == account_id,
            KeywordCollectionJob.status.in_(("pending", "partial", "failed")))).all():
        row.status = "canceled"
        row.cancel_requested = True
        row.current_step = reason
        row.next_allowed_at = None
        row.finished_at = datetime.utcnow()
        session.add(row)
        counts["canceled_tasks"] += 1
    for model in (MonitorTarget, CommentWatch, DanmakuWatch,
                  CommentRule, DmAutoReplyRule):
        for row in session.exec(select(model).where(
                model.account_id == account_id, model.enabled == True)).all():  # noqa:E712
            row.enabled = False
            if hasattr(row, "last_error"):
                row.last_error = reason
            session.add(row)
            key = "disabled_rules" if model in (CommentRule, DmAutoReplyRule) else "disabled_monitors"
            counts[key] += 1
    return counts
