"""总览统计、工作台聚合视图、聚合私信收件箱与浏览器会话网格接口。"""
from __future__ import annotations

from datetime import datetime
import json

from fastapi import APIRouter, HTTPException
from sqlalchemy import func
from sqlmodel import select

from app.db import get_session
from app.models import AccountActionTask, AccountOperationLog, AccountWork, CommentRecord, CommentTask, ContentRecord, DmConversation, DouyinAccount, FollowEdge, KeywordCollectionJob, MonitorTarget, PublishTask
from app.services import login_sessions as login_sessions_service
from app.services import metadata as metadata_service

router = APIRouter()


@router.get("/api/overview/summary")
async def overview_summary(platform: str = "douyin"):
    if platform not in {"douyin", "xhs", "rednote", "kuaishou", "shipinhao", "tiktok", "bilibili", "instagram", "twitter"}:
        raise HTTPException(422, "平台类型无效")
    with get_session() as session:
        def count(model, *conditions):
            return int(session.exec(select(func.count()).select_from(model).where(
                model.platform == platform, *conditions)).one())
        return {
            "platform": platform,
            "accounts": count(DouyinAccount),
            "monitors": count(MonitorTarget, MonitorTarget.enabled == True),  # noqa:E712
            "downloaded": count(ContentRecord, ContentRecord.download_status == "done"),
            "comments": count(CommentRecord),
        }


# ─────────── 本账号管理:计数汇总(账号管理面板徽章,纯查库不触发抓取)───────────
@router.get("/api/hub/summary")
async def hub_summary(account_id: int):
    with get_session() as s:
        def _n(q):
            return len(s.exec(q).all())
        return {
            "works": _n(select(AccountWork.id)
                        .where(AccountWork.account_id == account_id)),
            "following": _n(select(FollowEdge.id)
                            .where(FollowEdge.account_id == account_id,
                                   FollowEdge.direction == "following")),
            "fans": _n(select(FollowEdge.id)
                       .where(FollowEdge.account_id == account_id,
                              FollowEdge.direction == "fan")),
            "dm": _n(select(DmConversation.id)
                     .where(DmConversation.account_id == account_id)),
        }


# ─────────── 统一运营工作台 / 事件中心 ───────────
@router.get("/api/account-operations")
async def list_account_operations(account_id: int | None = None,
                                  platform: str = "", task_type: str = "",
                                  limit: int = 200):
    limit = max(1, min(int(limit or 200), 1000))
    with get_session() as session:
        stmt = select(AccountOperationLog)
        if account_id is not None:
            stmt = stmt.where(AccountOperationLog.account_id == account_id)
        if platform:
            stmt = stmt.where(AccountOperationLog.platform == platform)
        if task_type:
            stmt = stmt.where(AccountOperationLog.task_type == task_type)
        rows = session.exec(stmt.order_by(
            AccountOperationLog.created_at.desc()).limit(limit)).all()
    return [{
        "id": row.id, "platform": row.platform, "account_id": row.account_id,
        "task_type": row.task_type, "task_id": row.task_id,
        "action": row.action, "target": row.target, "channel": row.channel,
        "status": row.status, "risk_signal": row.risk_signal,
        "detail": row.detail, "duration_ms": row.duration_ms,
        "created_at": metadata_service._queue_iso(row.created_at),
    } for row in rows]


