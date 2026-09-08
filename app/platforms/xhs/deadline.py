"""One monotonic budget for preparation, uploads, polling and submission."""
from __future__ import annotations

import math
import time

from .responses import XhsApiError


def deadline_after(timeout_seconds: float) -> float:
    if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
        raise ValueError("API 发布总时限须为正的有限秒数")
    return time.monotonic() + timeout_seconds


def timeout_error() -> XhsApiError:
    return XhsApiError("API 发布总时限已到", category="network", signal="publish_timeout")


def remaining_seconds(deadline: float | None, cancel_event=None) -> float | None:
    remaining = None if deadline is None else deadline - time.monotonic()
    # curl converts seconds to integer milliseconds: a sub-ms timeout becomes
    # zero (unlimited). Stop instead of accidentally disabling the time limit.
    if remaining is not None and remaining < 0.001:
        raise timeout_error()
    if cancel_event is not None and cancel_event.is_set():
        raise XhsApiError("API 发布已取消", signal="publish_cancelled")
    return remaining
