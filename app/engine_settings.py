"""Allowlisted, persistent runtime settings for the beginner-facing settings UI."""
from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import Config, EngineConfig
from .db import get_session
from .models import AppSetting


ENGINE_SETTINGS_KEY = "engine.runtime.v1"


class EngineSettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)

    xhs_read_mode: Literal["browser", "api"] | None = None
    monitor_initial_backfill_count: int | None = Field(None, ge=-1, le=1000)
    comment_recent_works: int | None = Field(None, ge=1, le=100)
    comment_recent_days: int | None = Field(None, ge=1, le=365)
    comment_max_scrolls: int | None = Field(None, ge=1, le=30)
    request_timeout_seconds: int | None = Field(None, ge=5, le=300)
    download_timeout_seconds: int | None = Field(None, ge=30, le=1800)
    xhs_item_gap_seconds: float | None = Field(None, ge=0, le=120)
    xhs_request_jitter: float | None = Field(None, ge=0, le=1)
    xhs_publish_mode: Literal["browser", "api"] | None = None
    xhs_comment_write_mode: Literal["browser", "api", "manual"] | None = None
    xhs_comment_review_before_publish: bool | None = None
    work_health_enabled: bool | None = None
    work_health_interval_seconds: int | None = Field(None, ge=600, le=86400)
    work_health_zero_play_hours: float | None = Field(None, ge=1, le=168)
    work_health_recent_days: int | None = Field(None, ge=1, le=90)
    work_health_stat_snapshots: bool | None = None

    @field_validator("*", mode="before")
    @classmethod
    def reject_explicit_null(cls, value):
        # Missing fields mean "leave unchanged"; explicit null is not a reset.
        if value is None:
            raise ValueError("请填写有效值或使用推荐值")
        return value


def _validated_patch(payload: dict) -> dict:
    return EngineSettingsPatch.model_validate(payload).model_dump(exclude_unset=True)


def _read_patch(session) -> dict:
    row = session.get(AppSetting, ENGINE_SETTINGS_KEY)
    if row is None:
        return {}
    try:
        return _validated_patch(json.loads(row.value))
    except (ValueError, TypeError):
        # A malformed saved blob must not partially change startup state.
        return {}


def _export(cfg: Config, saved: dict) -> dict:
    defaults = EngineConfig()
    fields = EngineSettingsPatch.model_fields
    return {
        "values": {key: getattr(cfg.engine, key) for key in fields},
        "defaults": {key: getattr(defaults, key) for key in fields},
        "saved_fields": sorted(saved),
        "apply_scope": "next_operation",
    }


def export_engine_settings(cfg: Config) -> dict:
    with get_session() as session:
        return _export(cfg, _read_patch(session))


def save_engine_settings(cfg: Config, payload: dict) -> dict:
    """Validate and persist one atomic patch BEFORE changing live config.

    Store only edited fields: config.yaml remains the base configuration for
    everything else. No credentials, paths or restart-only engine fields are
    accepted by this endpoint.
    """
    patch = _validated_patch(payload)
    if patch:
        with get_session() as session:
            merged = {**_read_patch(session), **patch}
            row = session.get(AppSetting, ENGINE_SETTINGS_KEY)
            if row is None:
                row = AppSetting(key=ENGINE_SETTINGS_KEY)
            row.value = json.dumps(merged, ensure_ascii=False, allow_nan=False)
            session.add(row)
            session.commit()
        for key, value in patch.items():
            setattr(cfg.engine, key, value)
        return _export(cfg, merged)
    return export_engine_settings(cfg)


def load_persisted_engine_settings(cfg: Config) -> bool:
    with get_session() as session:
        patch = _read_patch(session)
    for key, value in patch.items():
        setattr(cfg.engine, key, value)
    return bool(patch)
