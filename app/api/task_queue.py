"""全站统一任务队列聚合检索、异常结果人工核对与批量停止接口。"""
from __future__ import annotations

from datetime import datetime
from typing import Any
import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field as PydanticField
from sqlalchemy import update
from sqlmodel import select

from app import runtime as app_runtime
from app.db import get_session
from app.models import AccountActionTask, CommentTask, ContentRecord, DouyinAccount, KeywordCollectionContent, KeywordCollectionJob, MonitorTarget, PublishTask, RiskAdminAudit
from app.operations import record_account_operation
from app.services import metadata as metadata_service

router = APIRouter()


# ─────────── 统一任务队列 ───────────
_QUEUE_TYPES = {
    "collections", "publishes", "comments", "actions",
    "monitor_downloads", "collection_downloads",
}


_QUEUE_STATES = {"active", "pending", "running", "blocked", "failed", "completed", "all"}


_QUEUE_RUNNING = {"running", "publishing", "doing", "downloading"}


_QUEUE_COMPLETED = {"done", "canceled", "skipped"}


def _queue_state(status: str, *, blocked_reason: str = "",
                 next_allowed_at: datetime | None = None) -> str:
    status = str(status or "").lower()
    if status in metadata_service._QUEUE_FAILED:
        return "failed"
    if status in _QUEUE_COMPLETED:
        return "completed"
    if blocked_reason or (next_allowed_at and next_allowed_at > datetime.utcnow()):
        return "blocked"
    if status in _QUEUE_RUNNING:
        return "running"
    return "pending"


def _queue_keywords(value: str) -> str:
    try:
        values = json.loads(value or "[]")
    except (TypeError, ValueError):
        values = []
    if isinstance(values, list):
        text = "、".join(str(item).strip() for item in values if str(item).strip())
        if text:
            return text
    return str(value or "").strip() or "未命名关键词任务"


