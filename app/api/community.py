"""社群成员快照采集、群变动事件查询与直播线索管理接口。"""
from __future__ import annotations

from datetime import datetime
import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field as PydanticField
from sqlmodel import select

from app.db import get_session
from app.event_center import emit_event
from app.group_snapshots import ingest_group_snapshot
from app.models import DouyinAccount, GroupConversation, GroupMember, GroupMemberEvent, LiveLead
from app.operations import record_account_operation
from app.services import collections as collections_service
from app.services import metadata as metadata_service

router = APIRouter()


class GroupMemberIn(BaseModel):
    member_id: str = ""
    user_id: str = ""
    uid: str = ""
    nickname: str = ""
    avatar: str = ""
    role: str = "member"


class GroupSnapshotIn(BaseModel):
    platform: str
    account_id: int
    group_id: str
    group_name: str = ""
    members: list[GroupMemberIn] = PydanticField(default_factory=list)


@router.post("/api/group-snapshots")
async def create_group_snapshot(body: GroupSnapshotIn):
    platform = body.platform.strip().lower()
    group_id = body.group_id.strip()
    if platform not in {"douyin", "xhs", "kuaishou"} or not group_id:
        raise HTTPException(422, "平台或群 ID 无效")
    with get_session() as session:
        account = session.get(DouyinAccount, body.account_id)
        if not account or account.platform != platform:
            raise HTTPException(400, "账号不存在或与平台不匹配")
    try:
        result = ingest_group_snapshot(
            platform=platform, account_id=body.account_id,
            group_id=group_id, group_name=body.group_name.strip(),
            members=[value.model_dump() for value in body.members])
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    record_account_operation(
        platform=platform, account_id=body.account_id,
        task_type="group_snapshot", task_id=result["snapshot_id"],
        action="snapshot", status="done", target=body.group_name or group_id,
        channel="api", detail=(f"成员 {result['member_count']}，新增 {len(result['joined'])}，"
                               f"退出 {len(result['left'])}，变化 {len(result['changed'])}"))
    return result


@router.get("/api/groups")
async def list_groups(account_id: int | None = None, platform: str = ""):
    with get_session() as session:
        stmt = select(GroupConversation)
        if account_id is not None:
            stmt = stmt.where(GroupConversation.account_id == account_id)
        if platform:
            stmt = stmt.where(GroupConversation.platform == platform)
        rows = session.exec(stmt.order_by(GroupConversation.last_snapshot_at.desc())).all()
    return [{"id": row.id, "platform": row.platform, "account_id": row.account_id,
             "group_id": row.group_id, "name": row.name,
             "member_count": row.member_count,
             "last_snapshot_at": metadata_service._queue_iso(row.last_snapshot_at)} for row in rows]


@router.get("/api/groups/{group_id}/members")
async def list_group_members(group_id: str, account_id: int,
                             active_only: bool = True):
    with get_session() as session:
        stmt = select(GroupMember).where(
            GroupMember.group_id == group_id,
            GroupMember.account_id == account_id)
        if active_only:
            stmt = stmt.where(GroupMember.active == True)  # noqa:E712
        rows = session.exec(stmt.order_by(GroupMember.nickname, GroupMember.member_id)).all()
    return [{"member_id": row.member_id, "nickname": row.nickname,
             "avatar": row.avatar, "role": row.role, "active": row.active,
             "first_seen_at": metadata_service._queue_iso(row.first_seen_at),
             "last_seen_at": metadata_service._queue_iso(row.last_seen_at),
             "left_at": metadata_service._queue_iso(row.left_at)} for row in rows]


@router.get("/api/groups/{group_id}/events")
async def list_group_member_events(group_id: str, account_id: int, limit: int = 200):
    with get_session() as session:
        rows = session.exec(select(GroupMemberEvent).where(
            GroupMemberEvent.group_id == group_id,
            GroupMemberEvent.account_id == account_id).order_by(
                GroupMemberEvent.created_at.desc()).limit(max(1, min(limit, 1000)))).all()
    return [{"id": row.id, "snapshot_id": row.snapshot_id,
             "member_id": row.member_id, "change_type": row.change_type,
             "before": json.loads(row.before_json or "{}"),
             "after": json.loads(row.after_json or "{}"),
             "created_at": metadata_service._queue_iso(row.created_at)} for row in rows]


