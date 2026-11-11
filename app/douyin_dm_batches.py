"""Text DM composition and durable batches over the existing action queue."""
import json
import re
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlmodel import select

from .db import get_session
from .models import DouyinAccount, DmConversation, AccountActionTask, TaskSubmission
from .submissions import submit_once

router = APIRouter(prefix="/api/douyin/dm")


class PreviewIn(BaseModel):
    account_id: int
    links: list[str] = Field(default_factory=list, max_length=200)
    contact_ids: list[int] = Field(default_factory=list, max_length=200)


class Recipient(BaseModel):
    target_uid: str = ""
    target_sec_uid: str = ""
    target_nick: str = ""
    conv_id: str = ""


class BatchIn(BaseModel):
    account_id: int
    recipients: list[Recipient] = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=500)


def account_for_send(session, account_id):
    account = session.get(DouyinAccount, account_id)
    if not account or account.platform != "douyin":
        raise HTTPException(404, "抖音账号不存在")
    if account.status != "active":
        raise HTTPException(400, "账号登录已失效，请重新登录")
    return account


def deduplicate(recipients):
    groups = []
    for row in recipients:
        if not (row.target_uid or row.target_sec_uid):
            raise HTTPException(422, "接收人缺少用户标识")
        aliases = {(key, value) for key, value in (
            ("uid", row.target_uid), ("sec", row.target_sec_uid), ("conv", row.conv_id)) if value}
        matches = [group for group in groups if group[0] & aliases]
        combined = row.model_copy()
        for group in matches:
            aliases |= group[0]
            prior = group[1]
            for field in Recipient.model_fields:
                if getattr(prior, field):
                    setattr(combined, field, getattr(prior, field))
            groups.remove(group)
        groups.append((aliases, combined))
    return [group[1] for group in groups]



async def resolve_profile(account, link):
    # Only canonical user pages: no arbitrary URL fetches or redirect following.
    parsed = urlsplit(link.strip())
    if (parsed.scheme not in {"https", "http"}
            or parsed.hostname not in {"douyin.com", "www.douyin.com"}
            or not re.fullmatch(r"/user/[A-Za-z0-9_-]+/?", parsed.path)):
        raise ValueError("请输入完整抖音用户主页链接 https://www.douyin.com/user/…")
    from . import main
    cookie = main.douyin_cookie_from_state(
        account.storage_state or account.creator_storage_state or "")
    if not cookie:
        raise ValueError("账号缺少可用登录态，请重新登录")
    client = main.DouyinClient(cookie, account.ua or main.cfg.engine.user_agent,
        timeout=main.cfg.engine.request_timeout_seconds, proxy=account.proxy or "",
        **main.douyin_client_environment(account))
    async def read():
        async with client.session_scope():
            profile, error = await client.resolve_user_identifier(parsed.path.strip("/").split("/")[-1])
            return profile, error
    if main.engine is None:
        raise ValueError("引擎未就绪")
    profile, error = await main.engine.guarded_read_pair(
        account.id, main.OperationKind.READ_HEAVY, f"dm-preview:{account.id}", read, empty_result=None)
    if not profile:
        raise ValueError(error or "无法解析接收人")
    return Recipient(target_uid=str(profile.get("uid") or profile.get("user_id") or ""),
        target_sec_uid=str(profile.get("sec_uid") or ""),
        target_nick=str(profile.get("nickname") or ""))


@router.post("/preview")
async def preview(body: PreviewIn):
    rows, invalid = [], []
    with get_session() as session:
        account = account_for_send(session, body.account_id)
        session.expunge(account)
        for contact_id in body.contact_ids:
            row = session.get(DmConversation, contact_id)
            if not row or row.account_id != body.account_id or row.platform != "douyin":
                invalid.append({"input": str(contact_id), "error": "联系人不存在或不属于所选账号"})
                continue
            rows.append(Recipient(target_uid=row.peer_uid, target_sec_uid=row.peer_sec_uid,
                target_nick=row.peer_nickname, conv_id=row.conv_id))
    for link in body.links:
        try:
            rows.append(await resolve_profile(account, link))
        except Exception as exc:
            invalid.append({"input": link, "error": str(exc)})
    valid = []
    for row in rows:
        if row.target_uid or row.target_sec_uid:
            valid.append(row)
        else:
            invalid.append({"input": row.target_nick, "error": "接收人缺少用户标识"})
    unique = deduplicate(valid)
    return {"recipients": [r.model_dump() for r in unique], "invalid": invalid,
            "duplicates": len(valid) - len(unique)}


