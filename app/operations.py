"""Account-level operation journal shared by API and background workers."""
from __future__ import annotations

import logging

from .db import get_session
from .event_center import emit_event
from .models import AccountOperationLog

_op_log = logging.getLogger("creatorhub.operation")


def record_account_operation(*, platform: str, account_id: int,
                             task_type: str, task_id, action: str,
                             status: str, target: str = "", channel: str = "",
                             risk_signal: str = "", detail: str = "",
                             duration_ms: int = 0) -> int:
    with get_session() as session:
        row = AccountOperationLog(
            platform=platform, account_id=account_id,
            task_type=task_type, task_id=str(task_id or ""), action=action,
            target=str(target or "")[:500], channel=str(channel or "")[:100],
            status=status, risk_signal=str(risk_signal or "")[:160],
            detail=str(detail or "")[:3000], duration_ms=max(0, int(duration_ms or 0)),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        row_id = int(row.id)
    if status in {"failed", "error", "blocked", "uncertain"}:
        _op_log.warning(
            "操作异常 [%s/%s] 账号#%s 任务=%s 通道=%s 状态=%s%s%s",
            platform,
            task_type,
            account_id,
            task_id or "-",
            channel or "-",
            status,
            f" 信号={risk_signal}" if risk_signal else "",
            f" | {str(detail)[:240]}" if detail else "",
        )
    emit_event(
        "account.operation",
        {"operation_id": row_id, "platform": platform, "account_id": account_id,
         "task_type": task_type, "task_id": str(task_id or ""), "action": action,
         "status": status, "target": str(target or ""),
         "risk_signal": str(risk_signal or ""), "detail": str(detail or "")},
        aggregate_type="account", aggregate_id=str(account_id),
    )
    return row_id
