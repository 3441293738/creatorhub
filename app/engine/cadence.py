"""Stable, positive-only jitter for periodic background work.

The persisted task ID/creation time and last attempt anchor a cycle. Deriving
its fraction from those values means polling and restarts never re-roll it.
This schedules work only; account risk gates still decide whether it may run.
"""
from __future__ import annotations

import hashlib
import math
from datetime import datetime, timedelta


def bounded_ratio(value: float) -> float:
    ratio = float(value)
    return min(1.0, max(0.0, ratio)) if math.isfinite(ratio) else 0.0


def periodic_deadline(last_at: datetime | None, interval_seconds: float, *,
                      key: str, jitter: float, created_at: datetime | None = None,
                      first_spread_seconds: float = 0) -> datetime | None:
    """Return a cycle's earliest automatic run, never before its base interval.

    First-run spreading applies only to newly created periodic tasks, not user
    publication appointments. A missing historical creation time stays due.
    """
    anchor = last_at or created_at
    if anchor is None:
        return None
    seed = f"{key}|{created_at.isoformat() if created_at else ''}|{anchor.isoformat()}"
    fraction = int.from_bytes(hashlib.sha256(seed.encode("utf-8")).digest()[:8], "big") / 2**64
    if last_at is None:
        delay = max(0.0, float(first_spread_seconds)) * fraction
    else:
        base = max(0.0, float(interval_seconds))
        delay = base * (1.0 + bounded_ratio(jitter) * fraction)
    return anchor + timedelta(seconds=delay)


def row_deadline(row, cfg, *, kind: str) -> datetime | None:
    """One calculation shared by the scheduler and its next-run UI."""
    if not row.enabled:
        return None
    last_at = row.last_run_at if kind == "comment_rule" else row.last_scan_at
    interval = row.interval_seconds
    if kind == "danmaku" and not interval:
        interval = cfg.engine.scan_interval_seconds
    return periodic_deadline(
        last_at, interval, key=f"{kind}:{row.id}", created_at=row.created_at,
        jitter=cfg.engine.scan_jitter,
        first_spread_seconds=cfg.engine.initial_scan_spread_seconds)
