"""Runtime configuration helpers for the risk-control administration UI."""
from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from .config import Config, RiskControlConfig
from .settings import get_setting, set_setting


RISK_SETTINGS_KEY = "risk_control.runtime.v1"

_SCHEDULE_FIELDS = {
    "quiet_hours_enabled",
    "active_hours_start",
    "active_hours_end",
    "account_check_interval_seconds",
    "douyin_captcha_wait_seconds",
}

_RISK_BOUNDS: dict[str, tuple[int, int]] = {
    "network_group_concurrency": (1, 32),
    "read_light_gap_seconds": (0, 86400),
    "read_heavy_gap_seconds": (0, 86400),
    "shared_write_gap_seconds": (0, 86400),
    "comment_min_gap_seconds": (0, 86400),
    "comment_hourly_cap": (0, 10000),
    "comment_daily_cap": (0, 100000),
    "social_min_gap_seconds": (0, 86400),
    "social_hourly_cap": (0, 10000),
    "social_daily_cap": (0, 100000),
    "dm_min_gap_seconds": (0, 86400),
    "dm_hourly_cap": (0, 10000),
    "dm_daily_cap": (0, 100000),
    "publish_min_gap_seconds": (0, 604800),
    "publish_hourly_cap": (0, 10000),
    "publish_daily_cap": (0, 100000),
    "combined_action_hourly_cap": (0, 10000),
    "combined_action_daily_cap": (0, 100000),
    "recovery_successes": (1, 20),
    "recovery_probe_gap_seconds": (1, 86400),
    "event_retention_days": (1, 3650),
    "network_group_risk_accounts": (0, 1000),
    "network_group_risk_window_seconds": (1, 604800),
    "network_group_cooldown_seconds": (1, 604800),
}

_SCHEDULE_BOUNDS: dict[str, tuple[int, int]] = {
    "active_hours_start": (0, 23),
    "active_hours_end": (1, 47),
    "account_check_interval_seconds": (0, 604800),
    "douyin_captcha_wait_seconds": (0, 86400),
}


class RiskSettingsError(ValueError):
    pass


def export_risk_settings(cfg: Config) -> dict[str, Any]:
    return {
        "risk_control": asdict(cfg.risk_control),
        "schedule": {
            key: getattr(cfg.engine, key)
            for key in sorted(_SCHEDULE_FIELDS)
        },
    }


def _bounded_int(value: Any, name: str, bounds: tuple[int, int]) -> int:
    if isinstance(value, bool):
        raise RiskSettingsError(f"{name} 必须是整数")
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise RiskSettingsError(f"{name} 必须是整数") from None
    low, high = bounds
    if parsed < low or parsed > high:
        raise RiskSettingsError(f"{name} 必须在 {low}–{high} 之间")
    return parsed


def apply_risk_settings(cfg: Config, payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a complete/partial payload, then atomically update ``cfg``."""
    if not isinstance(payload, dict):
        raise RiskSettingsError("风控配置格式错误")
    risk_patch = payload.get("risk_control", {})
    schedule_patch = payload.get("schedule", {})
    if not isinstance(risk_patch, dict) or not isinstance(schedule_patch, dict):
        raise RiskSettingsError("风控规则和时间策略必须是对象")

    known_risk = set(RiskControlConfig.__dataclass_fields__)
    unknown = set(risk_patch) - known_risk
    if unknown:
        raise RiskSettingsError("未知风控字段：" + ", ".join(sorted(unknown)))
    unknown_schedule = set(schedule_patch) - _SCHEDULE_FIELDS
    if unknown_schedule:
        raise RiskSettingsError("未知时间策略字段：" + ", ".join(sorted(unknown_schedule)))

    merged = asdict(cfg.risk_control)
    merged.update(risk_patch)
    if not isinstance(merged.get("enabled"), bool):
        raise RiskSettingsError("enabled 必须是布尔值")
    mode = str(merged.get("mode", "")).strip().lower()
    if mode not in {"conservative", "custom"}:
        raise RiskSettingsError("mode 只能是 conservative 或 custom")
    merged["mode"] = mode

    for name, bounds in _RISK_BOUNDS.items():
        merged[name] = _bounded_int(merged.get(name), name, bounds)

    steps = merged.get("cooldown_steps_seconds")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 8:
        raise RiskSettingsError("cooldown_steps_seconds 需要包含 1–8 个冷却时间")
    normalized_steps = [
        _bounded_int(value, "cooldown_steps_seconds", (1, 604800))
        for value in steps
    ]
    if normalized_steps != sorted(normalized_steps):
        raise RiskSettingsError("冷却阶梯必须按从短到长排列")
    merged["cooldown_steps_seconds"] = normalized_steps

    next_schedule = {
        key: getattr(cfg.engine, key)
        for key in _SCHEDULE_FIELDS
    }
    next_schedule.update(schedule_patch)
    if not isinstance(next_schedule["quiet_hours_enabled"], bool):
        raise RiskSettingsError("quiet_hours_enabled 必须是布尔值")
    for name, bounds in _SCHEDULE_BOUNDS.items():
        next_schedule[name] = _bounded_int(next_schedule[name], name, bounds)
    if next_schedule["active_hours_end"] <= next_schedule["active_hours_start"]:
        raise RiskSettingsError("活跃结束时间必须晚于开始时间")

    # Build first so a bad payload cannot leave half-applied runtime state.
    new_policy = RiskControlConfig(**merged)
    cfg.risk_control = new_policy
    for key, value in next_schedule.items():
        setattr(cfg.engine, key, value)
    return export_risk_settings(cfg)


def save_risk_settings(cfg: Config) -> dict[str, Any]:
    payload = export_risk_settings(cfg)
    set_setting(RISK_SETTINGS_KEY, json.dumps(payload, ensure_ascii=False))
    return payload


def load_persisted_risk_settings(cfg: Config) -> bool:
    raw = get_setting(RISK_SETTINGS_KEY, "")
    if not raw:
        return False
    try:
        payload = json.loads(raw)
        apply_risk_settings(cfg, payload)
    except (json.JSONDecodeError, RiskSettingsError, TypeError, ValueError):
        return False
    return True
