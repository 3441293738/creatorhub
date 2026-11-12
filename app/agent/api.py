"""Local UI API; MCP exposes only the explicitly allowlisted read tools."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Literal

from fastapi import APIRouter, Body, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError
from sqlmodel import select

from ..db import get_session
from ..models import AgentRun
from .catalog import CatalogError, discover_models
from .config import (AgentSettings, ConnectionSettings, ModelCatalogRequest, SettingsUpdate,
                     activate_channel, delete_channel, get_settings, list_channels,
                     public_settings, resolve_api_key, save_channel, save_settings)
from .providers import ModelError, ModelSession
from .runtime import RunInput, cancel_run, get_conversation, get_run, public_run, start_run
from .tools import Platform, read_tool, tool_definitions
from .workflows import apply_plan


def validation_error(exc: ValidationError) -> HTTPException:
    return HTTPException(422, exc.errors(include_input=False, include_context=False, include_url=False))


class Approval(BaseModel):
    confirmed: Literal[True]
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


async def run_events(run_id, request):
    """Read-only SSE snapshots. Reconnection never starts/replays a model call."""
    previous, heartbeat = None, time.monotonic()
    while not await request.is_disconnected():
        record = await asyncio.to_thread(get_run, run_id)
        data = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        if data != previous:
            yield "event: snapshot\ndata: " + data + "\n\n"
            previous = data; heartbeat = time.monotonic()
        elif time.monotonic() - heartbeat >= 10:
            yield ": keepalive\n\n"
            heartbeat = time.monotonic()
        if record["status"] not in {"queued", "running"}:
            yield "event: done\ndata: " + json.dumps({"id": run_id}) + "\n\n"
            return
        await asyncio.sleep(.2)


def create_router(executors: dict) -> APIRouter:
    router = APIRouter(prefix="/api/agent", tags=["AI 助手"])

    @router.get("/settings")
    def settings(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return public_settings()

    @router.put("/settings")
    def configure(body: dict = Body(...)):
        try:
            return save_settings(SettingsUpdate.model_validate(body))
        except ValidationError as exc:
            raise validation_error(exc) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @router.get("/channels")
    def channels(response: Response):
        response.headers["Cache-Control"] = "no-store"
        return list_channels()

    @router.post("/channels", status_code=201)
    def add_channel(body: dict = Body(...)):
        try:
            return save_channel(SettingsUpdate.model_validate(body), create=True)
        except ValidationError as exc:
            raise validation_error(exc) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @router.put("/channels/{channel_id}")
    def edit_channel(channel_id: str, body: dict = Body(...)):
        try:
            return save_channel(SettingsUpdate.model_validate(body), channel_id)
        except ValidationError as exc:
            raise validation_error(exc) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @router.post("/channels/{channel_id}/activate")
    def choose_channel(channel_id: str):
        try:
            return activate_channel(channel_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @router.delete("/channels/{channel_id}")
    def remove_channel(channel_id: str):
        try:
            return delete_channel(channel_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    async def check_model(cfg):
        if not cfg.base_url or not cfg.model:
            raise HTTPException(409, "请先保存模型地址与模型名称")
        try:
            result = await ModelSession(cfg, "Reply with OK only.", "Connection test.").complete([])
            return {"ok": bool(result.text.strip()), "detail": (
                "已收到模型文本响应；不代表工具能力已通过验证" if result.text.strip()
                else "模型没有返回文本；请检查协议和模型配置")}
        except ModelError as exc:
            return {"ok": False, "detail": str(exc)}

    @router.post("/test")
    async def test_model():
        return await check_model(get_settings())

    @router.post("/channels/{channel_id}/test")
    async def test_channel(channel_id: str):
        try:
            cfg = get_settings(channel_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        return await check_model(cfg)

    @router.post("/models")
    async def models(response: Response, body: dict = Body(...)):
        response.headers["Cache-Control"] = "no-store"
        try:
            request = ModelCatalogRequest.model_validate(body)
            old = get_settings(request.channel_id) if request.channel_id != "" else AgentSettings()
            key = resolve_api_key(request, old)
            connection = ConnectionSettings(provider=request.provider, base_url=request.base_url, api_key=key)
        except ValidationError as exc:
            raise validation_error(exc) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        try:
            return await discover_models(connection)
        except CatalogError as exc:
            raise HTTPException(502, str(exc), headers={"Cache-Control": "no-store"}) from None

    @router.get("/context")
    def context(platform: Platform = "", account_id: int | None = Query(None, gt=0)):
        scope = {"platform": platform, "account_id": account_id}
        return {"accounts": read_tool("list_accounts", {"limit": 50}, **scope),
                "tasks": read_tool("list_tasks", {"limit": 50}, **scope),
                "metrics": read_tool("get_metrics", {}, **scope)}

    @router.get("/runs")
    def history(platform: Platform = "", limit: int = Query(20, ge=1, le=50)):
        with get_session() as s:
            query = select(AgentRun)
            if platform:
                query = query.where(AgentRun.platform == platform)
            rows = s.exec(query.order_by(AgentRun.created_at.desc()).limit(limit)).all()
            return [public_run(row, include_evidence=False) for row in rows]

    @router.post("/runs", status_code=202)
    async def create_run(body: RunInput):
        return start_run(body)

    @router.get("/runs/{run_id}")
    def run(run_id: str):
        return get_run(run_id)

    @router.get("/runs/{run_id}/conversation")
    def conversation(run_id: str):
        return get_conversation(run_id)

    @router.get("/runs/{run_id}/events")
    async def events(run_id: str, request: Request):
        await asyncio.to_thread(get_run, run_id)  # Report a missing ID before sending 200.
        return StreamingResponse(run_events(run_id, request), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache, no-store, no-transform", "X-Accel-Buffering": "no"})

    @router.post("/runs/{run_id}/cancel")
    async def cancel(run_id: str):
        return await cancel_run(run_id)

    @router.post("/workflows/{plan_id}/apply")
    async def apply(plan_id: str, body: Approval):
        if not get_settings().enabled:
            raise HTTPException(409, "AI 助手已停用；请先启用后再确认草稿")
        return await apply_plan(plan_id, body.plan_hash, executors)

    @router.get("/tools")
    def tools():
        if not get_settings().mcp_enabled:
            raise HTTPException(403, "MCP 查询服务尚未启用")
        return tool_definitions()

    @router.post("/tools/{name}")
    def call_tool(name: str, arguments: dict = Body(...)):
        if not get_settings().mcp_enabled:
            raise HTTPException(403, "MCP 查询服务尚未启用")
        try:
            return read_tool(name, arguments)
        except ValidationError as exc:
            raise validation_error(exc) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    return router
