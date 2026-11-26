"""统一实时进度 SSE 推送、快照查询与通用取消控制接口。"""
from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from app import runtime as app_runtime
from app.progress_hub import progress_hub

router = APIRouter()


def _all_tasks_snapshot() -> list[dict]:
    items = progress_hub.snapshot()
    comp = progress_hub.browser_component_task()
    if comp is not None:
        items = [comp, *items]
    return items


@router.get("/api/progress/tasks")
async def list_progress_tasks():
    """返回当前全部活跃及近期完成的实时任务快照。"""
    return {"tasks": _all_tasks_snapshot()}


@router.get("/api/progress/stream")
async def progress_stream(request: Request):
    """全站统一实时进度 SSE 流：建连即发快照，后续增量推送节流后的进度事件。"""
    queue = await progress_hub.subscribe()

    async def gen():
        try:
            snapshot_payload = json.dumps(
                {"tasks": _all_tasks_snapshot()},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            yield f"retry: 2500\nevent: snapshot\ndata: {snapshot_payload}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    packet = await asyncio.wait_for(queue.get(), timeout=20.0)
                    ev = str(packet.get("event") or "task")
                    seq = int(packet.get("id") or 0)
                    payload = json.dumps(
                        packet.get("data"),
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    yield f"id: {seq}\nevent: {ev}\ndata: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        except asyncio.CancelledError:
            pass
        finally:
            progress_hub.unsubscribe(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/api/progress/tasks/{task_key:path}/cancel")
async def cancel_progress_task(task_key: str):
    """通过统一 task_key 安全取消正在进行的后台任务。"""
    key = str(task_key or "").strip()
    if key.startswith("collection:") and app_runtime.engine is not None:
        raw_id = key.split(":", 1)[1]
        if raw_id.isdigit():
            ok = await app_runtime.engine.cancel_collection_job(int(raw_id))
            if ok:
                progress_hub.upsert(
                    key,
                    status="canceled",
                    phase="canceled",
                    phase_label="已取消",
                    cancelable=False,
                    immediate=True,
                )
            return {"ok": bool(ok)}
    result = await progress_hub.cancel_task(key)
    if not result.get("ok"):
        raise HTTPException(409, result.get("error") or "无法取消该任务")
    return result


@router.post("/api/progress/clear-finished")
async def clear_finished_progress():
    """清理活动中心中已完成/失败/已取消的近期任务记录。"""
    cleared = progress_hub.clear_finished()
    return {"ok": True, "cleared": cleared}
