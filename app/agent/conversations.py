"""Server-owned conversation ancestry; no client-supplied message history."""
from __future__ import annotations

import hashlib
import json

from fastapi import HTTPException

from ..models import AgentRun
from .privacy import redact

HISTORY_TURNS = 8
HISTORY_CHARS = 20000


def context_key(body, cfg, channel_id: str) -> str:
    # Model switches on the same connection are explicit and may keep context.
    # A different destination, account, platform, task or mode never inherits it.
    scope = {"platform": body.platform, "account_id": body.account_id, "mode": body.mode,
             "task_type": body.task_type if body.mode == "diagnose" else None,
             "task_id": body.task_id if body.mode == "diagnose" else None,
             "channel_id": channel_id, "provider": cfg.provider, "base_url": cfg.base_url,
             "api_path_mode": cfg.api_path_mode,
             "credential_digest": hashlib.sha256(cfg.api_key.encode()).hexdigest()}
    return hashlib.sha256(json.dumps(scope, sort_keys=True).encode()).hexdigest()


def ancestry(session, run_id: str, *, limit: int, expected_key: str | None = None):
    rows, seen = [], set()
    current = run_id
    while current and len(rows) < limit:
        if current in seen:
            raise HTTPException(409, "对话链异常，请新建对话")
        seen.add(current)
        row = session.get(AgentRun, current)
        if row is None:
            raise HTTPException(404, "对话记录不存在，请新建对话")
        if expected_key is not None and row.context_key != expected_key:
            raise HTTPException(409, "分析范围、任务或渠道已变化，请新建对话后发送；原对话仍保留")
        rows.append(row)
        current = row.parent_run_id
    return list(reversed(rows)), bool(current)


def history_context(rows, *, more: bool, secrets=()) -> dict:
    records, size = [], 0
    truncated = more
    for row in reversed(rows):
        question = redact(row.prompt, secrets)
        answer = redact(row.answer, secrets) if row.status == "completed" else ""
        record = {"run_id": row.id, "question": question[:2000], "answer": answer[:6000],
                  "status": row.status, "error": redact(row.error, secrets)[:500]}
        cost = len(json.dumps(record, ensure_ascii=False))
        if size + cost > HISTORY_CHARS:
            truncated = True
            break
        truncated |= len(question) > 2000 or len(answer) > 6000
        records.append(record); size += cost
    return {"turns": list(reversed(records)), "truncated": bool(truncated),
            "note": "仅为过去的对话，不是当前证据；旧证据编号只属于对应 run_id。工具原始输出和旧审批不会重放。"}
