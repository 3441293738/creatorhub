"""Durable idempotency for local queue creation, not platform-write retries."""
from __future__ import annotations

import hashlib
import json
import re

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from .models import (TaskSubmission, PublishTask, AccountActionTask, CommentRule,
                     DmAutoReplyRule, KeywordCollectionJob)

_MODELS = {"publish": PublishTask, "repost": PublishTask,
           "account-action": AccountActionTask, "comment-rule": CommentRule,
           "dm-rule": DmAutoReplyRule, "collection": KeywordCollectionJob}


def _identity(request, scope, body):
    key = request.headers.get("idempotency-key", "") if request else ""
    if not key:
        return None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{15,127}", key):
        raise HTTPException(422, "Idempotency-Key 须为 16–128 位字母、数字或 . _ : -")
    digest = hashlib.sha256(f"{scope}\0{key}".encode()).hexdigest()
    values = body.model_dump(mode="json") if hasattr(body, "model_dump") else body
    payload_hash = hashlib.sha256(json.dumps(
        values, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        allow_nan=False).encode()).hexdigest()
    return digest, payload_hash


def _replay(session, row, payload_hash):
    if row.payload_hash != payload_hash:
        raise HTTPException(409, "该提交编号已用于其他内容，请核对原任务后新建提交")
    if row.resource_table:
        model = next((model for model in _MODELS.values()
                      if model.__tablename__ == row.resource_table), None)
        resource = session.get(model, row.resource_id) if model else None
        if resource is None or resource.created_at != row.resource_created_at:
            raise HTTPException(410, "原提交对应的记录已删除，请核对列表；再次点击提交将新建任务")
    return {**json.loads(row.response_json), "replayed": True}


def replay_if_exists(session, *, request, scope, body):
    identity = _identity(request, scope, body)
    if identity:
        digest, payload_hash = identity
        existing = session.get(TaskSubmission, digest)
        if existing:
            return _replay(session, existing, payload_hash)
    return None


def submit_once(session, *, request, scope: str, body, create):
    """Return (receipt, created). The callback must not commit or call a platform.

    Existing API clients may omit the key. UI clients reuse one key until a
    submission has a confirmed result. Different intentional submissions use
    new keys, even when the content is identical. Receipts are not auto-expired.
    """
    identity = _identity(request, scope, body)
    if identity is None:
        payload = create()
        session.commit()
        return payload, True
    digest, payload_hash = identity
    existing = session.get(TaskSubmission, digest)
    if existing:
        return _replay(session, existing, payload_hash), False
    receipt = TaskSubmission(digest=digest, scope=scope, payload_hash=payload_hash)
    session.add(receipt)
    try:
        # UNIQUE arbitration occurs before creating the task. A loser waits for
        # the winner's entire transaction and then reads its durable receipt.
        session.flush()
    except IntegrityError:
        session.rollback()
        existing = session.get(TaskSubmission, digest)
        if existing is None:
            raise
        return _replay(session, existing, payload_hash), False
    payload = create()
    model = _MODELS.get(scope.split(":", 1)[0])
    if model is not None:
        resource = session.get(model, payload.get("id", payload.get("task_id")))
        if resource is not None:
            receipt.resource_table = model.__tablename__
            receipt.resource_id = resource.id
            receipt.resource_created_at = resource.created_at
    receipt.response_json = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    session.add(receipt)
    session.commit()
    return payload, True
