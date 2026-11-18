"""Shared submit-boundary notification helper for platform publishers."""
from __future__ import annotations

import inspect
from typing import Callable


async def notify_submit(callback: Callable[[], object] | None) -> None:
    """Invoke a sync or coroutine submit-boundary marker before final click/POST."""
    if callback is None:
        return
    result = callback()
    if inspect.isawaitable(result):
        await result


async def notify_stage(callback: Callable[[str], object] | None, stage: str) -> None:
    """Invoke a sync or coroutine live-stage observer without interrupting publish."""
    if callback is None or not stage:
        return
    try:
        result = callback(str(stage))
        if inspect.isawaitable(result):
            await result
    except Exception:
        pass