class LiveLeadIn(BaseModel):
    platform: str = "douyin"
    account_id: int
    room_id: str
    user_id: str
    nickname: str = ""
    source_event: str = "comment"
    content: str = ""
    tags: list[str] = PydanticField(default_factory=list)
    score: int = 0
    occurred_at: datetime | None = None


class LiveLeadUpdate(BaseModel):
    status: str | None = None
    assignee: str | None = None
    note: str | None = None
    tags: list[str] | None = None
    score: int | None = None


@router.post("/api/live-leads")
async def create_live_lead(body: LiveLeadIn):
    if not body.room_id.strip() or not body.user_id.strip():
        raise HTTPException(422, "直播间 ID 和用户 ID 必填")
    with get_session() as session:
        account = session.get(DouyinAccount, body.account_id)
        if not account or account.platform != body.platform:
            raise HTTPException(400, "账号不存在或与平台不匹配")
        row = LiveLead(
            platform=body.platform, account_id=body.account_id,
            room_id=body.room_id.strip(), user_id=body.user_id.strip(),
            nickname=body.nickname.strip(), source_event=body.source_event.strip(),
            content=body.content.strip(),
            tags=json.dumps(collections_service._collection_keywords(body.tags), ensure_ascii=False),
            score=max(0, min(int(body.score or 0), 100)),
            occurred_at=body.occurred_at)
        session.add(row); session.commit(); session.refresh(row)
        lead_id = row.id
    emit_event("live.lead.created", {"lead_id": lead_id, **body.model_dump(mode="json")},
               aggregate_type="live_lead", aggregate_id=str(lead_id))
    return {"id": lead_id, "status": "new"}


@router.get("/api/live-leads")
async def list_live_leads(platform: str = "", account_id: int | None = None,
                          status: str = "", limit: int = 200):
    with get_session() as session:
        stmt = select(LiveLead)
        if platform:
            stmt = stmt.where(LiveLead.platform == platform)
        if account_id is not None:
            stmt = stmt.where(LiveLead.account_id == account_id)
        if status:
            stmt = stmt.where(LiveLead.status == status)
        rows = session.exec(stmt.order_by(
            LiveLead.created_at.desc()).limit(max(1, min(limit, 1000)))).all()
    return [{"id": row.id, "platform": row.platform, "account_id": row.account_id,
             "room_id": row.room_id, "user_id": row.user_id,
             "nickname": row.nickname, "source_event": row.source_event,
             "content": row.content, "tags": json.loads(row.tags or "[]"),
             "score": row.score, "status": row.status, "assignee": row.assignee,
             "note": row.note, "occurred_at": metadata_service._queue_iso(row.occurred_at),
             "updated_at": metadata_service._queue_iso(row.updated_at)} for row in rows]


@router.patch("/api/live-leads/{lead_id}")
async def update_live_lead(lead_id: int, body: LiveLeadUpdate):
    with get_session() as session:
        row = session.get(LiveLead, lead_id)
        if not row:
            raise HTTPException(404, "直播线索不存在")
        if body.status is not None:
            if body.status not in {"new", "following", "won", "lost"}:
                raise HTTPException(422, "线索状态无效")
            row.status = body.status
        if body.assignee is not None:
            row.assignee = body.assignee.strip()[:120]
        if body.note is not None:
            row.note = body.note.strip()[:2000]
        if body.tags is not None:
            row.tags = json.dumps(collections_service._collection_keywords(body.tags), ensure_ascii=False)
        if body.score is not None:
            row.score = max(0, min(int(body.score), 100))
        row.updated_at = datetime.utcnow()
        session.add(row); session.commit(); session.refresh(row)
        status = row.status
    emit_event("live.lead.updated", {"lead_id": lead_id, "status": status},
               aggregate_type="live_lead", aggregate_id=str(lead_id))
    return {"ok": True, "id": lead_id, "status": status}
