"""风控中心规则配置、账号风险状态汇总、人工验证解除与审计日志接口。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict
import json
import os

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlmodel import select

from app import runtime as app_runtime
from app.api import accounts as accounts_api
from app.db import get_session
from app.models import AccountActionTask, AccountRiskState, CommentTask, DouyinAccount, KeywordCollectionJob, PublishTask, RiskAdminAudit, RiskEvent
from app.risk import RiskCategory, RiskController
from app.risk_admin import RiskSettingsError, apply_risk_settings, export_risk_settings, save_risk_settings
from app.services import metadata as metadata_service
from app.services import proxies as proxies_service

router = APIRouter()


# ─────────── 风控中心 ───────────
def _risk_iso(value: datetime | None) -> str | None:
    return value.isoformat(timespec="seconds") + "Z" if value else None


def _risk_admin_required() -> bool:
    return bool(os.environ.get("CREATORHUB_ADMIN_TOKEN", "").strip())


def _risk_config_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    changes = {}
    for section in ("risk_control", "schedule"):
        old, new = before.get(section, {}), after.get(section, {})
        section_changes = {
            key: {"before": old.get(key), "after": new.get(key)}
            for key in sorted(set(old) | set(new)) if old.get(key) != new.get(key)
        }
        if section_changes:
            changes[section] = section_changes
    return changes


def _record_risk_admin_audit(action: str, *, actor: str,
                             account_id: int | None = None,
                             detail: dict[str, Any] | None = None) -> None:
    with get_session() as session:
        session.add(RiskAdminAudit(
            action=action, account_id=account_id, actor=actor,
            detail=json.dumps(detail or {}, ensure_ascii=False)[:8000],
        ))
        session.commit()


def _risk_task_rollups(session, account_ids: list[int]) -> dict[int, dict[str, Any]]:
    result = {account_id: {
        "collections": 0, "publishes": 0, "comments": 0, "actions": 0,
        "total": 0, "blocked": 0, "blocked_signals": {},
        "next_allowed_at": None, "latest_block_reason": "",
        "latest_blocked_at": None,
    } for account_id in account_ids}
    if not account_ids:
        return result
    specs = (
        (KeywordCollectionJob, "collections", ["pending", "running"]),
        (PublishTask, "publishes", ["pending", "publishing"]),
        (CommentTask, "comments", ["pending", "doing"]),
        (AccountActionTask, "actions", ["pending", "doing"]),
    )
    for model, key, statuses in specs:
        rows = session.exec(select(model).where(
            model.account_id.in_(account_ids), model.status.in_(statuses))).all()
        for row in rows:
            item = result[row.account_id]
            item[key] += 1
            item["total"] += 1
            reason = str(getattr(row, "blocked_reason", "") or "")
            if not reason:
                continue
            item["blocked"] += 1
            signal = str(getattr(row, "blocked_signal", "") or "deferred")
            item["blocked_signals"][signal] = item["blocked_signals"].get(signal, 0) + 1
            next_at = getattr(row, "next_allowed_at", None)
            if next_at and (item["next_allowed_at"] is None
                            or next_at < item["next_allowed_at"]):
                item["next_allowed_at"] = next_at
            blocked_at = getattr(row, "blocked_at", None)
            if blocked_at and (item["latest_blocked_at"] is None
                               or blocked_at > item["latest_blocked_at"]):
                item["latest_blocked_at"] = blocked_at
                item["latest_block_reason"] = reason
    return result


def _account_risk_view(account: DouyinAccount, now: datetime, *,
                       state: AccountRiskState | None,
                       latest_event: RiskEvent | None,
                       queued: dict[str, Any]) -> dict[str, Any]:
    risk_level = state.risk_level if state else 0
    reason = state.last_risk_reason if state else ""
    cooldown_until = state.cooldown_until if state else None
    status_code, status_label, status_tone = "normal", "正常", "success"
    if state and state.manual_review_required:
        status_code, status_label, status_tone = "verification_required", "待人工验证", "danger"
        reason = state.manual_review_reason or "请在账号浏览器中处理验证，然后人工解除暂停"
    elif account.status == "invalid":
        status_code, status_label, status_tone = "auth_invalid", "登录失效", "danger"
        reason = "账号登录态已失效，需要重新登录"
    elif account.proxy_status in {"bad", "auth_error", "blocked"}:
        status_code, status_label, status_tone = "proxy_error", "代理异常", "danger"
        reason = account.write_pause_reason or "账号绑定代理不可用"
    elif account.proxy_status in {"rotating", "drifted"}:
        status_code, status_label, status_tone = "proxy_rotation", "代理出口变化", "warn"
        reason = "代理连接正常，但服务商分配了新的出口 IP"
    elif cooldown_until and cooldown_until > now:
        if "出口组熔断" in reason:
            status_code, status_label = "network_circuit", "网络熔断"
        else:
            status_code, status_label = "cooldown", "风险冷却"
        status_tone = "danger"
    elif risk_level > 0:
        status_code, status_label, status_tone = "recovering", "渐进恢复", "warn"
        reason = reason or "冷却已结束，等待轻量探测确认账号恢复"
    elif account.write_paused_until and account.write_paused_until > now:
        status_code, status_label, status_tone = "write_paused", "写入暂停", "warn"
        reason = account.write_pause_reason or "账号写操作暂时停用"
    elif state and state.retry_not_before and state.retry_not_before > now:
        status_code, status_label, status_tone = "network_backoff", "网络退避", "warn"
        reason = "网络异常后等待再尝试"
    elif state and state.session_rest_until and state.session_rest_until > now:
        reason = "连续操作后的正常休息，任务保留在队列中"
    elif state and state.operation_not_before and state.operation_not_before > now:
        reason = "正常操作间隔，任务保留在队列中"
    elif status_code == "normal":
        reason = "未检测到风险信号"

    recovery_successes = state.recovery_successes if state else 0
    recovery_target = max(1, app_runtime.cfg.risk_control.recovery_successes)
    next_probe = None
    if risk_level > 0 and not (state and state.manual_review_required):
        if cooldown_until and cooldown_until > now:
            next_probe = cooldown_until
        elif state and (state.last_recovery_at or state.last_operation_at):
            anchor = max(value for value in (
                state.last_recovery_at, state.last_operation_at) if value is not None)
            next_probe = anchor + timedelta(
                seconds=app_runtime.cfg.risk_control.recovery_probe_gap_seconds)
        else:
            next_probe = now

    if account.douyin_id:
        platform_account_id = account.douyin_id
        platform_account_id_label = {
            "douyin": "抖音号", "xhs": "小红书号", "rednote": "Rednote ID",
            "kuaishou": "快手号", "shipinhao": "视频号",
            "bilibili": "B站 UID", "tiktok": "TikTok",
            "instagram": "Instagram", "twitter": "Twitter @",
        }.get(account.platform, "账号 ID")
    else:
        platform_account_id = account.sec_uid
        platform_account_id_label = {
            "douyin": "sec_uid", "xhs": "user_id", "rednote": "user_id",
            "kuaishou": "user_id", "shipinhao": "finder_id",
            "bilibili": "UID", "instagram": "pk", "twitter": "screen_name",
        }.get(account.platform, "账号 ID")
    return {
        "account_id": account.id,
        "platform_account_id": platform_account_id,
        "platform_account_id_label": platform_account_id_label,
        "nickname": account.nickname,
        "platform": account.platform,
        "account_status": account.status,
        "status": status_code,
        "status_label": status_label,
        "status_tone": status_tone,
        "risk_level": risk_level,
        "reason": reason,
        "cooldown_until": _risk_iso(cooldown_until),
        "cooldown_remaining_seconds": max(
            0, int((cooldown_until - now).total_seconds()))
            if cooldown_until else 0,
        "next_probe_at": _risk_iso(next_probe),
        "recovery_successes": recovery_successes,
        "recovery_target": recovery_target,
        "last_risk_at": _risk_iso(state.last_risk_at if state else None),
        "last_operation_at": _risk_iso(state.last_operation_at if state else None),
        "manual_review_required": bool(state and state.manual_review_required),
        "retry_not_before": _risk_iso(state.retry_not_before if state else None),
        "operation_not_before": _risk_iso(state.operation_not_before if state else None),
        "session_rest_until": _risk_iso(state.session_rest_until if state else None),
        "session_operation_count": state.session_operation_count if state else 0,
        "last_operation_kind": latest_event.operation_kind if latest_event else "",
        "proxy": proxies_service._mask_proxy(account.proxy),
        "proxy_status": account.proxy_status,
        "network_key": latest_event.network_key if latest_event else "",
        "queued_tasks": {
            key: queued[key] for key in (
                "collections", "publishes", "comments", "actions", "total")
        },
        "blocked_tasks": queued["blocked"],
        "blocked_signals": queued["blocked_signals"],
        "task_next_allowed_at": _risk_iso(queued["next_allowed_at"]),
        "latest_block_reason": queued["latest_block_reason"],
    }


def _risk_account_views(session, accounts: list[DouyinAccount],
                        now: datetime) -> list[dict[str, Any]]:
    ids = [account.id for account in accounts]
    states = {row.account_id: row for row in session.exec(
        select(AccountRiskState).where(AccountRiskState.account_id.in_(ids))
    ).all()} if ids else {}
    latest_events: dict[int, RiskEvent] = {}
    if ids:
        for event in session.exec(select(RiskEvent).where(
                RiskEvent.account_id.in_(ids)).order_by(
                    RiskEvent.occurred_at.desc())).all():
            latest_events.setdefault(event.account_id, event)
    rollups = _risk_task_rollups(session, ids)
    return [_account_risk_view(
        account, now, state=states.get(account.id),
        latest_event=latest_events.get(account.id), queued=rollups[account.id])
        for account in accounts]


@router.get("/api/risk-control/config")
async def get_risk_control_config():
    payload = export_risk_settings(app_runtime.cfg)
    payload["admin_token_required"] = _risk_admin_required()
    return payload


class RiskSettingsIn(BaseModel):
    risk_control: Dict[str, Any]
    schedule: Dict[str, Any]


@router.put("/api/risk-control/config")
async def put_risk_control_config(body: RiskSettingsIn, request: Request):
    actor = metadata_service._require_risk_admin(request)
    before = export_risk_settings(app_runtime.cfg)
    try:
        apply_risk_settings(app_runtime.cfg, body.model_dump())
    except RiskSettingsError as exc:
        raise HTTPException(400, str(exc)) from exc
    save_risk_settings(app_runtime.cfg)
    if app_runtime.engine is not None:
        app_runtime.engine.risk.update_policy(app_runtime.cfg.risk_control)
    after = export_risk_settings(app_runtime.cfg)
    _record_risk_admin_audit(
        "policy_updated", actor=actor,
        detail={"changes": _risk_config_diff(before, after)})
    after["admin_token_required"] = _risk_admin_required()
    return after


@router.get("/api/risk-control/accounts")
async def list_risk_control_accounts(platform: str | None = None):
    now = datetime.utcnow()
    with get_session() as session:
        query = select(DouyinAccount)
        if platform:
            query = query.where(DouyinAccount.platform == platform)
        accounts = session.exec(query.order_by(DouyinAccount.id)).all()
        return _risk_account_views(session, accounts, now)


@router.get("/api/risk-control/summary")
async def get_risk_control_summary(platform: str | None = None):
    now = datetime.utcnow()
    local_now = datetime.now().astimezone()
    today = local_now.replace(hour=0, minute=0, second=0, microsecond=0) \
        .astimezone(timezone.utc).replace(tzinfo=None)
    with get_session() as session:
        query = select(DouyinAccount)
        if platform:
            query = query.where(DouyinAccount.platform == platform)
        accounts = session.exec(query).all()
        rows = _risk_account_views(session, accounts, now)
        ids = [account.id for account in accounts]
        risk_today = 0
        if ids:
            risk_today = len(session.exec(select(RiskEvent.id).where(
                RiskEvent.account_id.in_(ids),
                RiskEvent.outcome == RiskCategory.RISK.value,
                RiskEvent.occurred_at >= today,
            )).all())
    counts = {
        key: sum(1 for row in rows if row["status"] == key)
        for key in ("normal", "cooldown", "recovering", "auth_invalid",
                    "proxy_error", "network_circuit", "write_paused",
                    "verification_required", "network_backoff")
    }
    return {
        "total": len(rows),
        "counts": counts,
        "abnormal": sum(1 for row in rows if row["status"] != "normal"),
        "risk_events_today": risk_today,
        "blocked_tasks": sum(row["blocked_tasks"] for row in rows),
        "policy_enabled": app_runtime.cfg.risk_control.enabled,
        "mode": app_runtime.cfg.risk_control.mode,
        "sampled_at": _risk_iso(now),
    }


@router.get("/api/risk-control/accounts/{account_id}/events")
async def list_account_risk_events(account_id: int, limit: int = 80):
    limit = max(1, min(200, limit))
    with get_session() as session:
        account = session.get(DouyinAccount, account_id)
        if not account:
            raise HTTPException(404, "账号不存在")
        events = session.exec(select(RiskEvent).where(
            RiskEvent.account_id == account_id).order_by(
                RiskEvent.occurred_at.desc()).limit(limit)).all()
        return {
            "account": {"id": account.id, "nickname": account.nickname,
                        "platform": account.platform},
            "events": [{
                "id": event.id,
                "operation_kind": event.operation_kind,
                "outcome": event.outcome,
                "signal": event.signal,
                "detail": event.detail,
                "network_key": event.network_key,
                "occurred_at": _risk_iso(event.occurred_at),
            } for event in events],
        }


@router.post("/api/risk-control/accounts/{account_id}/probe")
async def probe_account_risk(account_id: int, request: Request):
    actor = metadata_service._require_risk_admin(request)
    result = await accounts_api.refresh_account_profile(account_id)
    woken = 0
    skipped = bool(isinstance(result, dict) and result.get("skipped"))
    if app_runtime.engine is not None and not skipped:
        with get_session() as session:
            state = session.get(AccountRiskState, account_id)
            account = session.get(DouyinAccount, account_id)
            recovered = bool(state is not None and state.risk_level == 0
                             and account and account.status == "active"
                             and account.proxy_status not in {
                                 "bad", "auth_error", "blocked"})
        if recovered:
            woken = app_runtime.engine._wake_deferred_tasks(account_id)
    _record_risk_admin_audit(
        "manual_probe", actor=actor, account_id=account_id,
        detail={"skipped": skipped,
                "reason": result.get("reason", "") if isinstance(result, dict) else "",
                "woken_tasks": woken})
    return {"ok": True, "result": result, "woken_tasks": woken}


@router.post("/api/risk-control/accounts/{account_id}/clear")
async def clear_account_risk(account_id: int, body: metadata_service.RiskClearIn, request: Request):
    actor = metadata_service._require_risk_admin(request)
    reason = body.reason.strip()
    if not body.confirmed or len(reason) < 3:
        raise HTTPException(400, "解除前需要确认并填写至少 3 个字符的原因")
    with get_session() as session:
        if not session.get(DouyinAccount, account_id):
            raise HTTPException(404, "账号不存在")
        state = session.get(AccountRiskState, account_id)
        before = {
            "risk_level": state.risk_level if state else 0,
            "cooldown_until": _risk_iso(state.cooldown_until if state else None),
            "last_risk_reason": state.last_risk_reason if state else "",
            "manual_review_required": bool(state and state.manual_review_required),
            "manual_review_reason": state.manual_review_reason if state else "",
        }
    controller = app_runtime.engine.risk if app_runtime.engine else RiskController(app_runtime.cfg)
    controller.clear_account(account_id, reason=reason, actor=actor)
    woken = app_runtime.engine._wake_deferred_tasks(account_id) if app_runtime.engine else 0
    _record_risk_admin_audit(
        "account_risk_cleared", actor=actor, account_id=account_id,
        detail={"reason": reason, "before": before, "woken_tasks": woken})
    return {"ok": True, "woken_tasks": woken}


@router.get("/api/risk-control/audit")
async def list_risk_admin_audit(limit: int = 100):
    limit = max(1, min(300, limit))
    with get_session() as session:
        rows = session.exec(select(RiskAdminAudit).order_by(
            RiskAdminAudit.created_at.desc()).limit(limit)).all()
        return [{
            "id": row.id, "action": row.action, "account_id": row.account_id,
            "actor": row.actor, "detail": json.loads(row.detail or "{}"),
            "created_at": _risk_iso(row.created_at),
        } for row in rows]
