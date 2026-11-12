"""Immutable, preview-first plans. Execution reuses the existing task APIs."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import update
from sqlmodel import select
from starlette.requests import Request

from ..db import get_session
from ..models import AgentWorkflow, DouyinAccount
from .privacy import redact


class WorkflowStep(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["create_collection", "create_monitor"]
    platform: Literal["douyin", "xhs", "kuaishou", "tiktok"]
    account_id: int = Field(gt=0)
    keywords: list[str] = Field(default_factory=list, max_length=10)
    target: str = Field(default="", max_length=2000)
    monitor_kind: Literal["creator", "keyword"] = "creator"
    interval_seconds: int = Field(default=900, ge=60, le=86400)
    max_contents: int = Field(default=20, ge=1, le=100)
    download_media: bool = False

    @model_validator(mode="after")
    def validate_target(self):
        self.keywords = list(dict.fromkeys(value.strip() for value in self.keywords if value.strip()))
        self.target = self.target.strip()
        if self.kind == "create_collection":
            if not self.keywords or any(len(value) > 80 for value in self.keywords):
                raise ValueError("采集需填写 1–10 个关键词，每项最多 80 字")
            if self.target or self.monitor_kind != "creator":
                raise ValueError("关键词采集不接受监控目标")
        else:
            if not self.target or self.keywords:
                raise ValueError("监控需填写目标，关键词列表仅用于采集")
            if self.monitor_kind == "keyword":
                if self.platform != "xhs" or len(self.target) > 80:
                    raise ValueError("关键词监控适用于小红书，最多 80 字")
            elif "://" in self.target:
                parsed = urlsplit(self.target)
                domains = {"douyin": ("douyin.com", "iesdouyin.com"), "xhs": ("xiaohongshu.com", "xhslink.com"),
                           "kuaishou": ("kuaishou.com",), "tiktok": ("tiktok.com",)}[self.platform]
                host = (parsed.hostname or "").lower()
                if (parsed.scheme not in {"http", "https"} or parsed.username or parsed.password
                        or parsed.port not in {None, 80, 443}
                        or not any(host == domain or host.endswith("." + domain) for domain in domains)):
                    raise ValueError("监控目标须为对应平台的主页链接或账号 ID")
            elif not re.fullmatch(r"[A-Za-z0-9_@.\-]{1,200}", self.target):
                raise ValueError("请使用完整平台主页链接或账号 ID")
        return self

    def payload(self) -> dict:
        common = {"platform": self.platform, "account_id": self.account_id}
        if self.kind == "create_collection":
            return {**common, "keywords": self.keywords, "source_type": "keyword",
                    "max_contents_per_keyword": self.max_contents,
                    "max_comments_per_content": 0, "download_media": self.download_media}
        return {**common, "url_or_secuid": self.target, "target_kind": self.monitor_kind,
                "interval_seconds": self.interval_seconds, "initial_backfill_count": 0,
                "max_items_per_scan": self.max_contents, "download_enabled": self.download_media}


class WorkflowPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(min_length=1, max_length=100)
    steps: list[WorkflowStep] = Field(min_length=1, max_length=5)
    notes: list[str] = Field(default_factory=list, max_length=10)
    unsupported: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def bounded_notes(self):
        if any(len(item) > 500 for item in self.notes + self.unsupported):
            raise ValueError("计划说明过长")
        canonical = [json.dumps(step.model_dump(), sort_keys=True) for step in self.steps]
        if len(set(canonical)) != len(canonical):
            raise ValueError("计划包含重复步骤")
        return self


def validate_plan(arguments: dict, platform: str = "", account_id: int | None = None) -> WorkflowPlan:
    plan = WorkflowPlan.model_validate(arguments)
    with get_session() as s:
        for step in plan.steps:
            if platform and step.platform != platform or account_id and step.account_id != account_id:
                raise ValueError("计划超出本次选择的平台或账号范围")
            account = s.get(DouyinAccount, step.account_id)
            if not account or account.platform != step.platform or account.status != "active":
                raise ValueError("计划中的账号不存在、非活跃状态或与平台不匹配")
    return plan


def save_plan(run_id: str, plan: WorkflowPlan) -> dict:
    # Redact free-form explanatory text, but not validated business parameters.
    data = plan.model_dump()
    data["notes"], data["unsupported"] = redact(data["notes"]), redact(data["unsupported"])
    serialized = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(serialized.encode()).hexdigest()
    with get_session() as s:
        old = s.exec(select(AgentWorkflow).where(AgentWorkflow.run_id == run_id)).first()
        if old:
            return public_plan(old)
        row = AgentWorkflow(id=str(uuid.uuid4()), run_id=run_id, title=plan.title,
                            plan_json=serialized, plan_hash=digest)
        s.add(row); s.commit(); s.refresh(row)
        return public_plan(row)


def public_plan(row: AgentWorkflow) -> dict:
    return {"id": row.id, "run_id": row.run_id, "plan": json.loads(row.plan_json),
            "plan_hash": row.plan_hash, "status": row.status,
            "results": json.loads(row.results_json), "error": row.error,
            "effect": "确认后创建采集/持续监控任务，由现有引擎执行；不发布、不发送消息。"}


async def apply_plan(plan_id: str, plan_hash: str, executors: dict) -> dict:
    with get_session() as s:
        row = s.get(AgentWorkflow, plan_id)
        if row is None:
            raise HTTPException(404, "工作流草稿不存在")
        if row.plan_hash != plan_hash:
            raise HTTPException(409, "计划版本已变化，请重新查看并确认")
        if row.status == "applied":
            return public_plan(row)
        plan = WorkflowPlan.model_validate_json(row.plan_json)
        changed = s.execute(update(AgentWorkflow).where(
            AgentWorkflow.id == plan_id, AgentWorkflow.status.in_(["draft", "partial", "interrupted"])
        ).values(status="applying", error="", updated_at=datetime.utcnow())).rowcount
        if changed != 1:
            raise HTTPException(409, "该计划正在执行，请刷新查看结果")
        s.commit()
        results = json.loads(row.results_json)
    try:
        for index, step in enumerate(plan.steps):
            if index < len(results):
                continue
            # Existing handlers revalidate live state only for new submissions;
            # a persisted receipt is replayed even after an account is retired.
            key = f"agent-{plan_id}-{index}"
            request = Request({"type": "http", "headers": [(b"idempotency-key", key.encode())]})
            payload = await asyncio.wait_for(executors[step.kind](step.payload(), request), timeout=60)
            results.append({"step": index + 1, "kind": step.kind, "id": payload["id"],
                            "platform": step.platform, "status": payload.get("status", "created"),
                            "replayed": payload.get("replayed", False)})
            with get_session() as s:
                row = s.get(AgentWorkflow, plan_id)
                row.results_json = json.dumps(results, ensure_ascii=False)
                row.updated_at = datetime.utcnow()
                s.add(row); s.commit()
    except BaseException as exc:
        with get_session() as s:
            row = s.get(AgentWorkflow, plan_id)
            row.status = "interrupted" if isinstance(exc, asyncio.CancelledError) else "partial"
            if isinstance(exc, HTTPException) and isinstance(exc.detail, str):
                row.error = redact(exc.detail)[:500]
            elif isinstance(exc, ValueError):
                row.error = "账号或计划校验未通过，请检查账号状态和参数"
            else:
                row.error = "执行中断；已创建的任务保留，再次确认会复用原提交编号"
            row.updated_at = datetime.utcnow()
            s.add(row); s.commit(); s.refresh(row)
            result = public_plan(row)
        if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise
        return result
    with get_session() as s:
        row = s.get(AgentWorkflow, plan_id)
        row.status = "applied"
        row.updated_at = datetime.utcnow()
        s.add(row); s.commit(); s.refresh(row)
        return public_plan(row)


PLAN_TOOL = {"name": "propose_workflow", "description": (
    "仅生成待确认草稿，不执行。支持关键词采集和作品监控（抖音/小红书/快手/TikTok）。"
    "先查询真实账号 ID；缺少目标先询问。日报定时发送、代码修改、发布等未支持需求须列入 unsupported。"
    "每次只提交一个计划，最多五步。"), "inputSchema": WorkflowPlan.model_json_schema()}