@router.get("/api/accounts/workbench")
async def accounts_workbench(platform: str = ""):
    with get_session() as session:
        stmt = select(DouyinAccount)
        if platform:
            stmt = stmt.where(DouyinAccount.platform == platform)
        accounts = session.exec(stmt.order_by(DouyinAccount.id)).all()
        account_ids = [account.id for account in accounts]
        rollups = {account_id: {
            "total": 0, "pending": 0, "running": 0,
            "blocked": 0, "failed": 0} for account_id in account_ids}
        task_specs = (
            (KeywordCollectionJob, {"pending"}, {"running"}),
            (PublishTask, {"draft", "pending"}, {"publishing"}),
            (CommentTask, {"draft", "pending"}, {"doing"}),
            (AccountActionTask, {"draft", "pending"}, {"doing"}),
        )
        for model, pending_states, running_states in task_specs:
            rows = session.exec(select(model).where(
                model.account_id.in_(account_ids),
                model.status.in_(list(pending_states | running_states | metadata_service._QUEUE_FAILED)))).all()
            for row in rows:
                value = rollups[row.account_id]
                value["total"] += 1
                if row.status in metadata_service._QUEUE_FAILED:
                    value["failed"] += 1
                elif (getattr(row, "blocked_reason", "")
                      or (getattr(row, "next_allowed_at", None)
                          and row.next_allowed_at > datetime.utcnow())):
                    value["blocked"] += 1
                elif row.status in running_states:
                    value["running"] += 1
                else:
                    value["pending"] += 1
        latest: dict[int, AccountOperationLog] = {}
        for row in session.exec(select(AccountOperationLog).order_by(
                AccountOperationLog.created_at.desc()).limit(2000)).all():
            latest.setdefault(row.account_id, row)
        unread = {account.id: int(session.exec(select(func.sum(
            DmConversation.unread_count)).where(
                DmConversation.account_id == account.id)).one() or 0)
                  for account in accounts}
    open_ids = set(login_sessions_service._active_open_browser_account_ids())
    return [{
        "account_id": account.id, "platform": account.platform,
        "nickname": account.nickname, "avatar": account.avatar,
        "status": account.status, "proxy_status": account.proxy_status,
        "browser_open": account.id in open_ids,
        "unread_dm": unread.get(account.id, 0),
        "tasks": rollups.get(account.id, {
            "total": 0, "pending": 0, "running": 0, "blocked": 0, "failed": 0}),
        "last_operation": ({
            "action": latest[account.id].action,
            "status": latest[account.id].status,
            "detail": latest[account.id].detail,
            "created_at": metadata_service._queue_iso(latest[account.id].created_at),
        } if account.id in latest else None),
    } for account in accounts]


@router.get("/api/dm/inbox")
async def aggregate_dm_inbox(platform: str = "", unread_only: bool = False,
                             limit: int = 200):
    with get_session() as session:
        stmt = select(DmConversation)
        if platform:
            stmt = stmt.where(DmConversation.platform == platform)
        if unread_only:
            stmt = stmt.where(DmConversation.unread_count > 0)
        rows = session.exec(stmt.order_by(
            DmConversation.last_time.desc()).limit(max(1, min(limit, 1000)))).all()
        accounts = {row.id: row for row in session.exec(select(DouyinAccount)).all()}
    items = []
    for row in rows:
        try:
            raw = json.loads(row.raw_json or "{}")
        except (TypeError, ValueError):
            raw = {}
        account = accounts.get(row.account_id)
        items.append({
            "id": row.id, "platform": row.platform, "account_id": row.account_id,
            "account_name": account.nickname if account else "",
            "conv_id": row.conv_id, "peer_uid": row.peer_uid,
            "peer_nickname": row.peer_nickname, "peer_avatar": row.peer_avatar,
            "last_text": row.last_text, "last_time": row.last_time,
            "unread_count": row.unread_count,
            "group_chat": bool(raw.get("group_chat") or raw.get("conversation_type") == "group"),
        })
    return {"items": items, "unread": sum(item["unread_count"] for item in items)}


@router.get("/api/browser-grid")
async def browser_grid():
    open_ids = set(login_sessions_service._active_open_browser_account_ids())
    with get_session() as session:
        accounts = session.exec(select(DouyinAccount).order_by(DouyinAccount.id)).all()
        running = {row.account_id for row in session.exec(select(KeywordCollectionJob).where(
            KeywordCollectionJob.status == "running")).all()}
    return [{"account_id": row.id, "platform": row.platform,
             "nickname": row.nickname, "avatar": row.avatar,
             "account_status": row.status, "proxy_status": row.proxy_status,
             "browser_status": ("open" if row.id in open_ids else
                                "worker" if row.id in running else "closed"),
             "open_url": f"/api/accounts/{row.id}/open-browser"} for row in accounts]
