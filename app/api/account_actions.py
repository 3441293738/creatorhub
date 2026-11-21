"""跨平台账号互动动作（关注、私信、点赞、收藏等）队列管理与审核执行接口。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import select

from app import runtime as app_runtime
from app.db import get_session
from app.models import AccountActionTask, DouyinAccount
from app.services import metadata as metadata_service
from app.submissions import submit_once

router = APIRouter()


# ─────────── 本账号管理:写操作队列(取关/回关/发私信)───────────
class ActionIn(BaseModel):
    account_id: int
    action: str                 # follow | unfollow | send_dm
    target_uid: str = ""
    target_sec_uid: str = ""
    target_nick: str = ""
    conv_id: str = ""
    content: str = ""
    run_now: bool = False        # True=立即执行;False=入队(引擎节流后执行)


def _action_dict(t: AccountActionTask) -> dict:
    return {
        "id": t.id, "platform": t.platform, "account_id": t.account_id,
        "action": t.action, "target_uid": t.target_uid, "target_nick": t.target_nick,
        "conv_id": t.conv_id,
        "content": t.content, "status": t.status, "result": t.result,
        "method": t.method,
        "source_msg_id": t.source_msg_id, "source_rule_id": t.source_rule_id,
        "scheduled_at": t.scheduled_at.isoformat() if t.scheduled_at else None,
        "error": t.error, "created_at": t.created_at.isoformat() if t.created_at else None,
        "done_at": t.done_at.isoformat() if t.done_at else None,
    }


async def _exec_action(task_id: int) -> tuple[bool, str]:
    """立即执行一条写操作:委托引擎(带每账号串行锁,避免同号并发开窗)。"""
    if app_runtime.engine is None:
        raise HTTPException(503, "引擎未就绪")
    res = await app_runtime.engine.execute_action_task(task_id)
    return bool(res.get("ok")), (res.get("error") or "")


@router.get("/api/account-actions")
async def list_account_actions(account_id: int | None = None, limit: int = 100):
    with get_session() as s:
        q = select(AccountActionTask)
        if account_id:
            q = q.where(AccountActionTask.account_id == account_id)
        q = q.order_by(AccountActionTask.id.desc()).limit(limit)
        return [_action_dict(t) for t in s.exec(q).all()]


@router.post("/api/account-actions")
async def create_account_action(body: ActionIn, request: Request = None):
    if body.action not in ("follow", "unfollow", "send_dm"):
        raise HTTPException(400, "action 仅支持 follow | unfollow | send_dm")
    if body.action == "send_dm" and not body.content.strip():
        raise HTTPException(400, "发私信需填写内容")
    if not (body.target_uid or body.target_sec_uid):
        raise HTTPException(400, "缺目标用户")
    with get_session() as s:
        def create():
            acc = s.get(DouyinAccount, body.account_id)
            if not acc:
                raise HTTPException(404, "账号不存在")
            t = AccountActionTask(
                platform=acc.platform, account_id=body.account_id, action=body.action,
                target_uid=body.target_uid, target_sec_uid=body.target_sec_uid,
                target_nick=body.target_nick, conv_id=body.conv_id,
                content=body.content.strip(), status="pending")
            s.add(t); s.flush()
            return {"ok": True, "id": t.id, "ran": False}
        payload, created = submit_once(s, request=request, scope="account-action",
                                       body=body, create=create)
    # The durable receipt already exists before any external write. An HTTP
    # retry returns that task and never executes its run_now a second time.
    detail = ""
    if created and body.run_now:
        try:
            _, detail = await _exec_action(payload["id"])
        except HTTPException as exc:
            detail = str(exc.detail)
    with get_session() as s:
        task = s.get(AccountActionTask, payload["id"])
        payload.update(status=task.status if task else "deleted",
                       ran=bool(task and task.status == "done"),
                       method=(task.method if task else ""),
                       execution_error=detail or (task.error if task else ""))
    return payload


@router.post("/api/account-actions/{task_id}/run-now")
async def run_account_action(task_id: int):
    ok, detail = await _exec_action(task_id)
    if not ok:
        raise HTTPException(400, f"执行失败:{detail}")
    with get_session() as session:
        task = session.get(AccountActionTask, task_id)
        return {"ok": True, "method": task.method if task else ""}


@router.put("/api/account-actions/{task_id}")
async def edit_account_action(task_id: int, body: metadata_service.DmActionEditIn):
    content = " ".join(str(body.content or "").strip().split())[:500]
    if not content:
        raise HTTPException(400, "私信内容为空")
    with get_session() as session:
        task = session.get(AccountActionTask, task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        if task.action != "send_dm" or task.status not in {"draft", "pending"}:
            raise HTTPException(400, "仅可编辑未执行的私信任务")
        task.content = content
        session.add(task); session.commit(); session.refresh(task)
        return _action_dict(task)


@router.post("/api/account-actions/{task_id}/approve")
async def approve_account_action(task_id: int):
    with get_session() as session:
        task = session.get(AccountActionTask, task_id)
        if not task:
            raise HTTPException(404, "任务不存在")
        if task.status != "draft":
            raise HTTPException(400, "该任务不是待审核草稿")
        task.status = "pending"
        task.scheduled_at = max(task.scheduled_at or datetime.utcnow(), datetime.utcnow())
        session.add(task); session.commit(); session.refresh(task)
        return _action_dict(task)


@router.post("/api/account-actions/{task_id}/cancel")
async def cancel_account_action(task_id: int):
    with get_session() as s:
        t = s.get(AccountActionTask, task_id)
        if not t:
            raise HTTPException(404, "任务不存在")
        if t.status in ("done", "doing"):
            raise HTTPException(400, "该任务已执行,无法取消")
        t.status = "canceled"; s.add(t); s.commit()
    return {"ok": True}
