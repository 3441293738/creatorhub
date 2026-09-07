"""Scheduling boundary: explicit offsets in, naive UTC for existing DB code."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def parse_schedule(value: str | None, timezone_name: str = "Asia/Shanghai") -> datetime | None:
    if value is None or not value.strip():
        return None
    raw = value.strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", raw):
        raise ValueError("预约时间需包含有效日期和时分")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("预约时间格式无效") from exc
    if parsed.tzinfo is None:
        try:
            zone = ZoneInfo(timezone_name or "Asia/Shanghai")
        except ZoneInfoNotFoundError as exc:
            raise ValueError("账号时区无效，请先修正账号时区") from exc
        aware = parsed.replace(tzinfo=zone)
        if aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != parsed:
            raise ValueError("该时间在所选时区不存在，请重新选择")
        if aware.utcoffset() != parsed.replace(tzinfo=zone, fold=1).utcoffset():
            raise ValueError("该时间处于夏令时重复区间，请使用带时区偏移的时间")
        parsed = aware
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def utc_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return aware.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