def batch_view(session, receipt):
    from .main import _action_dict
    payload = json.loads(receipt.response_json)
    tasks = [session.get(AccountActionTask, task_id) for task_id in payload["task_ids"]]
    counts = dict.fromkeys(["pending", "doing", "done", "failed", "uncertain", "canceled"], 0)
    details = []
    for task_id, task in zip(payload["task_ids"], tasks):
        if task is None:
            counts["failed"] += 1
            details.append({"id": task_id, "status": "failed", "error": "任务记录已删除"})
        else:
            counts["pending" if task.status == "draft" else task.status] += 1
            detail = _action_dict(task)
            detail.update(blocked_reason=task.blocked_reason, next_allowed_at=task.next_allowed_at)
            details.append(detail)
    return {**payload, "total": len(tasks), "counts": counts, "tasks": details}


def get_receipt(session, batch_id):
    row = session.exec(select(TaskSubmission).where(TaskSubmission.scope == "douyin-dm-batch",
        TaskSubmission.digest == batch_id)).first()
    if row is None:
        raise HTTPException(404, "批次不存在")
    return row


@router.post("/batches")
async def create_batch(body: BatchIn, request: Request):
    if not request.headers.get("idempotency-key"):
        raise HTTPException(422, "必须提供 Idempotency-Key，重试请复用原值")
    if not body.content.strip():
        raise HTTPException(422, "私信内容不能为空")
    recipients = deduplicate(body.recipients)
    from .submissions import _identity
    batch_id = _identity(request, "douyin-dm-batch", body)[0]
    with get_session() as session:
        def create():
            account_for_send(session, body.account_id)
            task_ids = []
            for recipient in recipients:
                if recipient.conv_id:
                    conversation = session.exec(select(DmConversation).where(
                        DmConversation.account_id == body.account_id,
                        DmConversation.conv_id == recipient.conv_id,
                        DmConversation.platform == "douyin")).first()
                    if conversation is None or not (
                        (recipient.target_uid and recipient.target_uid == conversation.peer_uid)
                        or (recipient.target_sec_uid and recipient.target_sec_uid == conversation.peer_sec_uid)):
                        raise HTTPException(422, "会话与接收人或账号不匹配，请重新预览")
                task = AccountActionTask(platform="douyin", account_id=body.account_id,
                    action="send_dm", content=body.content.strip(), **recipient.model_dump())
                session.add(task); session.flush()
                task_ids.append(task.id)
            return {"batch_id": batch_id, "account_id": body.account_id,
                    "content": body.content.strip(), "task_ids": task_ids}
        payload, created = submit_once(session, request=request, scope="douyin-dm-batch",
                                      body=body, create=create)
        return {**batch_view(session, get_receipt(session, batch_id)), "replayed": not created}


@router.get("/batches")
async def list_batches(account_id: int):
    with get_session() as session:
        receipts = session.exec(select(TaskSubmission).where(
            TaskSubmission.scope == "douyin-dm-batch").order_by(TaskSubmission.created_at.desc())).all()
        return [batch_view(session, row) for row in receipts
                if json.loads(row.response_json)["account_id"] == account_id]


@router.get("/batches/{batch_id}")
async def get_batch(batch_id: str):
    with get_session() as session:
        return batch_view(session, get_receipt(session, batch_id))


@router.post("/batches/{batch_id}/cancel")
async def cancel_batch(batch_id: str):
    with get_session() as session:
        receipt = get_receipt(session, batch_id)
        ids = json.loads(receipt.response_json)["task_ids"]
        session.exec(update(AccountActionTask).where(AccountActionTask.id.in_(ids),
            AccountActionTask.status.in_(["draft", "pending"])).values(status="canceled"))
        session.commit()
        return batch_view(session, receipt)