@router.get("/api/task-queue")
async def list_task_queue(platform: str | None = None, queue_type: str = "",
                          state: str = "active", q: str = "", page: int = 1,
                          page_size: int = 20):
    """Return persistent jobs from every worker queue in one normalized view."""
    platform = str(platform or "").strip().lower()
    if platform == "all":
        platform = ""
    queue_type = str(queue_type or "").strip().lower()
    state = str(state or "active").strip().lower()
    if queue_type and queue_type not in _QUEUE_TYPES:
        raise HTTPException(400, "未知队列类型")
    if state not in _QUEUE_STATES:
        raise HTTPException(400, "未知队列状态")
    q_text = str(q or "").strip()[:200]
    q_folded = q_text.casefold()
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 20), 100))
    include_completed = state in {"completed", "all"}

    with get_session() as session:
        accounts = {row.id: row for row in session.exec(select(DouyinAccount)).all()}
        targets = {row.id: row for row in session.exec(select(MonitorTarget)).all()}
        jobs = {row.id: row for row in session.exec(select(KeywordCollectionJob)).all()}
        items: list[dict[str, Any]] = []

        def account_name(account_id: int | None) -> str:
            account = accounts.get(account_id)
            return (account.nickname or account.douyin_id or account.sec_uid) if account else ""

        def add(*, queue: str, queue_label: str, row_id: int | None,
                row_platform: str, account_id: int | None, title: str, detail: str,
                status: str, created_at: datetime | None,
                source_tab: str, scheduled_at: datetime | None = None,
                schedule_is_utc: bool = True,
                blocked_reason: str = "", blocked_signal: str = "",
                next_allowed_at: datetime | None = None, error: str = "") -> None:
            queue_state = _queue_state(
                status, blocked_reason=blocked_reason,
                next_allowed_at=next_allowed_at)
            items.append({
                "key": f"{queue}:{row_id}",
                "queue_type": queue,
                "queue_label": queue_label,
                "id": row_id,
                "platform": row_platform,
                "account_id": account_id,
                "account_name": account_name(account_id),
                "title": str(title or "未命名任务")[:500],
                "detail": str(detail or "")[:1000],
                "status": str(status or "pending"),
                "state": queue_state,
                "scheduled_at": (metadata_service._queue_iso(scheduled_at) if schedule_is_utc
                                 else scheduled_at.isoformat() if scheduled_at else None),
                "schedule_needs_confirmation": bool(scheduled_at and not schedule_is_utc),
                "created_at": metadata_service._queue_iso(created_at),
                "next_allowed_at": metadata_service._queue_iso(next_allowed_at),
                "blocked_reason": str(blocked_reason or "")[:1000],
                "blocked_signal": str(blocked_signal or "")[:120],
                "error": str(error or "")[:1000],
                "source_tab": source_tab,
            })

        task_specs = (
            (KeywordCollectionJob,
             ["pending", "running", "failed", "partial"], ["done", "canceled"]),
            (PublishTask,
             ["draft", "pending", "publishing", "failed", "uncertain"], ["done", "canceled"]),
            (CommentTask,
             ["draft", "pending", "doing", "failed", "uncertain"], ["done", "canceled"]),
            (AccountActionTask,
             ["draft", "pending", "doing", "failed", "uncertain"], ["done", "canceled"]),
            (ContentRecord,
             ["pending", "downloading", "failed"], ["done"]),
            (KeywordCollectionContent,
             ["pending", "downloading", "failed"], ["done", "skipped"]),
        )
        loaded: dict[type, list[Any]] = {}
        for model, current_statuses, finished_statuses in task_specs:
            status_column = (model.download_status
                             if model in {ContentRecord, KeywordCollectionContent}
                             else model.status)
            active_statuses = [value for value in current_statuses
                               if value not in metadata_service._QUEUE_FAILED]
            failed_statuses = [value for value in current_statuses
                               if value in metadata_service._QUEUE_FAILED]
            rows = list(session.exec(
                select(model).where(status_column.in_(active_statuses)).order_by(
                    model.created_at.desc())).all())
            # 活动任务必须完整展示；历史失败/完成记录只取每类最近 1000 条，
            # 避免长期运行后“已完成”筛选一次性加载全部媒体记录。
            if failed_statuses:
                rows.extend(session.exec(
                    select(model).where(status_column.in_(failed_statuses)).order_by(
                        model.created_at.desc()).limit(1000)).all())
            if include_completed:
                rows.extend(session.exec(
                    select(model).where(status_column.in_(finished_statuses)).order_by(
                        model.created_at.desc()).limit(1000)).all())
            loaded[model] = rows

        for row in loaded[KeywordCollectionJob]:
            collection_title = (_queue_keywords(row.keywords)
                                if row.source_type == "keyword"
                                else row.source_name or row.source_type or "采集任务")
            add(
                queue="collections", queue_label="内容采集", row_id=row.id,
                row_platform=row.platform, account_id=row.account_id,
                title=collection_title,
                detail=(f"{row.current_step or '等待执行'} · 已采集 {row.content_count} 个作品 / "
                        f"{row.comment_count} 条评论"),
                status=row.status, created_at=row.created_at,
                scheduled_at=row.started_at, source_tab="collections",
                blocked_reason=row.blocked_reason, blocked_signal=row.blocked_signal,
                next_allowed_at=row.next_allowed_at, error=row.error)

        for row in loaded[PublishTask]:
            add(
                queue="publishes", queue_label="内容发布", row_id=row.id,
                row_platform=row.platform, account_id=row.account_id,
                title=row.title or row.desc or f"{row.media_type} 发布任务",
                detail=row.desc if row.title else (row.topics or row.media_type),
                status=row.status, created_at=row.created_at,
                scheduled_at=row.scheduled_at, source_tab="publish",
                schedule_is_utc=row.scheduled_at_is_utc,
                blocked_reason=row.blocked_reason, blocked_signal=row.blocked_signal,
                next_allowed_at=row.next_allowed_at, error=row.error)

        for row in loaded[CommentTask]:
            target = f"回复 {row.target_nick}" if row.target_nick else f"作品 {row.aweme_id}"
            add(
                queue="comments", queue_label="自动评论", row_id=row.id,
                row_platform=row.platform, account_id=row.account_id,
                title=row.content or "待发送评论", detail=target,
                status=row.status, created_at=row.created_at,
                scheduled_at=row.scheduled_at, source_tab="autocomment",
                blocked_reason=row.blocked_reason, blocked_signal=row.blocked_signal,
                next_allowed_at=row.next_allowed_at, error=row.error)

        action_labels = {"follow": "关注", "unfollow": "取关", "send_dm": "发送私信"}
        for row in loaded[AccountActionTask]:
            action_label = action_labels.get(row.action, row.action or "账号动作")
            target = row.target_nick or row.target_uid or row.target_sec_uid or "目标账号"
            add(
                queue="actions", queue_label="账号动作", row_id=row.id,
                row_platform=row.platform, account_id=row.account_id,
                title=f"{action_label} · {target}", detail=row.content,
                status=row.status, created_at=row.created_at,
                scheduled_at=row.scheduled_at, source_tab="hub",
                blocked_reason=row.blocked_reason, blocked_signal=row.blocked_signal,
                next_allowed_at=row.next_allowed_at, error=row.error)

        for row in loaded[ContentRecord]:
            target = targets.get(row.target_id)
            account_id = target.account_id if target else None
            target_name = ((target.alias or target.nickname or target.keyword)
                           if target else "作品监控")
            add(
                queue="monitor_downloads", queue_label="监控下载", row_id=row.id,
                row_platform=row.platform, account_id=account_id,
                title=row.desc or f"作品 {row.aweme_id}", detail=target_name,
                status=row.download_status, created_at=row.created_at,
                source_tab="monitors", error=row.error)

        for row in loaded[KeywordCollectionContent]:
            job = jobs.get(row.job_id)
            add(
                queue="collection_downloads", queue_label="采集下载", row_id=row.id,
                row_platform=row.platform,
                account_id=job.account_id if job else None,
                title=row.desc or f"作品 {row.aweme_id}",
                detail=f"关键词：{row.keyword}" if row.keyword else "关键词采集媒体",
                status=row.download_status, created_at=row.created_at,
                source_tab="collections", error=row.error)

    if platform:
        items = [item for item in items if item["platform"] == platform]
    if queue_type:
        items = [item for item in items if item["queue_type"] == queue_type]
    if q_folded:
        def matches(item: dict[str, Any]) -> bool:
            haystack = " ".join(str(item.get(key) or "") for key in (
                "queue_label", "title", "detail", "account_name", "status",
                "blocked_reason", "blocked_signal", "error", "platform"))
            return q_folded in haystack.casefold()
        items = [item for item in items if matches(item)]

    summary = {name: 0 for name in (
        "total", "active", "pending", "running", "blocked", "failed", "completed")}
    summary["total"] = len(items)
    for item in items:
        item_state = item["state"]
        summary[item_state] += 1
        if item_state in {"pending", "running", "blocked"}:
            summary["active"] += 1

    if state == "active":
        filtered = [item for item in items if item["state"] in {"pending", "running", "blocked"}]
    elif state == "all":
        filtered = items
    else:
        filtered = [item for item in items if item["state"] == state]

    state_rank = {"running": 0, "blocked": 1, "pending": 2, "failed": 3, "completed": 4}

    def sort_key(item: dict[str, Any]) -> tuple[int, float]:
        timestamp = item.get("scheduled_at") or item.get("created_at") or ""
        try:
            epoch = datetime.fromisoformat(timestamp.rstrip("Z")).timestamp()
        except (TypeError, ValueError):
            epoch = 0.0
        rank = state_rank.get(item["state"], 9)
        if item["state"] in {"failed", "completed"}:
            epoch = -epoch
        return rank, epoch

    filtered.sort(key=sort_key)
    total = len(filtered)
    pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, pages)
    start = (page - 1) * page_size
    return {
        "items": filtered[start:start + page_size],
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": pages,
        "summary": summary,
        "history_limit_per_queue": 1000,
        "generated_at": metadata_service._queue_iso(datetime.utcnow()),
    }


