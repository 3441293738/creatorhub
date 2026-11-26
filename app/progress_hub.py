"""统一内存进度中心（SSE 推送、节流合并、取消注册与专用有界线程池）。"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import functools
import inspect
import logging
import os
import threading
import time
from typing import Any, Callable

log = logging.getLogger("creatorhub.progress")

TERMINAL_STATUSES = frozenset({"completed", "failed", "canceled"})
ACTIVE_STATUSES = frozenset({"queued", "running", "waiting_user", "blocked"})
MAX_FINISHED_TASKS = 80
FINISHED_TTL_SECONDS = 1200.0
DEFAULT_THROTTLE_SECONDS = 0.2


def _utc_iso(ts: float | None = None) -> str:
    dt = datetime.utcfromtimestamp(ts) if ts is not None else datetime.utcnow()
    return dt.isoformat(timespec="milliseconds") + "Z"


class _BoundedExecutors:
    """按负载特征隔离阻塞/CPU 线程池，避免单个长任务占满默认 executor。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._media: ThreadPoolExecutor | None = None
        self._io: ThreadPoolExecutor | None = None

    @property
    def media(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._media is None:
                self._media = ThreadPoolExecutor(
                    max_workers=3, thread_name_prefix="ch-media"
                )
            return self._media

    @property
    def io(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._io is None:
                self._io = ThreadPoolExecutor(
                    max_workers=4, thread_name_prefix="ch-io"
                )
            return self._io

    def shutdown(self) -> None:
        with self._lock:
            media, self._media = self._media, None
            io_pool, self._io = self._io, None
        for pool in (media, io_pool):
            if pool is not None:
                try:
                    pool.shutdown(wait=False, cancel_futures=True)
                except Exception:
                    pass


executors = _BoundedExecutors()


async def run_in_media_pool(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """将 yt-dlp、FFmpeg 合流、视频解密等媒体重任务投递到专用媒体线程池。"""
    loop = asyncio.get_running_loop()
    call = functools.partial(func, *args, **kwargs) if kwargs else functools.partial(func, *args)
    return await loop.run_in_executor(executors.media, call)


async def run_in_io_pool(func: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """将磁盘落盘、Excel 导出、本地目录扫描等阻塞 I/O 投递到专用 I/O 线程池。"""
    loop = asyncio.get_running_loop()
    call = functools.partial(func, *args, **kwargs) if kwargs else functools.partial(func, *args)
    return await loop.run_in_executor(executors.io, call)


class ProgressHub:
    """线程安全 + 协程安全的全局实时进度总线，供 SSE 流与各 API/引擎共享。"""

    def __init__(self, *, throttle_seconds: float = DEFAULT_THROTTLE_SECONDS) -> None:
        self._lock = threading.RLock()
        self._throttle_seconds = max(0.05, float(throttle_seconds))
        self._loop: asyncio.AbstractEventLoop | None = None
        self._tasks: dict[str, dict[str, Any]] = {}
        self._meta: dict[str, dict[str, Any]] = {}
        self._cancels: dict[str, Callable[[], Any]] = {}
        self._subscribers: set[asyncio.Queue] = set()
        self._flush_handles: dict[str, asyncio.TimerHandle] = {}
        self._seq: int = 0

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        with self._lock:
            self._loop = loop

    def _capture_loop(self) -> asyncio.AbstractEventLoop | None:
        with self._lock:
            if self._loop is not None and not self._loop.is_closed():
                return self._loop
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            return None
        with self._lock:
            self._loop = running
        return running

    def _compute_public_task(self, raw: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
        item = dict(raw)
        started_mono = meta.get("started_mono")
        finished_mono = meta.get("finished_mono")
        if started_mono is not None:
            end_mono = finished_mono if finished_mono is not None else time.monotonic()
            item["elapsed_seconds"] = max(0, int(end_mono - started_mono))
        else:
            item["elapsed_seconds"] = 0
        return item

    def _prune_locked(self, now_mono: float) -> list[str]:
        finished = [
            (key, self._meta.get(key, {}).get("finished_mono", now_mono))
            for key, task in self._tasks.items()
            if task.get("status") in TERMINAL_STATUSES
        ]
        removed: list[str] = []
        for key, fin_mono in finished:
            if (now_mono - fin_mono) > FINISHED_TTL_SECONDS:
                self._tasks.pop(key, None)
                self._meta.pop(key, None)
                self._cancels.pop(key, None)
                removed.append(key)
        remaining = [
            (key, self._meta.get(key, {}).get("finished_mono", now_mono))
            for key, task in self._tasks.items()
            if task.get("status") in TERMINAL_STATUSES
        ]
        if len(remaining) > MAX_FINISHED_TASKS:
            remaining.sort(key=lambda pair: pair[1])
            for key, _ in remaining[: len(remaining) - MAX_FINISHED_TASKS]:
                self._tasks.pop(key, None)
                self._meta.pop(key, None)
                self._cancels.pop(key, None)
                removed.append(key)
        return removed

    def upsert(
        self,
        task_key: str,
        *,
        immediate: bool = False,
        **fields: Any,
    ) -> dict[str, Any]:
        """创建或增量更新一条进度任务；可从任意协程或工作线程安全调用。"""
        key = str(task_key or "").strip()
        if not key:
            raise ValueError("task_key 不能为空")
        now_mono = time.monotonic()
        now_iso = _utc_iso()
        with self._lock:
            existing = self._tasks.get(key)
            meta = self._meta.get(key)
            prev_status = existing.get("status") if existing else None
            if existing is None:
                existing = {
                    "task_key": key,
                    "category": "task",
                    "title": "后台任务",
                    "platform": "",
                    "account_id": None,
                    "account_name": "",
                    "source_tab": "queue",
                    "status": "queued",
                    "phase": "queued",
                    "phase_label": "排队中",
                    "detail": "",
                    "error": "",
                    "determinate": False,
                    "percent": None,
                    "current": 0,
                    "total": 0,
                    "unit": "",
                    "speed_bps": None,
                    "eta_seconds": None,
                    "metrics": {},
                    "steps": [],
                    "action_required": None,
                    "next_allowed_at": None,
                    "cancelable": False,
                    "started_at": now_iso,
                    "updated_at": now_iso,
                    "finished_at": None,
                    "extra": {},
                }
                meta = {
                    "started_mono": now_mono,
                    "finished_mono": None,
                    "last_push_mono": 0.0,
                }
                self._tasks[key] = existing
                self._meta[key] = meta

            for name, value in fields.items():
                if value is None and name not in {
                    "percent", "speed_bps", "eta_seconds", "action_required",
                    "next_allowed_at", "account_id", "finished_at",
                }:
                    continue
                if name == "metrics" and isinstance(value, dict):
                    merged_metrics = dict(existing.get("metrics") or {})
                    merged_metrics.update(value)
                    existing["metrics"] = merged_metrics
                elif name == "extra" and isinstance(value, dict):
                    merged_extra = dict(existing.get("extra") or {})
                    merged_extra.update(value)
                    existing["extra"] = merged_extra
                else:
                    existing[name] = value

            status = str(existing.get("status") or "running")
            if status in TERMINAL_STATUSES:
                existing["cancelable"] = False
                if meta.get("finished_mono") is None:
                    meta["finished_mono"] = now_mono
                if not existing.get("finished_at"):
                    existing["finished_at"] = now_iso
                if status == "completed":
                    if existing.get("determinate") or existing.get("percent") is not None:
                        existing["percent"] = 100.0
                    existing["speed_bps"] = None
                    existing["eta_seconds"] = 0
                else:
                    existing["speed_bps"] = None
                    existing["eta_seconds"] = None
                self._cancels.pop(key, None)
            else:
                meta["finished_mono"] = None
                existing["finished_at"] = None
                total_val = existing.get("total") or 0
                current_val = existing.get("current") or 0
                if "percent" not in fields and total_val and float(total_val) > 0:
                    pct = max(0.0, min(99.9, (float(current_val) * 100.0) / float(total_val)))
                    existing["percent"] = round(pct, 1)
                    if "determinate" not in fields:
                        existing["determinate"] = True

            existing["updated_at"] = now_iso
            status_changed = (prev_status != status)
            action_changed = "action_required" in fields
            force_now = immediate or status_changed or action_changed or (prev_status is None)
            snapshot = self._compute_public_task(existing, meta)
            self._prune_locked(now_mono)

        self._schedule_broadcast(key, snapshot, force_now=force_now)
        return snapshot

    def register_cancel(self, task_key: str, callback: Callable[[], Any]) -> None:
        key = str(task_key or "").strip()
        if not key or not callable(callback):
            return
        with self._lock:
            self._cancels[key] = callback
            if key in self._tasks and self._tasks[key].get("status") not in TERMINAL_STATUSES:
                self._tasks[key]["cancelable"] = True

    def unregister_cancel(self, task_key: str) -> None:
        key = str(task_key or "").strip()
        with self._lock:
            self._cancels.pop(key, None)
            if key in self._tasks:
                self._tasks[key]["cancelable"] = False

    async def cancel_task(self, task_key: str) -> dict[str, Any]:
        key = str(task_key or "").strip()
        with self._lock:
            cb = self._cancels.get(key)
            task = self._tasks.get(key)
        if task is None and cb is None:
            return {"ok": False, "error": "任务不存在或已结束"}
        if task is not None and task.get("status") in TERMINAL_STATUSES:
            return {"ok": True, "status": task.get("status"), "already_finished": True}
        if cb is None:
            return {"ok": False, "error": "该任务当前阶段不支持取消"}
        try:
            res = cb()
            if inspect.isawaitable(res):
                res = await res
            return {"ok": True, "result": res}
        except Exception as exc:
            log.warning("取消任务 %s 失败: %r", key, exc)
            return {"ok": False, "error": str(exc) or repr(exc)}

    def remove(self, task_key: str) -> None:
        key = str(task_key or "").strip()
        with self._lock:
            existed = self._tasks.pop(key, None) is not None
            self._meta.pop(key, None)
            self._cancels.pop(key, None)
        if existed:
            self._enqueue_event("remove", {"task_key": key})

    def clear_finished(self) -> int:
        with self._lock:
            keys = [
                key for key, task in self._tasks.items()
                if task.get("status") in TERMINAL_STATUSES
            ]
            for key in keys:
                self._tasks.pop(key, None)
                self._meta.pop(key, None)
                self._cancels.pop(key, None)
        if keys:
            self._enqueue_event("clear_finished", {"task_keys": keys})
        return len(keys)

    def get(self, task_key: str) -> dict[str, Any] | None:
        with self._lock:
            raw = self._tasks.get(task_key)
            if raw is None:
                return None
            return self._compute_public_task(raw, self._meta.get(task_key, {}))

    def snapshot(self) -> list[dict[str, Any]]:
        now_mono = time.monotonic()
        with self._lock:
            self._prune_locked(now_mono)
            items = [
                self._compute_public_task(raw, self._meta.get(key, {}))
                for key, raw in self._tasks.items()
            ]
        rank = {
            "waiting_user": 0,
            "running": 1,
            "queued": 2,
            "blocked": 3,
            "failed": 4,
            "canceled": 5,
            "completed": 6,
        }
        items.sort(key=lambda item: (rank.get(item.get("status", ""), 9), -(item.get("elapsed_seconds") or 0)))
        return items

    def active_by_prefix(self, prefix: str) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {
                key: self._compute_public_task(raw, self._meta.get(key, {}))
                for key, raw in self._tasks.items()
                if key.startswith(prefix)
            }

    def emit_event(self, event_name: str, payload: dict[str, Any]) -> None:
        """向全部在线客户端广播轻量业务通知（如单行账号/任务变更，避免整表轮询）。"""
        self._enqueue_event("event", {"name": event_name, "payload": payload})

    def _schedule_broadcast(self, task_key: str, snapshot: dict[str, Any], *, force_now: bool) -> None:
        loop = self._capture_loop()
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._schedule_on_loop, task_key, snapshot, force_now)
        except RuntimeError:
            pass

    def _schedule_on_loop(self, task_key: str, snapshot: dict[str, Any], force_now: bool) -> None:
        now = time.monotonic()
        with self._lock:
            meta = self._meta.get(task_key)
            last_push = float(meta.get("last_push_mono", 0.0)) if meta else 0.0
            pending = self._flush_handles.get(task_key)
            elapsed = now - last_push
            if force_now or elapsed >= self._throttle_seconds:
                if pending is not None:
                    pending.cancel()
                    self._flush_handles.pop(task_key, None)
                if meta is not None:
                    meta["last_push_mono"] = now
                self._deliver_on_loop("task", snapshot)
                return
            if pending is None:
                delay = max(0.02, self._throttle_seconds - elapsed)
                loop = asyncio.get_running_loop()
                self._flush_handles[task_key] = loop.call_later(
                    delay, self._flush_deferred_on_loop, task_key
                )

    def _flush_deferred_on_loop(self, task_key: str) -> None:
        with self._lock:
            self._flush_handles.pop(task_key, None)
            raw = self._tasks.get(task_key)
            meta = self._meta.get(task_key)
            if raw is None or meta is None:
                return
            meta["last_push_mono"] = time.monotonic()
            snapshot = self._compute_public_task(raw, meta)
        self._deliver_on_loop("task", snapshot)

    def _enqueue_event(self, event_type: str, data: dict[str, Any]) -> None:
        loop = self._capture_loop()
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._deliver_on_loop, event_type, data)
        except RuntimeError:
            pass

    def _deliver_on_loop(self, event_type: str, data: dict[str, Any]) -> None:
        with self._lock:
            if not self._subscribers:
                return
            self._seq += 1
            packet = {"id": self._seq, "event": event_type, "data": data}
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(packet)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(packet)
                except asyncio.QueueFull:
                    pass

    async def subscribe(self) -> asyncio.Queue:
        self._capture_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers.discard(q)

    def browser_component_task(self) -> dict[str, Any] | None:
        """读取桌面版浏览器内核按需下载状态，合并呈现到全局活动中心。"""
        home = os.environ.get("CREATORHUB_DESKTOP_HOME")
        if not home:
            return None
        try:
            from app.browser.runtime_install import read_status
            info = read_status(home)
        except Exception:
            return None
        status = str(info.get("status") or "")
        if status != "downloading":
            return None
        pct = info.get("progress")
        return {
            "task_key": "runtime:chromium",
            "category": "runtime",
            "title": "浏览器内核组件准备",
            "platform": "",
            "account_id": None,
            "account_name": "",
            "source_tab": "accounts",
            "status": "running",
            "phase": "downloading",
            "phase_label": str(info.get("message") or "正在下载浏览器组件"),
            "detail": "",
            "error": "",
            "determinate": pct is not None,
            "percent": float(pct) if pct is not None else None,
            "current": int(pct or 0),
            "total": 100 if pct is not None else 0,
            "unit": "",
            "speed_bps": None,
            "eta_seconds": None,
            "metrics": {},
            "steps": [],
            "action_required": None,
            "next_allowed_at": None,
            "cancelable": False,
            "started_at": _utc_iso(info.get("updated_at")),
            "updated_at": _utc_iso(info.get("updated_at")),
            "finished_at": None,
            "elapsed_seconds": 0,
            "extra": {},
        }

    def shutdown(self) -> None:
        with self._lock:
            for handle in self._flush_handles.values():
                try:
                    handle.cancel()
                except Exception:
                    pass
            self._flush_handles.clear()
            self._subscribers.clear()
            self._cancels.clear()
        executors.shutdown()


progress_hub = ProgressHub()
