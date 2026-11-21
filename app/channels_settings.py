"""Persistent visual settings for the local WeChat Channels (Finder) CDP channel."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import Config
from .db import get_session
from .engine_settings import save_engine_settings
from .models import AppSetting
from .platforms.channels.client import ChannelsProtocolTransportError, validate_loopback_url
from .platforms.channels.wmpf_cdp import inspect_endpoint_file

CHANNELS_SETTINGS_KEY = "channels.protocol.v1"
_STR_FIELDS = (
    "channels_cdp_endpoint",
    "channels_cdp_endpoint_file",
)
_BOOL_FIELDS = ("channels_proxy_enabled", "channels_proxy_manage_sysproxy")
_BOOL_DEFAULTS = {
    "channels_proxy_enabled": False,
    "channels_proxy_manage_sysproxy": True,
}
_INT_FIELDS = ("channels_proxy_port",)
_PERSISTED_FIELDS = _STR_FIELDS + _BOOL_FIELDS + _INT_FIELDS


class ChannelsProtocolPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    connection_mode: Literal["none", "proxy", "endpoint", "endpoint_file"] | None = None
    channels_proxy_enabled: bool | None = None
    channels_proxy_port: int | None = Field(default=None, ge=1024, le=65535)
    channels_proxy_manage_sysproxy: bool | None = None
    channels_cdp_endpoint: str | None = Field(default=None, max_length=512)
    channels_cdp_endpoint_file: str | None = Field(default=None, max_length=512)
    channels_read_mode: Literal["auto", "protocol", "browser"] | None = None

    @field_validator("channels_cdp_endpoint", "channels_cdp_endpoint_file", mode="before")
    @classmethod
    def _reject_control_chars(cls, value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("配置项必须是文本")
        if any(ch in value for ch in ("\x00", "\r", "\n")):
            raise ValueError("配置项不能包含换行或空字符")
        return value.strip()


def snapshot_channels_base_config(cfg: Config) -> dict[str, Any]:
    """Capture the base config.yaml / env values once before UI overrides are applied."""
    snapshot = getattr(cfg, "_channels_base_snapshot", None)
    if isinstance(snapshot, dict):
        return snapshot
    snap: dict[str, Any] = {}
    for key in _STR_FIELDS:
        snap[key] = str(getattr(cfg.engine, key, "") or "").strip()
    for key in _BOOL_FIELDS:
        snap[key] = bool(getattr(cfg.engine, key, _BOOL_DEFAULTS.get(key, False)))
    for key in _INT_FIELDS:
        snap[key] = int(getattr(cfg.engine, key, 2023) or 2023)
    setattr(cfg, "_channels_base_snapshot", snap)
    return snap


def _read_saved_channels_dict(session) -> dict[str, Any]:
    row = session.get(AppSetting, CHANNELS_SETTINGS_KEY)
    if row is None or not row.value:
        return {}
    try:
        raw = json.loads(row.value)
    except (ValueError, TypeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key in _STR_FIELDS:
        val = raw.get(key)
        if isinstance(val, str) and not any(ch in val for ch in ("\x00", "\r", "\n")):
            out[key] = val.strip()
    for key in _BOOL_FIELDS:
        val = raw.get(key)
        if isinstance(val, bool):
            out[key] = val
    for key in _INT_FIELDS:
        val = raw.get(key)
        if isinstance(val, int) and not isinstance(val, bool) and 1024 <= val <= 65535:
            out[key] = val
    return out


def validate_channels_endpoint(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        return validate_loopback_url(raw, schemes=("http", "https", "ws", "wss"))
    except ChannelsProtocolTransportError as exc:
        raise ValueError(str(exc)) from exc


def validate_channels_endpoint_file(value: str, *, require_valid_if_exists: bool = True) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    path = Path(raw).expanduser()
    if path.exists() and path.is_dir():
        raise ValueError("请填写具体口令文件路径，而不是目录路径")
    if require_valid_if_exists and path.is_file():
        info = inspect_endpoint_file(path)
        if not info.get("valid"):
            raise ValueError(str(info.get("error") or "口令文件格式无效"))
    return raw


def export_channels_settings(cfg: Config, *, saved_fields: list[str] | None = None) -> dict[str, Any]:
    read_mode = str(getattr(cfg.engine, "channels_read_mode", "auto") or "auto").strip().lower()
    if read_mode not in {"auto", "protocol", "browser"}:
        read_mode = "auto"
    proxy_enabled = bool(getattr(cfg.engine, "channels_proxy_enabled", False))
    proxy_port = int(getattr(cfg.engine, "channels_proxy_port", 2023) or 2023)
    proxy_manage_sysproxy = bool(getattr(cfg.engine, "channels_proxy_manage_sysproxy", True))
    endpoint = str(getattr(cfg.engine, "channels_cdp_endpoint", "") or "").strip()
    endpoint_file = str(getattr(cfg.engine, "channels_cdp_endpoint_file", "") or "").strip()
    if saved_fields is None:
        with get_session() as session:
            saved_fields = sorted(_read_saved_channels_dict(session))

    connection_mode = (
        "proxy"
        if proxy_enabled
        else ("endpoint_file" if endpoint_file else ("endpoint" if endpoint else "none"))
    )
    return {
        "read_mode": read_mode,
        "connection_mode": connection_mode,
        "proxy_enabled": proxy_enabled,
        "proxy_port": proxy_port,
        "proxy_manage_sysproxy": proxy_manage_sysproxy,
        "cdp_endpoint": endpoint,
        "cdp_endpoint_file": endpoint_file,
        "configured": bool(proxy_enabled or endpoint or endpoint_file),
        "endpoint_file_info": inspect_endpoint_file(endpoint_file) if endpoint_file else None,
        "saved_fields": saved_fields,
    }


def save_channels_settings(cfg: Config, payload: dict[str, Any] | ChannelsProtocolPatch) -> dict[str, Any]:
    """Validate and persist local WeChat Channels connection settings atomically."""
    snapshot_channels_base_config(cfg)
    patch = (
        payload
        if isinstance(payload, ChannelsProtocolPatch)
        else ChannelsProtocolPatch.model_validate(payload)
    )

    updates: dict[str, Any] = {}
    mode = patch.connection_mode
    if mode == "none":
        updates["channels_proxy_enabled"] = False
        updates["channels_cdp_endpoint"] = ""
        updates["channels_cdp_endpoint_file"] = ""
    elif mode == "proxy":
        updates["channels_proxy_enabled"] = True
        if patch.channels_cdp_endpoint is None:
            updates["channels_cdp_endpoint"] = ""
        if patch.channels_cdp_endpoint_file is None:
            updates["channels_cdp_endpoint_file"] = ""
    elif mode == "endpoint":
        raw_ep = (
            patch.channels_cdp_endpoint
            if patch.channels_cdp_endpoint is not None
            else str(getattr(cfg.engine, "channels_cdp_endpoint", "") or "")
        ).strip()
        if not raw_ep:
            raise ValueError("直连模式下请填写本机回环 CDP 端点地址（如 http://127.0.0.1:9222）")
        updates["channels_proxy_enabled"] = False
        updates["channels_cdp_endpoint"] = validate_channels_endpoint(raw_ep)
        if patch.channels_cdp_endpoint_file is None:
            updates["channels_cdp_endpoint_file"] = ""
    elif mode == "endpoint_file":
        raw_file = (
            patch.channels_cdp_endpoint_file
            if patch.channels_cdp_endpoint_file is not None
            else str(getattr(cfg.engine, "channels_cdp_endpoint_file", "") or "")
        ).strip()
        if not raw_file:
            raise ValueError("口令文件模式下请填写 WMPF 控制口令文件路径")
        updates["channels_proxy_enabled"] = False
        updates["channels_cdp_endpoint_file"] = validate_channels_endpoint_file(raw_file)
        if patch.channels_cdp_endpoint is None:
            updates["channels_cdp_endpoint"] = ""

    if patch.channels_proxy_enabled is not None and "channels_proxy_enabled" not in updates:
        updates["channels_proxy_enabled"] = bool(patch.channels_proxy_enabled)
    if patch.channels_proxy_port is not None:
        updates["channels_proxy_port"] = int(patch.channels_proxy_port)
    if patch.channels_proxy_manage_sysproxy is not None:
        updates["channels_proxy_manage_sysproxy"] = bool(patch.channels_proxy_manage_sysproxy)

    if patch.channels_cdp_endpoint is not None and "channels_cdp_endpoint" not in updates:
        updates["channels_cdp_endpoint"] = validate_channels_endpoint(patch.channels_cdp_endpoint)
    if patch.channels_cdp_endpoint_file is not None and "channels_cdp_endpoint_file" not in updates:
        updates["channels_cdp_endpoint_file"] = validate_channels_endpoint_file(patch.channels_cdp_endpoint_file)

    if patch.channels_read_mode is not None:
        save_engine_settings(cfg, {"channels_read_mode": patch.channels_read_mode})

    saved_fields: list[str]
    if updates:
        with get_session() as session:
            merged = {**_read_saved_channels_dict(session), **updates}
            row = session.get(AppSetting, CHANNELS_SETTINGS_KEY)
            if row is None:
                row = AppSetting(key=CHANNELS_SETTINGS_KEY)
            row.value = json.dumps(merged, ensure_ascii=False, allow_nan=False)
            session.add(row)
            session.commit()
        for key, value in updates.items():
            setattr(cfg.engine, key, value)
        saved_fields = sorted(merged)
    else:
        with get_session() as session:
            saved_fields = sorted(_read_saved_channels_dict(session))

    return export_channels_settings(cfg, saved_fields=saved_fields)


def reset_channels_settings(cfg: Config) -> dict[str, Any]:
    """Remove UI overrides for WeChat Channels protocol settings and restore startup defaults."""
    base = snapshot_channels_base_config(cfg)
    with get_session() as session:
        row = session.get(AppSetting, CHANNELS_SETTINGS_KEY)
        if row is not None:
            session.delete(row)
            session.commit()
    for key in _PERSISTED_FIELDS:
        setattr(cfg.engine, key, base.get(key, ""))
    return export_channels_settings(cfg, saved_fields=[])


def load_persisted_channels_settings(cfg: Config) -> bool:
    """Apply saved UI overrides onto cfg.engine after snapshot of config.yaml / env defaults."""
    snapshot_channels_base_config(cfg)
    with get_session() as session:
        saved = _read_saved_channels_dict(session)
    applied = False
    for key, value in saved.items():
        if key == "channels_cdp_endpoint" and value:
            try:
                value = validate_channels_endpoint(value)
            except ValueError:
                continue
        if key == "channels_cdp_endpoint_file" and value:
            try:
                value = validate_channels_endpoint_file(value, require_valid_if_exists=False)
            except ValueError:
                continue
        setattr(cfg.engine, key, value)
        applied = True
    return applied