class TaskResolutionIn(BaseModel):
    outcome: str
    note: str = PydanticField(min_length=1, max_length=500)


@router.post("/api/task-queue/{queue_type}/{task_id}/resolve")
async def resolve_task_result(queue_type: str, task_id: int,
                              body: TaskResolutionIn, request: Request):
    """Record a human platform-result check; never execute or retry a write."""
    actor = metadata_service._require_risk_admin(request)
    model = {"publishes": PublishTask, "comments": CommentTask,
             "actions": AccountActionTask}.get(queue_type)
    if model is None or body.outcome not in {"done", "canceled"}:
        raise HTTPException(422, "仅支持将写操作核对为已完成或取消，不会自动重试")
    note = body.note.strip()
    if not note:
        raise HTTPException(422, "请填写平台核对说明")
    with get_session() as session:
        row = session.get(model, task_id)
        if row is None:
            raise HTTPException(404, "任务不存在")
        if row.status != "uncertain":
            raise HTTPException(409, "只有结果待确认的任务可以人工核对")
        account_id, previous_error = row.account_id, row.error
        result = session.exec(update(model).where(
            model.id == task_id, model.status == "uncertain").values(
                status=body.outcome, scheduled_at=None,
                done_at=datetime.utcnow() if body.outcome == "done" else None,
                error="已人工核对：" + note,
                blocked_reason="", blocked_signal="", blocked_operation="",
                blocked_at=None, next_allowed_at=None))
        if result.rowcount != 1:
            raise HTTPException(409, "任务状态已变化，请刷新后再核对")
        session.add(RiskAdminAudit(
            action="task_result_resolved", account_id=account_id, actor=actor,
            detail=json.dumps({"queue_type": queue_type, "task_id": task_id,
                               "outcome": body.outcome, "note": note,
                               "previous_error": previous_error[:1000]}, ensure_ascii=False)))
        session.commit()
    return {"ok": True, "status": body.outcome}


