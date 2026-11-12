"""Read-only business tools. Never serialize a database row wholesale."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func
from sqlmodel import select

from ..db import get_session
from ..models import (AccountActionTask, AccountOperationLog, AccountStatSnapshot,
                      AccountWork, CommentTask, DouyinAccount, KeywordCollectionJob,
                      MonitorTarget, PublishTask, RiskEvent)
from ..risk import classify_platform_error
from .privacy import redact

Platform = Literal["", "douyin", "xhs", "kuaishou", "shipinhao", "tiktok"]
TaskType = Literal["collections", "publishes", "comments", "actions", "monitors"]
TASK_MODELS = {"collections": KeywordCollectionJob, "publishes": PublishTask,
               "comments": CommentTask, "actions": AccountActionTask, "monitors": MonitorTarget}


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    platform: Platform = ""
    account_id: int | None = Field(default=None, gt=0)


class ListArgs(Scope):
    limit: int = Field(default=20, ge=1, le=50)


class MetricsArgs(Scope):
    days: int = Field(default=7, ge=1, le=31)


class TaskArgs(Scope):
    task_type: TaskType
    task_id: int = Field(gt=0)


def iso(value):
    return value.isoformat() + "Z" if isinstance(value, datetime) else value


def fields(row, names: str) -> dict:
    return redact({name: iso(getattr(row, name, None)) for name in names.split()})


def scoped(query, model, args: Scope):
    if args.platform:
        query = query.where(model.platform == args.platform)
    if args.account_id is not None:
        query = query.where((model.id if model is DouyinAccount else model.account_id) == args.account_id)
    return query


def list_accounts(args: ListArgs) -> dict:
    with get_session() as s:
        query = scoped(select(DouyinAccount), DouyinAccount, args)
        rows = s.exec(query.order_by(DouyinAccount.id).limit(args.limit + 1)).all()
        return {"accounts": [fields(row, "id platform nickname status proxy_status follower_count aweme_count")
                             for row in rows[:args.limit]], "truncated": len(rows) > args.limit,
                "note": "仅当前本地记录；未刷新平台，也不包含登录凭据"}


def task_record(row, kind) -> dict:
    record = fields(row, "id platform account_id status error blocked_reason blocked_signal "
                         "current_step next_allowed_at created_at started_at finished_at")
    record["task_type"] = kind
    record["evidence_id"] = f"task:{kind}:{row.id}"
    if kind == "monitors":
        record.update(fields(row, "enabled last_error last_scan_at interval_seconds nickname alias"))
    return record


def list_tasks(args: ListArgs) -> dict:
    rows = []
    with get_session() as s:
        for kind, model in TASK_MODELS.items():
            items = s.exec(scoped(select(model), model, args).order_by(
                model.created_at.desc(), model.id.desc()).limit(args.limit + 1)).all()
            rows.extend(task_record(row, kind) for row in items)
    rows.sort(key=lambda row: row["created_at"] or "", reverse=True)
    return {"tasks": rows[:args.limit], "truncated": len(rows) > args.limit,
            "note": "各任务的当前状态，并非历史状态变化次数"}


def get_risk_events(args: ListArgs) -> dict:
    with get_session() as s:
        account_ids = scoped(select(DouyinAccount.id), DouyinAccount, args)
        query = select(RiskEvent).where(RiskEvent.account_id.in_(account_ids))
        rows = s.exec(query.order_by(RiskEvent.occurred_at.desc(), RiskEvent.id.desc()).limit(args.limit + 1)).all()
        return {"events": [dict(fields(row, "id account_id operation_kind outcome signal detail occurred_at"),
                                evidence_id=f"risk:{row.id}") for row in rows[:args.limit]],
                "truncated": len(rows) > args.limit, "note": "账号级事件；时间接近不代表已证明因果关系"}


def get_task(args: TaskArgs) -> dict:
    model = TASK_MODELS[args.task_type]
    with get_session() as s:
        row = s.exec(scoped(select(model).where(model.id == args.task_id), model, args)).first()
        if row is None:
            raise ValueError("所选范围内没有此任务")
        result = {"task": task_record(row, args.task_type)}
        account = s.get(DouyinAccount, row.account_id) if row.account_id else None
        result["account"] = fields(account, "id platform nickname status proxy_status") if account else None
        # IDs overlap between queues. Always constrain task type as well as ID.
        types = {args.task_type, {"collections": "collection", "publishes": "publish",
                                  "comments": "comment", "actions": "account-action"}.get(args.task_type, args.task_type)}
        query = select(AccountOperationLog).where(
            AccountOperationLog.task_id == str(row.id), AccountOperationLog.task_type.in_(types),
            AccountOperationLog.platform == row.platform,
            AccountOperationLog.account_id == row.account_id)
        logs = s.exec(query.order_by(AccountOperationLog.created_at.desc()).limit(20)).all()
        result["operations"] = [dict(fields(log, "id action status risk_signal detail duration_ms created_at"),
                                     evidence_id=f"operation:{log.id}") for log in logs]
        error = getattr(row, "error", "") or getattr(row, "last_error", "") or getattr(row, "blocked_reason", "")
        category, signal = classify_platform_error(error)
        checks = {
            "auth": ["检查账号登录状态，必要时重新登录", "登录恢复后先做只读检查"],
            "network": ["检查本机网络、代理连通性和超时", "对比同时间其他任务是否出现相同错误"],
            "risk": ["查看现有风险事件和允许恢复时间", "保持冷却与写操作保护，按平台提示处理"],
            "business": ["检查任务参数、素材要求和执行阶段", "补充对应步骤日志；当前证据尚不足以确认根因"],
        }
        result["triage"] = {"classification": category.value if error else "no_error_recorded",
                            "signal": signal if error else "", "basis": "现有错误文本的规则分类，不等同于根因确认",
                            "checks": checks[category.value] if error else ["当前没有错误记录，核对任务是否尚在等待执行"]}
        if getattr(row, "status", "") == "uncertain":
            result["triage"]["checks"].insert(0, "平台写入结果不确定：先核对是否已发布或发送，避免重复执行")
        result["gaps"] = [] if logs else ["该任务缺少统一操作流水；仅能使用当前任务字段分析"]
    if row.account_id:
        result["account_risk_events"] = get_risk_events(ListArgs(
            platform=row.platform, account_id=row.account_id, limit=10))
    return result


def get_metrics(args: MetricsArgs) -> dict:
    now = datetime.utcnow()
    since = now - timedelta(days=args.days)
    counts = {}
    with get_session() as s:
        for kind, model in TASK_MODELS.items():
            if kind == "monitors":
                continue
            query = scoped(select(model.status, func.count()).where(model.created_at >= since), model, args)
            counts[kind] = dict(s.exec(query.group_by(model.status)).all())
        operations = scoped(select(func.count(), func.avg(AccountOperationLog.duration_ms)).where(
            AccountOperationLog.created_at >= since, AccountOperationLog.duration_ms > 0), AccountOperationLog, args)
        samples, mean = s.exec(operations).one()
        accounts = s.exec(scoped(select(func.count()).select_from(DouyinAccount), DouyinAccount, args)).one()
        works = s.exec(scoped(select(AccountWork), AccountWork, args).order_by(
            AccountWork.like_count.desc(), AccountWork.id.desc()).limit(10)).all()
        snapshots = s.exec(scoped(select(AccountStatSnapshot).where(
            AccountStatSnapshot.date >= since.date().isoformat()), AccountStatSnapshot, args).order_by(
                AccountStatSnapshot.date.desc(), AccountStatSnapshot.id.desc()).limit(100)).all()
        return {"window": {"start": iso(since), "end": iso(now), "timezone": "UTC"},
                "accounts": accounts, "task_status_counts": counts,
                "duration": {"samples": samples, "mean_ms": round(mean, 1) if mean is not None else None,
                             "coverage": "仅已记录且耗时大于零的操作，不代表所有请求"},
                "top_works": [fields(row, "id account_id platform desc like_count comment_count play_count fetched_at") for row in works],
                "account_snapshots": [fields(row, "id account_id date follower_count aweme_count total_like total_comment total_play") for row in snapshots],
                "limitations": ["任务计数按创建时间筛选，再统计当前状态，不是历史成功率",
                                "作品为当前已同步记录，按点赞排序；不是时间窗口内新增互动",
                                "部分平台缺失播放数据时旧记录为 0；不据此推断零播放或限流",
                                "账号每日快照按账号时区记日，最多返回最近 100 条，可能不连续",
                                "未主动访问平台；数据新鲜度以各记录同步时间为准"]}


READ_TOOLS = {
    "list_accounts": (ListArgs, list_accounts, "查询本地账号状态，不读取凭据或刷新平台。"),
    "list_tasks": (ListArgs, list_tasks, "查询最近的任务和监控状态；用于选择待诊断任务。"),
    "get_task": (TaskArgs, get_task, "读取指定任务证据、操作流水、账号风险事件和规则初步分类。"),
    "get_metrics": (MetricsArgs, get_metrics, "计算本地任务统计、已同步作品和账号快照，包含统计口径与数据缺口。"),
    "get_risk_events": (ListArgs, get_risk_events, "读取范围内账号的风险事件；不解除风险状态。"),
}


def tool_definitions() -> list[dict]:
    return [{"name": name, "description": spec[2], "inputSchema": spec[0].model_json_schema()}
            for name, spec in READ_TOOLS.items()]


def read_tool(name: str, arguments: dict, *, platform: str = "", account_id: int | None = None) -> dict:
    if name not in READ_TOOLS:
        raise ValueError("未开放此工具")
    schema, handler, _ = READ_TOOLS[name]
    args = schema.model_validate(arguments)
    if platform:
        if args.platform and args.platform != platform:
            raise ValueError("工具平台超出本次选择范围")
        args.platform = platform
    if account_id is not None:
        if args.account_id is not None and args.account_id != account_id:
            raise ValueError("工具账号超出本次选择范围")
        args.account_id = account_id
    return {"scope": {"platform": args.platform, "account_id": args.account_id},
            "observed_at": iso(datetime.utcnow()), **handler(args)}