class BatchStopItem(BaseModel):
    queue_type: str
    id: int


class BatchStopIn(BaseModel):
    items: list[BatchStopItem] = PydanticField(default_factory=list)
    account_ids: list[int] = PydanticField(default_factory=list)
    platform: str = ""


@router.post("/api/task-queue/batch-stop")
async def batch_stop_tasks(body: BatchStopIn):
    specs = {"collections": KeywordCollectionJob, "publishes": PublishTask,
             "comments": CommentTask, "actions": AccountActionTask}
    requested = {(item.queue_type, item.id) for item in body.items}
    stopped: list[dict] = []
    skipped: list[dict] = []
    with get_session() as session:
        candidates = []
        if requested:
            for queue_type, task_id in requested:
                model = specs.get(queue_type)
                row = session.get(model, task_id) if model else None
                candidates.append((queue_type, row, task_id))
        else:
            if not body.account_ids and not body.platform:
                raise HTTPException(422, "请选择任务，或指定账号/平台范围")
            for queue_type, model in specs.items():
                stmt = select(model)
                if body.account_ids:
                    stmt = stmt.where(model.account_id.in_(body.account_ids))
                if body.platform:
                    stmt = stmt.where(model.platform == body.platform)
                for row in session.exec(stmt).all():
                    candidates.append((queue_type, row, row.id))
        for queue_type, row, task_id in candidates:
            if row is None:
                skipped.append({"queue_type": queue_type, "id": task_id,
                                "reason": "任务不存在"})
                continue
            if queue_type == "collections" and row.status == "running":
                row.cancel_requested = True
                row.current_step = "正在安全停止"
                live_task = getattr(app_runtime.engine, "_collection_tasks", {}).get(row.id) if app_runtime.engine is not None else None
                if live_task is not None and not live_task.done():
                    live_task.cancel()
            elif row.status in {"pending", "draft", "failed", "partial"}:
                row.status = "canceled"
                if hasattr(row, "cancel_requested"):
                    row.cancel_requested = True
                if hasattr(row, "current_step"):
                    row.current_step = "已取消"
                if hasattr(row, "scheduled_at"):
                    row.scheduled_at = None
                if hasattr(row, "finished_at"):
                    row.finished_at = datetime.utcnow()
            else:
                skipped.append({"queue_type": queue_type, "id": task_id,
                                "reason": f"状态 {row.status} 不支持安全停止"})
                continue
            session.add(row)
            stopped.append({"queue_type": queue_type, "id": task_id,
                            "account_id": row.account_id, "platform": row.platform,
                            "status": row.status})
        session.commit()
    for item in stopped:
        record_account_operation(
            platform=item["platform"], account_id=item["account_id"],
            task_type=item["queue_type"], task_id=item["id"],
            action="batch_stop", status=item["status"], channel="api")
    return {"ok": True, "stopped": stopped, "skipped": skipped,
            "stopped_count": len(stopped), "skipped_count": len(skipped)}
